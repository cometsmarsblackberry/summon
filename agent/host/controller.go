package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/summon/agent/boot"
	"github.com/summon/agent/podman"
	"github.com/summon/agent/sysinfo"
	wsprotocol "github.com/summon/agent/websocket"
)

const maxRememberedCommands = 128

// Transport is the authenticated backend connection used by host mode.
type Transport interface {
	Send(message interface{}) error
	Messages() <-chan []byte
	Connections() <-chan struct{}
	IsConnected() bool
}

type podmanRuntime interface {
	CheckRootlessNetwork() error
	PullImage(context.Context, string, podman.ProgressCallback) error
	ImageDigest(context.Context, string) (string, error)
	ListManagedContainers(context.Context) ([]podman.ManagedContainer, error)
	ContainerStats(context.Context, string) (map[string]any, error)
	StopContainer(context.Context, string) error
}

type serverSequence interface {
	RunReconfigure() error
	SaveConfig() error
	GetContainerID() string
	GetConfig() *boot.ReservationConfig
	ExecuteRCON(context.Context, string) (string, error)
	ConfigureUploads(context.Context, bool, bool) error
	CollectLogs(context.Context) (string, error)
	UploadCollectedLogs(context.Context, string) error
}

type sequenceFactory func(boot.Reporter, *boot.ReservationConfig, string) serverSequence

// Config controls one persistent host agent.
type Config struct {
	HostID       int
	StateDir     string
	Credential   string
	AgentVersion string
	Heartbeat    time.Duration
}

// Controller owns all slot runtimes on one operator-managed VPS.
type Controller struct {
	config    Config
	transport Transport
	podman    podmanRuntime
	factory   sequenceFactory
	now       func() time.Time
	restart   func()

	mu       sync.RWMutex
	slots    map[int]*slotRuntime
	slotDefs map[int]SlotDefinition

	imageMu              sync.Mutex
	imagePrepareMu       sync.Mutex
	desiredImage         string
	readyImageDigest     string
	imageStatus          string
	imageError           string
	lastImageCheck       time.Time
	updateMu             sync.Mutex
	updateDraining       atomic.Bool
	updateStatusMu       sync.Mutex
	updateStatus         string
	updateStatusDraining bool
	updateStatusError    string

	configurationMu sync.RWMutex
	manifestURL     string
	versionPin      string
	configured      bool
	healthError     string
	preflight       map[string]any
	basePreflightOK bool
	preflightOK     bool

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

type slotRuntime struct {
	opMu sync.Mutex
	mu   sync.RWMutex

	definition    SlotDefinition
	generation    int
	reservationID int
	assignmentID  int
	state         string
	containerID   string
	sequence      serverSequence
	leaseExpires  time.Time
	leaseCancel   context.CancelFunc
	lastReady     map[string]any

	responses     map[string]map[string]any
	responseOrder []string
}

type slotSnapshot struct {
	Definition    SlotDefinition
	Generation    int
	ReservationID int
	AssignmentID  int
	State         string
	ContainerID   string
	LeaseExpires  time.Time
	LastReady     map[string]any
}

// NewController constructs the production host controller.
func NewController(config Config, transport Transport, client *podman.Client) *Controller {
	return newController(config, transport, client, func(
		reporter boot.Reporter, config *boot.ReservationConfig, containerID string,
	) serverSequence {
		if containerID != "" {
			return boot.NewAttachedSequence(reporter, config, containerID)
		}
		return boot.NewSequenceWithConfig(reporter, config)
	})
}

func newController(
	config Config,
	transport Transport,
	client podmanRuntime,
	factory sequenceFactory,
) *Controller {
	if config.Heartbeat <= 0 {
		config.Heartbeat = 10 * time.Second
	}
	if config.StateDir == "" {
		config.StateDir = "/var/lib/summon-agent"
	}
	return &Controller{
		config:    config,
		transport: transport,
		podman:    client,
		factory:   factory,
		now:       time.Now,
		restart: func() {
			if err := syscall.Kill(os.Getpid(), syscall.SIGTERM); err != nil {
				log.Printf("Unable to request agent restart: %v", err)
			}
		},
		slots:       make(map[int]*slotRuntime),
		slotDefs:    make(map[int]SlotDefinition),
		imageStatus: "unprepared",
		preflight:   make(map[string]any),
	}
}

// Run reconciles existing containers, processes commands, and reports a full
// inventory every heartbeat until the context is cancelled.
func (c *Controller) Run(parent context.Context) error {
	c.ctx, c.cancel = context.WithCancel(parent)
	defer c.cancel()

	if err := os.MkdirAll(filepath.Join(c.config.StateDir, "slots"), 0700); err != nil {
		return fmt.Errorf("create host state directory: %w", err)
	}
	c.loadImageState()
	c.runPreflight()
	if err := c.reconcileLocalContainers(c.ctx); err != nil {
		c.setHealthError(err.Error())
		log.Printf("Host inventory reconciliation failed: %v", err)
	}

	c.wg.Add(3)
	go c.messageLoop()
	go c.heartbeatLoop()
	go c.pendingUpdateWatchdog()

	// Connect() queues an initial connection notification before Run starts.
	select {
	case <-c.transport.Connections():
		c.onConnected()
	default:
		if c.transport.IsConnected() {
			c.onConnected()
		}
	}

	<-c.ctx.Done()
	c.cancelAllLeases()
	c.wg.Wait()
	return nil
}

func (c *Controller) messageLoop() {
	defer c.wg.Done()
	for {
		select {
		case <-c.ctx.Done():
			return
		case <-c.transport.Connections():
			c.onConnected()
		case message := <-c.transport.Messages():
			if len(message) == 0 {
				continue
			}
			c.handleMessage(message)
		}
	}
}

func (c *Controller) heartbeatLoop() {
	defer c.wg.Done()
	heartbeat := time.NewTicker(c.config.Heartbeat)
	imageCheck := time.NewTicker(15 * time.Minute)
	updateCheck := time.NewTicker(15 * time.Minute)
	defer heartbeat.Stop()
	defer imageCheck.Stop()
	defer updateCheck.Stop()
	for {
		select {
		case <-c.ctx.Done():
			return
		case <-heartbeat.C:
			c.sendStatus("host.status")
		case <-imageCheck.C:
			c.imageMu.Lock()
			desired := c.desiredImage
			c.imageMu.Unlock()
			if desired != "" {
				go c.prepareImage(desired, true)
			}
		case <-updateCheck.C:
			c.configurationMu.RLock()
			manifestURL, pin := c.manifestURL, c.versionPin
			c.configurationMu.RUnlock()
			if manifestURL != "" {
				go c.requestUpdate(manifestURL, pin, false)
			}
		}
	}
}

// pendingUpdateWatchdog rolls back a newly activated binary if it connected
// at the WebSocket layer but never completed a compatible backend
// configuration and host preflight before the manifest deadline.
func (c *Controller) pendingUpdateWatchdog() {
	defer c.wg.Done()
	deadline, pending := PendingUpdateDeadline(c.config.StateDir)
	if !pending {
		return
	}
	delay := time.Until(deadline)
	if delay < 0 {
		delay = 0
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-c.ctx.Done():
		return
	case <-timer.C:
	}
	if _, stillPending := PendingUpdateDeadline(c.config.StateDir); !stillPending {
		return
	}
	failure := fmt.Errorf("updated agent did not complete its backend handshake before the rollback deadline")
	c.sendUpdateStatus("failed", true, failure)
	if err := RollbackPendingUpdate(c.config.StateDir); err != nil {
		c.sendUpdateStatus("rollback_failed", true, err)
		return
	}
	c.sendUpdateStatus("rolled_back", false, nil)
	c.restart()
}

func (c *Controller) onConnected() {
	c.sendStatus("host.hello")
	// Replaying readiness after a reconnect is safe: backend events are tied to
	// assignment and generation, and prevents a lost ready frame from stranding
	// a live reservation in provisioning.
	for _, slot := range c.allSlots() {
		snapshot := snapshotSlot(slot)
		if snapshot.LastReady != nil && snapshot.ContainerID != "" {
			_ = c.transport.Send(snapshot.LastReady)
		}
	}
}

func (c *Controller) handleMessage(raw []byte) {
	var base struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &base); err != nil {
		log.Printf("Invalid host command: %v", err)
		return
	}
	switch base.Type {
	case "host.configure":
		var configuration hostConfiguration
		if err := json.Unmarshal(raw, &configuration); err != nil {
			log.Printf("Invalid host configuration: %v", err)
			return
		}
		c.applyConfiguration(configuration)
	case "image.prepare":
		var command imagePrepareCommand
		if err := json.Unmarshal(raw, &command); err != nil || command.HostID != c.config.HostID {
			return
		}
		go c.prepareImage(command.Image, true)
	case "agent.update":
		var command updateCommand
		if err := json.Unmarshal(raw, &command); err != nil || command.HostID != c.config.HostID {
			return
		}
		go c.requestUpdate(command.ManifestURL, command.VersionPin, true)
	case "server.start", "server.stop", "server.restart", "server.rcon", "server.uploads.configure":
		var command CommandEnvelope
		if err := json.Unmarshal(raw, &command); err != nil {
			log.Printf("Invalid %s command: %v", base.Type, err)
			return
		}
		slot := c.slotForCommand(command)
		if slot == nil {
			c.sendFailure(command, "invalid_command", "unknown_slot", "Slot is not configured on this host")
			return
		}
		go c.executeSlotCommand(slot, command)
	default:
		log.Printf("Unknown host command type %q", base.Type)
	}
}

