package tinyagent

import (
	"errors"
	"os"
	"os/exec"
)

func configureProcessGroup(command *exec.Cmd) {}

func killProcessGroup(process *os.Process) error {
	err := process.Kill()
	if errors.Is(err, os.ErrProcessDone) {
		return nil
	}
	return err
}
