package podman

import (
	"slices"
	"strings"
	"testing"
)

func TestBuildRunArgsUsesSlotPortsAndKeepsRCONPrivate(t *testing.T) {
	args := BuildRunArgs(ContainerConfig{
		Name: "summon-h4-s2", Image: "example.invalid/tf2@sha256:abc",
		GamePort: 27035, TVPort: 27040,
		Labels: map[string]string{
			"summon.slot_id": "12", "summon.runtime": "instant",
		},
	})
	joined := strings.Join(args, " ")
	for _, mapping := range []string{"27035:27015/udp", "27040:27020/udp"} {
		if !slices.Contains(args, mapping) {
			t.Fatalf("missing port mapping %q in %s", mapping, joined)
		}
	}
	if strings.Contains(joined, "27035:27015/tcp") || strings.Contains(joined, "RCON_PORT") {
		t.Fatalf("RCON must not be exposed publicly: %s", joined)
	}
	if !strings.Contains(joined, "summon.runtime=instant") ||
		!strings.Contains(joined, "summon.slot_id=12") {
		t.Fatalf("assignment labels missing: %s", joined)
	}
}

func TestBuildRunArgsPreservesCloudDefaults(t *testing.T) {
	args := BuildRunArgs(ContainerConfig{Name: "cloud", Image: "tf2:test"})
	for _, mapping := range []string{"27015:27015/tcp", "27015:27015/udp", "27020:27020/udp"} {
		if !slices.Contains(args, mapping) {
			t.Fatalf("cloud default mapping %q missing", mapping)
		}
	}
}
