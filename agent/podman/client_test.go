package podman

import (
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestRunPodmanCommandKeepsWarningsOutOfMachineOutput(t *testing.T) {
	cmd := exec.Command(
		"sh", "-c",
		`printf '[{"Id":"container-id"}]'; printf 'time="now" level=warning msg="fallback"\n' >&2`,
	)
	output, err := runPodmanCommand(cmd)
	if err != nil {
		t.Fatalf("successful command returned an error: %v", err)
	}
	if got, want := string(output), `[{"Id":"container-id"}]`; got != want {
		t.Fatalf("stdout was contaminated by stderr: got %q, want %q", got, want)
	}
}

func TestRunPodmanCommandPreservesDiagnosticsOnFailure(t *testing.T) {
	cmd := exec.Command(
		"sh", "-c",
		`printf 'partial stdout'; printf 'podman stderr' >&2; exit 7`,
	)
	output, err := runPodmanCommand(cmd)
	if err == nil {
		t.Fatal("failed command unexpectedly succeeded")
	}
	text := string(output)
	if !strings.Contains(text, "partial stdout") || !strings.Contains(text, "podman stderr") {
		t.Fatalf("failure diagnostics were lost: %q", text)
	}
}

func TestCleanupStaleImageStagingUsesStrictNameOwnerAndAgeChecks(t *testing.T) {
	root := t.TempDir()
	now := time.Now()
	old := now.Add(-2 * time.Hour)

	stale := filepath.Join(root, "container_images_storage123")
	recent := filepath.Join(root, "container_images_storage456")
	malformed := filepath.Join(root, "container_images_storageABC")
	target := filepath.Join(root, "unrelated-target")
	for _, directory := range []string{stale, recent, malformed, target} {
		if err := os.Mkdir(directory, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(directory, "layer"), []byte("data"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chtimes(stale, old, old); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(malformed, old, old); err != nil {
		t.Fatal(err)
	}
	symlink := filepath.Join(root, "container_images_storage789")
	if err := os.Symlink(target, symlink); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(symlink, old, old); err != nil {
		t.Fatal(err)
	}

	removed, err := cleanupStaleImageStaging(root, now.Add(-time.Hour), os.Getuid())
	if err != nil {
		t.Fatal(err)
	}
	if removed != 1 {
		t.Fatalf("removed %d staging directories, want 1", removed)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("stale staging directory remains: %v", err)
	}
	for _, path := range []string{recent, malformed, symlink, target} {
		if _, err := os.Lstat(path); err != nil {
			t.Fatalf("cleanup removed protected path %s: %v", path, err)
		}
	}
}

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

func TestBuildRunArgsProvidesValidatedStartupMapWithBadlandsCommandFallback(t *testing.T) {
	tests := []struct {
		name     string
		firstMap string
		want     string
	}{
		{name: "requested map", firstMap: "cp_process_f12", want: "cp_process_f12"},
		{name: "surrounding whitespace", firstMap: "  koth_product_rcx  ", want: "koth_product_rcx"},
		{name: "empty map", firstMap: "", want: "cp_badlands"},
		{name: "unsafe map", firstMap: "cp_process;quit", want: "cp_badlands"},
		{name: "path traversal", firstMap: "../cp_process", want: "cp_badlands"},
		{name: "too long", firstMap: strings.Repeat("a", 65), want: "cp_badlands"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			args := BuildRunArgs(ContainerConfig{
				Name: "startup-map", Image: "tf2:test", FirstMap: test.firstMap,
			})
			if env := "SUMMON_START_MAP=" + test.want; !slices.Contains(args, env) {
				t.Fatalf("startup map environment %q missing from %q", env, args)
			}
			if len(args) < 2 || !slices.Equal(args[len(args)-2:], []string{"+map", "cp_badlands"}) {
				t.Fatalf("badlands command fallback missing from %q", args)
			}
		})
	}
}

func TestBuildRunArgsNormalizesFastDLMapDirectory(t *testing.T) {
	tests := []struct {
		name   string
		fastDL string
		want   string
	}{
		{name: "site root", fastDL: "https://fastdl.example.test", want: "https://fastdl.example.test/maps/"},
		{name: "site root slash", fastDL: "https://fastdl.example.test/", want: "https://fastdl.example.test/maps/"},
		{name: "maps directory", fastDL: "https://fastdl.example.test/maps", want: "https://fastdl.example.test/maps/"},
		{name: "maps directory slash", fastDL: "https://fastdl.example.test/maps/", want: "https://fastdl.example.test/maps/"},
		{name: "nested root", fastDL: "https://fastdl.example.test/tf/", want: "https://fastdl.example.test/tf/maps/"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			args := BuildRunArgs(ContainerConfig{
				Name: "fastdl", Image: "tf2:test", FastDLURL: test.fastDL,
			})
			if env := "SM_MAP_DOWNLOAD_BASE=" + test.want; !slices.Contains(args, env) {
				t.Fatalf("map download environment %q missing from %q", env, args)
			}
		})
	}
}