func (c *Controller) applyConfiguration(configuration hostConfiguration) {
	if configuration.HostID != c.config.HostID {
		c.setHealthError("backend host configuration does not match enrolled host")
		return
	}
	if configuration.Protocol < ProtocolMin || configuration.Protocol > ProtocolMax {
		c.configurationMu.Lock()
		c.preflight["backend_protocol"] = configuration.Protocol
		c.preflightOK = false
		c.healthError = fmt.Sprintf(
			"backend host protocol %d is incompatible with this agent", configuration.Protocol,
		)
		c.configurationMu.Unlock()
		c.completePendingUpdate()
		c.sendStatus("host.status")
		return
	}
	if configuration.HeartbeatIntervalSeconds >= 1 && configuration.HeartbeatIntervalSeconds <= 60 {
		// Applied on next process start; changing a live ticker is unnecessary.
		c.config.Heartbeat = time.Duration(configuration.HeartbeatIntervalSeconds) * time.Second
	}

	definitions := make(map[int]SlotDefinition, len(configuration.Slots))
	ports := make(map[int]int)
	configurationError := ""
	for _, definition := range configuration.Slots {
		if err := validateSlotDefinition(definition); err != nil {
			configurationError = err.Error()
			continue
		}
		if _, exists := definitions[definition.SlotID]; exists {
			configurationError = fmt.Sprintf("duplicate slot id %d", definition.SlotID)
			continue
		}
		if other, exists := ports[definition.GamePort]; exists {
			configurationError = fmt.Sprintf("slot %d overlaps slot %d", definition.SlotIndex, other)
			continue
		}
		ports[definition.GamePort] = definition.SlotIndex
		if other, exists := ports[definition.TVPort]; exists {
			configurationError = fmt.Sprintf("slot %d overlaps slot %d", definition.SlotIndex, other)
			continue
		}
		ports[definition.TVPort] = definition.SlotIndex
		definitions[definition.SlotID] = definition
		c.ensureSlot(definition)
	}
	c.mu.Lock()
	c.slotDefs = definitions
	c.mu.Unlock()
	c.runConfiguredPreflight(definitions, configurationError == "")
	if configurationError != "" {
		c.setHealthError(configurationError)
	}

	c.configurationMu.Lock()
	c.manifestURL = configuration.AgentManifestURL
	c.versionPin = configuration.VersionPin
	firstConfiguration := !c.configured
	c.configured = true
	c.configurationMu.Unlock()

	if firstConfiguration {
		c.completePendingUpdate()
	}
	// Publish port/platform preflight immediately so an image-ready event cannot
	// create a brief window in which a failing host appears schedulable.
	c.sendStatus("host.status")
	c.imageMu.Lock()
	needsImage := configuration.DesiredImage != "" &&
		(configuration.ForceImagePrepare || configuration.DesiredImage != c.desiredImage || c.readyImageDigest == "")
	c.imageMu.Unlock()
	if needsImage {
		go c.prepareImage(configuration.DesiredImage, configuration.ForceImagePrepare)
	}
	if configuration.AgentManifestURL != "" {
		go c.requestUpdate(configuration.AgentManifestURL, configuration.VersionPin, false)
	}
}

