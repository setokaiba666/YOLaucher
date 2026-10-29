//go:build !windows

package sala

// Sin el tor.exe de Windows: extraerTor usa el tor del sistema (brew install tor).
var torBin []byte
