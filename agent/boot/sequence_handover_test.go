package boot

import (
	"errors"
	"testing"

	"github.com/summon/agent/websocket"
)

type recordingReporter struct {
	stages []string
}

func (r *recordingReporter) SendBootProgress(stage string, progress int, message string) error {
	r.stages = append(r.stages, stage)
	return nil
}

func (r *recordingReporter) SendServerReady(ip string, port, tvPort int) error {
	r.stages = append(r.stages, "server_ready")
	return nil
}

func (r *recordingReporter) SendServerReadyWithSDR(info websocket.ServerReadyInfo) error {
	r.stages = append(r.stages, "server_ready")
	return nil
}

func (r *recordingReporter) SendCompetitiveConfigs(configs []string, containerImage string) error {
	return nil
}

func TestAdoptConfigOnlyBeforeContainerStart(t *testing.T) {
	cancelled := &ReservationConfig{ReservationNumber: 135, ContainerImage: "image"}
	next := &ReservationConfig{ReservationNumber: 138, ContainerImage: "image"}
	s := NewSequenceWithConfig(&recordingReporter{}, cancelled)

	if s.AdoptConfig(next) {
		t.Fatal("adopted config before the sequence started")
	}
	if err := s.begin(false); err != nil {
		t.Fatal(err)
	}
	if !s.AdoptConfig(next) {
		t.Fatal("did not adopt config while pulling")
	}
	if s.GetConfig() != next {
		t.Fatalf("config = #%d, want #138", s.GetConfig().ReservationNumber)
	}
	if !s.fixConfig("image") {
		t.Fatal("config was not fixed for the pulled image")
	}
	if s.AdoptConfig(cancelled) {
		t.Fatal("adopted config after the container started")
	}

	s.finish()
	if s.InProgress() {
		t.Fatal("finished sequence reported in progress")
	}
}

func TestHandoverWithAnotherImageRequiresAnotherPull(t *testing.T) {
	s := NewSequenceWithConfig(&recordingReporter{}, &ReservationConfig{ContainerImage: "old"})
	if err := s.begin(false); err != nil {
		t.Fatal(err)
	}
	if !s.AdoptConfig(&ReservationConfig{ContainerImage: "new"}) {
		t.Fatal("did not adopt config while pulling")
	}
	if s.fixConfig("old") {
		t.Fatal("fixed config although the pulled image is not the config's")
	}
	if !s.AdoptConfig(&ReservationConfig{ContainerImage: "new"}) {
		t.Fatal("config was fixed by a stale pull")
	}
}

func TestReconfigureConfigIsFixed(t *testing.T) {
	s := NewSequenceWithConfig(&recordingReporter{}, &ReservationConfig{ContainerImage: "image"})
	if err := s.begin(true); err != nil {
		t.Fatal(err)
	}
	if s.AdoptConfig(&ReservationConfig{ContainerImage: "image"}) {
		t.Fatal("adopted config into a reconfigure, which starts its container immediately")
	}
}

func TestSupersededSequenceStopsReporting(t *testing.T) {
	reporter := &recordingReporter{}
	s := NewSequenceWithConfig(reporter, &ReservationConfig{})
	if err := s.begin(false); err != nil {
		t.Fatal(err)
	}
	s.ws.SendBootProgress("pulling_container", 40, "")

	finished := s.Supersede()
	s.ws.SendBootProgress("starting_container", 80, "")
	s.ws.SendServerReady("192.0.2.1", 27015, 27020)
	s.ws.SendServerReadyWithSDR(websocket.ServerReadyInfo{})

	if len(reporter.stages) != 1 || reporter.stages[0] != "pulling_container" {
		t.Fatalf("reported stages = %v, want only pulling_container", reporter.stages)
	}
	if !s.Superseded() {
		t.Fatal("Superseded() = false after Supersede")
	}
	select {
	case <-finished:
		t.Fatal("finished channel closed while the sequence is running")
	default:
	}
	s.finish()
	<-finished
}

func TestSequenceSupersededBeforeRunDoesNotBoot(t *testing.T) {
	reporter := &recordingReporter{}
	s := NewSequenceWithConfig(reporter, &ReservationConfig{})
	finished := s.Supersede()

	if err := s.Run(); !errors.Is(err, errSuperseded) {
		t.Fatalf("Run() error = %v, want errSuperseded", err)
	}
	<-finished
	if len(reporter.stages) != 0 {
		t.Fatalf("reported stages = %v, want none", reporter.stages)
	}
}
