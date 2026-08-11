package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/summon/agent/boot"
	"github.com/summon/agent/podman"
	wsprotocol "github.com/summon/agent/websocket"
)

type fakeTransport struct {
	messages    chan []byte
	connections chan struct{}
	mu          sync.Mutex
	sent        []map[string]any
}

func newFakeTransport() *fakeTransport {
	return &fakeTransport{
		messages: make(chan []byte, 16), connections: make(chan struct{}, 4),
	}
}

func (f *fakeTransport) Send(value interface{}) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	var message map[string]any
	if err := json.Unmarshal(data, &message); err != nil {
		return err
	}
	f.mu.Lock()
	f.sent = append(f.sent, message)
	f.mu.Unlock()
	return nil
}
func (f *fakeTransport) Messages() <-chan []byte      { return f.messages }
func (f *fakeTransport) Connections() <-chan struct{} { return f.connections }
func (f *fakeTransport) IsConnected() bool            { return true }

type fakePodman struct {
	pullErr    error
	digest     string
	containers []podman.ManagedContainer
	stopped    chan string
}

func (f *fakePodman) PullImage(
	_ context.Context, _ string, callback podman.ProgressCallback,
) error {
	callback("pulling_container", 50, "halfway")
	return f.pullErr
}
func (f *fakePodman) ImageDigest(context.Context, string) (string, error) {
	if f.digest == "" {
		return "", errors.New("missing digest")
	}
	return f.digest, nil
}
func (f *fakePodman) ListManagedContainers(context.Context) ([]podman.ManagedContainer, error) {
	return f.containers, nil
}
func (f *fakePodman) ContainerStats(context.Context, string) (map[string]any, error) {
	return map[string]any{"cpu": "1%"}, nil
}
func (f *fakePodman) StopContainer(_ context.Context, id string) error {
	if f.stopped != nil {
		f.stopped <- id
	}
	return nil
}

type sequenceHarness struct {
	started chan struct{}
	release chan struct{}
	active  atomic.Int32
	maximum atomic.Int32
	created atomic.Int32
	ids     atomic.Int32

	rconStarted chan struct{}
	rconRelease chan struct{}
	rconActive  atomic.Int32
	rconMaximum atomic.Int32
	configMu    sync.Mutex
	configs     []boot.ReservationConfig
}

type fakeSequence struct {
	harness  *sequenceHarness
	reporter boot.Reporter
	config   *boot.ReservationConfig
	id       string
}

func (f *fakeSequence) RunReconfigure() error {
	active := f.harness.active.Add(1)
	for {
		maximum := f.harness.maximum.Load()
		if active <= maximum || f.harness.maximum.CompareAndSwap(maximum, active) {
			break
		}
	}
	if f.harness.started != nil {
		f.harness.started <- struct{}{}
	}
	if f.harness.release != nil {
		<-f.harness.release
	}
	f.harness.active.Add(-1)
	return f.reporter.SendServerReadyWithSDR(wsprotocol.ServerReadyInfo{
		SDRIP: "169.254.1.2", SDRPort: 30001, SDRTVPort: 30002, Map: f.config.FirstMap,
	})
}
func (f *fakeSequence) SaveConfig() error                  { return nil }
func (f *fakeSequence) GetContainerID() string             { return f.id }
func (f *fakeSequence) GetConfig() *boot.ReservationConfig { return f.config }
func (f *fakeSequence) ExecuteRCON(context.Context, string) (string, error) {
	if f.harness.rconRelease != nil {
		active := f.harness.rconActive.Add(1)
		for {
			maximum := f.harness.rconMaximum.Load()
			if active <= maximum || f.harness.rconMaximum.CompareAndSwap(maximum, active) {
				break
			}
		}
		if f.harness.rconStarted != nil {
			f.harness.rconStarted <- struct{}{}
		}
		<-f.harness.rconRelease
		f.harness.rconActive.Add(-1)
	}
	return "ok", nil
}
func (f *fakeSequence) ConfigureUploads(context.Context, bool, bool) error { return nil }
func (f *fakeSequence) CollectLogs(context.Context) (string, error)        { return "", nil }
func (f *fakeSequence) UploadCollectedLogs(context.Context, string) error  { return nil }

