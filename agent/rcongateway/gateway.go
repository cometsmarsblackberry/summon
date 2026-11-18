// Package rcongateway lets a reservation owner's remote tools, such as lobby
// and pickup sites, use Source RCON without holding the game server's RCON
// password.
//
// The gateway listens on the host's game port over TCP, where RCON clients
// expect it, and authenticates a password of its own. It passes every command
// to the Summon plugin, which runs it only if the reservation owner is
// allowed to. The game server's own RCON port stays unreachable from outside.
package rcongateway

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"log"
	"net"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/summon/agent/rcon"
)

const (
	// A client has this long to authenticate, and to send each packet.
	authTimeout = 10 * time.Second
	// Lobby sites keep one connection open for a whole match.
	idleTimeout  = 30 * time.Minute
	writeTimeout = 10 * time.Second

	maxConnections      = 16
	maxConnectionsPerIP = 4
	// maxRequestPacket is the largest packet SRCDS accepts from a client.
	maxRequestPacket = rcon.MaxBodySize + 10

	// minPasswordLength refuses a password short enough to guess.
	minPasswordLength = 12

	// Commands run on the game thread, so their rate is bounded.
	commandBurst      = 60
	commandsPerSecond = 20

	maxReconnectBackoff = 15 * time.Second
)

var steamID64RE = regexp.MustCompile(`^[0-9]{17}$`)

// Executor runs one console line on the game server and returns its output.
type Executor interface {
	Execute(ctx context.Context, line string) (string, error)
}

// Config describes one gateway.
type Config struct {
	// Addr is the TCP address to listen on, such as ":27015".
	Addr string
	// Password is what remote clients authenticate with. It is not the game
	// server's RCON password.
	Password string
	// ActorSteamID is the reservation owner's SteamID64. The plugin logs it
	// with every command.
	ActorSteamID string
	// Upstream runs the wrapped commands on the game server.
	Upstream Executor
	// Label names the gateway in log lines.
	Label string
}

// Gateway is one listening RCON endpoint.
type Gateway struct {
	config   Config
	listener net.Listener
	failures *failureLimiter
	rate     *rateLimiter

	ctx    context.Context
	cancel context.CancelFunc

	mu      sync.Mutex
	conns   map[net.Conn]string
	perIP   map[string]int
	closing bool
	wg      sync.WaitGroup
}

