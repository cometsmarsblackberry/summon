package websocket

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestWithInstanceID(t *testing.T) {
	tests := map[string]string{
		"wss://summon.example/internal/ws/agent/tf2-135-6ad0fccc":  "wss://summon.example/internal/ws/agent/tf2-138-6ad0ffba",
		"wss://summon.example/internal/ws/agent/tf2-135-6ad0fccc/": "wss://summon.example/internal/ws/agent/tf2-138-6ad0ffba",
		"ws://127.0.0.1:8000/internal/ws/agent/tf2-135-6ad0fccc":   "ws://127.0.0.1:8000/internal/ws/agent/tf2-138-6ad0ffba",
	}
	for in, want := range tests {
		got, err := WithInstanceID(in, "tf2-138-6ad0ffba")
		if err != nil {
			t.Fatalf("WithInstanceID(%q): %v", in, err)
		}
		if got != want {
			t.Errorf("WithInstanceID(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSetIdentityChangesReconnectTarget(t *testing.T) {
	c := NewClient("wss://summon.example/internal/ws/agent/tf2-135-6ad0fccc", "old-token")
	if err := c.SetIdentity("tf2-138-6ad0ffba", "new-token"); err != nil {
		t.Fatal(err)
	}
	if c.url != "wss://summon.example/internal/ws/agent/tf2-138-6ad0ffba" || c.token != "new-token" {
		t.Fatalf("identity = %q / %q", c.url, c.token)
	}
}

func TestReconnectResendsCompetitiveConfigs(t *testing.T) {
	received := make(chan map[string]interface{}, 4)
	var connections atomic.Int32
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		connections.Add(1)
		var message map[string]interface{}
		if err := conn.ReadJSON(&message); err != nil {
			return
		}
		received <- message
		// Closing the first connection stands in for a backend restart.
	}))
	defer server.Close()

	c := NewClient("ws"+strings.TrimPrefix(server.URL, "http"), "token")
	if err := c.Connect(); err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.SendCompetitiveConfigs([]string{"rgl_6s_5cp"}, "image"); err != nil {
		t.Fatal(err)
	}
	for want := 1; want <= 2; want++ {
		select {
		case message := <-received:
			if message["type"] != "competitive_configs" || message["container_image"] != "image" {
				t.Fatalf("message %d = %#v", want, message)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("config list not received on connection %d", want)
		}
	}
	if got := connections.Load(); got < 2 {
		t.Fatalf("connections = %d, want a reconnect", got)
	}
}