func harnessFactory(harness *sequenceHarness) sequenceFactory {
	return func(reporter boot.Reporter, config *boot.ReservationConfig, attached string) serverSequence {
		harness.created.Add(1)
		harness.configMu.Lock()
		harness.configs = append(harness.configs, *config)
		harness.configMu.Unlock()
		id := attached
		if id == "" {
			id = fmt.Sprintf("container-%d", harness.ids.Add(1))
		}
		return &fakeSequence{harness: harness, reporter: reporter, config: config, id: id}
	}
}

func configuredController(
	t *testing.T, definitions []SlotDefinition, harness *sequenceHarness, runtime *fakePodman,
) (*Controller, *fakeTransport, context.CancelFunc) {
	t.Helper()
	transport := newFakeTransport()
	controller := newController(Config{
		HostID: 7, StateDir: t.TempDir(), Credential: "secret", AgentVersion: "test",
	}, transport, runtime, harnessFactory(harness))
	ctx, cancel := context.WithCancel(context.Background())
	controller.ctx = ctx
	controller.applyConfiguration(hostConfiguration{
		HostID: 7, Protocol: ProtocolVersion, Slots: definitions,
	})
	return controller, transport, func() {
		controller.cancelAllLeases()
		cancel()
		controller.wg.Wait()
	}
}

func startCommand(definition SlotDefinition, assignmentID, generation int, lease time.Time) CommandEnvelope {
	config, _ := json.Marshal(boot.ReservationConfig{
		ReservationID:     assignmentID + 100,
		ReservationNumber: assignmentID,
		ContainerImage:    "registry.example/tf2@sha256:prepared",
		FirstMap:          "cp_badlands",
		ExternalGamePort:  definition.GamePort,
		ExternalTVPort:    definition.TVPort,
	})
	return CommandEnvelope{
		Type: "server.start", Protocol: ProtocolVersion,
		CommandID:     fmt.Sprintf("start-%d-%d", assignmentID, generation),
		ReservationID: assignmentID + 100, AssignmentID: assignmentID,
		SlotID: definition.SlotID, SlotIndex: definition.SlotIndex, Generation: generation,
		LeaseExpiresAt: lease.Unix(), GamePort: definition.GamePort, TVPort: definition.TVPort,
		ImageDigest: "sha256:prepared", Config: config,
	}
}

func TestDifferentSlotsStartConcurrently(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 2), release: make(chan struct{})}
	definitions := []SlotDefinition{
		{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020},
		{SlotID: 2, SlotIndex: 1, GamePort: 27025, TVPort: 27030},
	}
	controller, _, cleanup := configuredController(t, definitions, harness, &fakePodman{})
	defer cleanup()

	for i, definition := range definitions {
		command := startCommand(definition, i+1, 1, time.Now().Add(time.Hour))
		go controller.executeSlotCommand(controller.slotForCommand(command), command)
	}
	for range definitions {
		select {
		case <-harness.started:
		case <-time.After(time.Second):
			t.Fatal("slot starts did not overlap")
		}
	}
	if maximum := harness.maximum.Load(); maximum != 2 {
		t.Fatalf("expected two concurrent slots, observed %d", maximum)
	}
	close(harness.release)
}

func TestDuplicateCommandIsIdempotentWithinSlot(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 1), release: make(chan struct{})}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()
	command := startCommand(definition, 10, 1, time.Now().Add(time.Hour))
	slot := controller.slotForCommand(command)
	done := make(chan struct{}, 2)
	for range 2 {
		go func() {
			controller.executeSlotCommand(slot, command)
			done <- struct{}{}
		}()
	}
	<-harness.started
	close(harness.release)
	<-done
	<-done
	if count := harness.created.Load(); count != 1 {
		t.Fatalf("duplicate command created %d sequences", count)
	}
	transport.mu.Lock()
	defer transport.mu.Unlock()
	ready := 0
	for _, event := range transport.sent {
		if event["type"] == "server.ready" {
			ready++
		}
	}
	if ready < 2 {
		t.Fatalf("duplicate should replay its terminal response; got %d ready events", ready)
	}
}

