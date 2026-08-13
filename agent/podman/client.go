// Package podman provides a client for running containers via podman CLI.
package podman

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ensureXDGRuntimeDir ensures XDG_RUNTIME_DIR exists for rootless podman.
// This is required for non-login sessions where systemd hasn't created the directory.
func ensureXDGRuntimeDir() string {
	uid := os.Getuid()
	runtimeDir := fmt.Sprintf("/run/user/%d", uid)

	// Check if directory exists
	if _, err := os.Stat(runtimeDir); os.IsNotExist(err) {
		// Try to create it (may fail without privileges, but that's okay)
		if err := os.MkdirAll(runtimeDir, 0700); err != nil {
			log.Printf("Warning: could not create XDG_RUNTIME_DIR %s: %v", runtimeDir, err)
		} else {
			// Set ownership
			if err := os.Chown(runtimeDir, uid, syscall.Getgid()); err != nil {
				log.Printf("Warning: could not chown XDG_RUNTIME_DIR: %v", err)
			}
			log.Printf("Created XDG_RUNTIME_DIR: %s", runtimeDir)
		}
	}

	return runtimeDir
}

// buildPodmanCmd creates an exec.Cmd with proper environment for rootless podman
func buildPodmanCmd(ctx context.Context, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "podman", args...)

	// Copy existing environment
	cmd.Env = os.Environ()

	// Ensure XDG_RUNTIME_DIR is set
	runtimeDir := ensureXDGRuntimeDir()

	// Check if XDG_RUNTIME_DIR is already set
	hasXDG := false
	for i, env := range cmd.Env {
		if strings.HasPrefix(env, "XDG_RUNTIME_DIR=") {
			hasXDG = true
			// Update it if needed
			cmd.Env[i] = "XDG_RUNTIME_DIR=" + runtimeDir
			break
		}
	}

	if !hasXDG {
		cmd.Env = append(cmd.Env, "XDG_RUNTIME_DIR="+runtimeDir)
	}

	// Also ensure DBUS_SESSION_BUS_ADDRESS is set (some podman operations need it)
	hasDBus := false
	for _, env := range cmd.Env {
		if strings.HasPrefix(env, "DBUS_SESSION_BUS_ADDRESS=") {
			hasDBus = true
			break
		}
	}
	if !hasDBus {
		// Point to user's dbus socket if it exists
		dbusPath := fmt.Sprintf("unix:path=%s/bus", runtimeDir)
		cmd.Env = append(cmd.Env, "DBUS_SESSION_BUS_ADDRESS="+dbusPath)
	}

	// Set HOME if not set
	hasHome := false
	for _, env := range cmd.Env {
		if strings.HasPrefix(env, "HOME=") {
			hasHome = true
			break
		}
	}
	if !hasHome {
		if home := os.Getenv("HOME"); home != "" {
			cmd.Env = append(cmd.Env, "HOME="+home)
		} else {
			// Fall back to /root or user home based on uid
			uid := os.Getuid()
			if uid == 0 {
				cmd.Env = append(cmd.Env, "HOME=/root")
			} else {
				cmd.Env = append(cmd.Env, "HOME=/home/"+strconv.Itoa(uid))
			}
		}
	}

	return cmd
}

// runPodmanCommand keeps diagnostic warnings on stderr out of stdout. Podman
// can emit warnings even when a command succeeds; callers often parse stdout
// as JSON, a digest, a boolean, or a container ID.
func runPodmanCommand(cmd *exec.Cmd) ([]byte, error) {
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err != nil {
		combined := append([]byte(nil), stdout.Bytes()...)
		if len(combined) > 0 && len(stderr.Bytes()) > 0 && combined[len(combined)-1] != '\n' {
			combined = append(combined, '\n')
		}
		combined = append(combined, stderr.Bytes()...)
		return combined, err
	}
	if warning := strings.TrimSpace(stderr.String()); warning != "" {
		log.Printf("Podman warning: %s", warning)
	}
	return stdout.Bytes(), nil
}

// Client provides access to podman via API and CLI
type Client struct {
	socketPath string
	httpClient *http.Client
}

// ProgressCallback is called with pull progress updates
type ProgressCallback func(stage string, progress int, message string)

// NewClient creates a new Podman client that uses the REST API for image pulls
// and CLI for container operations.
func NewClient(socketPath string) *Client {
	if socketPath == "" {
		socketPath = defaultSocketPath()
	}

	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", socketPath)
		},
	}

	return &Client{
		socketPath: socketPath,
		httpClient: &http.Client{Transport: transport},
	}
}

