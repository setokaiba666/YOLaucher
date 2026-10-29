//go:build darwin

package juego

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBotellaYComandoCrossOver(t *testing.T) {
	t.Setenv("YO2_CX_BOTTLE", "")
	root := t.TempDir()
	bottle := filepath.Join(root, "Bottles", "yugioh-duel-evolution")
	juego := filepath.Join(bottle, "drive_c", "Program Files (x86)", "KONAMI", "Yu-Gi-Oh! ONLINE 2")
	if err := os.MkdirAll(juego, 0o755); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(bottle, "cxbottle.conf"), []byte("x"), 0o644)
	exe := filepath.Join(juego, "yo2.exe")
	os.WriteFile(exe, []byte("MZ"), 0o644)

	if got, err := botellaDe(juego); err != nil || got != "yugioh-duel-evolution" {
		t.Fatalf("botella: %q %v", got, err)
	}
	t.Setenv("YO2_CX_BOTTLE", "otra")
	if got, err := botellaDe(juego); err != nil || got != "otra" {
		t.Fatalf("YO2_CX_BOTTLE: %q %v", got, err)
	}
	t.Setenv("YO2_CX_BOTTLE", "")
	t.Setenv("YO2_CX_WINE", "/bin/echo")

	log := filepath.Join(root, "Library", "Application Support", "YO2Online", "yo2net.log")
	cmd, err := comandoJuego(exe, []string{"YO2ONLINE_NET=1", "YO2ONLINE_NET_LOG=" + log, "YO2ONLINE_OFFSET=0", "PATH=/usr/bin"})
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Join(cmd.Args, "\n")
	for _, quiero := range []string{"--bottle", "yugioh-duel-evolution", "--dll", "version=n,b", "--workdir", exe} {
		if !strings.Contains(args, quiero) {
			t.Fatalf("falta %q en\n%s", quiero, args)
		}
	}
	if !strings.Contains(args, `YO2ONLINE_NET_LOG="Z:`+log+`"`) && !strings.Contains(args, "YO2ONLINE_NET_LOG="+rutaWindows(log)) {
		t.Fatalf("el log no va como ruta de Windows:\n%s", args)
	}
	if got := rutaWindows(log); !strings.HasPrefix(got, "Z:/") {
		t.Fatalf("rutaWindows: %s", got)
	}
}

func TestBuscarJuegoCrossOver(t *testing.T) {
	root := t.TempDir()
	juego := filepath.Join(root, "mi-botella", "drive_c", "Program Files (x86)", "KONAMI", "Yu-Gi-Oh! ONLINE 2")
	os.MkdirAll(juego, 0o755)
	os.WriteFile(filepath.Join(juego, "yo2.exe"), []byte("MZ Ver.080912.00"), 0o644)
	viejo := dirBotellas
	dirBotellas = root
	t.Cleanup(func() { dirBotellas = viejo })
	if got := buscarJuegoCrossOver(); got != juego {
		t.Fatalf("juego: %q", got)
	}
}