func TestCommandsWithinOneSlotAreSerialized(t *testing.T) {
	harness := &sequenceHarness{
		rconStarted: make(chan struct{}, 2), rconRelease: make(chan struct{}),
	}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()
	start := startCommand(definition, 11, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(start), start)

	done := make(chan struct{}, 2)
	for index := range 2 {
		command := start
		command.Type = "server.rcon"
		command.CommandID = fmt.Sprintf("rcon-%d", index)
		command.Command = "status"
		go func() {
			controller.executeSlotCommand(controller.slotForCommand(command), command)
			done <- struct{}{}
		}()
	}
	select {
	case <-harness.rconStarted:
	case <-time.After(time.Second):
		t.Fatal("first RCON operation did not start")
	}
	time.Sleep(50 * time.Millisecond)
	if active := harness.rconActive.Load(); active != 1 {
		t.Fatalf("expected one active operation in a slot, got %d", active)
	}
	close(harness.rconRelease)
	<-done
	<-done
	if maximum := harness.rconMaximum.Load(); maximum != 1 {
		t.Fatalf("same-slot operations overlapped; maximum was %d", maximum)
	}
}

func TestSlotStateAndContainerNamesAreIsolated(t *testing.T) {
	harness := &sequenceHarness{}
	definitions := []SlotDefinition{
		{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020},
		{SlotID: 2, SlotIndex: 1, GamePort: 27025, TVPort: 27030},
	}
	controller, _, cleanup := configuredController(t, definitions, harness, &fakePodman{})
	defer cleanup()
	for index, definition := range definitions {
		command := startCommand(definition, 30+index, 1, time.Now().Add(time.Hour))
		controller.executeSlotCommand(controller.slotForCommand(command), command)
	}
	harness.configMu.Lock()
	configs := append([]boot.ReservationConfig(nil), harness.configs...)
	harness.configMu.Unlock()
	if len(configs) != 2 {
		t.Fatalf("created %d slot configurations, want 2", len(configs))
	}
	if configs[0].StateDir == configs[1].StateDir || configs[0].ContainerName == configs[1].ContainerName {
		t.Fatalf("slot runtime context was shared: %#v %#v", configs[0], configs[1])
	}
	for index, config := range configs {
		wantSuffix := filepath.Join("slots", fmt.Sprint(index))
		if filepath.Clean(config.StateDir) != filepath.Join(controller.config.StateDir, wantSuffix) {
			t.Fatalf("slot %d state dir = %q", index, config.StateDir)
		}
	}
}

