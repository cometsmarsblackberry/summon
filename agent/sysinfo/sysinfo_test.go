package sysinfo

import (
	"errors"
	"testing"
)

const gibibyte = uint64(1024 * 1024 * 1024)

func TestReadDiskReportsFullRootWhenSysrootIsUnavailable(t *testing.T) {
	total, used, percent := readDiskWith(func(path string, stat *statfsT) error {
		if path != "/" {
			return errors.New("missing sysroot")
		}
		*stat = statfsT{Bsize: int64(gibibyte), Blocks: 77, Bavail: 0}
		return nil
	})
	if total != 77 || used != 77 || percent != 100 {
		t.Fatalf("full root reported total=%v used=%v percent=%v", total, used, percent)
	}
}

func TestReadDiskUsesCoreOSSysrootWhenAvailable(t *testing.T) {
	total, used, percent := readDiskWith(func(path string, stat *statfsT) error {
		switch path {
		case "/":
			*stat = statfsT{Bsize: int64(gibibyte), Blocks: 1, Bavail: 0}
		case "/sysroot":
			*stat = statfsT{Bsize: int64(gibibyte), Blocks: 80, Bavail: 20}
		}
		return nil
	})
	if total != 80 || used != 60 || percent != 75 {
		t.Fatalf("sysroot reported total=%v used=%v percent=%v", total, used, percent)
	}
}

func TestCalcCPUUsageReportsStealExcludingGuestTime(t *testing.T) {
	prev := parseCPUTimes("cpu  100 0 100 700 0 0 0 100 50 0")
	cur := parseCPUTimes("cpu  200 0 200 1300 0 0 0 300 150 0")
	busy, steal := calcCPUUsage(prev, cur)
	if busy != 40 || steal != 20 {
		t.Fatalf("usage reported busy=%v steal=%v", busy, steal)
	}
}

func TestParseCPUTimesWithoutStealColumn(t *testing.T) {
	times := parseCPUTimes("cpu  10 20 30 40")
	if times == nil || times.total != 100 || times.idle != 40 || times.steal != 0 {
		t.Fatalf("parsed %+v", times)
	}
}
