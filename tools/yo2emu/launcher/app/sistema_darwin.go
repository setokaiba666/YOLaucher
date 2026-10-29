//go:build darwin

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/wailsapp/wails/v2/pkg/options/mac"
)

func dirDatos() string {
	if d := os.Getenv("YO2_LANZADOR_DATOS"); d != "" {
		publicarLocalAppData(d)
		return d
	}
	home, err := os.UserHomeDir()
	base := os.TempDir()
	if err == nil {
		base = filepath.Join(home, "Library", "Application Support")
	}
	datos := filepath.Join(base, "YO2Online")
	publicarLocalAppData(datos)
	return datos
}

// publicarLocalAppData: run.py escribe sus tokens en %LOCALAPPDATA%\YO2Online. En macOS esa variable no existe.
func publicarLocalAppData(datos string) {
	if os.Getenv("LOCALAPPDATA") != "" || filepath.Base(datos) != "YO2Online" {
		return
	}
	os.Setenv("LOCALAPPDATA", filepath.Dir(datos))
}

func abrirCarpeta(d string) error {
	return exec.Command("open", d).Start()
}

func versionWebView() (string, error) { return "WebKit", nil }

func opcionesMac() *mac.Options {
	return &mac.Options{TitleBar: mac.TitleBarHiddenInset()}
}

func esperarProceso(pid int, max time.Duration) {
	fin := time.Now().Add(max)
	for time.Now().Before(fin) {
		if err := syscall.Kill(pid, 0); err != nil {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// otrasInstancias: procesos con el mismo nombre de exe, sin este ni su padre.
func otrasInstancias(exe string) int {
	out, err := exec.Command("ps", "-axww", "-o", "pid=,ppid=,command=").Output()
	if err != nil {
		return 0
	}
	type proc struct {
		pid, padre int
		cmd        string
	}
	var ps []proc
	for _, linea := range strings.Split(string(out), "\n") {
		f := strings.Fields(linea)
		if len(f) < 3 {
			continue
		}
		pid, e1 := strconv.Atoi(f[0])
		ppid, e2 := strconv.Atoi(f[1])
		if e1 != nil || e2 != nil {
			continue
		}
		ps = append(ps, proc{pid, ppid, strings.Join(f[2:], " ")})
	}
	yo := os.Getpid()
	padre := 0
	for _, p := range ps {
		if p.pid == yo {
			padre = p.padre
		}
	}
	nombre := filepath.Base(exe)
	n := 0
	for _, p := range ps {
		if p.pid == yo || p.pid == padre {
			continue
		}
		for _, campo := range strings.Fields(p.cmd) {
			if strings.EqualFold(filepath.Base(campo), nombre) {
				n++
				break
			}
		}
	}
	return n
}
