// Package sysinfo collects system metrics from /proc.
package sysinfo

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Stats holds a snapshot of system metrics.
type Stats struct {
	CPUModel     string  `json:"cpu_model"`
	CPUPercent   float64 `json:"cpu_percent"`
	MemTotalMB   int     `json:"mem_total_mb"`
	MemUsedMB    int     `json:"mem_used_mb"`
	MemPercent   float64 `json:"mem_percent"`
	SwapTotalMB  int     `json:"swap_total_mb"`
	SwapUsedMB   int     `json:"swap_used_mb"`
	SwapPercent  float64 `json:"swap_percent"`
	DiskTotalGB  float64 `json:"disk_total_gb"`
	DiskUsedGB   float64 `json:"disk_used_gb"`
	DiskPercent  float64 `json:"disk_percent"`
	UptimeSec    int     `json:"uptime_sec"`
	LoadAvg1     float64 `json:"load_avg_1"`
	LoadAvg5     float64 `json:"load_avg_5"`
	LoadAvg15    float64 `json:"load_avg_15"`
	NumCPUs      int     `json:"num_cpus"`
}

// cpuTimes holds cumulative CPU jiffies from /proc/stat.
type cpuTimes struct {
	total uint64
	idle  uint64
}

var lastCPU *cpuTimes

// Collect gathers current system stats.
func Collect() Stats {
	var s Stats
	s.CPUModel = readCPUModel()
	s.UptimeSec = readUptime()
	s.LoadAvg1, s.LoadAvg5, s.LoadAvg15 = readLoadAvg()
	s.NumCPUs = readNumCPUs()
	s.MemTotalMB, s.MemUsedMB, s.MemPercent,
		s.SwapTotalMB, s.SwapUsedMB, s.SwapPercent = readMemory()
	s.DiskTotalGB, s.DiskUsedGB, s.DiskPercent = readDisk()
	s.CPUPercent = readCPUPercent()
	return s
}

func readCPUModel() string {
	data, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "model name") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				return strings.TrimSpace(parts[1])
			}
		}
	}
	return ""
}

func readUptime() int {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(data))
	if len(fields) < 1 {
		return 0
	}
	val, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0
	}
	return int(val)
}

func readLoadAvg() (float64, float64, float64) {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return 0, 0, 0
	}
	fields := strings.Fields(string(data))
	if len(fields) < 3 {
		return 0, 0, 0
	}
	a1, _ := strconv.ParseFloat(fields[0], 64)
	a5, _ := strconv.ParseFloat(fields[1], 64)
	a15, _ := strconv.ParseFloat(fields[2], 64)
	return a1, a5, a15
}

func readNumCPUs() int {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 1
	}
	count := 0
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "cpu") && len(line) > 3 && line[3] >= '0' && line[3] <= '9' {
			count++
		}
	}
	if count == 0 {
		return 1
	}
	return count
}

func readMemory() (totalMB, usedMB int, percent float64, swapTotalMB, swapUsedMB int, swapPercent float64) {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return
	}
	var total, available, swapTotal, swapFree uint64
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		val, _ := strconv.ParseUint(fields[1], 10, 64)
		switch fields[0] {
		case "MemTotal:":
			total = val
		case "MemAvailable:":
			available = val
		case "SwapTotal:":
			swapTotal = val
		case "SwapFree:":
			swapFree = val
		}
	}
	if total > 0 {
		used := total - available
		totalMB = int(total / 1024)
		usedMB = int(used / 1024)
		percent = float64(used) / float64(total) * 100
	}
	if swapTotal > 0 {
		swapUsed := swapTotal - swapFree
		swapTotalMB = int(swapTotal / 1024)
		swapUsedMB = int(swapUsed / 1024)
		swapPercent = float64(swapUsed) / float64(swapTotal) * 100
	}
	return
}

func readDisk() (totalGB, usedGB, percent float64) {
	// Use syscall-free approach: parse /proc/mounts then stat the filesystem
	// Simpler: use Statfs via syscall on /
	// On Fedora CoreOS / ostree, / is a tiny read-only composefs overlay.
	// The real disk is at /sysroot. Try / first, fall back to /sysroot if
	// the result looks empty (zero available blocks).
	var stat statfsT
	if err := statfs("/", &stat); err != nil {
		return 0, 0, 0
	}
	if stat.Bavail == 0 {
		if err := statfs("/sysroot", &stat); err != nil {
			return 0, 0, 0
		}
	}
	bsize := uint64(stat.Bsize)
	totalGB = float64(stat.Blocks*bsize) / (1024 * 1024 * 1024)
	freeGB := float64(stat.Bavail*bsize) / (1024 * 1024 * 1024)
	usedGB = totalGB - freeGB
	if totalGB > 0 {
		percent = usedGB / totalGB * 100
	}
	return
}

func readCPUPercent() float64 {
	cur := readCPUTimes()
	if cur == nil {
		return 0
	}
	defer func() { lastCPU = cur }()

	if lastCPU == nil {
		// First call — do a quick sample
		time.Sleep(200 * time.Millisecond)
		next := readCPUTimes()
		if next == nil {
			return 0
		}
		return calcCPUPercent(cur, next)
	}
	return calcCPUPercent(lastCPU, cur)
}

func calcCPUPercent(prev, cur *cpuTimes) float64 {
	totalDelta := cur.total - prev.total
	idleDelta := cur.idle - prev.idle
	if totalDelta == 0 {
		return 0
	}
	return float64(totalDelta-idleDelta) / float64(totalDelta) * 100
}

func readCPUTimes() *cpuTimes {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return nil
	}
	// First line: cpu  user nice system idle iowait irq softirq steal ...
	line := strings.SplitN(string(data), "\n", 2)[0]
	fields := strings.Fields(line)
	if len(fields) < 5 || fields[0] != "cpu" {
		return nil
	}
	var total, idle uint64
	for i, f := range fields[1:] {
		val, _ := strconv.ParseUint(f, 10, 64)
		total += val
		if i == 3 { // idle is the 4th value (index 3)
			idle = val
		}
	}
	return &cpuTimes{total: total, idle: idle}
}

// statfsT is a minimal statfs struct for Linux.
type statfsT struct {
	Bsize  int64
	Blocks uint64
	Bavail uint64
}

func statfs(path string, buf *statfsT) error {
	// Use the syscall package
	return statfsLinux(path, buf)
}

func init() {
	// Pre-seed CPU times so the first real Collect gets a delta
	lastCPU = readCPUTimes()
}

// formatBytes is unused but available for display
func formatBytes(bytes uint64) string {
	const unit = 1024
	if bytes < unit {
		return fmt.Sprintf("%d B", bytes)
	}
	div, exp := uint64(unit), 0
	for n := bytes / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(bytes)/float64(div), "KMGTPE"[exp])
}
