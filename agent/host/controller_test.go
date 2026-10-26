package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
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

func sentUpdateStatusCount(transport *fakeTransport, status string) int {
	transport.mu.Lock()
	defer transport.mu.Unlock()
	count := 0
	for _, message := range transport.sent {
		if message["type"] == "host.update" && message["status"] == status {
			count++
		}
	}
	return count
}

func waitForUpdateStatus(t *testing.T, transport *fakeTransport, status string, count int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if sentUpdateStatusCount(transport, status) >= count {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("did not receive %d host update status messages for %q", count, status)
}

type fakePodman struct {
	pullErr     error
	listErr     error
	networkErr  error
	digest      string
	containers  []podman.ManagedContainer
	stopped     chan string
	stopStarted chan string
	stopRelease chan struct{}
	listStarted chan struct{}
	listRelease chan struct{}
	pullCalls   atomic.Int32
	listCalls   atomic.Int32
}

func (f *fakePodman) CheckRootlessNetwork() error { return f.networkErr }

func (f *fakePodman) PullImage(
	_ context.Context, _ string, callback podman.ProgressCallback,
) error {
	f.pullCalls.Add(1)
	callback("pulling_container", 50, "halfway")
	return f.pullErr
}
func (f *fakePodman) ImageDigest(context.Context, string) (string, error) {
	if f.digest == "" {
		return "", errors.New("missing digest")
	}
	return f.digest, nil
}

func (f *fakePodman) ListManagedContainers(ctx context.Context) ([]podman.ManagedContainer, error) {
	f.listCalls.Add(1)
	if f.listStarted != nil {
		f.listStarted <- struct{}{}
	}
	if f.listRelease != nil {
		select {
		case <-f.listRelease:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if f.listErr != nil {
		return nil, f.listErr
	}
	return f.containers, nil
}
func (f *fakePodman) ContainerStats(context.Context, string) (map[string]any, error) {
	return map[string]any{"cpu": "1%"}, nil
}
func (f *fakePodman) StopContainer(ctx context.Context, id string) error {
	if f.stopStarted != nil {
		f.stopStarted <- id
	}
	if f.stopRelease != nil {
		select {
		case <-f.stopRelease:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if f.stopped != nil {
		f.stopped <- id
	}
	return nil
}

type sequenceHarness struct {
	started     chan struct{}
	release     chan struct{}
	saveStarted chan struct{}
	saveRelease chan struct{}
	active      atomic.Int32
	maximum     atomic.Int32
	created     atomic.Int32
	ids         atomic.Int32

	rconStarted chan struct{}
	rconRelease chan struct{}
	rconActive  atomic.Int32
	rconMaximum atomic.Int32
	configMu    sync.Mutex
	configs     []boot.ReservationConfig
	runErr      error
}

type fakeSequence struct {
	harness  *sequenceHarness
	reporter boot.Reporter
	config   *boot.ReservationConfig
	id       string
}

func (f *fakeSequence) RunReconfigureContext(ctx context.Context) error {
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
		select {
		case <-f.harness.release:
		case <-ctx.Done():
			f.harness.active.Add(-1)
			return ctx.Err()
		}
	}
	f.harness.active.Add(-1)
	if f.harness.runErr != nil {
		return f.harness.runErr
	}
	return f.reporter.SendServerReadyWithSDR(wsprotocol.ServerReadyInfo{
		SDRIP: "169.254.1.2", SDRPort: 30001, SDRTVPort: 30002, Map: f.config.FirstMap,
	})
}
func (f *fakeSequence) SaveConfig() error {
	if f.harness.saveStarted != nil {
		f.harness.saveStarted <- struct{}{}
	}
	if f.harness.saveRelease != nil {
		<-f.harness.saveRelease
	}
	return nil
}
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

// stopCommand uses the expiry reason, which skips the in-game ending notice
// and its 10-second grace period.
func stopCommand(start CommandEnvelope) CommandEnvelope {
	stop := start
	stop.Type = "server.stop"
	stop.CommandID = fmt.Sprintf("stop-%d-%d", start.AssignmentID, start.Generation)
	stop.Reason = "expiry"
	return stop
}

func lastEventForAssignment(transport *fakeTransport, assignmentID int) map[string]any {
	transport.mu.Lock()
	defer transport.mu.Unlock()
	for index := len(transport.sent) - 1; index >= 0; index-- {
		if transport.sent[index]["assignment_id"] == float64(assignmentID) {
			return transport.sent[index]
		}
	}
	return nil
}

func TestEndedReservationReleasesSlotForNextReservation(t *testing.T) {
	harness := &sequenceHarness{}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()

	for _, previousGeneration := range []int{1, 2} {
		// The previous reservation may have been retried onto this host, but
		// every new reservation starts again at generation 1.
		previous := startCommand(definition, 40+previousGeneration, previousGeneration, time.Now().Add(time.Hour))
		controller.executeSlotCommand(controller.slotForCommand(previous), previous)
		stop := stopCommand(previous)
		controller.executeSlotCommand(controller.slotForCommand(stop), stop)
		if event := lastEventForAssignment(transport, previous.AssignmentID); event["type"] != "server.stopped" {
			t.Fatalf("previous reservation did not stop: %v", event)
		}

		next := startCommand(definition, 50+previousGeneration, 1, time.Now().Add(time.Hour))
		controller.executeSlotCommand(controller.slotForCommand(next), next)
		if event := lastEventForAssignment(transport, next.AssignmentID); event["type"] != "server.ready" {
			t.Fatalf("next reservation after generation %d was rejected: %v", previousGeneration, event)
		}
		nextStop := stopCommand(next)
		controller.executeSlotCommand(controller.slotForCommand(nextStop), nextStop)
	}
}

func TestFailedStartReleasesSlotForNextReservation(t *testing.T) {
	harness := &sequenceHarness{runErr: errors.New("srcds exited")}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()

	failed := startCommand(definition, 60, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(failed), failed)
	if event := lastEventForAssignment(transport, failed.AssignmentID); event["type"] != "server.failed" {
		t.Fatalf("expected the first start to fail: %v", event)
	}

	harness.runErr = nil
	next := startCommand(definition, 61, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(next), next)
	if event := lastEventForAssignment(transport, next.AssignmentID); event["type"] != "server.ready" {
		t.Fatalf("slot was not released after a failed start: %v", event)
	}
}

func TestOccupiedSlotRejectsOtherAssignments(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{stopped: make(chan string, 1)}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()

	owner := startCommand(definition, 70, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(owner), owner)

	// A retried reservation has a higher generation than the slot's owner;
	// it must still not take over or stop the running server.
	intruder := startCommand(definition, 71, 2, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(intruder), intruder)
	event := lastEventForAssignment(transport, intruder.AssignmentID)
	if event["type"] != "server.failed" || event["failure_class"] != "infrastructure" ||
		event["failure_code"] != "slot_busy" {
		t.Fatalf("start for another assignment should report a busy slot: %v", event)
	}
	stop := stopCommand(intruder)
	controller.executeSlotCommand(controller.slotForCommand(stop), stop)
	event = lastEventForAssignment(transport, intruder.AssignmentID)
	if event["failure_code"] != "assignment_conflict" {
		t.Fatalf("stop for another assignment should be rejected: %v", event)
	}
	select {
	case id := <-runtime.stopped:
		t.Fatalf("another assignment stopped the owner's container %q", id)
	default:
	}
	if snapshot := snapshotSlot(controller.slotForCommand(owner)); snapshot.AssignmentID != owner.AssignmentID ||
		snapshot.ContainerID == "" {
		t.Fatalf("owner lost the slot: %#v", snapshot)
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

func TestPeriodicImageRefreshRunsOnlyWhileHostIsIdle(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{digest: "sha256:refreshed"}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()

	controller.refreshImageIfIdle("registry.example/tf2:latest")
	if calls := runtime.pullCalls.Load(); calls != 1 {
		t.Fatalf("idle periodic refresh made %d pulls, want 1", calls)
	}

	command := startCommand(definition, 91, 1, time.Now().Add(time.Hour))
	controller.executeSlotCommand(controller.slotForCommand(command), command)
	controller.refreshImageIfIdle("registry.example/tf2:latest")
	if calls := runtime.pullCalls.Load(); calls != 1 {
		t.Fatalf("occupied periodic refresh made an image pull; total calls = %d", calls)
	}
}

func TestFailedPeriodicImageRefreshUsesExponentialBackoff(t *testing.T) {
	runtime := &fakePodman{pullErr: errors.New("registry unavailable")}
	controller, _, cleanup := configuredController(
		t, nil, &sequenceHarness{}, runtime,
	)
	defer cleanup()
	now := time.Date(2026, 8, 14, 0, 0, 0, 0, time.UTC)
	controller.now = func() time.Time { return now }

	controller.refreshImageIfIdle("registry.example/tf2:latest")
	controller.refreshImageIfIdle("registry.example/tf2:latest")
	if calls := runtime.pullCalls.Load(); calls != 1 {
		t.Fatalf("immediate retry made %d pulls, want 1", calls)
	}

	now = now.Add(imageRefreshInterval + time.Second)
	controller.refreshImageIfIdle("registry.example/tf2:latest")
	controller.refreshImageIfIdle("registry.example/tf2:latest")
	if calls := runtime.pullCalls.Load(); calls != 2 {
		t.Fatalf("second backoff window made %d pulls, want 2", calls)
	}

	now = now.Add(2*imageRefreshInterval + time.Second)
	controller.refreshImageIfIdle("registry.example/tf2:latest")
	if calls := runtime.pullCalls.Load(); calls != 3 {
		t.Fatalf("third backoff window made %d pulls, want 3", calls)
	}
}

func TestStatusDefersPodmanInventoryWhileImageIsPreparing(t *testing.T) {
	runtime := &fakePodman{}
	controller, transport, cleanup := configuredController(
		t, nil, &sequenceHarness{}, runtime,
	)
	defer cleanup()
	controller.imageMu.Lock()
	controller.imageStatus = "preparing"
	controller.imageMu.Unlock()
	before := runtime.listCalls.Load()

	controller.sendStatus("host.status")

	if calls := runtime.listCalls.Load(); calls != before {
		t.Fatalf("image preparation triggered Podman inventory: calls %d -> %d", before, calls)
	}
	transport.mu.Lock()
	message := transport.sent[len(transport.sent)-1]
	transport.mu.Unlock()
	if message["slots"] != nil {
		t.Fatalf("deferred inventory should be null, got %#v", message["slots"])
	}
}

func TestTransientInventoryHealthErrorClearsAfterSuccessfulProbe(t *testing.T) {
	runtime := &fakePodman{}
	controller, transport, cleanup := configuredController(
		t, nil, &sequenceHarness{}, runtime,
	)
	defer cleanup()
	runtime.listErr = errors.New("podman ps: signal: killed")
	controller.sendStatus("host.status")
	transport.mu.Lock()
	failed := transport.sent[len(transport.sent)-1]
	transport.mu.Unlock()
	if failed["health_error"] != "podman ps: signal: killed" {
		t.Fatalf("inventory error was not reported: %#v", failed["health_error"])
	}

	runtime.listErr = nil
	controller.sendStatus("host.status")
	transport.mu.Lock()
	recovered := transport.sent[len(transport.sent)-1]
	transport.mu.Unlock()
	if recovered["health_error"] != "" {
		t.Fatalf("successful inventory retained transient error: %#v", recovered["health_error"])
	}
}

func TestInventoryRecoveryDoesNotClearConfigurationHealthError(t *testing.T) {
	runtime := &fakePodman{listErr: errors.New("podman unavailable")}
	controller, transport, cleanup := configuredController(
		t, nil, &sequenceHarness{}, runtime,
	)
	defer cleanup()
	controller.setHealthError("configuration invalid")
	controller.sendStatus("host.status")
	runtime.listErr = nil
	controller.sendStatus("host.status")

	transport.mu.Lock()
	message := transport.sent[len(transport.sent)-1]
	transport.mu.Unlock()
	if message["health_error"] != "configuration invalid" {
		t.Fatalf("inventory recovery hid configuration error: %#v", message["health_error"])
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

func TestCurrentAgentRequiresMatchingVersionAndDigest(t *testing.T) {
	digest, err := currentExecutableSHA256()
	if err != nil {
		t.Fatal(err)
	}
	manifest := &agentManifest{Version: "same", SHA256: digest}
	current, err := currentAgentMatchesManifest("same", manifest)
	if err != nil {
		t.Fatal(err)
	}
	if !current {
		t.Fatal("matching version and executable digest was not current")
	}

	manifest.SHA256 = strings.Repeat("0", 64)
	current, err = currentAgentMatchesManifest("same", manifest)
	if err != nil {
		t.Fatal(err)
	}
	if current {
		t.Fatal("matching version hid a stale executable digest")
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

func TestContainerInventoryDoesNotWaitForSlotOperation(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 1), release: make(chan struct{})}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()
	command := startCommand(definition, 71, 1, time.Now().Add(time.Hour))
	startDone := make(chan struct{})
	go func() {
		controller.executeSlotCommand(controller.slotForCommand(command), command)
		close(startDone)
	}()
	select {
	case <-harness.started:
	case <-time.After(time.Second):
		t.Fatal("slot start did not reach the long-running operation")
	}

	inventoryDone := make(chan error, 1)
	go func() {
		_, err := controller.containerInventory(context.Background())
		inventoryDone <- err
	}()
	select {
	case err := <-inventoryDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(250 * time.Millisecond):
		t.Fatal("heartbeat inventory waited for the active slot operation")
	}

	close(harness.release)
	select {
	case <-startDone:
	case <-time.After(time.Second):
		t.Fatal("slot start did not finish after release")
	}
}

func TestContainerInventoryPreservesContainerStartedDuringPodmanSnapshot(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, _, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()
	runtime.listStarted = make(chan struct{}, 1)
	runtime.listRelease = make(chan struct{})

	inventoryDone := make(chan error, 1)
	go func() {
		_, err := controller.containerInventory(context.Background())
		inventoryDone <- err
	}()
	select {
	case <-runtime.listStarted:
	case <-time.After(time.Second):
		t.Fatal("inventory did not begin its Podman snapshot")
	}

	command := startCommand(definition, 72, 1, time.Now().Add(time.Hour))
	slot := controller.slotForCommand(command)
	controller.executeSlotCommand(slot, command)
	close(runtime.listRelease)
	select {
	case err := <-inventoryDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("inventory did not finish after Podman snapshot was released")
	}
	if snapshot := snapshotSlot(slot); snapshot.ContainerID == "" || snapshot.State != "ready" {
		t.Fatalf("inventory cleared a container created during its snapshot: %#v", snapshot)
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

func TestDuplicateUpdateRequestReReportsCurrentPhase(t *testing.T) {
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/manifest" {
			http.NotFound(response, request)
			return
		}
		_ = json.NewEncoder(response).Encode(agentManifest{
			Version: "next", ProtocolMin: ProtocolMin, ProtocolMax: ProtocolMax,
			DownloadURL: server.URL + "/agent", SHA256: strings.Repeat("0", 64),
			RollbackTimeoutSeconds: 90,
		})
	}))
	defer server.Close()

	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(
		t, []SlotDefinition{definition}, &sequenceHarness{}, &fakePodman{},
	)
	defer cleanup()
	slot := controller.slots[definition.SlotID]
	slot.mu.Lock()
	slot.state = "ready"
	slot.containerID = "active-container"
	slot.mu.Unlock()
	updateContext, cancelUpdate := context.WithCancel(context.Background())
	controller.ctx = updateContext

	firstDone := make(chan struct{})
	go func() {
		controller.requestUpdate(server.URL+"/manifest", "", false)
		close(firstDone)
	}()
	waitForUpdateStatus(t, transport, "waiting_for_idle", 1)

	duplicateDone := make(chan struct{})
	go func() {
		controller.requestUpdate(server.URL+"/manifest", "", true)
		close(duplicateDone)
	}()
	select {
	case <-duplicateDone:
	case <-time.After(250 * time.Millisecond):
		t.Fatal("duplicate update request blocked behind the active updater")
	}
	waitForUpdateStatus(t, transport, "waiting_for_idle", 2)

	cancelUpdate()
	select {
	case <-firstDone:
	case <-time.After(time.Second):
		t.Fatal("active updater did not stop after cancellation")
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

func TestPreflightRejectsMissingRootlessNetworkHelper(t *testing.T) {
	controller := newController(
		Config{HostID: 7, StateDir: t.TempDir()},
		newFakeTransport(),
		&fakePodman{networkErr: errors.New("pasta executable is missing")},
		harnessFactory(&sequenceHarness{}),
	)

	controller.runPreflight()
	controller.configurationMu.RLock()
	defer controller.configurationMu.RUnlock()
	if controller.basePreflightOK || controller.preflightOK {
		t.Fatal("host without rootless network helper unexpectedly passed preflight")
	}
	if controller.preflight["rootless_network"] != "failed" {
		t.Fatalf("missing helper was not reported: %#v", controller.preflight)
	}
	if !strings.Contains(controller.healthError, "pasta") {
		t.Fatalf("missing helper health error was not actionable: %q", controller.healthError)
	}
}

func TestConnectReplaysCompetitiveConfigsAfterAgentRestart(t *testing.T) {
	stateDir := t.TempDir()
	first := newController(Config{HostID: 7, StateDir: stateDir}, newFakeTransport(),
		&fakePodman{}, harnessFactory(&sequenceHarness{}))
	reporter := &slotReporter{controller: first}
	if err := reporter.SendCompetitiveConfigs(
		[]string{"etf2l_6v6_5cp"}, "registry.example/tf2@sha256:prepared",
	); err != nil {
		t.Fatal(err)
	}

	// The backend may have restarted too, so the restarted agent replays the
	// list it saved even though no boot has reported it since.
	transport := newFakeTransport()
	restarted := newController(Config{HostID: 7, StateDir: stateDir}, transport,
		&fakePodman{}, harnessFactory(&sequenceHarness{}))
	restarted.loadCompetitiveConfigs()
	restarted.onConnected()

	transport.mu.Lock()
	defer transport.mu.Unlock()
	for _, message := range transport.sent {
		if message["type"] != "host.competitive_configs" {
			continue
		}
		configs, _ := message["configs"].([]any)
		if len(configs) != 1 || configs[0] != "etf2l_6v6_5cp" ||
			message["container_image"] != "registry.example/tf2@sha256:prepared" ||
			message["protocol"] != float64(ProtocolVersion) {
			t.Fatalf("replayed configs = %#v", message)
		}
		return
	}
	t.Fatalf("no competitive config list replayed on connect: %#v", transport.sent)
}

func eventTypesForAssignment(transport *fakeTransport, assignmentID int) []string {
	transport.mu.Lock()
	defer transport.mu.Unlock()
	var types []string
	for _, message := range transport.sent {
		if message["assignment_id"] == float64(assignmentID) {
			types = append(types, fmt.Sprint(message["type"]))
		}
	}
	return types
}

// A start whose boot never returns must not prevent a stop from
// terminating the container.
func TestStopInterruptsAStartThatNeverFinishes(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 1), release: make(chan struct{})}
	runtime := &fakePodman{stopped: make(chan string, 1)}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()
	defer close(harness.release)

	start := startCommand(definition, 70, 1, time.Now().Add(time.Hour))
	startDone := make(chan struct{})
	go func() {
		controller.executeSlotCommand(controller.slotForCommand(start), start)
		close(startDone)
	}()
	select {
	case <-harness.started:
	case <-time.After(time.Second):
		t.Fatal("start did not begin")
	}

	stop := stopCommand(start)
	stopDone := make(chan struct{})
	go func() {
		controller.executeSlotCommand(controller.slotForCommand(stop), stop)
		close(stopDone)
	}()
	select {
	case <-stopDone:
	case <-time.After(2 * time.Second):
		t.Fatal("stop waited for the start that never finishes")
	}
	<-startDone
	select {
	case id := <-runtime.stopped:
		if id != "container-1" {
			t.Fatalf("stopped container %q, want container-1", id)
		}
	default:
		t.Fatal("the interrupted start's container was not stopped")
	}
	if types := eventTypesForAssignment(transport, start.AssignmentID); strings.Join(types, ",") != "server.stopped" {
		t.Fatalf("events = %v, want only server.stopped", types)
	}

	// The slot is free again for the next reservation.
	next := startCommand(definition, 71, 1, time.Now().Add(time.Hour))
	nextDone := make(chan struct{})
	go func() {
		controller.executeSlotCommand(controller.slotForCommand(next), next)
		close(nextDone)
	}()
	<-harness.started
	harness.release <- struct{}{}
	<-nextDone
	if event := lastEventForAssignment(transport, next.AssignmentID); event["type"] != "server.ready" {
		t.Fatalf("next reservation did not start: %v", event)
	}
}

func TestLeaseExpiryInterruptsAStartThatNeverFinishes(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 1), release: make(chan struct{})}
	runtime := &fakePodman{stopped: make(chan string, 1)}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()
	defer close(harness.release)

	start := startCommand(definition, 72, 1, time.Now().Add(1200*time.Millisecond))
	go controller.executeSlotCommand(controller.slotForCommand(start), start)
	select {
	case id := <-runtime.stopped:
		if id != "container-1" {
			t.Fatalf("stopped container %q, want container-1", id)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("lease expiry did not interrupt the start")
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if event := lastEventForAssignment(transport, start.AssignmentID); event != nil && event["type"] == "server.stopped" {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("events = %v, want server.stopped", eventTypesForAssignment(transport, start.AssignmentID))
}

func TestStopForAnotherAssignmentDoesNotInterruptStart(t *testing.T) {
	harness := &sequenceHarness{started: make(chan struct{}, 1), release: make(chan struct{})}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
	defer cleanup()

	start := startCommand(definition, 73, 2, time.Now().Add(time.Hour))
	startDone := make(chan struct{})
	go func() {
		controller.executeSlotCommand(controller.slotForCommand(start), start)
		close(startDone)
	}()
	<-harness.started

	slot := controller.slotForCommand(start)
	for _, stop := range []CommandEnvelope{
		stopCommand(startCommand(definition, 74, 2, time.Now().Add(time.Hour))),
		stopCommand(startCommand(definition, 73, 1, time.Now().Add(time.Hour))),
	} {
		controller.interruptStart(slot, stop)
	}
	close(harness.release)
	<-startDone
	if event := lastEventForAssignment(transport, start.AssignmentID); event["type"] != "server.ready" {
		t.Fatalf("a foreign or stale stop interrupted the start: %v", event)
	}
}

func TestStopDuringConfigPersistenceSkipsBoot(t *testing.T) {
	for _, operation := range []string{"server.start", "server.restart"} {
		t.Run(operation, func(t *testing.T) {
			harness := &sequenceHarness{}
			definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
			controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, &fakePodman{})
			defer cleanup()
			command := startCommand(definition, 75, 1, time.Now().Add(time.Hour))
			slot := controller.slotForCommand(command)
			if operation == "server.restart" {
				controller.executeSlotCommand(slot, command)
				command.Type = operation
				command.CommandID = "restart-75-1"
			}
			harness.saveStarted = make(chan struct{}, 1)
			harness.saveRelease = make(chan struct{})
			harness.started = make(chan struct{}, 1)
			harness.release = make(chan struct{})
			defer close(harness.release)
			done := make(chan struct{})
			go func() {
				controller.executeSlotCommand(slot, command)
				close(done)
			}()
			select {
			case <-harness.saveStarted:
			case <-time.After(time.Second):
				t.Fatal("config persistence did not begin")
			}

			stop := stopCommand(command)
			controller.interruptStart(slot, stop)
			close(harness.saveRelease)
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("stop was lost while config persistence was running")
			}
			select {
			case <-harness.started:
				t.Fatal("boot began after the stop interrupted config persistence")
			default:
			}
			controller.executeSlotCommand(slot, stop)
			if event := lastEventForAssignment(transport, command.AssignmentID); event["type"] != "server.stopped" {
				t.Fatalf("last event = %v, want server.stopped", event)
			}
		})
	}
}

func TestStopInterruptsRestartBeforeNewBoot(t *testing.T) {
	harness := &sequenceHarness{}
	runtime := &fakePodman{}
	definition := SlotDefinition{SlotID: 1, SlotIndex: 0, GamePort: 27015, TVPort: 27020}
	controller, transport, cleanup := configuredController(t, []SlotDefinition{definition}, harness, runtime)
	defer cleanup()
	command := startCommand(definition, 76, 1, time.Now().Add(time.Hour))
	slot := controller.slotForCommand(command)
	controller.executeSlotCommand(slot, command)
	command.Type = "server.restart"
	command.CommandID = "restart-76-1"
	runtime.stopStarted = make(chan string, 1)
	runtime.stopRelease = make(chan struct{})
	defer close(runtime.stopRelease)
	done := make(chan struct{})
	go func() {
		controller.executeSlotCommand(slot, command)
		close(done)
	}()
	select {
	case <-runtime.stopStarted:
	case <-time.After(time.Second):
		t.Fatal("restart did not begin stopping the old container")
	}
	stop := stopCommand(command)
	controller.interruptStart(slot, stop)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("stop did not interrupt the restart's old-container stop")
	}
	if got := harness.created.Load(); got != 1 {
		t.Fatalf("created %d sequences, want only the original boot", got)
	}
	runtime.stopStarted = nil
	runtime.stopRelease = nil
	controller.executeSlotCommand(slot, stop)
	if types := eventTypesForAssignment(transport, command.AssignmentID); strings.Join(types, ",") != "server.ready,server.stopped" {
		t.Fatalf("events = %v, want ready followed by stopped", types)
	}
}
