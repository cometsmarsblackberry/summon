package rcongateway

import (
	"context"
	"errors"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/summon/agent/rcon"
)

const serverPassword = "real-server-password"

// fakeSRCDS speaks the server side of RCON over in-memory pipes the way SRCDS
// does, including the mirrored response value that ends a reply.
type fakeSRCDS struct {
	dials atomic.Int32

	mu    sync.Mutex
	conns []net.Conn
	down  bool
}

func (s *fakeSRCDS) dial(context.Context) (io.ReadWriteCloser, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dials.Add(1)
	if s.down {
		return nil, errors.New("connection refused")
	}
	client, server := net.Pipe()
	s.conns = append(s.conns, server)
	go s.serve(server)
	return client, nil
}

func (s *fakeSRCDS) serve(conn net.Conn) {
	defer conn.Close()
	authenticated := false
	for {
		packet, err := rcon.ReadPacket(conn, 8192)
		if err != nil {
			return
		}
		switch {
		case packet.Type == rcon.TypeAuth:
			authenticated = string(packet.Body) == serverPassword
			_ = rcon.WritePacket(conn, rcon.Packet{ID: packet.ID, Type: rcon.TypeResponseValue})
			id := int32(-1)
			if authenticated {
				id = packet.ID
			}
			_ = rcon.WritePacket(conn, rcon.Packet{ID: id, Type: rcon.TypeAuthResponse})
		case !authenticated:
			return
		case packet.Type == rcon.TypeExecCommand:
			_ = rcon.WritePacket(conn, rcon.Packet{
				ID: packet.ID, Type: rcon.TypeResponseValue, Body: []byte("ran: " + string(packet.Body)),
			})
		case packet.Type == rcon.TypeResponseValue:
			_ = rcon.WritePacket(conn, rcon.Packet{ID: packet.ID, Type: rcon.TypeResponseValue})
			_ = rcon.WritePacket(conn, rcon.Packet{
				ID: packet.ID, Type: rcon.TypeResponseValue, Body: []byte{0, 1, 0, 0},
			})
		}
	}
}

// stop closes every connection and, when down is set, refuses new ones.
func (s *fakeSRCDS) stop(down bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.down = down
	for _, conn := range s.conns {
		_ = conn.Close()
	}
	s.conns = nil
}

func TestUpstreamExecutesAndReconnects(t *testing.T) {
	server := &fakeSRCDS{}
	upstream := NewUpstream(server.dial, serverPassword)
	defer upstream.Close()
	ctx := testContext(t)

	for _, command := range []string{"first", "second"} {
		output, err := upstream.Execute(ctx, command)
		if err != nil || output != "ran: "+command {
			t.Fatalf("Execute(%q) = %q, %v", command, output, err)
		}
	}
	if dials := server.dials.Load(); dials != 1 {
		t.Fatalf("dialled %d times for two commands, want 1", dials)
	}

	server.stop(false)
	select {
	case <-upstream.Lost():
	case <-ctx.Done():
		t.Fatal("the lost connection went unnoticed")
	}
	output, err := upstream.Execute(ctx, "third")
	if err != nil || output != "ran: third" {
		t.Fatalf("Execute after reconnect = %q, %v", output, err)
	}
}

func TestUpstreamRejectsAWrongServerPassword(t *testing.T) {
	server := &fakeSRCDS{}
	upstream := NewUpstream(server.dial, "not-the-password")
	defer upstream.Close()

	if _, err := upstream.Execute(testContext(t), "status"); !errors.Is(err, rcon.ErrAuthFailed) {
		t.Fatalf("Execute = %v, want ErrAuthFailed", err)
	}
}

func TestGatewayClosesOnceTheGameServerIsGone(t *testing.T) {
	server := &fakeSRCDS{}
	upstream := NewUpstream(server.dial, serverPassword)
	ctx := testContext(t)
	if err := upstream.Connect(ctx); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	gateway := startGateway(t, upstream)
	var gone atomic.Bool
	gateway.Follow(upstream, func(context.Context) bool { return gone.Load() })

	// A dropped connection alone, as during a level change, is repaired.
	server.stop(false)
	client := login(t, gateway)
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := client.Execute(ctx, "status"); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the gateway did not recover from a dropped connection")
		}
		client = login(t, gateway)
	}
	select {
	case <-gateway.Done():
		t.Fatal("the gateway closed although the game server still exists")
	default:
	}

	gone.Store(true)
	server.stop(true)
	select {
	case <-gateway.Done():
	case <-ctx.Done():
		t.Fatal("the gateway stayed open after the game server was gone")
	}
	if _, err := net.DialTimeout("tcp4", gateway.Addr().String(), time.Second); err == nil {
		t.Fatal("the gateway still accepts connections")
	}
}