// CheckRootlessNetwork verifies the helper used by Podman's default rootless
// network mode is installed before the host is admitted into scheduling.
func (c *Client) CheckRootlessNetwork() error {
	if _, err := exec.LookPath("pasta"); err != nil {
		return fmt.Errorf(
			"Podman rootless networking requires the pasta executable; install the passt package: %w",
			err,
		)
	}
	return nil
}

// defaultSocketPath returns the default podman socket path for rootless mode.
func defaultSocketPath() string {
	runtimeDir := ensureXDGRuntimeDir()
	return filepath.Join(runtimeDir, "podman", "podman.sock")
}

// ensureService starts the podman system service if the socket is not available.
func (c *Client) ensureService(ctx context.Context) error {
	conn, err := net.DialTimeout("unix", c.socketPath, 2*time.Second)
	if err == nil {
		conn.Close()
		return nil
	}

	log.Printf("Podman socket not available at %s, starting service...", c.socketPath)
	cmd := buildPodmanCmd(ctx, "system", "service", "--time", "120")
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start podman service: %w", err)
	}
	go func() { _ = cmd.Wait() }()

	deadline := time.After(10 * time.Second)
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-deadline:
			return fmt.Errorf("timeout waiting for podman socket at %s", c.socketPath)
		case <-ticker.C:
			conn, err := net.DialTimeout("unix", c.socketPath, time.Second)
			if err == nil {
				conn.Close()
				log.Printf("Podman service ready")
				return nil
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

const (
	// StallTimeout is how long we wait without any output before considering the
	// pull stalled. Podman can legitimately be silent for several minutes while a
	// large layer is unpacked on slow disks or heavily oversubscribed CPUs.
	StallTimeout = 5 * time.Minute

	// Podman's API transport stages large image layers in directories named
	// container_images_storage* under /var/tmp. Interrupted requests can leave
	// those directories behind, so clear only this user's old staging data before
	// starting another serialized pull.
	staleImageStagingAge = time.Hour
	imageStagingPrefix   = "container_images_storage"
)

// pullEvent represents a Docker-compatible image pull progress event.
type pullEvent struct {
	Status         string         `json:"status"`
	ID             string         `json:"id"`
	ProgressDetail progressDetail `json:"progressDetail"`
	Error          string         `json:"error"`
}

type progressDetail struct {
	Current int64 `json:"current"`
	Total   int64 `json:"total"`
}

// PullImage pulls a container image using the Podman REST API with byte-level
// progress reporting. It connects to the Docker-compatible API endpoint which
// streams JSON progress events per layer, giving accurate download percentages.
func (c *Client) PullImage(ctx context.Context, image string, progressCb ProgressCallback) error {
	log.Printf("Pulling image via API: %s", image)
	progressCb("pulling_container", 0, "Downloading container image...")
	if removed, err := cleanupStaleImageStaging(
		"/var/tmp", time.Now().Add(-staleImageStagingAge), os.Getuid(),
	); err != nil {
		log.Printf("Warning: stale Podman image staging cleanup was incomplete: %v", err)
	} else if removed > 0 {
		log.Printf("Removed %d stale Podman image staging directories", removed)
	}

	if err := c.ensureService(ctx); err != nil {
		return fmt.Errorf("ensure podman service: %w", err)
	}

	reqURL := fmt.Sprintf("http://d/v1.40/images/create?fromImage=%s", url.QueryEscape(image))
	req, err := http.NewRequestWithContext(ctx, "POST", reqURL, nil)
	if err != nil {
		return fmt.Errorf("create pull request: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("podman API request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("podman API pull failed (HTTP %d): %s", resp.StatusCode, body)
	}

	// Decode streamed JSON events in a goroutine
	events := make(chan pullEvent)
	decodeErr := make(chan error, 1)
	go func() {
		defer close(events)
		dec := json.NewDecoder(resp.Body)
		for {
			var ev pullEvent
			if err := dec.Decode(&ev); err != nil {
				if err != io.EOF {
					decodeErr <- err
				}
				return
			}
			events <- ev
		}
	}()

	// Per-layer byte tracking for download and extraction phases
	layerTotal := make(map[string]int64)
	layerCurrent := make(map[string]int64)
	extractTotal := make(map[string]int64)
	extractCurrent := make(map[string]int64)
	cachedLayers := 0

	stallTimer := time.NewTimer(StallTimeout)
	defer stallTimer.Stop()

	heartbeat := time.NewTicker(3 * time.Second)
	defer heartbeat.Stop()
	lastMsg := "Downloading container image..."
	lastPct := 0

	for {
		select {
		case ev, ok := <-events:
			if !ok {
				select {
				case err := <-decodeErr:
					return fmt.Errorf("decode pull stream: %w", err)
				default:
				}
				progressCb("container_pulled", 100, "Image downloaded")
				return nil
			}

			stallTimer.Reset(StallTimeout)

			if ev.Error != "" {
				return fmt.Errorf("pull error: %s", ev.Error)
			}

			switch ev.Status {
			case "Downloading":
				if ev.ID != "" && ev.ProgressDetail.Total > 0 {
					layerTotal[ev.ID] = ev.ProgressDetail.Total
					layerCurrent[ev.ID] = ev.ProgressDetail.Current
				}
			case "Download complete":
				if ev.ID != "" {
					if t, ok := layerTotal[ev.ID]; ok {
						layerCurrent[ev.ID] = t
					} else {
						layerTotal[ev.ID] = 0
						layerCurrent[ev.ID] = 0
					}
				}
			case "Extracting":
				if ev.ID != "" && ev.ProgressDetail.Total > 0 {
					extractTotal[ev.ID] = ev.ProgressDetail.Total
					extractCurrent[ev.ID] = ev.ProgressDetail.Current
				}
			case "Pull complete":
				if ev.ID != "" {
					if t, ok := extractTotal[ev.ID]; ok {
						extractCurrent[ev.ID] = t
					} else if t, ok := layerTotal[ev.ID]; ok {
						extractTotal[ev.ID] = t
						extractCurrent[ev.ID] = t
					}
					if t, ok := layerTotal[ev.ID]; ok {
						layerCurrent[ev.ID] = t
					}
				}
			case "Already exists":
				if ev.ID != "" {
					cachedLayers++
					if t, ok := layerTotal[ev.ID]; ok {
						layerCurrent[ev.ID] = t
					} else {
						// Don't add zero-byte entries to layerTotal;
						// they pollute the byte-level progress calculation.
					}
				}
			}

			// Calculate overall byte-level progress
			// Download phase: 0-80%, Extraction phase: 80-100%
			var totalBytes, currentBytes int64
			for id, t := range layerTotal {
				totalBytes += t
				currentBytes += layerCurrent[id]
			}

			var extractTotalBytes, extractCurrentBytes int64
			for id, t := range extractTotal {
				extractTotalBytes += t
				extractCurrentBytes += extractCurrent[id]
			}

			if totalBytes > 0 {
				dlPct := float64(currentBytes) / float64(totalBytes) // 0.0-1.0
				if extractTotalBytes > 0 {
					exPct := float64(extractCurrentBytes) / float64(extractTotalBytes)
					lastPct = int(dlPct*80 + exPct*20) // download: 0-80%, extract: 80-100%
					lastMsg = fmt.Sprintf("Extracting layers (%s / %s)...",
						formatBytes(extractCurrentBytes), formatBytes(extractTotalBytes))
				} else if dlPct >= 1.0 {
					lastPct = 80
					lastMsg = "Preparing container image..."
				} else {
					lastPct = int(dlPct * 80)
					lastMsg = fmt.Sprintf("Downloading layers (%s / %s)...",
						formatBytes(currentBytes), formatBytes(totalBytes))
				}
				if lastPct > 100 {
					lastPct = 100
				}
			} else if cachedLayers > 0 {
				// Some or all layers are cached. Don't jump lastPct to 80 here
				// because downloading layers may arrive next, resetting byte-level
				// progress to near-zero.  The frontend uses Math.max so the
				// premature 80 (scaled to 63%) would lock the display for the
				// entire download.  For fully-cached pulls this phase is
				// near-instant and the final "container_pulled 100" fires
				// immediately after the event stream closes.
				lastMsg = "Using cached image layers..."
			} else if len(layerTotal) > 0 {
				lastMsg = fmt.Sprintf("Downloading layers (%d)...", len(layerTotal))
			}

			progressCb("pulling_container", lastPct, lastMsg)

		case <-heartbeat.C:
			progressCb("pulling_container", lastPct, lastMsg)

		case <-stallTimer.C:
			return fmt.Errorf("podman pull stalled: no progress for %s", StallTimeout)

		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func cleanupStaleImageStaging(root string, cutoff time.Time, uid int) (int, error) {
	entries, err := os.ReadDir(root)
	if os.IsNotExist(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}

	removed := 0
	var cleanupErrors []error
	for _, entry := range entries {
		if !imageStagingDirectoryName(entry.Name()) {
			continue
		}
		path := filepath.Join(root, entry.Name())
		// Re-read metadata immediately before removal. In addition to the strict
		// name, require a real directory owned by the agent and old enough that it
		// cannot belong to the preceding pull request.
		info, statErr := os.Lstat(path)
		if statErr != nil {
			if !os.IsNotExist(statErr) {
				cleanupErrors = append(cleanupErrors, fmt.Errorf("inspect %s: %w", path, statErr))
			}
			continue
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || !info.IsDir() || int(stat.Uid) != uid || !info.ModTime().Before(cutoff) {
			continue
		}
		if removeErr := os.RemoveAll(path); removeErr != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("remove %s: %w", path, removeErr))
			continue
		}
		removed++
	}
	return removed, errors.Join(cleanupErrors...)
}

func imageStagingDirectoryName(name string) bool {
	if !strings.HasPrefix(name, imageStagingPrefix) {
		return false
	}
	suffix := strings.TrimPrefix(name, imageStagingPrefix)
	if suffix == "" {
		return false
	}
	for _, character := range suffix {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

// formatBytes formats a byte count as a human-readable string.
func formatBytes(b int64) string {
	const (
		KB = 1024
		MB = KB * 1024
		GB = MB * 1024
	)
	switch {
	case b >= GB:
		return fmt.Sprintf("%.2f GB", float64(b)/float64(GB))
	case b >= MB:
		return fmt.Sprintf("%.1f MB", float64(b)/float64(MB))
	case b >= KB:
		return fmt.Sprintf("%.0f KB", float64(b)/float64(KB))
	default:
		return fmt.Sprintf("%d B", b)
	}
}

// ContainerConfig holds configuration for creating a TF2 container
type ContainerConfig struct {
	Name              string
	Image             string
	ReservationNumber int
	Location          string
	LocationCity      string
	Password          string
	RCONPassword      string
	TVPassword        string
	FirstMap          string
	LogSecret         string
	DemosTFAPIKey     string
	LogsTFAPIKey      string
	MOTDURL           string
	// Server settings from config
	FastDLURL      string
	HostnameFormat string
	AdminSteamIDs  []string
	// External host ports. Zero values preserve the cloud defaults.
	GamePort int
	TVPort   int
	Labels   map[string]string
}

// BuildRunArgs builds deterministic rootless Podman arguments. RCON remains
// reachable only inside the container; only game and SourceTV ports are bound.
func BuildRunArgs(cfg ContainerConfig) []string {
	// Build hostname from format string (e.g., "My Server #{number} | {location}")
	hostname := cfg.HostnameFormat
	hostname = strings.ReplaceAll(hostname, "{number}", fmt.Sprintf("%d", cfg.ReservationNumber))
	hostname = strings.ReplaceAll(hostname, "{location}", strings.Title(cfg.Location))
	hostname = strings.ReplaceAll(hostname, "{location_city}", cfg.LocationCity)

	// Build FastDL map download URL from base FastDL URL
	mapDownloadURL := cfg.FastDLURL
	if !strings.HasSuffix(mapDownloadURL, "/") {
		mapDownloadURL += "/"
	}
	mapDownloadURL += "maps/"
	instantRuntime := cfg.GamePort != 0 || cfg.TVPort != 0
	gamePort := cfg.GamePort
	if gamePort == 0 {
		gamePort = 27015
	}
	tvPort := cfg.TVPort
	if tvPort == 0 {
		tvPort = 27020
	}

	// Build podman run command
	args := []string{
		"run",
		"-d", // Detached mode
		"--name", cfg.Name,
		"--rm", // Auto-remove when stopped
	}
	// Instant hosts deliberately publish UDP only. Source RCON listens on the
	// game server's TCP port, so a TCP mapping would make it public. Preserve
	// the legacy cloud mapping exactly for disposable single-server VMs.
	if !instantRuntime {
		args = append(args, "-p", fmt.Sprintf("%d:27015/tcp", gamePort))
	}
	args = append(args,
		"-p", fmt.Sprintf("%d:27015/udp", gamePort),
		"-p", fmt.Sprintf("%d:27020/udp", tvPort), // STV
		// Environment variables
		"-e", fmt.Sprintf("SERVER_PASSWORD=%s", cfg.Password),
		"-e", fmt.Sprintf("RCON_PASSWORD=%s", cfg.RCONPassword),
		"-e", fmt.Sprintf("STV_PASSWORD=%s", cfg.TVPassword),
		"-e", fmt.Sprintf("SERVER_HOSTNAME=%s", hostname),
		"-e", "STV_NAME=SourceTV",
		"-e", "ENABLE_FAKE_IP=1",
		"-e", fmt.Sprintf("DOWNLOAD_URL=%s", cfg.FastDLURL),
		"-e", fmt.Sprintf("SM_MAP_DOWNLOAD_BASE=%s", mapDownloadURL),
		"-e", fmt.Sprintf("DEMOS_TF_APIKEY=%s", cfg.DemosTFAPIKey),
		"-e", fmt.Sprintf("LOGS_TF_APIKEY=%s", cfg.LogsTFAPIKey),
		"-e", fmt.Sprintf("MOTD_URL=%s", cfg.MOTDURL),
	)
	labelKeys := make([]string, 0, len(cfg.Labels))
	for key := range cfg.Labels {
		labelKeys = append(labelKeys, key)
	}
	sort.Strings(labelKeys)
	for _, key := range labelKeys {
		args = append(args, "--label", fmt.Sprintf("%s=%s", key, cfg.Labels[key]))
	}

	// Pass site admins as SourceMod admins
	if len(cfg.AdminSteamIDs) > 0 {
		args = append(args, "-e", fmt.Sprintf("SM_ADMINS=%s", strings.Join(cfg.AdminSteamIDs, ",")))
	}

	// Image and command
	args = append(args,
		cfg.Image,
		"+map", "cp_badlands", // Start map (will be changed via RCON)
	)
	return args
}

// StartContainer creates and starts the TF2 container using podman CLI
func (c *Client) StartContainer(ctx context.Context, cfg ContainerConfig) (string, error) {
	log.Printf("Starting container: %s", cfg.Name)
	args := BuildRunArgs(cfg)

	cmd := buildPodmanCmd(ctx, args...)
	output, err := runPodmanCommand(cmd)
	if err != nil {
		log.Printf("Container start failed: %s", string(output))
		return "", fmt.Errorf("podman run failed: %w (output: %s)", err, string(output))
	}

	containerID := strings.TrimSpace(string(output))
	shortID := containerID
	if len(shortID) > 12 {
		shortID = shortID[:12]
	}
	log.Printf("Container created and started: %s", shortID)
	return containerID, nil
}

// StopContainer stops a running container using podman CLI
func (c *Client) StopContainer(ctx context.Context, containerID string) error {
	shortID := containerID
	if len(containerID) > 12 {
		shortID = containerID[:12]
	}
	log.Printf("Stopping container: %s", shortID)

	cmd := buildPodmanCmd(ctx, "stop", "-t", "10", containerID)
	output, err := cmd.CombinedOutput()
	if err != nil {
		lower := strings.ToLower(string(output))
		if strings.Contains(lower, "no such container") || strings.Contains(lower, "not found") {
			return nil
		}
		log.Printf("Stop failed: %s", string(output))
		return fmt.Errorf("podman stop failed: %w (output: %s)", err, string(output))
	}

	log.Println("Container stopped")
	return nil
}

// ManagedContainer is one labeled TF2 container discovered after an agent restart.
type ManagedContainer struct {
	ID     string            `json:"container_id"`
	Name   string            `json:"name"`
	State  string            `json:"state"`
	Labels map[string]string `json:"labels"`
	Stats  map[string]any    `json:"stats,omitempty"`
}

// ListManagedContainers returns complete Instant container inventory from labels.
func (c *Client) ListManagedContainers(ctx context.Context) ([]ManagedContainer, error) {
	cmd := buildPodmanCmd(ctx, "ps", "-a", "--filter", "label=summon.runtime=instant", "--format", "json")
	output, err := runPodmanCommand(cmd)
	if err != nil {
		return nil, fmt.Errorf("podman ps: %w (output: %s)", err, string(output))
	}
	var raw []struct {
		ID     string            `json:"Id"`
		IDAlt  string            `json:"ID"`
		Names  []string          `json:"Names"`
		State  string            `json:"State"`
		Status string            `json:"Status"`
		Labels map[string]string `json:"Labels"`
	}
	if len(strings.TrimSpace(string(output))) == 0 {
		return nil, nil
	}
	if err := json.Unmarshal(output, &raw); err != nil {
		return nil, fmt.Errorf("parse podman inventory: %w", err)
	}
	containers := make([]ManagedContainer, 0, len(raw))
	for _, item := range raw {
		id := item.ID
		if id == "" {
			id = item.IDAlt
		}
		name := ""
		if len(item.Names) > 0 {
			name = item.Names[0]
		}
		state := item.State
		if state == "" {
			state = item.Status
		}
		containers = append(containers, ManagedContainer{
			ID: id, Name: name, State: state, Labels: item.Labels,
		})
	}
	return containers, nil
}

// ContainerStats returns one no-stream Podman stats sample.
func (c *Client) ContainerStats(ctx context.Context, containerID string) (map[string]any, error) {
	cmd := buildPodmanCmd(ctx, "stats", "--no-stream", "--format", "json", containerID)
	output, err := runPodmanCommand(cmd)
	if err != nil {
		return nil, fmt.Errorf("podman stats: %w (output: %s)", err, string(output))
	}
	var rows []map[string]any
	if err := json.Unmarshal(output, &rows); err != nil {
		return nil, fmt.Errorf("parse podman stats: %w", err)
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0], nil
}

// ImageDigest resolves a prepared mutable image reference to its immutable digest.
func (c *Client) ImageDigest(ctx context.Context, image string) (string, error) {
	cmd := buildPodmanCmd(ctx, "image", "inspect", "--format", "{{.Digest}}", image)
	output, err := runPodmanCommand(cmd)
	if err != nil {
		return "", fmt.Errorf("podman image inspect: %w (output: %s)", err, string(output))
	}
	digest := strings.TrimSpace(string(output))
	if digest == "" || digest == "<no value>" {
		return "", fmt.Errorf("image has no repository digest")
	}
	return digest, nil
}

// GetContainerStatus returns whether the container is running using podman CLI
func (c *Client) GetContainerStatus(ctx context.Context, containerID string) (bool, error) {
	cmd := buildPodmanCmd(ctx, "inspect", "--format", "{{.State.Running}}", containerID)
	output, err := runPodmanCommand(cmd)
	if err != nil {
		// Container might not exist
		return false, nil
	}

	running := strings.TrimSpace(string(output)) == "true"
	return running, nil
}

func execCommandLabel(cmd []string) string {
	if len(cmd) == 0 {
		return "command"
	}
	return filepath.Base(cmd[0])
}

// ExecInContainer executes a command inside a running container using podman CLI
func (c *Client) ExecInContainer(ctx context.Context, containerID string, cmd []string) error {
	// Handle short container IDs
	shortID := containerID
	if len(containerID) > 12 {
		shortID = containerID[:12]
	}
	log.Printf("Executing %s in container %s", execCommandLabel(cmd), shortID)

	// Build podman exec command
	args := append([]string{"exec", containerID}, cmd...)
	execCmd := buildPodmanCmd(ctx, args...)

	output, err := execCmd.CombinedOutput()
	outputStr := strings.TrimSpace(string(output))

	if err != nil {
		return fmt.Errorf("podman exec failed: %w (output: %s)", err, outputStr)
	}

	log.Printf("Exec completed successfully. Output: %s", outputStr)
	return nil
}

// CopyFromContainer copies a file or directory from a container to the host.
func (c *Client) CopyFromContainer(ctx context.Context, containerID, srcPath, dstPath string) error {
	args := []string{"cp", containerID + ":" + srcPath, dstPath}
	cmd := buildPodmanCmd(ctx, args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("podman cp failed: %w (output: %s)", err, strings.TrimSpace(string(output)))
	}
	return nil
}

// ExecInContainerWithOutput executes a command inside a running container and returns the output.
// This is useful for commands where we need to capture and parse the output (like RCON status).
func (c *Client) ExecInContainerWithOutput(ctx context.Context, containerID string, cmd []string) (string, error) {
	// Handle short container IDs for logging
	shortID := containerID
	if len(containerID) > 12 {
		shortID = containerID[:12]
	}
	log.Printf("Executing %s in container %s (with output capture)", execCommandLabel(cmd), shortID)

	// Build podman exec command
	args := append([]string{"exec", containerID}, cmd...)
	execCmd := buildPodmanCmd(ctx, args...)

	output, err := execCmd.CombinedOutput()
	outputStr := strings.TrimSpace(string(output))

	if err != nil {
		log.Printf("Exec failed with error: %v, output: %s", err, outputStr)
		return "", fmt.Errorf("podman exec failed: %w (output: %s)", err, outputStr)
	}

	return outputStr, nil
}