func TestLeaseExpiryStopsContainerWithoutBackendConnectivity(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{stopped: make(chan string, 1)}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()
	command := startCommand(definition, 20, 1, time.Now().Add(1200*time.Millisecond))
	controller.executeSlotCommand(controller.slotForCommand(command), command)
	select {
	case id := <-runtime.stopped:
		if id != "container-1" {
			t.Fatalf("unexpected stopped container %q", id)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("local lease expiry did not stop the container")
	}
}

func TestFailedImageRefreshRetainsLastKnownGoodDigest(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{pullErr: errors.New("registry unavailable")}
	controller, _, cleanup := configuredController(t, nil, harness, runtime)
	defer cleanup()
	controller.readyImageDigest = "sha256:last-known-good"
	controller.desiredImage = "registry.example/tf2:old"
	controller.prepareImage("registry.example/tf2:new", true)
	if controller.readyImageDigest != "sha256:last-known-good" {
		t.Fatalf("failed refresh discarded last-known-good digest: %q", controller.readyImageDigest)
	}
	if controller.imageStatus != "failed" {
		t.Fatalf("expected degraded image status, got %q", controller.imageStatus)
	}
}

func TestPinnedImageReferencePreservesRegistryAndDropsMutableTag(t *testing.T) {
	got := pinnedImageReference(
		"registry.example:5000/team/tf2:nightly", "sha256:prepared",
	)
	want := "registry.example:5000/team/tf2@sha256:prepared"
	if got != want {
		t.Fatalf("pinned image = %q, want %q", got, want)
	}
}

func TestFailedAgentVersionRequiresExplicitRetry(t *testing.T) {
	directory := t.TempDir()
	if err := markFailedUpdate(directory, "0.3.0"); err != nil {
		t.Fatal(err)
	}
	if got := failedUpdateVersion(directory); got != "0.3.0" {
		t.Fatalf("failed update version = %q, want 0.3.0", got)
	}
}

func TestRollbackPendingUpdateRestoresPreviousBinary(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "tf2-agent")
	backup := target + ".previous"
	if err := os.WriteFile(target, []byte("new"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte("old"), 0755); err != nil {
		t.Fatal(err)
	}
	pending := pendingUpdate{
		TargetPath: target, BackupPath: backup,
		FromVersion: "1.0.0", ToVersion: "2.0.0", Deadline: time.Now().Add(time.Minute),
	}
	data, err := json.Marshal(pending)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pendingUpdatePath(directory), data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := RollbackPendingUpdate(directory); err != nil {
		t.Fatal(err)
	}
	restored, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(restored) != "old" || failedUpdateVersion(directory) != "2.0.0" {
		t.Fatalf("rollback result = %q, failed version = %q", restored, failedUpdateVersion(directory))
	}
	if _, err := os.Stat(pendingUpdatePath(directory)); !os.IsNotExist(err) {
		t.Fatalf("pending update state remains after rollback: %v", err)
	}
}

func TestPendingUpdateWatchdogRollsBackMissingBackendHandshake(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "tf2-agent")
	backup := target + ".previous"
	if err := os.WriteFile(target, []byte("new"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte("old"), 0755); err != nil {
		t.Fatal(err)
	}
	pending := pendingUpdate{
		TargetPath: target, BackupPath: backup,
		FromVersion: "1.0.0", ToVersion: "2.0.0",
		Deadline: time.Now().Add(50 * time.Millisecond),
	}
	data, err := json.Marshal(pending)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pendingUpdatePath(directory), data, 0600); err != nil {
		t.Fatal(err)
	}

	controller := newController(
		Config{HostID: 7, StateDir: directory, AgentVersion: "2.0.0"},
		newFakeTransport(), &fakePodman{}, harnessFactory(&sequenceHarness{}),
	)
	controller.ctx, controller.cancel = context.WithCancel(context.Background())
	restarted := make(chan struct{}, 1)
	controller.restart = func() { restarted <- struct{}{} }
	controller.wg.Add(1)
	go controller.pendingUpdateWatchdog()
	select {
	case <-restarted:
	case <-time.After(time.Second):
		t.Fatal("missing backend handshake did not trigger rollback")
	}
	controller.cancel()
	controller.wg.Wait()
	restored, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(restored) != "old" || failedUpdateVersion(directory) != "2.0.0" {
		t.Fatalf("watchdog rollback result = %q, failed version = %q", restored, failedUpdateVersion(directory))
	}
}