func validateSlotDefinition(definition SlotDefinition) error {
	if definition.SlotID <= 0 || definition.SlotIndex < 0 {
		return fmt.Errorf("invalid slot identity")
	}
	if definition.GamePort < 1024 || definition.GamePort > 65535 ||
		definition.TVPort < 1024 || definition.TVPort > 65535 ||
		definition.GamePort == definition.TVPort {
		return fmt.Errorf("invalid ports for slot %d", definition.SlotIndex)
	}
	return nil
}

func (c *Controller) slotForCommand(command CommandEnvelope) *slotRuntime {
	c.mu.RLock()
	definition, configured := c.slotDefs[command.SlotID]
	slot := c.slots[command.SlotID]
	c.mu.RUnlock()
	if !configured || slot == nil || definition.SlotIndex != command.SlotIndex {
		return nil
	}
	return slot
}

func (c *Controller) ensureSlot(definition SlotDefinition) *slotRuntime {
	c.mu.Lock()
	defer c.mu.Unlock()
	if slot := c.slots[definition.SlotID]; slot != nil {
		slot.mu.Lock()
		slot.definition = definition
		slot.mu.Unlock()
		return slot
	}
	slot := &slotRuntime{
		definition: definition,
		state:      "idle",
		responses:  make(map[string]map[string]any),
	}
	c.slots[definition.SlotID] = slot
	return slot
}

func (c *Controller) executeSlotCommand(slot *slotRuntime, command CommandEnvelope) {
	slot.opMu.Lock()
	defer slot.opMu.Unlock()

	if command.CommandID == "" || command.ReservationID <= 0 || command.AssignmentID <= 0 ||
		command.Generation <= 0 || command.Protocol < ProtocolMin || command.Protocol > ProtocolMax {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "invalid_command", "invalid_envelope", "Command envelope is invalid",
		))
		return
	}
	if response, exists := rememberedResponse(slot, command.CommandID); exists {
		if response != nil {
			_ = c.transport.Send(response)
		}
		return
	}
	rememberResponse(slot, command.CommandID, nil)

	snapshot := snapshotSlot(slot)
	if command.Generation < snapshot.Generation {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "invalid_command", "stale_generation", "Command generation is stale",
		))
		return
	}
	if command.Generation == snapshot.Generation && snapshot.AssignmentID != 0 &&
		snapshot.AssignmentID != command.AssignmentID {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "invalid_command", "assignment_conflict", "Slot is owned by a different assignment",
		))
		return
	}

	switch command.Type {
	case "server.start":
		c.startSlot(slot, command)
	case "server.stop":
		c.stopSlot(slot, command)
	case "server.restart":
		c.restartSlot(slot, command)
	case "server.rcon":
		c.rconSlot(slot, command)
	case "server.uploads.configure":
		c.configureUploads(slot, command)
	}
}

func (c *Controller) startSlot(slot *slotRuntime, command CommandEnvelope) {
	snapshot := snapshotSlot(slot)
	if command.Generation == snapshot.Generation && snapshot.AssignmentID == command.AssignmentID &&
		snapshot.ContainerID != "" {
		response := snapshot.LastReady
		if response == nil {
			response = c.readyEvent(command, snapshot.ContainerID, "", 0, 0, "")
		}
		c.rememberAndSend(slot, command.CommandID, response)
		return
	}
	if c.updateDraining.Load() {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "host", "host_draining", "Host is draining for an agent update",
		))
		return
	}
	if snapshot.ContainerID != "" || snapshot.State == "starting" || snapshot.State == "stopping" {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "infrastructure", "slot_busy", "Slot still has a managed container",
		))
		return
	}
	if command.LeaseExpiresAt <= c.now().Unix() {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "expired_lease", "Reservation lease has already expired",
		))
		return
	}

	var config boot.ReservationConfig
	if err := json.Unmarshal(command.Config, &config); err != nil {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "invalid_config", err.Error(),
		))
		return
	}
	definition := snapshot.Definition
	if command.GamePort != definition.GamePort || command.TVPort != definition.TVPort ||
		(config.ExternalGamePort != 0 && config.ExternalGamePort != definition.GamePort) ||
		(config.ExternalTVPort != 0 && config.ExternalTVPort != definition.TVPort) {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "port_mismatch", "Reservation ports do not match the configured slot",
		))
		return
	}
	if config.ContainerImage == "" || config.ReservationID != command.ReservationID {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "invalid_config", "Reservation or image configuration is missing",
		))
		return
	}
	if command.ImageDigest == "" || !strings.Contains(config.ContainerImage, command.ImageDigest) {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "image_digest_mismatch", "Container image is not pinned to the assigned prepared digest",
		))
		return
	}

	stateDir := filepath.Join(c.config.StateDir, "slots", strconv.Itoa(definition.SlotIndex))
	config.StateDir = stateDir
	config.ExternalGamePort = definition.GamePort
	config.ExternalTVPort = definition.TVPort
	config.ContainerName = fmt.Sprintf("summon-h%d-s%d", c.config.HostID, definition.SlotIndex)
	config.EndsAt = command.LeaseExpiresAt
	config.Labels = assignmentLabels(c.config.HostID, command)

	reporter := &slotReporter{controller: c, command: command}
	sequence := c.factory(reporter, &config, "")
	slot.mu.Lock()
	slot.generation = command.Generation
	slot.reservationID = command.ReservationID
	slot.assignmentID = command.AssignmentID
	slot.state = "starting"
	slot.sequence = sequence
	slot.leaseExpires = time.Unix(command.LeaseExpiresAt, 0)
	slot.lastReady = nil
	slot.mu.Unlock()
	c.scheduleLease(slot, command)

	if err := sequence.SaveConfig(); err != nil {
		c.failStart(slot, command, "reservation_data", "persist_config", err)
		return
	}
	if err := sequence.RunReconfigure(); err != nil {
		if containerID := sequence.GetContainerID(); containerID != "" {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			_ = c.podman.StopContainer(ctx, containerID)
			cancel()
		}
		failureClass := "infrastructure"
		failureCode := "start_failed"
		if strings.Contains(strings.ToLower(err.Error()), "image") {
			failureClass, failureCode = "image", "image_unavailable"
		}
		c.failStart(slot, command, failureClass, failureCode, err)
		return
	}

	containerID := sequence.GetContainerID()
	slot.mu.Lock()
	slot.state = "ready"
	slot.containerID = containerID
	ready := slot.lastReady
	slot.mu.Unlock()
	if ready == nil {
		ready = c.readyEvent(command, containerID, "", 0, 0, config.FirstMap)
		slot.mu.Lock()
		slot.lastReady = ready
		slot.mu.Unlock()
		_ = c.transport.Send(ready)
	}
	rememberResponse(slot, command.CommandID, ready)
}

