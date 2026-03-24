// Package main is the entry point for the TF2 server agent.
// The agent runs on each Vultr instance and manages the TF2 container.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/summon/agent/boot"
	"github.com/summon/agent/podman"
	"github.com/summon/agent/s3upload"
	"github.com/summon/agent/sysinfo"
	"github.com/summon/agent/websocket"
)

// Config holds agent configuration from environment
type Config struct {
	BackendURL    string
	AuthToken     string
	InstanceID    string
	ReservationID string
	HeartbeatSec  int
}

// Global state
var (
	bootSeq         *boot.Sequence
	podmanClient    *podman.Client
	initialConfigCh = make(chan boot.ReservationConfig, 1)
)

func loadConfig() *Config {
	cfg := &Config{
		BackendURL:    os.Getenv("BACKEND_URL"),
		AuthToken:     os.Getenv("AUTH_TOKEN"),
		InstanceID:    os.Getenv("INSTANCE_ID"),
		ReservationID: os.Getenv("RESERVATION_ID"),
		HeartbeatSec:  10,
	}

	if v := os.Getenv("HEARTBEAT_INTERVAL_SEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 60 {
			cfg.HeartbeatSec = n
		} else {
			log.Printf("Invalid HEARTBEAT_INTERVAL_SEC=%q (expected 1-60); using %ds", v, cfg.HeartbeatSec)
		}
	}

	if cfg.BackendURL == "" {
		log.Fatal("BACKEND_URL environment variable required")
	}
	if cfg.AuthToken == "" {
		log.Fatal("AUTH_TOKEN environment variable required")
	}
	if cfg.InstanceID == "" {
		log.Fatal("INSTANCE_ID environment variable required")
	}

	// Check config file for potentially updated credentials from warm pool reconfigure
	// The config file may have newer auth_token and instance_id if the agent was
	// reconfigured for a different reservation
	for _, configPath := range boot.ConfigFilePaths() {
		data, err := os.ReadFile(configPath)
		if err != nil {
			continue
		}

		var fileConfig struct {
			AuthToken  string `json:"auth_token"`
			InstanceID string `json:"instance_id"`
		}
		if err := json.Unmarshal(data, &fileConfig); err != nil {
			log.Printf("Warning: Failed to parse config file %s: %v", configPath, err)
			continue
		}
		if fileConfig.AuthToken == "" || fileConfig.InstanceID == "" {
			continue
		}

		// Config file has credentials - use them instead of env vars.
		// This happens after a warm pool reconfigure.
		log.Printf("Found updated credentials in config file %s, using instance_id: %s", configPath, fileConfig.InstanceID)
		cfg.AuthToken = fileConfig.AuthToken
		cfg.InstanceID = fileConfig.InstanceID
		break
	}

	return cfg
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	log.Println("TF2 Agent starting...")

	cfg := loadConfig()
	log.Printf("Instance ID: %s", cfg.InstanceID)
	log.Printf("Backend URL: %s", cfg.BackendURL)

	// Initialize Podman client
	podmanClient = podman.NewClient("")

	// Create WebSocket client
	wsClient := websocket.NewClient(cfg.BackendURL, cfg.AuthToken)

	// Connect to backend
	if err := wsClient.Connect(); err != nil {
		log.Fatalf("Failed to connect to backend: %v", err)
	}
	defer wsClient.Close()

	// Start message handler
	go handleMessages(wsClient)

	// Start heartbeat (before boot so stats are available during provisioning)
	go runHeartbeat(wsClient, time.Duration(cfg.HeartbeatSec)*time.Second)

	// Wait for initial config from backend (sent via WebSocket after connect).
	// Fall back to local config file if it exists (backwards compatibility /
	// agent restart after warm pool reconfigure).
	var reservationConfig *boot.ReservationConfig
	select {
	case cfg := <-initialConfigCh:
		log.Println("Received initial config from backend via WebSocket")
		reservationConfig = &cfg
	case <-time.After(30 * time.Second):
		log.Println("Timeout waiting for initial config from backend, checking local config file")
	}

	// Run boot sequence with the received config (or fall back to local file)
	if reservationConfig != nil {
		bootSeq = boot.NewSequenceWithConfig(wsClient, reservationConfig)
		// Save config locally so agent can reconnect after restart
		if err := bootSeq.SaveConfig(); err != nil {
			log.Printf("Warning: Failed to save config: %v", err)
		}
	} else {
		bootSeq = boot.NewSequence(wsClient)
	}
	if err := bootSeq.Run(); err != nil {
		log.Printf("Boot sequence failed: %v", err)
		wsClient.SendBootProgress("boot_failed", 0, err.Error())
		// Keep running to allow debugging
	}

	// Wait for shutdown signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	log.Println("Shutting down agent...")
}

