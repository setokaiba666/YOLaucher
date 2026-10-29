//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/wailsapp/go-webview2/webviewloader"
	"github.com/wailsapp/wails/v2/pkg/options/mac"
	"golang.org/x/sys/windows"
)

func dirDatos() string {
	datos := filepath.Join(os.Getenv("LOCALAPPDATA"), "YO2Online")
	if d := os.Getenv("YO2_LANZADOR_DATOS"); d != "" {
		datos = d
	}
	return datos
}

func abrirCarpeta(d string) error {
	return exec.Command("explorer.exe", d).Start()
}

func versionWebView() (string, error) {
	return webviewloader.GetAvailableCoreWebView2BrowserVersionString("")
}

func opcionesMac() *mac.Options { return nil }

func esperarProceso(pid int, max time.Duration) {
	h, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return
	}
	defer windows.CloseHandle(h)
	windows.WaitForSingleObject(h, uint32(max.Milliseconds()))
}