func (c *Controller) failStart(
	slot *slotRuntime, command CommandEnvelope, class, code string, err error,
) {
	event := c.failureEvent(command, class, code, err.Error())
	slot.mu.Lock()
	slot.state = "failed"
	slot.containerID = ""
	slot.sequence = nil
	slot.lastReady = nil
	if slot.leaseCancel != nil {
		slot.leaseCancel()
		slot.leaseCancel = nil
	}
	slot.mu.Unlock()
	c.rememberAndSend(slot, command.CommandID, event)
}

func (c *Controller) stopSlot(slot *slotRuntime, command CommandEnvelope) {
	snapshot := snapshotSlot(slot)
	if snapshot.Generation > command.Generation {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "invalid_command", "stale_generation", "Stop command generation is stale",
		))
		return
	}
	slot.mu.Lock()
	slot.state = "stopping"
	sequence := slot.sequence
	containerID := slot.containerID
	if slot.leaseCancel != nil {
		slot.leaseCancel()
		slot.leaseCancel = nil
	}
	slot.mu.Unlock()

	if sequence != nil && command.Reason != "expiry" {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, rconErr := sequence.ExecuteRCON(ctx, "sm_reservation_ending")
		cancel()
		if rconErr == nil {
			time.Sleep(10 * time.Second)
		}
	}
	logDirectory := ""
	if sequence != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		var collectErr error
		logDirectory, collectErr = sequence.CollectLogs(ctx)
		cancel()
		if collectErr != nil {
			log.Printf("Slot %d log collection failed: %v", command.SlotIndex, collectErr)
		}
	}
	if containerID != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		err := c.podman.StopContainer(ctx, containerID)
		cancel()
		if err != nil {
			if logDirectory != "" {
				_ = os.RemoveAll(logDirectory)
			}
			event := c.baseEvent("server.progress", command)
			event["stage"] = "stop_failed"
			event["progress"] = 0
			event["message"] = err.Error()
			event["error"] = true
			c.rememberAndSend(slot, command.CommandID, event)
			slot.mu.Lock()
			slot.state = "degraded"
			slot.mu.Unlock()
			return
		}
	}
	event := c.baseEvent("server.stopped", command)
	event["reason"] = command.Reason
	slot.mu.Lock()
	slot.state = "idle"
	slot.containerID = ""
	slot.sequence = nil
	slot.lastReady = nil
	slot.leaseExpires = time.Time{}
	slot.mu.Unlock()
	c.rememberAndSend(slot, command.CommandID, event)
	if sequence != nil && logDirectory != "" {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancel()
			if err := sequence.UploadCollectedLogs(ctx, logDirectory); err != nil {
				log.Printf("Slot %d log upload failed: %v", command.SlotIndex, err)
			}
		}()
	}
}

func (c *Controller) restartSlot(slot *slotRuntime, command CommandEnvelope) {
	snapshot := snapshotSlot(slot)
	if snapshot.SequenceUnavailable(slot) {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "infrastructure", "not_running", "No managed container is attached",
		))
		return
	}
	slot.mu.RLock()
	oldSequence := slot.sequence
	slot.mu.RUnlock()
	config := oldSequence.GetConfig()
	if config == nil {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "missing_config", "Slot configuration is unavailable",
		))
		return
	}
	configCopy := *config
	if err := mergeRestartConfig(&configCopy, command.Config); err != nil {
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "reservation_data", "invalid_config", err.Error(),
		))
		return
	}
	if command.LeaseExpiresAt > 0 {
		configCopy.EndsAt = command.LeaseExpiresAt
	}

	slot.mu.Lock()
	slot.state = "restarting"
	slot.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := c.podman.StopContainer(ctx, snapshot.ContainerID); err != nil {
		cancel()
		c.rememberAndSend(slot, command.CommandID, c.failureEvent(
			command, "infrastructure", "restart_stop_failed", err.Error(),
		))
		return
	}
	cancel()
	reporter := &slotReporter{controller: c, command: command}
	sequence := c.factory(reporter, &configCopy, "")
	slot.mu.Lock()
	slot.sequence = sequence
	slot.containerID = ""
	slot.lastReady = nil
	slot.mu.Unlock()
	if err := sequence.SaveConfig(); err != nil {
		c.failStart(slot, command, "reservation_data", "persist_config", err)
		return
	}
	if err := sequence.RunReconfigure(); err != nil {
		c.failStart(slot, command, "infrastructure", "restart_failed", err)
		return
	}
	containerID := sequence.GetContainerID()
	slot.mu.Lock()
	slot.containerID = containerID
	slot.state = "ready"
	ready := slot.lastReady
	slot.mu.Unlock()
	if ready == nil {
		ready = c.readyEvent(command, containerID, "", 0, 0, configCopy.FirstMap)
		_ = c.transport.Send(ready)
	}
	rememberResponse(slot, command.CommandID, ready)
}

func (snapshot slotSnapshot) SequenceUnavailable(slot *slotRuntime) bool {
	slot.mu.RLock()
	defer slot.mu.RUnlock()
	return snapshot.ContainerID == "" || slot.sequence == nil
}