func handleMessages(client *websocket.Client) {
	for msg := range client.Messages() {
		var baseMsg struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(msg, &baseMsg); err != nil {
			log.Printf("Failed to parse message: %v", err)
			continue
		}

		switch baseMsg.Type {
		case "container.initial_config":
			log.Println("Received initial config from backend")
			handleInitialConfig(msg)
		case "container.stop":
			log.Println("Received container.stop command")
			handleContainerStop()
		case "container.reconfigure":
			log.Println("Received container.reconfigure command")
			handleContainerReconfigure(msg, client)
		case "container.restart":
			log.Println("Received container.restart command")
			handleContainerRestart(msg, client)
		case "rcon":
			log.Println("Received RCON command")
			handleRconCommand(msg, client)
		case "reservation.end":
			log.Println("Received reservation.end command")
			handleReservationEnd()
		default:
			log.Printf("Unknown message type: %s", baseMsg.Type)
		}
	}
}

func handleInitialConfig(msg []byte) {
	var configMsg struct {
		Type   string                 `json:"type"`
		Config boot.ReservationConfig `json:"config"`
	}
	if err := json.Unmarshal(msg, &configMsg); err != nil {
		log.Printf("Failed to parse initial config: %v", err)
		return
	}

	// Non-blocking send — if the channel already has a value (e.g., duplicate
	// message), we skip silently.
	select {
	case initialConfigCh <- configMsg.Config:
		log.Printf("Initial config queued for reservation #%d", configMsg.Config.ReservationNumber)
	default:
		log.Println("Initial config channel full, ignoring duplicate")
	}
}

