//go:build windows

package main

import (
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// otrasInstancias: procesos con el mismo nombre de exe que este, sin contar este ni el que lo arrancó (el lanzador
// viejo que se acaba de cerrar para reiniciar puede tardar un momento en desaparecer de la lista).
func otrasInstancias(exe string) int {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(snap)
	type proc struct {
		pid, padre uint32
		nombre     string
	}
	var ps []proc
	var pe windows.ProcessEntry32
	pe.Size = uint32(unsafe.Sizeof(pe))
	for err := windows.Process32First(snap, &pe); err == nil; err = windows.Process32Next(snap, &pe) {
		ps = append(ps, proc{pe.ProcessID, pe.ParentProcessID, windows.UTF16ToString(pe.ExeFile[:])})
	}
	yo, padre := uint32(os.Getpid()), uint32(0)
	for _, p := range ps {
		if p.pid == yo {
			padre = p.padre
		}
	}
	n := 0
	for _, p := range ps {
		if p.pid != yo && p.pid != padre && strings.EqualFold(p.nombre, filepath.Base(exe)) {
			n++
		}
	}
	return n
}