func (c *Controller) rconSlot(slot *slotRuntime, command CommandEnvelope) {
	slot.mu.RLock()
	sequence := slot.sequence
	slot.mu.RUnlock()
	if sequence == nil || strings.TrimSpace(command.Command) == "" {
		event := c.baseEvent("server.rcon.result", command)
		event["error"] = "RCON command or running server is missing"
		c.rememberAndSend(slot, command.CommandID, event)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	output, err := sequence.ExecuteRCON(ctx, command.Command)
	cancel()
	event := c.baseEvent("server.rcon.result", command)
	if err != nil {
		event["error"] = err.Error()
	} else {
		event["output"] = output
	}
	c.rememberAndSend(slot, command.CommandID, event)
}

func (c *Controller) configureUploads(slot *slotRuntime, command CommandEnvelope) {
	slot.mu.RLock()
	sequence := slot.sequence
	slot.mu.RUnlock()
	if sequence == nil {
		event := c.baseEvent("server.progress", command)
		event["stage"] = "uploads_failed"
		event["progress"] = 0
		event["message"] = "Running server is unavailable"
		event["error"] = true
		c.rememberAndSend(slot, command.CommandID, event)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	err := sequence.ConfigureUploads(ctx, command.LogsTF, command.DemosTF)
	cancel()
	event := c.baseEvent("server.progress", command)
	event["stage"] = "uploads_configured"
	event["progress"] = 100
	if err != nil {
		event["stage"] = "uploads_failed"
		event["progress"] = 0
		event["message"] = err.Error()
		event["error"] = true
	} else {
		event["message"] = "External upload settings applied"
	}
	c.rememberAndSend(slot, command.CommandID, event)
}

func (c *Controller) scheduleLease(slot *slotRuntime, command CommandEnvelope) {
	slot.mu.Lock()
	if slot.leaseCancel != nil {
		slot.leaseCancel()
	}
	leaseContext, cancel := context.WithCancel(c.ctx)
	slot.leaseCancel = cancel
	slot.mu.Unlock()

	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		delay := time.Until(time.Unix(command.LeaseExpiresAt, 0))
		if delay < 0 {
			delay = 0
		}
		timer := time.NewTimer(delay)
		defer timer.Stop()
		select {
		case <-leaseContext.Done():
			return
		case <-timer.C:
			expiry := command
			expiry.Type = "server.stop"
			expiry.CommandID = fmt.Sprintf("lease-expiry-%d-%d", command.AssignmentID, command.Generation)
			expiry.Reason = "expiry"
			c.executeSlotCommand(slot, expiry)
		}
	}()
}

func (c *Controller) cancelAllLeases() {
	for _, slot := range c.allSlots() {
		slot.mu.Lock()
		if slot.leaseCancel != nil {
			slot.leaseCancel()
			slot.leaseCancel = nil
		}
		slot.mu.Unlock()
	}
}

func assignmentLabels(hostID int, command CommandEnvelope) map[string]string {
	return map[string]string{
		"summon.runtime":          "instant",
		"summon.host_id":          strconv.Itoa(hostID),
		"summon.slot_id":          strconv.Itoa(command.SlotID),
		"summon.slot_index":       strconv.Itoa(command.SlotIndex),
		"summon.assignment_id":    strconv.Itoa(command.AssignmentID),
		"summon.reservation_id":   strconv.Itoa(command.ReservationID),
		"summon.generation":       strconv.Itoa(command.Generation),
		"summon.lease_expires_at": strconv.FormatInt(command.LeaseExpiresAt, 10),
	}
}

func (c *Controller) baseEvent(eventType string, command CommandEnvelope) map[string]any {
	return map[string]any{
		"type":           eventType,
		"protocol":       ProtocolVersion,
		"command_id":     command.CommandID,
		"reservation_id": command.ReservationID,
		"assignment_id":  command.AssignmentID,
		"slot_id":        command.SlotID,
		"slot_index":     command.SlotIndex,
		"generation":     command.Generation,
	}
}

func (c *Controller) failureEvent(
	command CommandEnvelope, failureClass, failureCode, message string,
) map[string]any {
	event := c.baseEvent("server.failed", command)
	event["failure_class"] = failureClass
	event["failure_code"] = failureCode
	event["message"] = message
	return event
}

func (c *Controller) sendFailure(
	command CommandEnvelope, failureClass, failureCode, message string,
) {
	_ = c.transport.Send(c.failureEvent(command, failureClass, failureCode, message))
}

func (c *Controller) readyEvent(
	command CommandEnvelope,
	containerID, sdrIP string,
	sdrPort, sdrTVPort int,
	currentMap string,
) map[string]any {
	event := c.baseEvent("server.ready", command)
	event["container_id"] = containerID
	event["real_port"] = command.GamePort
	event["real_tv_port"] = command.TVPort
	if sdrIP != "" {
		event["sdr_ip"] = sdrIP
		event["sdr_port"] = sdrPort
		event["sdr_tv_port"] = sdrTVPort
	}
	if currentMap != "" {
		event["map"] = currentMap
	}
	return event
}

func rememberedResponse(slot *slotRuntime, commandID string) (map[string]any, bool) {
	slot.mu.RLock()
	defer slot.mu.RUnlock()
	response, exists := slot.responses[commandID]
	return response, exists
}

func rememberResponse(slot *slotRuntime, commandID string, response map[string]any) {
	if commandID == "" {
		return
	}
	slot.mu.Lock()
	defer slot.mu.Unlock()
	if _, exists := slot.responses[commandID]; !exists {
		slot.responseOrder = append(slot.responseOrder, commandID)
	}
	slot.responses[commandID] = response
	for len(slot.responseOrder) > maxRememberedCommands {
		oldest := slot.responseOrder[0]
		slot.responseOrder = slot.responseOrder[1:]
		delete(slot.responses, oldest)
	}
}

func (c *Controller) rememberAndSend(slot *slotRuntime, commandID string, event map[string]any) {
	rememberResponse(slot, commandID, event)
	_ = c.transport.Send(event)
}

func snapshotSlot(slot *slotRuntime) slotSnapshot {
	slot.mu.RLock()
	defer slot.mu.RUnlock()
	return slotSnapshot{
		Definition:    slot.definition,
		Generation:    slot.generation,
		ReservationID: slot.reservationID,
		AssignmentID:  slot.assignmentID,
		State:         slot.state,
		ContainerID:   slot.containerID,
		LeaseExpires:  slot.leaseExpires,
		LastReady:     slot.lastReady,
	}
}

func (c *Controller) allSlots() []*slotRuntime {
	c.mu.RLock()
	defer c.mu.RUnlock()
	slots := make([]*slotRuntime, 0, len(c.slots))
	for _, slot := range c.slots {
		slots = append(slots, slot)
	}
	sort.Slice(slots, func(i, j int) bool {
		return snapshotSlot(slots[i]).Definition.SlotIndex < snapshotSlot(slots[j]).Definition.SlotIndex
	})
	return slots
}

func (c *Controller) isIdle() bool {
	for _, slot := range c.allSlots() {
		snapshot := snapshotSlot(slot)
		if snapshot.ContainerID != "" || snapshot.State == "starting" ||
			snapshot.State == "restarting" || snapshot.State == "stopping" {
			return false
		}
	}
	return true
}

// waitForSlotOperationBarrier waits for commands that entered a slot before
// update draining was enabled. New starts observe updateDraining and fail
// without creating a container, closing the backend-drain propagation race.
func (c *Controller) waitForSlotOperationBarrier() {
	for _, slot := range c.allSlots() {
		slot.opMu.Lock()
		slot.opMu.Unlock()
	}
}

func (c *Controller) sendStatus(messageType string) {
	inventory, err := c.containerInventory(context.Background())
	if err != nil {
		c.setHealthError(err.Error())
	}
	c.imageMu.Lock()
	image := map[string]any{
		"desired":      c.desiredImage,
		"ready_digest": c.readyImageDigest,
		"status":       c.imageStatus,
		"error":        c.imageError,
	}
	c.imageMu.Unlock()
	c.configurationMu.RLock()
	healthError := c.healthError
	preflight := make(map[string]any, len(c.preflight))
	for key, value := range c.preflight {
		preflight[key] = value
	}
	preflightOK := c.preflightOK
	c.configurationMu.RUnlock()

	slotStates := make([]map[string]any, 0)
	for _, slot := range c.allSlots() {
		snapshot := snapshotSlot(slot)
		slotStates = append(slotStates, map[string]any{
			"slot_id": snapshot.Definition.SlotID, "slot_index": snapshot.Definition.SlotIndex,
			"state": snapshot.State, "assignment_id": snapshot.AssignmentID,
			"generation": snapshot.Generation,
		})
	}
	message := map[string]any{
		"type":             messageType,
		"protocol_version": ProtocolVersion,
		"protocol_min":     ProtocolMin,
		"protocol_max":     ProtocolMax,
		"agent_version":    c.config.AgentVersion,
		"host_id":          c.config.HostID,
		"platform":         runtime.GOOS,
		"architecture":     runtime.GOARCH,
		"capabilities": map[string]any{
			"multi_slot": true, "reconciliation": true, "local_lease_expiry": true,
			"image_digest": true, "self_update": true, "container_stats": true,
			"rcon": true, "uploads": true, "sdr": true, "sourcetv": true,
		},
		"sysinfo":      sysinfo.Collect(),
		"preflight":    preflight,
		"preflight_ok": preflightOK,
		"health_error": healthError,
		"image":        image,
		"slots":        inventory,
		"slot_states":  slotStates,
	}
	if err := c.transport.Send(message); err != nil {
		log.Printf("Failed to send %s: %v", messageType, err)
	}
}

func (c *Controller) containerInventory(ctx context.Context) ([]map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	slots := c.allSlots()
	observedSlots := make(map[*slotRuntime]slotSnapshot, len(slots))
	for _, slot := range slots {
		observedSlots[slot] = snapshotSlot(slot)
	}
	containers, err := c.podman.ListManagedContainers(ctx)
	if err != nil {
		return nil, err
	}
	inventory := make([]map[string]any, 0, len(containers))
	seenContainerIDs := make([]string, 0, len(containers))
	for _, container := range containers {
		if container.Labels["summon.host_id"] != strconv.Itoa(c.config.HostID) {
			continue
		}
		seenContainerIDs = append(seenContainerIDs, container.ID)
		item := map[string]any{
			"container_id": container.ID,
			"name":         container.Name,
			"state":        container.State,
		}
		for label, field := range map[string]string{
			"summon.slot_id": "slot_id", "summon.slot_index": "slot_index",
			"summon.assignment_id": "assignment_id", "summon.reservation_id": "reservation_id",
			"summon.generation": "generation", "summon.lease_expires_at": "lease_expires_at",
		} {
			if value, parseErr := strconv.ParseInt(container.Labels[label], 10, 64); parseErr == nil {
				item[field] = value
			}
		}
		statsCtx, statsCancel := context.WithTimeout(ctx, 3*time.Second)
		if stats, statsErr := c.podman.ContainerStats(statsCtx, container.ID); statsErr == nil {
			item["stats"] = stats
		}
		statsCancel()
		inventory = append(inventory, item)
	}
	for _, slot := range slots {
		// Inventory is collected on the heartbeat goroutine, so it must never
		// wait for a start, stop, or restart that can take several minutes.
		if !slot.opMu.TryLock() {
			continue
		}
		snapshot := snapshotSlot(slot)
		observed := observedSlots[slot]
		if !sameInventoryEpoch(observed, snapshot) {
			slot.opMu.Unlock()
			continue
		}
		present := snapshot.ContainerID == ""
		for _, seenID := range seenContainerIDs {
			if containerIDsMatch(snapshot.ContainerID, seenID) {
				present = true
				break
			}
		}
		if snapshot.State == "ready" && snapshot.ContainerID != "" && !present {
			// The backend will observe a missing desired container and reissue
			// server.start. Clear terminal command responses as well, otherwise
			// the original idempotency key would only replay stale readiness.
			slot.mu.Lock()
			slot.state = "idle"
			slot.containerID = ""
			slot.sequence = nil
			slot.lastReady = nil
			slot.responses = make(map[string]map[string]any)
			slot.responseOrder = nil
			slot.mu.Unlock()
			log.Printf(
				"Slot %d container %s disappeared; awaiting idempotent reconciliation",
				snapshot.Definition.SlotIndex, snapshot.ContainerID,
			)
		}
		slot.opMu.Unlock()
	}
	return inventory, nil
}

func sameInventoryEpoch(observed, current slotSnapshot) bool {
	return observed.Generation == current.Generation &&
		observed.ReservationID == current.ReservationID &&
		observed.AssignmentID == current.AssignmentID &&
		observed.State == current.State &&
		observed.ContainerID == current.ContainerID &&
		observed.LeaseExpires.Equal(current.LeaseExpires)
}

func containerIDsMatch(first, second string) bool {
	if first == "" || second == "" {
		return false
	}
	return first == second || strings.HasPrefix(first, second) || strings.HasPrefix(second, first)
}

func (c *Controller) reconcileLocalContainers(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	containers, err := c.podman.ListManagedContainers(ctx)
	if err != nil {
		return err
	}
	seenSlots := make(map[int]string)
	conflicts := make([]string, 0)
	for _, container := range containers {
		labels := container.Labels
		if labels["summon.host_id"] != strconv.Itoa(c.config.HostID) {
			continue
		}
		slotID, slotErr := strconv.Atoi(labels["summon.slot_id"])
		slotIndex, indexErr := strconv.Atoi(labels["summon.slot_index"])
		assignmentID, assignmentErr := strconv.Atoi(labels["summon.assignment_id"])
		reservationID, reservationErr := strconv.Atoi(labels["summon.reservation_id"])
		generation, generationErr := strconv.Atoi(labels["summon.generation"])
		leaseUnix, leaseErr := strconv.ParseInt(labels["summon.lease_expires_at"], 10, 64)
		if errors.Join(slotErr, indexErr, assignmentErr, reservationErr, generationErr, leaseErr) != nil {
			conflicts = append(conflicts, fmt.Sprintf("container %s has incomplete Summon labels", container.ID))
			continue
		}
		if first, duplicate := seenSlots[slotID]; duplicate {
			conflicts = append(conflicts, fmt.Sprintf("slot %d has containers %s and %s", slotID, first, container.ID))
			continue
		}
		seenSlots[slotID] = container.ID
		if leaseUnix <= c.now().Unix() {
			stopCtx, stopCancel := context.WithTimeout(context.Background(), 30*time.Second)
			if stopErr := c.podman.StopContainer(stopCtx, container.ID); stopErr != nil {
				conflicts = append(conflicts, fmt.Sprintf("expired container %s could not stop: %v", container.ID, stopErr))
			}
			stopCancel()
			continue
		}

		definition := SlotDefinition{SlotID: slotID, SlotIndex: slotIndex}
		slot := c.ensureSlot(definition)
		stateDir := filepath.Join(c.config.StateDir, "slots", strconv.Itoa(slotIndex))
		config, configErr := boot.LoadReservationConfig(stateDir)
		if configErr != nil {
			conflicts = append(conflicts, fmt.Sprintf("container %s has no usable slot config: %v", container.ID, configErr))
			continue
		}
		command := CommandEnvelope{
			Type: "server.start", Protocol: ProtocolVersion, CommandID: "reconcile",
			ReservationID: reservationID, AssignmentID: assignmentID, SlotID: slotID,
			SlotIndex: slotIndex, Generation: generation, LeaseExpiresAt: leaseUnix,
			GamePort: config.ExternalGamePort, TVPort: config.ExternalTVPort,
		}
		reporter := &slotReporter{controller: c, command: command}
		sequence := c.factory(reporter, config, container.ID)
		ready := c.readyEvent(command, container.ID, "", 0, 0, config.FirstMap)
		slot.mu.Lock()
		slot.generation = generation
		slot.assignmentID = assignmentID
		slot.reservationID = reservationID
		slot.containerID = container.ID
		slot.sequence = sequence
		slot.state = "ready"
		slot.leaseExpires = time.Unix(leaseUnix, 0)
		slot.lastReady = ready
		slot.mu.Unlock()
		c.scheduleLease(slot, command)
	}
	if len(conflicts) > 0 {
		return fmt.Errorf("%s", strings.Join(conflicts, "; "))
	}
	return nil
}

func (c *Controller) runPreflight() {
	osID, osVersion := readOSRelease()
	report := map[string]any{
		"platform": runtime.GOOS, "architecture": runtime.GOARCH,
		"unprivileged": os.Geteuid() != 0,
		"os_id":        osID, "os_version": osVersion,
	}
	ok := runtime.GOOS == "linux" && runtime.GOARCH == "amd64" &&
		os.Geteuid() != 0 && osID == "ubuntu" && osVersion == "26.04"
	rootlessNetworkError := ""
	if err := c.podman.CheckRootlessNetwork(); err != nil {
		rootlessNetworkError = err.Error()
		report["rootless_network"] = "failed"
		report["rootless_network_error"] = rootlessNetworkError
		ok = false
	} else {
		report["rootless_network"] = "ready"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	_, err := c.podman.ListManagedContainers(ctx)
	cancel()
	if err != nil {
		report["podman"] = "failed"
		report["podman_error"] = err.Error()
		ok = false
	} else {
		report["podman"] = "ready"
	}
	c.configurationMu.Lock()
	c.preflight = report
	c.basePreflightOK = ok
	c.preflightOK = ok
	if rootlessNetworkError != "" {
		c.healthError = rootlessNetworkError
	}
	c.configurationMu.Unlock()
}

func readOSRelease() (string, string) {
	data, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return "", ""
	}
	values := make(map[string]string)
	for _, line := range strings.Split(string(data), "\n") {
		key, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		values[key] = strings.Trim(strings.TrimSpace(value), `"'`)
	}
	return strings.ToLower(values["ID"]), values["VERSION_ID"]
}

func (c *Controller) runConfiguredPreflight(
	definitions map[int]SlotDefinition, configurationOK bool,
) {
	portReport := make(map[string]any, len(definitions))
	portsOK := true
	for slotID, definition := range definitions {
		c.mu.RLock()
		slot := c.slots[slotID]
		c.mu.RUnlock()
		if slot != nil && snapshotSlot(slot).ContainerID != "" {
			portReport[strconv.Itoa(definition.SlotIndex)] = "occupied_by_managed_container"
			continue
		}
		err := checkSlotPortsAvailable(definition)
		if err != nil {
			portsOK = false
			portReport[strconv.Itoa(definition.SlotIndex)] = err.Error()
		} else {
			portReport[strconv.Itoa(definition.SlotIndex)] = "available"
		}
	}
	c.configurationMu.Lock()
	c.preflight["ports"] = portReport
	c.preflightOK = c.basePreflightOK && configurationOK && portsOK
	if !portsOK {
		c.healthError = "one or more configured slot ports are already in use"
	} else if c.healthError == "one or more configured slot ports are already in use" {
		c.healthError = ""
	}
	c.configurationMu.Unlock()
}

func checkSlotPortsAvailable(definition SlotDefinition) error {
	gameUDP, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: definition.GamePort})
	if err != nil {
		return fmt.Errorf("UDP %d unavailable: %w", definition.GamePort, err)
	}
	_ = gameUDP.Close()
	tvUDP, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: definition.TVPort})
	if err != nil {
		return fmt.Errorf("UDP %d unavailable: %w", definition.TVPort, err)
	}
	_ = tvUDP.Close()
	return nil
}

