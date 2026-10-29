package main

// Al arrancar, antes de abrir la ventana: si hay una versión del paquete preparada (app/nucleo/paquete_act.go), se
// aplica aquí, cuando todavía no está abierto nada. Si cambia el exe, se arranca el nuevo y este proceso sale.

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"

	"yo2online/app/nucleo"
)

// argSoloAplicar: «YO2OnlineLauncher.exe -aplicar-actualizacion» aplica lo preparado y sale, sin ventana y sin relanzar
// (para comprobar a mano una actualización atascada, y para la prueba con exes de verdad).
const argSoloAplicar = "-aplicar-actualizacion"

// aplicarPaqueteAlArrancar: true = ya está en marcha el exe nuevo y este proceso tiene que salir.
func aplicarPaqueteAlArrancar(exe string, offset int, logf func(string, ...any)) bool {
	if n := otrasInstancias(exe); n > 0 {
		// otra ventana de este mismo lanzador abierta: ahora no se cambia nada (se aplicará en el próximo arranque)
		logf("[paquete] hay %d proceso(s) más de %s: la actualización se aplicará en otro arranque", n, filepath.Base(exe))
		return false
	}
	if !nucleo.AplicarAlArrancar(exe, offset, logf) || (len(os.Args) > 1 && os.Args[1] == argSoloAplicar) {
		return false
	}
	cmd := exec.Command(exe, os.Args[1:]...)
	cmd.Env = append(os.Environ(), nucleo.EnvPadrePID+"="+strconv.Itoa(os.Getpid()))
	if err := cmd.Start(); err != nil {
		logf("[paquete] no puedo arrancar el lanzador nuevo (%v): sigo con este proceso", err)
		return false
	}
	logf("[paquete] lanzador nuevo arrancado (pid %d); este proceso sale", cmd.Process.Pid)
	return true
}
