//go:build windows

package juego

import _ "embed"

//go:embed frida-inject.exe
var fridaBin []byte
