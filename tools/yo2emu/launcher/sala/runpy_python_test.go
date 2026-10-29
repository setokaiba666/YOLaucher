package sala

import (
	"os"
	"testing"
)

func TestBuscarPython(t *testing.T) {
	p := buscarPython()
	if p == "" {
		t.Skip("no hay Python en este PC")
	}
	mayor, menor, ok := versionPython(p)
	if !ok {
		t.Fatalf("no pude leer la versión de %s", p)
	}
	if _, err := os.Stat("/opt/homebrew/bin/python3"); err == nil && (mayor < 3 || (mayor == 3 && menor < 12)) {
		t.Fatalf("eligió %s (%d.%d) aunque Homebrew tiene uno más nuevo", p, mayor, menor)
	}
}
