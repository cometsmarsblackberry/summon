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
