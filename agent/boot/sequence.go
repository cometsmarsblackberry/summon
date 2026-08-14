// Package boot manages the server boot sequence.
package boot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/summon/agent/podman"
	"github.com/summon/agent/s3upload"
	"github.com/summon/agent/sdr"
	"github.com/summon/agent/websocket"
)

const (
	// MaxRetries is the maximum number of retries for each step
	MaxRetries = 3
	// RetryDelay is the base delay between retries (exponential backoff)
	RetryDelay = 5 * time.Second
	// RCONReadyTimeout is the max time to wait for RCON to become available
	// Startup can include the image's bounded map prefetch before SRCDS begins.
	// Ninety seconds leaves the prefetch up to thirty seconds while preserving
	// the previous sixty-second allowance for the game server itself.
	RCONReadyTimeout = 90 * time.Second
	// RCONProbeTimeout bounds one readiness check so an unresponsive RCON
	// process cannot prevent the overall readiness deadline from being observed.
	RCONProbeTimeout = 2 * time.Second
	// RCONPollInterval is the delay between RCON readiness checks
	RCONPollInterval = 1 * time.Second
)

// ServerSettings holds deployment-wide server settings
type ServerSettings struct {
	FastDLURL      string `json:"fastdl_url"`
	HostnameFormat string `json:"hostname_format"`
}

// S3Config holds S3-compatible storage settings for log uploads
type S3Config struct {
	Endpoint  string `json:"endpoint"`
	AccessKey string `json:"access_key"`
	SecretKey string `json:"secret_key"`
	Bucket    string `json:"bucket"`
	Region    string `json:"region"`
}

// Configured returns true if all required S3 fields are set
func (c *S3Config) Configured() bool {
	return c.Endpoint != "" && c.AccessKey != "" && c.SecretKey != "" && c.Bucket != ""
}

// ReservationConfig holds the reservation configuration
type ReservationConfig struct {
	ReservationID       int            `json:"reservation_id"`
	ReservationNumber   int            `json:"reservation_number"`
	Location            string         `json:"location"`
	LocationCity        string         `json:"location_city"`
	Password            string         `json:"password"`
	RCONPassword        string         `json:"rcon_password"`
	TVPassword          string         `json:"tv_password"`
	FirstMap            string         `json:"first_map"`
	ConfigFile          string         `json:"config_file"`
	LogSecret           string         `json:"logsecret"`
	OwnerSteamID        string         `json:"owner_steam_id"`
	OwnerName           string         `json:"owner_name"`
	EndsAt              int64          `json:"ends_at"`
	BackendURL          string         `json:"backend_url"`
	InternalAPIKey      string         `json:"internal_api_key"`
	ContainerImage      string         `json:"container_image"`
	DemosTFAPIKey       string         `json:"demos_tf_apikey"`
	LogsTFAPIKey        string         `json:"logs_tf_apikey"`
	EnableLogsTFUpload  bool           `json:"enable_logs_tf_upload"`
	EnableDemosTFUpload bool           `json:"enable_demos_tf_upload"`
	MOTDURL             string         `json:"motd_url"`
	EnableDirectConnect bool           `json:"enable_direct_connect"`
	ServerSettings      ServerSettings `json:"server_settings"`
	AdminSteamIDs       []string       `json:"admin_steam_ids"`
	// S3 storage for server log uploads (optional)
	S3Config S3Config `json:"s3_config"`
	// Added for warm pool reconfigure - allows agent to reconnect after restart
	AuthToken  string `json:"auth_token,omitempty"`
	InstanceID string `json:"instance_id,omitempty"`
	// Persistent-host runtime context. Zero values preserve cloud behavior.
	ExternalGamePort int               `json:"external_game_port,omitempty"`
	ExternalTVPort   int               `json:"external_tv_port,omitempty"`
	ContainerName    string            `json:"container_name,omitempty"`
	StateDir         string            `json:"state_dir,omitempty"`
	Labels           map[string]string `json:"labels,omitempty"`
}

// Reporter receives lifecycle events from a boot sequence. Both the legacy
// cloud WebSocket client and a slot-scoped host reporter implement it.
type Reporter interface {
	SendBootProgress(stage string, progress int, message string) error
	SendServerReady(ip string, port, tvPort int) error
	SendServerReadyWithSDR(info websocket.ServerReadyInfo) error
	SendCompetitiveConfigs(configs []string, containerImage string) error
}

// Sequence manages the boot sequence
type Sequence struct {
	ws          Reporter
	podman      *podman.Client
	config      *ReservationConfig
	containerID string
}

