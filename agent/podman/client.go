// Package podman provides a client for running containers via podman CLI.
package podman

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
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

// StallTimeout is how long we wait without any output before considering the pull stalled.
const StallTimeout = 90 * time.Second

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
	MOTDURL string
	// Server settings from config
	FastDLURL      string
	HostnameFormat string
	AdminSteamIDs  []string
}

// StartContainer creates and starts the TF2 container using podman CLI
func (c *Client) StartContainer(ctx context.Context, cfg ContainerConfig) (string, error) {
	log.Printf("Starting container: %s", cfg.Name)

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

	// Build podman run command
	args := []string{
		"run",
		"-d", // Detached mode
		"--name", cfg.Name,
		"--rm", // Auto-remove when stopped
		// Port mappings
		"-p", "27015:27015/tcp",
		"-p", "27015:27015/udp",
		"-p", "27020:27020/udp", // STV
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

	cmd := buildPodmanCmd(ctx, args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("Container start failed: %s", string(output))
		return "", fmt.Errorf("podman run failed: %w (output: %s)", err, string(output))
	}

	containerID := strings.TrimSpace(string(output))
	log.Printf("Container created and started: %s", containerID[:12])
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
		log.Printf("Stop failed: %s", string(output))
		return fmt.Errorf("podman stop failed: %w (output: %s)", err, string(output))
	}

	log.Println("Container stopped")
	return nil
}

// GetContainerStatus returns whether the container is running using podman CLI
func (c *Client) GetContainerStatus(ctx context.Context, containerID string) (bool, error) {
	cmd := buildPodmanCmd(ctx, "inspect", "--format", "{{.State.Running}}", containerID)
	output, err := cmd.CombinedOutput()
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
