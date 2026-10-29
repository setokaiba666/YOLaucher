//go:build !windows

package juego

// En macOS el juego se abre con la DLL dentro de CrossOver. frida-inject.exe es de Windows.
var fridaBin []byte
