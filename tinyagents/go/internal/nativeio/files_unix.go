//go:build !windows

package nativeio

import (
	"context"
	"errors"
	"os"
	"sync"
	"syscall"
)

type borrowedFile struct {
	original syscall.RawConn
	flags    int
	fd       int
	stream   *os.File
	changed  bool
}

// Open borrows file descriptions and owns pollable, close-on-exec duplicates.
// Cancellation closes the duplicates; the caller joins I/O before restoring flags.
func Open(ctx context.Context, originals []*os.File) (files []*os.File, cleanup func() error, openErr error) {
	files = make([]*os.File, len(originals))
	owned := make([]*borrowedFile, len(originals))
	var closeOnce sync.Once
	var closeErr error
	closeOwned := func() {
		closeOnce.Do(func() {
			for _, file := range owned {
				if file == nil {
					continue
				}
				if file.stream != nil {
					if err := file.stream.Close(); !errors.Is(err, os.ErrClosed) {
						closeErr = errors.Join(closeErr, err)
					}
				} else if file.fd >= 0 {
					closeErr = errors.Join(closeErr, syscall.Close(file.fd))
				}
			}
		})
	}
	restore := func() error {
		var result error
		for _, file := range owned {
			if file == nil || !file.changed {
				continue
			}
			var flagErr error
			err := file.original.Control(func(fd uintptr) {
				flagErr = syscall.SetNonblock(int(fd), file.flags&syscall.O_NONBLOCK != 0)
			})
			result = errors.Join(result, err, flagErr)
		}
		return result
	}
	defer func() {
		if openErr != nil {
			closeOwned()
			openErr = errors.Join(openErr, closeErr, restore())
		}
	}()
	// Capture every original mode before changing shared file-description flags.
	for index, original := range originals {
		if original == nil {
			continue
		}
		raw, err := original.SyscallConn()
		if err != nil {
			return nil, nil, err
		}
		file := &borrowedFile{original: raw, fd: -1}
		owned[index] = file
		var duplicateErr error
		err = raw.Control(func(fd uintptr) {
			flags, _, errno := syscall.Syscall(syscall.SYS_FCNTL, fd, syscall.F_GETFL, 0)
			if errno != 0 {
				duplicateErr = errno
				return
			}
			file.flags = int(flags)
			duplicate, _, errno := syscall.Syscall(syscall.SYS_FCNTL, fd, syscall.F_DUPFD_CLOEXEC, 3)
			if errno != 0 {
				duplicateErr = errno
				return
			}
			file.fd = int(duplicate)
		})
		if err = errors.Join(err, duplicateErr); err != nil {
			return nil, nil, err
		}
	}
	for index, file := range owned {
		if file == nil {
			continue
		}
		if err := syscall.SetNonblock(file.fd, true); err != nil {
			return nil, nil, err
		}
		file.changed = file.flags&syscall.O_NONBLOCK == 0
		file.stream = os.NewFile(uintptr(file.fd), originals[index].Name())
		files[index] = file.stream
	}
	stopped := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		closeOwned()
		close(stopped)
	})
	return files, func() error {
		if !stop() {
			<-stopped
		}
		closeOwned()
		return errors.Join(closeErr, restore())
	}, nil
}