var (
	competitiveConfigsMu       sync.Mutex
	competitiveConfigsReported bool
	competitiveConfigsImage    string
	safeMapNameRE              = regexp.MustCompile(`^[A-Za-z0-9_]{1,64}$`)
	safeConfigNameRE           = regexp.MustCompile(`^[A-Za-z0-9_]{1,64}$`)
)

// NewSequence creates a new boot sequence
func NewSequence(ws Reporter) *Sequence {
	return &Sequence{
		ws:     ws,
		podman: podman.NewClient(""),
	}
}

// NewSequenceWithConfig creates a new boot sequence with a pre-loaded config.
// Used for warm pool reconfiguration where config comes from WebSocket, not disk.
func NewSequenceWithConfig(ws Reporter, config *ReservationConfig) *Sequence {
	return &Sequence{
		ws:     ws,
		podman: podman.NewClient(""),
		config: config,
	}
}

// NewAttachedSequence reconstructs a sequence for a labeled container found
// after a persistent host-agent restart. It does not start another container.
func NewAttachedSequence(ws Reporter, config *ReservationConfig, containerID string) *Sequence {
	return &Sequence{
		ws:          ws,
		podman:      podman.NewClient(""),
		config:      config,
		containerID: containerID,
	}
}

// LoadReservationConfig reads the slot-local configuration persisted by
// SaveConfig. Host mode uses this to reattach RCON and upload operations after
// either the backend or agent restarts.
func LoadReservationConfig(stateDir string) (*ReservationConfig, error) {
	if stateDir == "" {
		return nil, fmt.Errorf("state directory is required")
	}
	data, err := os.ReadFile(filepath.Join(stateDir, "reservation.json"))
	if err != nil {
		return nil, fmt.Errorf("read reservation config: %w", err)
	}
	var config ReservationConfig
	if err := json.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("parse reservation config: %w", err)
	}
	return &config, nil
}

// retry executes a function with exponential backoff
func retry(name string, maxRetries int, fn func() error) error {
	var lastErr error
	for attempt := 1; attempt <= maxRetries; attempt++ {
		lastErr = fn()
		if lastErr == nil {
			return nil
		}

		if attempt < maxRetries {
			delay := RetryDelay * time.Duration(attempt) // Exponential backoff
			log.Printf("%s failed (attempt %d/%d): %v. Retrying in %v...",
				name, attempt, maxRetries, lastErr, delay)
			time.Sleep(delay)
		}
	}
	return fmt.Errorf("%s failed after %d attempts: %w", name, maxRetries, lastErr)
}

