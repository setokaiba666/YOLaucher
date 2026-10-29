//go:build darwin

package juego

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const wineCrossOver = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"

// dirBotellas: las botellas privadas de CrossOver. Las pruebas la cambian.
var dirBotellas = func() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, "Library", "Application Support", "CrossOver", "Bottles")
}()

func init() {
	if d := buscarJuegoCrossOver(); d != "" {
		RutaPorDefecto = d
	}
}

func soloDLL() bool { return true }

func RequiereCrossOver() bool { return true }

func CrossOverListo() error {
	_, err := buscarWine()
	return err
}

func buscarWine() (string, error) {
	if p := os.Getenv("YO2_CX_WINE"); p != "" {
		if _, err := os.Stat(p); err != nil {
			return "", fmt.Errorf("CrossOver: YO2_CX_WINE no existe (%s)", p)
		}
		return p, nil
	}
	if _, err := os.Stat(wineCrossOver); err != nil {
		return "", fmt.Errorf("CrossOver: no está instalado (%s)", wineCrossOver)
	}
	return wineCrossOver, nil
}

// botellaDe: el nombre de la botella. YO2_CX_BOTTLE manda; si no, la carpeta drive_c que contiene el juego.
func botellaDe(carpeta string) (string, error) {
	if b := strings.TrimSpace(os.Getenv("YO2_CX_BOTTLE")); b != "" {
		return b, nil
	}
	dir := filepath.Clean(carpeta)
	for i := 0; i < 16; i++ {
		if filepath.Base(dir) == "drive_c" {
			bottle := filepath.Dir(dir)
			if _, err := os.Stat(filepath.Join(bottle, "cxbottle.conf")); err == nil {
				return filepath.Base(bottle), nil
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return "", fmt.Errorf("CrossOver: no veo la botella de %s (define YO2_CX_BOTTLE)", carpeta)
}

// rutaWindows: un path de macOS visto desde el juego. La botella de CrossOver mapea Z: a /.
func rutaWindows(unix string) string {
	unix = filepath.Clean(unix)
	if !strings.HasPrefix(unix, "/") {
		return unix
	}
	return "Z:" + unix
}

func comandoJuego(exe string, env []string) (*exec.Cmd, error) {
	wine, err := buscarWine()
	if err != nil {
		return nil, err
	}
	bottle, err := botellaDe(filepath.Dir(exe))
	if err != nil {
		return nil, err
	}
	var win []string
	for _, e := range env {
		k, v, ok := strings.Cut(e, "=")
		if !ok || !strings.HasPrefix(k, "YO2ONLINE_") || v == "" {
			continue
		}
		if k == "YO2ONLINE_NET_LOG" {
			v = rutaWindows(v)
		}
		win = append(win, k+"="+entrecomillar(v))
	}
	args := []string{"--bottle", bottle, "--dll", "version=n,b", "--wait", "--workdir", filepath.Dir(exe)}
	if len(win) > 0 {
		args = append(args, "--env", strings.Join(win, " "))
	}
	args = append(args, exe)
	cmd := exec.Command(wine, args...)
	cmd.Env = env
	return cmd, nil
}

func entrecomillar(v string) string {
	if strings.ContainsAny(v, " \t") {
		return `"` + strings.ReplaceAll(v, `"`, "") + `"`
	}
	return v
}

func buscarJuegoCrossOver() string {
	if dirBotellas == "" {
		return ""
	}
	ents, err := os.ReadDir(dirBotellas)
	if err != nil {
		return ""
	}
	var otra string
	for _, e := range ents {
		if !e.IsDir() {
			continue
		}
		carpeta := filepath.Join(dirBotellas, e.Name(), "drive_c", "Program Files (x86)", "KONAMI", "Yu-Gi-Oh! ONLINE 2")
		if !CarpetaValida(carpeta) {
			continue
		}
		if v, err := VersionJuego(carpeta); err == nil && v == VersionEsperada {
			return carpeta
		}
		if otra == "" {
			otra = carpeta
		}
	}
	return otra
}
