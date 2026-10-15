package websocket

import "testing"

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