func (c *Controller) setHealthError(message string) {
	c.configurationMu.Lock()
	c.healthError = message
	c.configurationMu.Unlock()
}

type imageState struct {
	DesiredImage string    `json:"desired_image"`
	ReadyDigest  string    `json:"ready_digest"`
	CheckedAt    time.Time `json:"checked_at"`
}

func (c *Controller) imageStatePath() string {
	return filepath.Join(c.config.StateDir, "image-state.json")
}

func (c *Controller) loadImageState() {
	data, err := os.ReadFile(c.imageStatePath())
	if err != nil {
		return
	}
	var state imageState
	if json.Unmarshal(data, &state) != nil {
		return
	}
	c.imageMu.Lock()
	c.desiredImage = state.DesiredImage
	c.readyImageDigest = state.ReadyDigest
	c.lastImageCheck = state.CheckedAt
	if state.ReadyDigest != "" {
		c.imageStatus = "ready"
	}
	c.imageMu.Unlock()
}

func (c *Controller) saveImageState(state imageState) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	temporary := c.imageStatePath() + ".new"
	if err := os.WriteFile(temporary, data, 0600); err != nil {
		return err
	}
	return os.Rename(temporary, c.imageStatePath())
}

func (c *Controller) prepareImage(image string, force bool) {
	image = strings.TrimSpace(image)
	if image == "" {
		return
	}
	c.imagePrepareMu.Lock()
	defer c.imagePrepareMu.Unlock()

	c.imageMu.Lock()
	if !force && image == c.desiredImage && c.readyImageDigest != "" {
		c.imageMu.Unlock()
		return
	}
	c.desiredImage = image
	c.imageStatus = "preparing"
	c.imageError = ""
	lastKnownGood := c.readyImageDigest
	c.imageMu.Unlock()
	_ = c.transport.Send(map[string]any{
		"type": "host.image", "protocol": ProtocolVersion, "status": "preparing",
		"desired_image": image, "ready_digest": lastKnownGood,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	err := c.podman.PullImage(ctx, image, func(_ string, progress int, message string) {
		c.imageMu.Lock()
		readyDigest := c.readyImageDigest
		c.imageMu.Unlock()
		_ = c.transport.Send(map[string]any{
			"type": "host.image", "protocol": ProtocolVersion, "status": "preparing",
			"progress": progress, "message": message, "desired_image": image,
			"ready_digest": readyDigest,
		})
	})
	resolvedDigest := ""
	if err == nil {
		resolvedDigest, err = c.podman.ImageDigest(ctx, image)
		if err == nil {
			resolvedDigest = pinnedImageReference(image, resolvedDigest)
		}
	}
	cancel()
	checkedAt := c.now()
	if err != nil {
		c.imageMu.Lock()
		c.lastImageCheck = checkedAt
		c.imageStatus = "failed"
		c.imageError = err.Error()
		lastKnownGood = c.readyImageDigest
		c.imageMu.Unlock()
		_ = c.transport.Send(map[string]any{
			"type": "host.image", "protocol": ProtocolVersion, "status": "failed",
			"desired_image": image, "ready_digest": lastKnownGood, "error": err.Error(),
		})
		return
	}
	if err := c.saveImageState(imageState{
		DesiredImage: image, ReadyDigest: resolvedDigest, CheckedAt: checkedAt,
	}); err != nil {
		c.imageMu.Lock()
		c.lastImageCheck = checkedAt
		c.imageStatus = "failed"
		c.imageError = err.Error()
		lastKnownGood = c.readyImageDigest
		c.imageMu.Unlock()
		_ = c.transport.Send(map[string]any{
			"type": "host.image", "protocol": ProtocolVersion, "status": "failed",
			"desired_image": image, "ready_digest": lastKnownGood, "error": err.Error(),
		})
		return
	}
	c.imageMu.Lock()
	c.readyImageDigest = resolvedDigest
	c.lastImageCheck = checkedAt
	c.imageStatus = "ready"
	c.imageError = ""
	c.imageMu.Unlock()
	_ = c.transport.Send(map[string]any{
		"type": "host.image", "protocol": ProtocolVersion, "status": "ready",
		"desired_image": image, "ready_digest": resolvedDigest,
	})
}

func pinnedImageReference(image, digest string) string {
	digest = strings.TrimSpace(digest)
	if digest == "" || strings.Contains(digest, "@") || !strings.HasPrefix(digest, "sha256:") {
		return digest
	}
	repository := strings.SplitN(strings.TrimSpace(image), "@", 2)[0]
	lastSlash := strings.LastIndex(repository, "/")
	if lastColon := strings.LastIndex(repository, ":"); lastColon > lastSlash {
		repository = repository[:lastColon]
	}
	return repository + "@" + digest
}

// slotReporter translates legacy boot-sequence callbacks into assignment-
// scoped host protocol events.
type slotReporter struct {
	controller *Controller
	command    CommandEnvelope
}

func (r *slotReporter) SendBootProgress(stage string, progress int, message string) error {
	event := r.controller.baseEvent("server.progress", r.command)
	event["stage"] = stage
	event["progress"] = progress
	event["message"] = message
	return r.controller.transport.Send(event)
}

func (r *slotReporter) SendServerReady(_ string, _, _ int) error {
	return r.sendReady(wsprotocol.ServerReadyInfo{})
}

func (r *slotReporter) SendServerReadyWithSDR(info wsprotocol.ServerReadyInfo) error {
	return r.sendReady(info)
}

func (r *slotReporter) sendReady(info wsprotocol.ServerReadyInfo) error {
	containerID := ""
	if slot := r.controller.slotForCommand(r.command); slot != nil {
		slot.mu.RLock()
		if slot.sequence != nil {
			containerID = slot.sequence.GetContainerID()
		}
		slot.mu.RUnlock()
	}
	event := r.controller.readyEvent(
		r.command, containerID, info.SDRIP, info.SDRPort, info.SDRTVPort, info.Map,
	)
	if slot := r.controller.slotForCommand(r.command); slot != nil {
		slot.mu.Lock()
		slot.containerID = containerID
		slot.lastReady = event
		slot.mu.Unlock()
	}
	return r.controller.transport.Send(event)
}

func (r *slotReporter) SendCompetitiveConfigs(configs []string, containerImage string) error {
	return r.controller.transport.Send(map[string]any{
		"type": "host.competitive_configs", "protocol": ProtocolVersion,
		"configs": configs, "container_image": containerImage,
	})
}
