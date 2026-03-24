package sysinfo

import "syscall"

func statfsLinux(path string, buf *statfsT) error {
	var s syscall.Statfs_t
	if err := syscall.Statfs(path, &s); err != nil {
		return err
	}
	// Blocks and Bavail are in units of Frsize, not Bsize.
	// Frsize is the fragment size; Bsize is the optimal I/O size.
	buf.Bsize = s.Frsize
	if buf.Bsize == 0 {
		buf.Bsize = s.Bsize
	}
	buf.Blocks = s.Blocks
	buf.Bavail = s.Bavail
	return nil
}
