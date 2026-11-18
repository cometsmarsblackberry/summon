package rcongateway

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/summon/agent/rcon"
)

const (
	// connectTimeout covers opening the connection and authenticating. SRCDS
	// answers RCON between frames, so a level change can hold it for a while.
	connectTimeout = 30 * time.Second
	// commandTimeout bounds one command. SRCDS can leave a command that
	// arrives during a level change without a reply.
	commandTimeout = 15 * time.Second
)

// Dialer opens a byte stream to the game server's own RCON port.
type Dialer func(ctx context.Context) (io.ReadWriteCloser, error)

// Upstream is the gateway's single connection to the game server. It
// authenticates with the server's real RCON password, which never leaves the
// agent, and reconnects when the connection is lost.
type Upstream struct {
	dial     Dialer
	password string

	mu     sync.Mutex
	client *rcon.Client
	closed bool
}

// NewUpstream returns an upstream that connects on first use.
func NewUpstream(dial Dialer, password string) *Upstream {
	return &Upstream{dial: dial, password: password}
}

var errUpstreamClosed = errors.New("rcon gateway: upstream closed")

// Connect makes sure a connection is open and authenticated.
func (u *Upstream) Connect(ctx context.Context) error {
	u.mu.Lock()
	defer u.mu.Unlock()
	_, err := u.connectLocked(ctx)
	return err
}

func (u *Upstream) connectLocked(ctx context.Context) (*rcon.Client, error) {
	if u.closed {
		return nil, errUpstreamClosed
	}
	if u.client != nil {
		select {
		case <-u.client.Done():
			u.dropLocked()
		default:
			return u.client, nil
		}
	}

	ctx, cancel := context.WithTimeout(ctx, connectTimeout)
	defer cancel()
	conn, err := u.dial(ctx)
	if err != nil {
		return nil, fmt.Errorf("rcon gateway: connect to game server: %w", err)
	}
	client := rcon.NewClient(conn)
	if err := client.Authenticate(ctx, u.password); err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("rcon gateway: authenticate with game server: %w", err)
	}
	u.client = client
	return client, nil
}

func (u *Upstream) dropLocked() {
	if u.client != nil {
		_ = u.client.Close()
		u.client = nil
	}
}

// Execute runs one console line on the game server. Calls are serialized. A
// failed call drops the connection, so the next one starts from a clean state.
func (u *Upstream) Execute(ctx context.Context, line string) (string, error) {
	u.mu.Lock()
	defer u.mu.Unlock()
	client, err := u.connectLocked(ctx)
	if err != nil {
		return "", err
	}
	ctx, cancel := context.WithTimeout(ctx, commandTimeout)
	defer cancel()
	output, err := client.Execute(ctx, line)
	if err != nil {
		u.dropLocked()
		return "", fmt.Errorf("rcon gateway: game server command: %w", err)
	}
	return output, nil
}

// Lost returns a channel that is closed when there is no live connection.
func (u *Upstream) Lost() <-chan struct{} {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.client == nil {
		lost := make(chan struct{})
		close(lost)
		return lost
	}
	return u.client.Done()
}

// Close ends the connection and refuses further use.
func (u *Upstream) Close() {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.closed = true
	u.dropLocked()
}