func handleContainerStop() {
	containerID := ""
	if bootSeq != nil {
		containerID = bootSeq.GetContainerID()
	}

	if containerID == "" {
		log.Println("No container to stop")
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := podmanClient.StopContainer(ctx, containerID); err != nil {
		log.Printf("Failed to stop container: %v", err)
	} else {
		log.Println("Container stopped successfully")
	}
}

func handleContainerReconfigure(msg []byte, client *websocket.Client) {
	// Parse the reconfigure message containing new reservation config
	var reconfigMsg struct {
		Type   string                 `json:"type"`
		Config boot.ReservationConfig `json:"config"`
	}
	if err := json.Unmarshal(msg, &reconfigMsg); err != nil {
		log.Printf("Failed to parse container.reconfigure message: %v", err)
		client.SendBootProgress("boot_failed", 0, fmt.Sprintf("Failed to parse reconfigure: %v", err))
		return
	}

	log.Printf("Reconfiguring for reservation #%d", reconfigMsg.Config.ReservationNumber)
	client.SendBootProgress("reconfiguring", 5, "Reconfiguring server for new reservation...")

	// Stop existing container (if any)
	if bootSeq != nil && bootSeq.GetContainerID() != "" {
		log.Println("Stopping existing container for reconfigure...")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		if err := podmanClient.StopContainer(ctx, bootSeq.GetContainerID()); err != nil {
			log.Printf("Failed to stop existing container: %v (continuing anyway)", err)
		}
		cancel()
	}

	// Create new boot sequence with the received config
	bootSeq = boot.NewSequenceWithConfig(client, &reconfigMsg.Config)

	// Save the new config to disk so agent can reconnect after restart
	if err := bootSeq.SaveConfig(); err != nil {
		log.Printf("Warning: Failed to save config: %v (agent may fail to reconnect after restart)", err)
	}

	// Run reconfigure sequence (skips image pull since image is already present)
	go func() {
		if err := bootSeq.RunReconfigure(); err != nil {
			log.Printf("Reconfigure boot sequence failed: %v", err)
			client.SendBootProgress("boot_failed", 0, err.Error())
		}
	}()
}

func handleContainerRestart(msg json.RawMessage, client *websocket.Client) {
	if bootSeq == nil || bootSeq.GetConfig() == nil {
		log.Println("No boot config available to restart container")
		client.SendBootProgress("boot_failed", 0, "Restart failed: missing boot config")
		return
	}

	containerID := bootSeq.GetContainerID()
	if containerID != "" {
		client.SendBootProgress("restarting", 10, "Stopping TF2 server...")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		if err := podmanClient.StopContainer(ctx, containerID); err != nil {
			log.Printf("Failed to stop container for restart: %v", err)
		}
		cancel()
	}

	// Apply updated config fields (e.g. regenerated passwords)
	cfg := bootSeq.GetConfig()
	var restartMsg struct {
		Config *struct {
			Password     string `json:"password"`
			RCONPassword string `json:"rcon_password"`
			TVPassword   string `json:"tv_password"`
		} `json:"config"`
	}
	if err := json.Unmarshal(msg, &restartMsg); err == nil && restartMsg.Config != nil {
		if restartMsg.Config.Password != "" {
			cfg.Password = restartMsg.Config.Password
		}
		if restartMsg.Config.RCONPassword != "" {
			cfg.RCONPassword = restartMsg.Config.RCONPassword
		}
		if restartMsg.Config.TVPassword != "" {
			cfg.TVPassword = restartMsg.Config.TVPassword
		}
		log.Println("Applied updated passwords from restart config")
	}

	client.SendBootProgress("restarting", 30, "Starting TF2 server...")

	bootSeq = boot.NewSequenceWithConfig(client, cfg)
	go func() {
		if err := bootSeq.RunReconfigure(); err != nil {
			log.Printf("Restart boot sequence failed: %v", err)
			client.SendBootProgress("boot_failed", 0, err.Error())
		}
	}()
}

func handleRconCommand(msg json.RawMessage, client *websocket.Client) {
	var rconMsg struct {
		Command string `json:"command"`
	}
	if err := json.Unmarshal(msg, &rconMsg); err != nil || rconMsg.Command == "" {
		log.Println("Invalid or empty RCON command")
		return
	}

	containerID := ""
	rconPassword := ""
	if bootSeq != nil {
		containerID = bootSeq.GetContainerID()
		if cfg := bootSeq.GetConfig(); cfg != nil {
			rconPassword = cfg.RCONPassword
		}
	}

	if containerID == "" || rconPassword == "" {
		log.Println("Cannot execute RCON: container or config not available")
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	output, err := podmanClient.ExecInContainerWithOutput(ctx, containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", rconPassword,
		rconMsg.Command,
	})
	if err != nil {
		log.Printf("RCON command failed: %v", err)
	} else {
		log.Printf("RCON command executed: %s", output)
	}
}

func runHeartbeat(client *websocket.Client, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for range ticker.C {
		stats := sysinfo.Collect()
		extra := map[string]interface{}{
			"sysinfo": stats,
		}
		if err := client.SendStatus("running", stats.UptimeSec, extra); err != nil {
			log.Printf("Failed to send heartbeat: %v", err)
		}
	}
}

func handleReservationEnd() {
	containerID := ""
	rconPassword := ""
	var cfg *boot.ReservationConfig
	if bootSeq != nil {
		containerID = bootSeq.GetContainerID()
		cfg = bootSeq.GetConfig()
		if cfg != nil {
			rconPassword = cfg.RCONPassword
		}
	}

	if containerID == "" || rconPassword == "" {
		log.Println("Cannot handle reservation.end: container or config not available")
		return
	}

	// Trigger the plugin's end sequence via RCON
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	err := podmanClient.ExecInContainer(ctx, containerID, []string{
		"/home/tf2/server/rcon",
		"-H", "127.0.0.1",
		"-p", "27015",
		"-P", rconPassword,
		"sm_reservation_ending",
	})
	if err != nil {
		log.Printf("Failed to execute sm_reservation_ending: %v", err)
	} else {
		log.Println("Reservation end triggered via plugin")
	}

	// Wait for kicks to complete
	time.Sleep(10 * time.Second)

	// Copy logs from container before stopping (container has --rm, filesystem
	// is lost on stop). The podman cp is a fast local operation.
	var logTmpDir string
	if cfg != nil && cfg.S3Config.Configured() {
		logTmpDir = copyLogsFromContainer(containerID)
	}

	handleContainerStop()

	// Upload to S3 in a background goroutine after the container is stopped.
	// The agent still runs on the cloud instance — if the instance is destroyed
	// before the upload finishes, we lose the logs (acceptable for rare debugging).
	if logTmpDir != "" {
		go uploadLogsToS3(logTmpDir, cfg)
	}
}

// copyLogsFromContainer copies TF2 server and SourceMod logs from the
// container to a temporary directory on the host. Returns the temp dir path
// (caller must clean up), or "" on failure.
func copyLogsFromContainer(containerID string) string {
	tmpDir, err := os.MkdirTemp("", "tf2-logs-*")
	if err != nil {
		log.Printf("S3 log upload: failed to create temp dir: %v", err)
		return ""
	}

	// Copy SourceMod logs
	smDst := filepath.Join(tmpDir, "sourcemod")
	os.MkdirAll(smDst, 0755)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := podmanClient.CopyFromContainer(ctx, containerID,
		"/home/tf2/server/tf/addons/sourcemod/logs/.", smDst); err != nil {
		log.Printf("S3 log upload: failed to copy sourcemod logs: %v", err)
	}
	cancel()

	// Copy server logs
	srvDst := filepath.Join(tmpDir, "server")
	os.MkdirAll(srvDst, 0755)
	ctx, cancel = context.WithTimeout(context.Background(), 30*time.Second)
	if err := podmanClient.CopyFromContainer(ctx, containerID,
		"/home/tf2/server/tf/logs/.", srvDst); err != nil {
		log.Printf("S3 log upload: failed to copy server logs: %v", err)
	}
	cancel()

	return tmpDir
}

// uploadLogsToS3 uploads the copied log files to S3 and cleans up the temp dir.
func uploadLogsToS3(tmpDir string, cfg *boot.ReservationConfig) {
	defer os.RemoveAll(tmpDir)

	s3Cfg := &s3upload.Config{
		Endpoint:  cfg.S3Config.Endpoint,
		AccessKey: cfg.S3Config.AccessKey,
		SecretKey: cfg.S3Config.SecretKey,
		Bucket:    cfg.S3Config.Bucket,
		Region:    cfg.S3Config.Region,
	}

	prefix := fmt.Sprintf("reservations/%d", cfg.ReservationNumber)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	if err := s3upload.UploadDirectoryTarGz(ctx, s3Cfg, tmpDir, prefix); err != nil {
		log.Printf("S3 log upload failed: %v", err)
	} else {
		log.Printf("Server logs uploaded to S3 (s3://%s/%s)", cfg.S3Config.Bucket, prefix)
	}
}
