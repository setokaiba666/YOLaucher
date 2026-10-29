//go:build windows

package juego

import "os/exec"

func soloDLL() bool { return false }

func RequiereCrossOver() bool { return false }

func CrossOverListo() error { return nil }

func comandoJuego(exe string, env []string) (*exec.Cmd, error) {
	cmd := exec.Command(exe)
	cmd.Env = env
	return cmd, nil
}
