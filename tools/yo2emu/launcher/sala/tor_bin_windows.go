//go:build windows

package sala

import _ "embed"

//go:embed tor.exe
var torBin []byte