// Listen validates the configuration, binds the address, and starts serving.
func Listen(config Config) (*Gateway, error) {
	if len(config.Password) < minPasswordLength {
		return nil, fmt.Errorf("rcon gateway: password must be at least %d characters", minPasswordLength)
	}
	if !forwardable(config.Password) || len(config.Password) > rcon.MaxBodySize {
		return nil, errors.New("rcon gateway: password contains unsupported characters")
	}
	if !steamID64RE.MatchString(config.ActorSteamID) {
		return nil, errors.New("rcon gateway: owner SteamID64 is invalid")
	}
	if config.Upstream == nil {
		return nil, errors.New("rcon gateway: upstream is required")
	}
	// IPv4 only, like the game server itself.
	listener, err := net.Listen("tcp4", config.Addr)
	if err != nil {
		return nil, fmt.Errorf("rcon gateway: listen on %s: %w", config.Addr, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	gateway := &Gateway{
		config:   config,
		listener: listener,
		failures: newFailureLimiter(time.Now),
		rate:     newRateLimiter(commandBurst, commandsPerSecond, time.Now),
		ctx:      ctx,
		cancel:   cancel,
		conns:    make(map[net.Conn]string),
		perIP:    make(map[string]int),
	}
	gateway.wg.Add(1)
	go gateway.serve()
	return gateway, nil
}

// Addr returns the address the gateway listens on.
func (g *Gateway) Addr() net.Addr {
	return g.listener.Addr()
}

// Done is closed once the gateway has been closed.
func (g *Gateway) Done() <-chan struct{} {
	return g.ctx.Done()
}

// Close stops listening, disconnects every client, and closes the upstream.
func (g *Gateway) Close() {
	g.mu.Lock()
	if g.closing {
		g.mu.Unlock()
		return
	}
	g.closing = true
	g.cancel()
	_ = g.listener.Close()
	for conn := range g.conns {
		_ = conn.Close()
	}
	g.mu.Unlock()
	if closer, ok := g.config.Upstream.(interface{ Close() }); ok {
		closer.Close()
	}
	g.wg.Wait()
}

// Follow keeps the upstream connected for as long as the game server exists
// and closes the gateway once gone reports that it does not. The connection
// ends when the server's container stops, which is what triggers the check,
// so the gateway needs no notice from whoever stops the container.
func (g *Gateway) Follow(upstream *Upstream, gone func(context.Context) bool) {
	go func() {
		backoff := time.Second
		for {
			select {
			case <-g.ctx.Done():
				return
			case <-upstream.Lost():
			}
			if gone(g.ctx) {
				if g.ctx.Err() == nil {
					g.logf("game server is gone; closing")
					g.Close()
				}
				return
			}
			if err := upstream.Connect(g.ctx); err == nil {
				backoff = time.Second
				continue
			}
			select {
			case <-time.After(backoff):
			case <-g.ctx.Done():
				return
			}
			if backoff < maxReconnectBackoff {
				backoff *= 2
			}
		}
	}()
}

var (
	activeMu sync.Mutex
	active   = make(map[string]*Gateway)
)

// Replace starts a gateway after closing the one this process started
// earlier on the same address, if it is still open. A slot's next reservation
// uses the same port as the previous one.
func Replace(config Config) (*Gateway, error) {
	activeMu.Lock()
	defer activeMu.Unlock()
	if previous := active[config.Addr]; previous != nil {
		previous.Close()
		delete(active, config.Addr)
	}
	gateway, err := Listen(config)
	if err != nil {
		return nil, err
	}
	active[config.Addr] = gateway
	return gateway, nil
}

func (g *Gateway) logf(format string, args ...any) {
	log.Printf("RCON gateway %s: %s", g.config.Label, fmt.Sprintf(format, args...))
}

func (g *Gateway) serve() {
	defer g.wg.Done()
	for {
		conn, err := g.listener.Accept()
		if err != nil {
			if g.ctx.Err() != nil {
				return
			}
			g.logf("accept failed: %v", err)
			select {
			case <-time.After(100 * time.Millisecond):
			case <-g.ctx.Done():
				return
			}
			continue
		}
		ip := remoteIP(conn)
		if !g.track(conn, ip) {
			_ = conn.Close()
			continue
		}
		g.wg.Add(1)
		go func() {
			defer g.wg.Done()
			defer g.untrack(conn, ip)
			g.handle(conn, ip)
		}()
	}
}

func remoteIP(conn net.Conn) string {
	host, _, err := net.SplitHostPort(conn.RemoteAddr().String())
	if err != nil {
		return conn.RemoteAddr().String()
	}
	return host
}

func (g *Gateway) track(conn net.Conn, ip string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.closing || len(g.conns) >= maxConnections || g.perIP[ip] >= maxConnectionsPerIP {
		return false
	}
	g.conns[conn] = ip
	g.perIP[ip]++
	return true
}

func (g *Gateway) untrack(conn net.Conn, ip string) {
	_ = conn.Close()
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.conns, conn)
	if g.perIP[ip]--; g.perIP[ip] <= 0 {
		delete(g.perIP, ip)
	}
}

func write(conn net.Conn, packet rcon.Packet) error {
	_ = conn.SetWriteDeadline(time.Now().Add(writeTimeout))
	return rcon.WritePacket(conn, packet)
}

func (g *Gateway) handle(conn net.Conn, ip string) {
	authenticated := false
	for {
		timeout := authTimeout
		if authenticated {
			timeout = idleTimeout
		}
		_ = conn.SetReadDeadline(time.Now().Add(timeout))
		packet, err := rcon.ReadRequest(conn, maxRequestPacket)
		if err != nil {
			return
		}

		switch {
		case packet.Type == rcon.TypeAuth:
			// A blocked address is refused before the password is looked at,
			// so it learns nothing from further guesses. Its attempts are
			// not counted either: a client that keeps retrying with the
			// right password must get back in once the block ends.
			blocked := g.failures.blocked(ip)
			allowed := !blocked &&
				subtle.ConstantTimeCompare(packet.Body, []byte(g.config.Password)) == 1
			// Counted before the verdict is sent, so the client cannot start its
			// next attempt ahead of the count.
			switch {
			case allowed:
				g.failures.reset(ip)
			case !blocked:
				if g.failures.record(ip) {
					g.logf("blocking %s after repeated failed logins", ip)
				}
			}
			// SRCDS sends an empty response value before its verdict.
			if write(conn, rcon.Packet{ID: packet.ID, Type: rcon.TypeResponseValue}) != nil {
				return
			}
			verdict := rcon.Packet{ID: -1, Type: rcon.TypeAuthResponse}
			if allowed {
				verdict.ID = packet.ID
			}
			if write(conn, verdict) != nil || !allowed {
				return
			}
			if !authenticated {
				g.logf("client %s authenticated", ip)
			}
			authenticated = true

		case !authenticated:
			// SRCDS answers anything sent before authentication the same way.
			_ = write(conn, rcon.Packet{ID: -1, Type: rcon.TypeAuthResponse})
			return

		case packet.Type == rcon.TypeExecCommand:
			output, err := g.run(string(packet.Body))
			if err != nil {
				// The game server did not answer. Dropping the connection
				// tells the client so; an empty reply would look like success.
				g.logf("command from %s failed: %v", ip, err)
				return
			}
			if writeResponse(conn, packet.ID, output) != nil {
				return
			}

		case packet.Type == rcon.TypeResponseValue:
			// Clients send an empty response value after a command to find
			// the end of a multi-packet reply. SRCDS mirrors it and follows
			// with a second packet holding these four bytes.
			if write(conn, rcon.Packet{ID: packet.ID, Type: rcon.TypeResponseValue}) != nil {
				return
			}
			if write(conn, rcon.Packet{
				ID: packet.ID, Type: rcon.TypeResponseValue, Body: []byte{0x00, 0x01, 0x00, 0x00},
			}) != nil {
				return
			}
		}
		// SRCDS ignores other packet types.
	}
}

func writeResponse(conn net.Conn, id int32, output string) error {
	body := []byte(output)
	for {
		chunk := body
		if len(chunk) > rcon.MaxBodySize {
			chunk = chunk[:rcon.MaxBodySize]
		}
		if err := write(conn, rcon.Packet{ID: id, Type: rcon.TypeResponseValue, Body: chunk}); err != nil {
			return err
		}
		body = body[len(chunk):]
		if len(body) == 0 {
			return nil
		}
	}
}

// run executes every command in one RCON request and returns their combined
// output. It returns an error only when the game server could not be reached.
func (g *Gateway) run(request string) (string, error) {
	commands := splitCommands(request)
	if len(commands) > maxCommandsPerRequest {
		return messageTooMany + "\n", nil
	}

	var output strings.Builder
	for _, command := range commands {
		if !forwardable(command) {
			output.WriteString(messageUnsupported + "\n")
			continue
		}
		line := wrapCommand(g.config.ActorSteamID, command)
		if len(line) > maxForwardedLine {
			output.WriteString(messageTooLong + "\n")
			continue
		}
		if err := g.rate.wait(g.ctx); err != nil {
			return "", err
		}
		raw, err := g.config.Upstream.Execute(g.ctx, line)
		if err != nil {
			return "", err
		}
		body, ok := parsePluginResult(raw)
		if !ok {
			body = messageUnavailable + "\n"
		}
		output.WriteString(maskStatusAddresses(body))
	}
	return output.String(), nil
}
