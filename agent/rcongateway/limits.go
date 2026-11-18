package rcongateway

import (
	"context"
	"sync"
	"time"
)

const (
	// An address that fails this often within failureWindow is refused for
	// blockDuration. The game server's own RCON bans an address from the
	// game as well; this block only covers the gateway.
	maxAuthFailures = 5
	failureWindow   = 10 * time.Minute
	blockDuration   = 10 * time.Minute
	// maxTrackedAddresses bounds the memory failed logins can take up.
	maxTrackedAddresses = 4096
)

type failureEntry struct {
	count        int
	windowStart  time.Time
	blockedUntil time.Time
}

// failureLimiter counts failed logins per remote address.
type failureLimiter struct {
	now func() time.Time

	mu      sync.Mutex
	entries map[string]*failureEntry
}

func newFailureLimiter(now func() time.Time) *failureLimiter {
	return &failureLimiter{now: now, entries: make(map[string]*failureEntry)}
}

func (l *failureLimiter) blocked(address string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	entry := l.entries[address]
	return entry != nil && l.now().Before(entry.blockedUntil)
}

// record counts one failed login and reports whether it started a block.
func (l *failureLimiter) record(address string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	entry := l.entries[address]
	if entry == nil {
		if len(l.entries) >= maxTrackedAddresses {
			l.pruneLocked(now)
		}
		entry = &failureEntry{windowStart: now}
		l.entries[address] = entry
	}
	if now.Sub(entry.windowStart) > failureWindow {
		entry.count = 0
		entry.windowStart = now
	}
	entry.count++
	if entry.count < maxAuthFailures {
		return false
	}
	entry.count = 0
	entry.windowStart = now
	entry.blockedUntil = now.Add(blockDuration)
	return true
}

func (l *failureLimiter) reset(address string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.entries, address)
}

// pruneLocked drops entries that no longer matter. If every entry is still
// current, it drops the unblocked ones, which costs those addresses nothing
// but a fresh count.
func (l *failureLimiter) pruneLocked(now time.Time) {
	for address, entry := range l.entries {
		if now.After(entry.blockedUntil) && now.Sub(entry.windowStart) > failureWindow {
			delete(l.entries, address)
		}
	}
	if len(l.entries) < maxTrackedAddresses {
		return
	}
	for address, entry := range l.entries {
		if now.After(entry.blockedUntil) {
			delete(l.entries, address)
		}
	}
}

// rateLimiter is a token bucket shared by every client of one gateway.
type rateLimiter struct {
	now       func() time.Time
	burst     float64
	perSecond float64

	mu     sync.Mutex
	tokens float64
	last   time.Time
}

func newRateLimiter(burst, perSecond int, now func() time.Time) *rateLimiter {
	return &rateLimiter{
		now: now, burst: float64(burst), perSecond: float64(perSecond),
		tokens: float64(burst), last: now(),
	}
}

// reserve takes one token and returns how long the caller must wait for it.
func (r *rateLimiter) reserve() time.Duration {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	r.tokens += now.Sub(r.last).Seconds() * r.perSecond
	if r.tokens > r.burst {
		r.tokens = r.burst
	}
	r.last = now
	r.tokens--
	if r.tokens >= 0 {
		return 0
	}
	return time.Duration(-r.tokens / r.perSecond * float64(time.Second))
}

func (r *rateLimiter) wait(ctx context.Context) error {
	delay := r.reserve()
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
