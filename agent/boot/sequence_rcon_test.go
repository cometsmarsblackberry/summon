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

func TestRCONCommandRetryBoundsAnUnansweredAttempt(t *testing.T) {
	var attempts, waits atomic.Int32
	err := runRCONCommandWithRetry(
		context.Background(),
		3,
		20*time.Millisecond,
		func(attemptCtx context.Context) error {
			if attempts.Add(1) == 1 {
				// The reply never arrives, as with a command sent mid level change.
				<-attemptCtx.Done()
				return attemptCtx.Err()
			}
			return nil
		},
		func(context.Context) error {
			waits.Add(1)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("command returned an error after a successful retry: %v", err)
	}
	if got := attempts.Load(); got != 2 {
		t.Fatalf("attempts = %d, want 2", got)
	}
	if got := waits.Load(); got != 1 {
		t.Fatalf("readiness waits = %d, want 1 before the retry", got)
	}
}

func TestRCONCommandRetryGivesUpAfterLastAttempt(t *testing.T) {
	var attempts, waits atomic.Int32
	failure := errors.New("exec failed")
	err := runRCONCommandWithRetry(
		context.Background(),
		3,
		time.Second,
		func(context.Context) error {
			attempts.Add(1)
			return failure
		},
		func(context.Context) error {
			waits.Add(1)
			return nil
		},
	)
	if !errors.Is(err, failure) {
		t.Fatalf("error = %v, want the last attempt's error", err)
	}
	if got := attempts.Load(); got != 3 {
		t.Fatalf("attempts = %d, want 3", got)
	}
	if got := waits.Load(); got != 2 {
		t.Fatalf("readiness waits = %d, want 2 (none after the last attempt)", got)
	}
}

func TestRCONCommandRetryStopsWhenParentIsCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var attempts atomic.Int32
	result := make(chan error, 1)
	go func() {
		result <- runRCONCommandWithRetry(
			ctx,
			3,
			time.Hour,
			func(attemptCtx context.Context) error {
				attempts.Add(1)
				<-attemptCtx.Done()
				return attemptCtx.Err()
			},
			func(context.Context) error {
				t.Error("waited for RCON after the parent was cancelled")
				return nil
			},
		)
	}()
	time.Sleep(20 * time.Millisecond)
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("error = %v, want context canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("command did not observe parent cancellation")
	}
	if got := attempts.Load(); got != 1 {
		t.Fatalf("attempts = %d, want 1", got)
	}
}

func TestRetryStopsWaitingWhenContextIsCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var attempts atomic.Int32
	result := make(chan error, 1)
	go func() {
		result <- retry(ctx, "start container", MaxRetries, func() error {
			attempts.Add(1)
			return errors.New("podman run failed")
		})
	}()
	time.Sleep(20 * time.Millisecond)
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("error = %v, want context canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("retry kept sleeping after cancellation")
	}
	if got := attempts.Load(); got != 1 {
		t.Fatalf("attempts = %d, want 1", got)
	}
}
