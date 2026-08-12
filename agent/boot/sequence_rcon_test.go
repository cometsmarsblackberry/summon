package boot

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestWaitForRCONWithProbeBoundsEachAttempt(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	probeFinished := make(chan error, 1)
	result := make(chan error, 1)

	go func() {
		result <- waitForRCONWithProbe(
			ctx,
			5*time.Second,
			20*time.Millisecond,
			time.Hour,
			func(probeCtx context.Context) error {
				<-probeCtx.Done()
				probeFinished <- probeCtx.Err()
				return probeCtx.Err()
			},
		)
	}()

	select {
	case err := <-probeFinished:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("probe ended with %v, want context deadline exceeded", err)
		}
	case <-time.After(time.Second):
		t.Fatal("individual RCON probe did not time out")
	}

	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("wait ended with %v, want context canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("RCON wait did not observe parent cancellation")
	}
}

func TestWaitForRCONWithProbeRetriesUntilSuccess(t *testing.T) {
	var attempts atomic.Int32
	err := waitForRCONWithProbe(
		context.Background(),
		time.Second,
		50*time.Millisecond,
		time.Millisecond,
		func(context.Context) error {
			if attempts.Add(1) < 3 {
				return errors.New("not ready")
			}
			return nil
		},
	)
	if err != nil {
		t.Fatalf("RCON wait returned an error: %v", err)
	}
	if got := attempts.Load(); got != 3 {
		t.Fatalf("probe attempts = %d, want 3", got)
	}
}

func TestWaitForRCONWithProbeHonorsOverallDeadline(t *testing.T) {
	var attempts atomic.Int32
	err := waitForRCONWithProbe(
		context.Background(),
		60*time.Millisecond,
		10*time.Millisecond,
		time.Millisecond,
		func(probeCtx context.Context) error {
			attempts.Add(1)
			<-probeCtx.Done()
			return probeCtx.Err()
		},
	)
	if err == nil || !strings.Contains(err.Error(), "RCON not ready after 60ms") {
		t.Fatalf("RCON wait error = %v, want overall timeout", err)
	}
	if got := attempts.Load(); got < 2 {
		t.Fatalf("probe attempts = %d, want multiple bounded attempts", got)
	}
}

func TestWaitForRCONWithProbeRejectsLateSuccess(t *testing.T) {
	err := waitForRCONWithProbe(
		context.Background(),
		10*time.Millisecond,
		50*time.Millisecond,
		time.Millisecond,
		func(context.Context) error {
			time.Sleep(20 * time.Millisecond)
			return nil
		},
	)
	if err == nil || !strings.Contains(err.Error(), "RCON not ready after 10ms") {
		t.Fatalf("RCON wait error = %v, want overall timeout", err)
	}
}