func TestContainerDiscoveryReattachesLabeledRuntime(t *testing.T) {
	directory := t.TempDir()
	stateDir := filepath.Join(directory, "slots", "0")
	if err := os.MkdirAll(stateDir, 0700); err != nil {
		t.Fatal(err)
	}
	config := boot.ReservationConfig{
		ReservationID: 44, ReservationNumber: 44, FirstMap: "cp_process_f12",
		ExternalGamePort: 27015, ExternalTVPort: 27020,
	}
	data, err := json.Marshal(config)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stateDir, "reservation.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	runtime := &fakePodman{containers: []podman.ManagedContainer{{
		ID: "reattached-container", Name: "summon-h7-s0", State: "running",
		Labels: map[string]string{
			"summon.host_id": "7", "summon.slot_id": "9", "summon.slot_index": "0",
			"summon.assignment_id": "55", "summon.reservation_id": "44",
			"summon.generation":       "2",
			"summon.lease_expires_at": fmt.Sprint(time.Now().Add(time.Hour).Unix()),
		},
	}}}
	controller := newController(
		Config{HostID: 7, StateDir: directory}, newFakeTransport(), runtime,
		harnessFactory(&sequenceHarness{}),
	)
	controller.ctx, controller.cancel = context.WithCancel(context.Background())
	if err := controller.reconcileLocalContainers(controller.ctx); err != nil {
		t.Fatal(err)
	}
	slot := controller.slots[9]
	if slot == nil {
		t.Fatal("discovered slot was not reconstructed")
	}
	snapshot := snapshotSlot(slot)
	if snapshot.ContainerID != "reattached-container" || snapshot.AssignmentID != 55 ||
		snapshot.Generation != 2 || snapshot.State != "ready" {
		t.Fatalf("unexpected reconstructed runtime: %#v", snapshot)
	}
	controller.cancelAllLeases()
	controller.cancel()
	controller.wg.Wait()
}

func TestMissingLiveContainerClearsStaleIdempotencyResponse(t *testing.T) {
	harness := &sequenceHarness{}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()
	command := startCommand(definition, 70, 1, time.Now().Add(time.Hour))
	slot := controller.slotForCommand(command)
	controller.executeSlotCommand(slot, command)
	if harness.created.Load() != 1 {
		t.Fatal("initial container did not start")
	}
	if _, err := controller.containerInventory(context.Background()); err != nil {
		t.Fatal(err)
	}
	if snapshot := snapshotSlot(slot); snapshot.ContainerID != "" || snapshot.State != "idle" {
		t.Fatalf("missing container retained stale runtime state: %#v", snapshot)
	}
	controller.executeSlotCommand(slot, command)
	if harness.created.Load() != 2 {
		t.Fatalf("reissued idempotent start did not recreate container; sequences=%d", harness.created.Load())
	}
}

func TestUpdateDrainRejectsAConcurrentNewStart(t *testing.T) {
	harness := &sequenceHarness{}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()
	controller.updateDraining.Store(true)
	command := startCommand(definition, 80, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(command), command)
	if harness.created.Load() != 0 {
		t.Fatal("update-draining host started a new container")
	}
	transport.mu.Lock()
	defer transport.mu.Unlock()
	last := transport.sent[len(transport.sent)-1]
	if last["type"] != "server.failed" || last["failure_code"] != "host_draining" {
		t.Fatalf("unexpected drain response: %#v", last)
	}
}

func TestConfiguredPreflightRecoversAfterPortIsReleased(t *testing.T) {
	listener, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		t.Fatal(err)
	}
	gamePort := listener.LocalAddr().(*net.UDPAddr).Port
	if gamePort > 65530 {
		_ = listener.Close()
		t.Skip("ephemeral port cannot accommodate the SourceTV offset")
	}
	controller := newController(
		Config{HostID: 7, StateDir: t.TempDir()}, newFakeTransport(), &fakePodman{},
		harnessFactory(&sequenceHarness{}),
	)
	controller.configurationMu.Lock()
	controller.basePreflightOK = true
	controller.preflightOK = true
	controller.configurationMu.Unlock()
	definitions := map[int]SlotDefinition{
		1: {SlotID: 1, SlotIndex: 0, GamePort: gamePort, TVPort: gamePort + 5},
	}

	controller.runConfiguredPreflight(definitions, true)
	if controller.preflightOK {
		t.Fatal("occupied port unexpectedly passed preflight")
	}
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	controller.runConfiguredPreflight(definitions, true)
	if !controller.preflightOK {
		t.Fatal("preflight did not recover after the port was released")
	}
	if controller.healthError != "" {
		t.Fatalf("stale port health error was retained: %q", controller.healthError)
	}
}