func quoteRCONValue(value string) string {
	sanitized := strings.Map(func(r rune) rune {
		switch {
		case r == '\r' || r == '\n' || r == ';':
			return ' '
		case r == '"':
			return '\''
		case r < 0x20:
			return ' '
		default:
			return r
		}
	}, value)
	sanitized = strings.Join(strings.Fields(sanitized), " ")
	sanitized = strings.ReplaceAll(sanitized, `\`, `\\`)
	return sanitized
}

func formatRCONSetString(name, value string) string {
	return fmt.Sprintf(`%s "%s"`, name, quoteRCONValue(value))
}

func isSafeMapName(name string) bool {
	return safeMapNameRE.MatchString(strings.TrimSpace(name))
}

func desiredMapAlreadyLoaded(desiredMap string, serverInfo *sdr.ServerInfo, statusErr error) bool {
	if statusErr != nil || serverInfo == nil {
		return false
	}
	return strings.EqualFold(
		strings.TrimSpace(serverInfo.Map),
		strings.TrimSpace(desiredMap),
	)
}

// ensureDesiredMap preserves the RCON-based startup path for older images but
// avoids an unnecessary level reload when a newer image already prefetched and
// started directly on the reservation's requested map.
func (s *Sequence) ensureDesiredMap(ctx context.Context, progress int, phase string) {
	desiredMap := strings.TrimSpace(s.config.FirstMap)
	if desiredMap == "" || strings.EqualFold(desiredMap, "cp_badlands") {
		return
	}

	phaseSuffix := ""
	if phase != "" {
		phaseSuffix = fmt.Sprintf(" (%s)", phase)
	}
	if !isSafeMapName(desiredMap) {
		log.Printf("Skipping unsafe map name during boot%s: %q", phaseSuffix, desiredMap)
		return
	}

	statusCtx, cancelStatus := context.WithTimeout(ctx, RCONProbeTimeout)
	serverInfo, statusErr := s.getServerInfo(statusCtx)
	cancelStatus()
	if desiredMapAlreadyLoaded(desiredMap, serverInfo, statusErr) {
		log.Printf("Server already started on desired map %s%s", desiredMap, phaseSuffix)
		return
	}
	if statusErr != nil {
		log.Printf(
			"Unable to confirm startup map%s: %v; using legacy changelevel fallback",
			phaseSuffix, statusErr,
		)
	} else if serverInfo == nil || strings.TrimSpace(serverInfo.Map) == "" {
		log.Printf("Startup map was not reported%s; using legacy changelevel fallback", phaseSuffix)
	} else {
		log.Printf(
			"Server started on %s instead of %s%s; using legacy changelevel fallback",
			serverInfo.Map, desiredMap, phaseSuffix,
		)
	}

	log.Printf("Boot stage: changing_map to %s%s", desiredMap, phaseSuffix)
	s.ws.SendBootProgress("changing_map", progress, fmt.Sprintf("Changing map to %s...", desiredMap))

	rconCmd := fmt.Sprintf("changelevel %s", desiredMap)
	changeCtx, cancelChange := context.WithTimeout(ctx, RCONProbeTimeout)
	err := s.podman.ExecInContainer(changeCtx, s.containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", s.config.RCONPassword,
		rconCmd,
	})
	cancelChange()
	if err != nil {
		log.Printf("RCON changelevel failed%s: %v (will use default map)", phaseSuffix, err)
		return
	}

	log.Printf("Map change to %s initiated, waiting for RCON%s", desiredMap, phaseSuffix)
	// Wait for RCON to come back after map change instead of a long fixed sleep.
	time.Sleep(3 * time.Second) // brief grace period for map transition
	if err := s.waitForRCON(ctx); err != nil {
		log.Printf("Warning: RCON not ready after map change%s: %v", phaseSuffix, err)
	}
}

// Run executes the full boot sequence with retries
func (s *Sequence) Run() error {
	ctx := context.Background()

	// Stage 1: Report agent started
	log.Println("Boot stage: agent_started")
	s.ws.SendBootProgress("agent_started", 0, "Agent connected")

	// Stage 2: Load reservation config
	log.Println("Boot stage: loading_config")
	s.ws.SendBootProgress("loading_config", 5, "Loading reservation config...")

	// If the agent was started/reconfigured with a config already (delivered via
	// authenticated WebSocket), skip legacy disk-based loading.
	if s.config == nil {
		if err := s.loadConfig(); err != nil {
			return fmt.Errorf("load config: %w", err)
		}
	} else {
		log.Printf("Using preloaded config for reservation #%d", s.config.ReservationNumber)
	}

	// Stage 3: Pull container image (with retries)
	log.Println("Boot stage: pulling_container")
	pullAttempt := 0
	err := retry("pull image", MaxRetries, func() error {
		pullAttempt++
		if pullAttempt > 1 {
			log.Printf("Retrying image pull (attempt %d)", pullAttempt)
			s.ws.SendBootProgress("pulling_container", 5, fmt.Sprintf("Retrying image download (attempt %d)...", pullAttempt))
		}
		return s.podman.PullImage(ctx, s.config.ContainerImage, func(stage string, progress int, message string) {
			// Scale progress to 5-78% range (pull reports 0-100)
			scaledProgress := 5 + int(float64(progress)*0.73)
			if scaledProgress > 78 {
				scaledProgress = 78
			}
			s.ws.SendBootProgress(stage, scaledProgress, message)
		})
	})
	if err != nil {
		return err
	}

	// Stage 4: Start container (with retries)
	log.Println("Boot stage: starting_container")
	s.ws.SendBootProgress("starting_container", 80, "Starting TF2 server...")

	var containerID string
	err = retry("start container", MaxRetries, func() error {
		var startErr error
		containerName := s.config.ContainerName
		if containerName == "" {
			containerName = fmt.Sprintf("tf2-reservation-%d", s.config.ReservationNumber)
		}
		containerID, startErr = s.podman.StartContainer(ctx, podman.ContainerConfig{
			Name:              containerName,
			Image:             s.config.ContainerImage,
			ReservationNumber: s.config.ReservationNumber,
			Location:          s.config.Location,
			LocationCity:      s.config.LocationCity,
			Password:          s.config.Password,
			RCONPassword:      s.config.RCONPassword,
			TVPassword:        s.config.TVPassword,
			FirstMap:          s.config.FirstMap,
			LogSecret:         s.config.LogSecret,
			DemosTFAPIKey:     enabledAPIKey(s.config.EnableDemosTFUpload, s.config.DemosTFAPIKey),
			LogsTFAPIKey:      enabledAPIKey(s.config.EnableLogsTFUpload, s.config.LogsTFAPIKey),
			MOTDURL:           s.config.MOTDURL,
			FastDLURL:         s.config.ServerSettings.FastDLURL,
			HostnameFormat:    s.config.ServerSettings.HostnameFormat,
			AdminSteamIDs:     s.config.AdminSteamIDs,
			GamePort:          s.config.ExternalGamePort,
			TVPort:            s.config.ExternalTVPort,
			Labels:            s.config.Labels,
		})
		return startErr
	})
	if err != nil {
		return err
	}
	s.containerID = containerID
	s.maybeReportCompetitiveConfigsAsync()

	// Stage 5: Wait for RCON to become available (polls instead of fixed sleep)
	log.Println("Boot stage: waiting_for_server")
	s.ws.SendBootProgress("waiting_for_server", 85, "Waiting for server to initialize...")

	if err := s.waitForRCON(ctx); err != nil {
		return fmt.Errorf("waiting for server: %w", err)
	}
	log.Println("RCON is ready")

	// Stage 6: Keep the legacy changelevel fallback for old images, but skip it
	// when startup-map prefetching already launched the requested map.
	// Do this BEFORE setting passwords, as map changes can reset server settings.
	s.ensureDesiredMap(ctx, 88, "")

	// Stage 7: Set passwords and plugin ConVars via individual RCON commands
	log.Println("Boot stage: configuring server and plugin")
	s.ws.SendBootProgress("configuring_server", 92, "Configuring server settings...")
	if err := s.ensureContainerRunning(ctx, "configuring server"); err != nil {
		return err
	}

	var rconCommands []string
	if configFile := strings.TrimSpace(s.config.ConfigFile); configFile != "" {
		if safeConfigNameRE.MatchString(configFile) {
			rconCommands = append(rconCommands, fmt.Sprintf("sm_config %s", configFile))
		} else {
			log.Printf("Skipping unsafe config name during boot: %q", configFile)
		}
	}
	if s.config.Password != "" {
		rconCommands = append(rconCommands, formatRCONSetString("sv_password", s.config.Password))
	} else {
		log.Println("WARNING: No password configured for reservation!")
	}
	if s.config.TVPassword != "" {
		rconCommands = append(rconCommands, formatRCONSetString("tv_password", s.config.TVPassword))
	}
	rconCommands = append(rconCommands,
		fmt.Sprintf("sm_reserve_owner %s", s.config.OwnerSteamID),
		formatRCONSetString("sm_reserve_owner_name", s.config.OwnerName),
		fmt.Sprintf("sm_reserve_number %d", s.config.ReservationNumber),
		fmt.Sprintf("sm_reserve_ends_at %d", s.config.EndsAt),
		formatRCONSetString("sm_reserve_backend_url", s.config.BackendURL),
		formatRCONSetString("sm_reserve_api_key", s.config.InternalAPIKey),
	)

	if err := s.execRCONCommands(ctx, rconCommands); err != nil {
		log.Printf("RCON config failed: %v", err)
	} else {
		log.Printf("Server configured successfully (%d commands)", len(rconCommands))
	}

	// Stage 8: Detect SDR FakeIP and report server ready
	log.Println("Boot stage: detecting SDR FakeIP")
	s.ws.SendBootProgress("detecting_sdr", 95, "Detecting server address...")
	if err := s.ensureContainerRunning(ctx, "detecting SDR"); err != nil {
		return err
	}

	serverInfo, err := s.getServerInfo(ctx)
	if err != nil {
		log.Printf("Warning: Failed to get server info, using local IP: %v", err)
		// Fall back to local IP if SDR detection fails
		ip := getLocalIP()
		s.ws.SendServerReady(ip, 27015, 27020)
	} else {
		// Send full server info including SDR FakeIP
		readyInfo := websocket.ServerReadyInfo{
			RealIP:     serverInfo.RealIP,
			RealPort:   serverInfo.RealPort,
			RealTVPort: serverInfo.RealTVPort,
			SDRIP:      serverInfo.SDRIP,
			SDRPort:    serverInfo.SDRPort,
			SDRTVPort:  serverInfo.SDRTVPort,
			Map:        serverInfo.Map,
		}

		// Fill in defaults if parsing failed
		if readyInfo.RealIP == "" {
			readyInfo.RealIP = getLocalIP()
		}
		if readyInfo.RealPort == 0 {
			readyInfo.RealPort = 27015
		}
		if readyInfo.RealTVPort == 0 {
			readyInfo.RealTVPort = 27020
		}

		if serverInfo.HasSDR() {
			log.Printf("Boot stage: server_ready with SDR FakeIP (SDR: %s:%d, Real: %s:%d)",
				serverInfo.SDRIP, serverInfo.SDRPort, serverInfo.RealIP, serverInfo.RealPort)
		} else {
			log.Printf("Boot stage: server_ready (Real IP: %s:%d, no SDR detected)",
				serverInfo.RealIP, serverInfo.RealPort)
		}

		s.ws.SendServerReadyWithSDR(readyInfo)
	}

	log.Println("Boot sequence complete!")
	return nil
}

// RunReconfigure performs a boot sequence for warm pool reuse.
// It skips config loading (config comes from WebSocket) and image pull (image already present).
func (s *Sequence) RunReconfigure() error {
	ctx := context.Background()

	// Stage 1: Report reconfigure started
	log.Println("Boot stage: reconfigure_started")
	s.ws.SendBootProgress("reconfigure_started", 10, "Reconfiguring server...")

	// Config is already set via NewSequenceWithConfig, no need to load from disk

	// Stage 2: Start container (with retries) - image is already pulled
	log.Println("Boot stage: starting_container (reconfigure)")
	s.ws.SendBootProgress("starting_container", 30, "Starting TF2 server...")

	var containerID string
	err := retry("start container", MaxRetries, func() error {
		var startErr error
		containerName := s.config.ContainerName
		if containerName == "" {
			containerName = fmt.Sprintf("tf2-reservation-%d", s.config.ReservationNumber)
		}
		containerID, startErr = s.podman.StartContainer(ctx, podman.ContainerConfig{
			Name:              containerName,
			Image:             s.config.ContainerImage,
			ReservationNumber: s.config.ReservationNumber,
			Location:          s.config.Location,
			LocationCity:      s.config.LocationCity,
			Password:          s.config.Password,
			RCONPassword:      s.config.RCONPassword,
			TVPassword:        s.config.TVPassword,
			FirstMap:          s.config.FirstMap,
			LogSecret:         s.config.LogSecret,
			DemosTFAPIKey:     enabledAPIKey(s.config.EnableDemosTFUpload, s.config.DemosTFAPIKey),
			LogsTFAPIKey:      enabledAPIKey(s.config.EnableLogsTFUpload, s.config.LogsTFAPIKey),
			MOTDURL:           s.config.MOTDURL,
			FastDLURL:         s.config.ServerSettings.FastDLURL,
			HostnameFormat:    s.config.ServerSettings.HostnameFormat,
			AdminSteamIDs:     s.config.AdminSteamIDs,
			GamePort:          s.config.ExternalGamePort,
			TVPort:            s.config.ExternalTVPort,
			Labels:            s.config.Labels,
		})
		return startErr
	})
	if err != nil {
		return err
	}
	s.containerID = containerID
	s.maybeReportCompetitiveConfigsAsync()

	// Stage 3: Wait for RCON to become available (polls instead of fixed sleep)
	log.Println("Boot stage: waiting_for_server (reconfigure)")
	s.ws.SendBootProgress("waiting_for_server", 50, "Waiting for server to initialize...")

	if err := s.waitForRCON(ctx); err != nil {
		return fmt.Errorf("waiting for server (reconfigure): %w", err)
	}
	log.Println("RCON is ready (reconfigure)")

	// Stage 4: Keep the legacy changelevel fallback for old images, but skip it
	// when startup-map prefetching already launched the requested map.
	s.ensureDesiredMap(ctx, 60, "reconfigure")

	// Stage 5: Set passwords and plugin ConVars via individual RCON commands
	log.Println("Boot stage: configuring server and plugin (reconfigure)")
	s.ws.SendBootProgress("configuring_server", 75, "Configuring server settings...")
	if err := s.ensureContainerRunning(ctx, "configuring server"); err != nil {
		return err
	}

	var rconCommands []string
	if configFile := strings.TrimSpace(s.config.ConfigFile); configFile != "" {
		if safeConfigNameRE.MatchString(configFile) {
			rconCommands = append(rconCommands, fmt.Sprintf("sm_config %s", configFile))
		} else {
			log.Printf("Skipping unsafe config name during reconfigure: %q", configFile)
		}
	}
	if s.config.Password != "" {
		rconCommands = append(rconCommands, formatRCONSetString("sv_password", s.config.Password))
	}
	if s.config.TVPassword != "" {
		rconCommands = append(rconCommands, formatRCONSetString("tv_password", s.config.TVPassword))
	}
	rconCommands = append(rconCommands,
		fmt.Sprintf("sm_reserve_owner %s", s.config.OwnerSteamID),
		formatRCONSetString("sm_reserve_owner_name", s.config.OwnerName),
		fmt.Sprintf("sm_reserve_number %d", s.config.ReservationNumber),
		fmt.Sprintf("sm_reserve_ends_at %d", s.config.EndsAt),
		formatRCONSetString("sm_reserve_backend_url", s.config.BackendURL),
		formatRCONSetString("sm_reserve_api_key", s.config.InternalAPIKey),
	)

	if err := s.execRCONCommands(ctx, rconCommands); err != nil {
		log.Printf("RCON config failed (reconfigure): %v", err)
	} else {
		log.Printf("Server configured successfully (%d commands, reconfigure)", len(rconCommands))
	}

	// Stage 7: Detect SDR FakeIP and report server ready
	log.Println("Boot stage: detecting SDR FakeIP (reconfigure)")
	s.ws.SendBootProgress("detecting_sdr", 95, "Detecting server address...")
	if err := s.ensureContainerRunning(ctx, "detecting SDR"); err != nil {
		return err
	}

	serverInfo, err := s.getServerInfo(ctx)
	if err != nil {
		log.Printf("Warning: Failed to get server info, using local IP: %v", err)
		ip := getLocalIP()
		s.ws.SendServerReady(ip, 27015, 27020)
	} else {
		readyInfo := websocket.ServerReadyInfo{
			RealIP:     serverInfo.RealIP,
			RealPort:   serverInfo.RealPort,
			RealTVPort: serverInfo.RealTVPort,
			SDRIP:      serverInfo.SDRIP,
			SDRPort:    serverInfo.SDRPort,
			SDRTVPort:  serverInfo.SDRTVPort,
			Map:        serverInfo.Map,
		}

		if readyInfo.RealIP == "" {
			readyInfo.RealIP = getLocalIP()
		}
		if readyInfo.RealPort == 0 {
			readyInfo.RealPort = 27015
		}
		if readyInfo.RealTVPort == 0 {
			readyInfo.RealTVPort = 27020
		}

		if serverInfo.HasSDR() {
			log.Printf("Boot stage: server_ready with SDR FakeIP (SDR: %s:%d, Real: %s:%d)",
				serverInfo.SDRIP, serverInfo.SDRPort, serverInfo.RealIP, serverInfo.RealPort)
		} else {
			log.Printf("Boot stage: server_ready (Real IP: %s:%d, no SDR detected)",
				serverInfo.RealIP, serverInfo.RealPort)
		}

		s.ws.SendServerReadyWithSDR(readyInfo)
	}

	log.Println("Reconfigure boot sequence complete!")
	return nil
}

func enabledAPIKey(enabled bool, apiKey string) string {
	if !enabled {
		return ""
	}
	return apiKey
}

// ConfigureUploads updates logs.tf and demos.tf without restarting the server.
func (s *Sequence) ConfigureUploads(ctx context.Context, logsTF, demosTF bool) error {
	if s.config == nil || s.containerID == "" {
		return fmt.Errorf("container or reservation config not available")
	}

	commands := []string{
		formatRCONSetString("logstf_apikey", enabledAPIKey(logsTF, s.config.LogsTFAPIKey)),
		formatRCONSetString("sm_demostf_apikey", enabledAPIKey(demosTF, s.config.DemosTFAPIKey)),
	}
	if err := s.execRCONCommands(ctx, commands); err != nil {
		return err
	}

	s.config.EnableLogsTFUpload = logsTF
	s.config.EnableDemosTFUpload = demosTF
	return nil
}

func (s *Sequence) maybeReportCompetitiveConfigsAsync() {
	competitiveConfigsMu.Lock()
	alreadyReported := competitiveConfigsReported && competitiveConfigsImage == s.config.ContainerImage
	competitiveConfigsMu.Unlock()
	if alreadyReported {
		return
	}
	if s.ws == nil || s.podman == nil || s.containerID == "" || s.config == nil {
		return
	}

	// Run in background so we don't slow provisioning.
	go func(containerID string, containerImage string) {
		for attempt := 1; attempt <= 3; attempt++ {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			out, err := s.podman.ExecInContainerWithOutput(ctx, containerID, []string{
				"sh", "-lc", "ls -1 /home/tf2/server/tf/cfg/*.cfg 2>/dev/null || true",
			})
			cancel()
			if err != nil {
				log.Printf("Competitive config list attempt %d failed: %v", attempt, err)
				time.Sleep(time.Duration(attempt) * time.Second)
				continue
			}

			var cfgFiles []string
			for _, line := range strings.Split(out, "\n") {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				base := filepath.Base(line)
				if !strings.HasSuffix(base, ".cfg") {
					continue
				}
				stem := strings.TrimSuffix(base, ".cfg")
				if stem == "" {
					continue
				}
				cfgFiles = append(cfgFiles, stem)
			}

			if len(cfgFiles) == 0 {
				log.Printf("Competitive config list attempt %d returned no configs", attempt)
				time.Sleep(time.Duration(attempt) * time.Second)
				continue
			}

			if err := s.ws.SendCompetitiveConfigs(cfgFiles, containerImage); err != nil {
				log.Printf("Failed to send competitive config list: %v", err)
				time.Sleep(time.Duration(attempt) * time.Second)
				continue
			}

			competitiveConfigsMu.Lock()
			competitiveConfigsReported = true
			competitiveConfigsImage = containerImage
			competitiveConfigsMu.Unlock()
			log.Printf("Reported %d competitive configs to backend", len(cfgFiles))
			return
		}
		log.Printf("Giving up reporting competitive config list (non-fatal)")
	}(s.containerID, s.config.ContainerImage)
}

// waitForRCON polls the server's RCON port until it responds or the timeout
// is reached. This replaces hard-coded time.Sleep calls so the boot sequence
// proceeds as soon as the server is actually ready.
func (s *Sequence) waitForRCON(ctx context.Context) error {
	return waitForRCONWithProbe(
		ctx,
		RCONReadyTimeout,
		RCONProbeTimeout,
		RCONPollInterval,
		func(probeCtx context.Context) error {
			_, err := s.podman.ExecInContainerWithOutput(probeCtx, s.containerID, []string{
				"/home/tf2/server/rcon",
				"-H", "127.0.0.1",
				"-p", "27015",
				"-P", s.config.RCONPassword,
				"status",
			})
			return err
		},
	)
}

func waitForRCONWithProbe(
	ctx context.Context,
	readyTimeout time.Duration,
	probeTimeout time.Duration,
	pollInterval time.Duration,
	probe func(context.Context) error,
) error {
	waitCtx, cancelWait := context.WithTimeout(ctx, readyTimeout)
	defer cancelWait()

	for {
		probeCtx, cancelProbe := context.WithTimeout(waitCtx, probeTimeout)
		err := probe(probeCtx)
		cancelProbe()
		if err == nil && waitCtx.Err() == nil {
			return nil
		}
		if waitCtx.Err() != nil {
			return rconWaitError(ctx, readyTimeout)
		}

		timer := time.NewTimer(pollInterval)
		select {
		case <-timer.C:
		case <-waitCtx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			return rconWaitError(ctx, readyTimeout)
		}
	}
}

func rconWaitError(ctx context.Context, readyTimeout time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return fmt.Errorf("RCON not ready after %v", readyTimeout)
}

func (s *Sequence) execRCONCommand(ctx context.Context, command string) error {
	return s.podman.ExecInContainer(ctx, s.containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", s.config.RCONPassword,
		command,
	})
}

// execRCONCommands executes commands one at a time so untrusted values never
// share a semicolon-delimited command buffer.
func (s *Sequence) execRCONCommands(ctx context.Context, commands []string) error {
	for _, command := range commands {
		if err := s.execRCONCommand(ctx, command); err != nil {
			return err
		}
	}
	return nil
}

// getServerInfo runs the status command and parses the output to get server info.
func (s *Sequence) getServerInfo(ctx context.Context) (*sdr.ServerInfo, error) {
	// Run status command via RCON using the container's RCON tool
	output, err := s.podman.ExecInContainerWithOutput(ctx, s.containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", s.config.RCONPassword,
		"status",
	})
	if err != nil {
		return nil, fmt.Errorf("RCON status failed: %w", err)
	}

	return sdr.ParseStatus(output), nil
}

func (s *Sequence) ensureContainerRunning(ctx context.Context, stage string) error {
	if s.containerID == "" {
		return fmt.Errorf("no container ID available")
	}

	running, err := s.podman.GetContainerStatus(ctx, s.containerID)
	if err != nil {
		return fmt.Errorf("check container status: %w", err)
	}
	if !running {
		log.Printf("Container exited before %s", stage)
		return fmt.Errorf("TF2 container exited during startup")
	}

	return nil
}

// loadConfig loads the reservation config from file (written by Ignition)
func (s *Sequence) loadConfig() error {
	var lastReadErr error
	for _, configPath := range ConfigFilePaths() {
		data, err := os.ReadFile(configPath)
		if err != nil {
			lastReadErr = err
			continue
		}

		var config ReservationConfig
		if err := json.Unmarshal(data, &config); err != nil {
			return fmt.Errorf("parse config: %w", err)
		}

		s.config = &config
		log.Printf("Loaded config for reservation #%d from %s", config.ReservationNumber, configPath)
		return nil
	}

	if lastReadErr != nil {
		return fmt.Errorf("read config: %w", lastReadErr)
	}
	return fmt.Errorf("read config: no config file found")
}

// SaveConfig saves the current reservation config to file.
// This is used during warm pool reconfigure to persist new credentials
// so the agent can reconnect if restarted.
func (s *Sequence) SaveConfig() error {
	if s.config == nil {
		return fmt.Errorf("no config to save")
	}

	configPath := ""
	if s.config.StateDir != "" {
		configPath = filepath.Join(s.config.StateDir, "reservation.json")
	} else {
		configPaths := ConfigFilePaths()
		if len(configPaths) == 0 {
			return fmt.Errorf("no config path available")
		}
		configPath = configPaths[0]
	}

	data, err := json.MarshalIndent(s.config, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}

	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	if err := os.WriteFile(configPath, data, 0600); err != nil {
		return fmt.Errorf("write config: %w", err)
	}

	log.Printf("Saved config for reservation #%d to %s", s.config.ReservationNumber, configPath)
	return nil
}

// GetContainerID returns the current container ID
func (s *Sequence) GetContainerID() string {
	return s.containerID
}

// GetConfig returns the current reservation config
func (s *Sequence) GetConfig() *ReservationConfig {
	return s.config
}

// ExecuteRCON runs one command in this sequence's isolated container.
func (s *Sequence) ExecuteRCON(ctx context.Context, command string) (string, error) {
	if s.config == nil || s.containerID == "" {
		return "", fmt.Errorf("container or configuration unavailable")
	}
	return s.podman.ExecInContainerWithOutput(ctx, s.containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", s.config.RCONPassword,
		command,
	})
}

// CollectLogs copies volatile server logs out of the --rm container before it
// is stopped. An empty path means S3 storage is not configured.
func (s *Sequence) CollectLogs(ctx context.Context) (string, error) {
	if s.config == nil || s.containerID == "" || !s.config.S3Config.Configured() {
		return "", nil
	}
	temporary, err := os.MkdirTemp("", "tf2-logs-*")
	if err != nil {
		return "", err
	}
	locations := []struct{ source, target string }{
		{"/home/tf2/server/tf/addons/sourcemod/logs/.", "sourcemod"},
		{"/home/tf2/server/tf/logs/.", "server"},
	}
	var copyErrors []error
	for _, location := range locations {
		destination := filepath.Join(temporary, location.target)
		if err := os.MkdirAll(destination, 0755); err != nil {
			copyErrors = append(copyErrors, err)
			continue
		}
		if err := s.podman.CopyFromContainer(
			ctx, s.containerID, location.source, destination,
		); err != nil {
			copyErrors = append(copyErrors, err)
		}
	}
	if len(copyErrors) == len(locations) {
		_ = os.RemoveAll(temporary)
		return "", fmt.Errorf("copy server logs: %w", errors.Join(copyErrors...))
	}
	return temporary, nil
}

// UploadCollectedLogs uploads a previously collected directory and always
// removes the temporary copy.
func (s *Sequence) UploadCollectedLogs(ctx context.Context, directory string) error {
	if directory == "" {
		return nil
	}
	defer os.RemoveAll(directory)
	if s.config == nil || !s.config.S3Config.Configured() {
		return nil
	}
	config := &s3upload.Config{
		Endpoint: s.config.S3Config.Endpoint, AccessKey: s.config.S3Config.AccessKey,
		SecretKey: s.config.S3Config.SecretKey, Bucket: s.config.S3Config.Bucket,
		Region: s.config.S3Config.Region,
	}
	prefix := fmt.Sprintf("reservations/%d", s.config.ReservationNumber)
	return s3upload.UploadDirectoryTarGz(ctx, config, directory, prefix)
}

// getLocalIP returns the local IP address
func getLocalIP() string {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return "0.0.0.0"
	}

	for _, addr := range addrs {
		if ipnet, ok := addr.(*net.IPNet); ok && !ipnet.IP.IsLoopback() {
			if ipnet.IP.To4() != nil {
				return ipnet.IP.String()
			}
		}
	}

	return "0.0.0.0"
}
