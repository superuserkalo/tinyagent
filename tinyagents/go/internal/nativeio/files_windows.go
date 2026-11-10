package nativeio

import (
	"context"
	"os"
)

// Open leaves Windows callers responsible for cooperative console I/O.
// The guarded Bash coding CLI requires a POSIX environment.
func Open(_ context.Context, originals []*os.File) ([]*os.File, func() error, error) {
	return make([]*os.File, len(originals)), func() error { return nil }, nil
}
