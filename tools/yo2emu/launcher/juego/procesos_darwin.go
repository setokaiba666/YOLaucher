//go:build darwin

package juego

import (
	"os/exec"
	"path/filepath"
	"strings"
)

// ProcesoActivo: ¿hay un proceso cuyo ejecutable o algún argumento se llame así?
// Bajo CrossOver yo2.exe no es el nombre del proceso: aparece en la línea de wine.
func ProcesoActivo(nombre string) bool {
	out, err := exec.Command("ps", "-axww", "-o", "command=").Output()
	if err != nil {
		return false
	}
	for _, linea := range strings.Split(string(out), "\n") {
		for _, f := range strings.Fields(linea) {
			if strings.EqualFold(filepath.Base(f), nombre) {
				return true
			}
		}
	}
	return false
}
