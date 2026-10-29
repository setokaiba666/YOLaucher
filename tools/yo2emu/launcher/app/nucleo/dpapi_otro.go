//go:build !windows

package nucleo

import "errors"

// Fuera de Windows no hay DPAPI. El sobre no es la clave en claro (ni su base64): un byte de marca y un XOR.
// Quien tenga el fichero puede revertirlo; el fichero vive en la carpeta del usuario. Un «clave» escrito a mano,
// sin la marca, se rechaza igual que un blob que no es de DPAPI.
// ponytail: no es cifrado. Para atarlo al usuario, el paso siguiente es el Llavero.
const marcaSobre = 0xA5

var errSinDPAPI = errors.New("sin DPAPI en este sistema")

func proteger(datos []byte) ([]byte, error) {
	out := make([]byte, 1+len(datos))
	out[0] = marcaSobre
	for i, b := range datos {
		out[1+i] = b ^ 0x5A
	}
	return out, nil
}

func desproteger(datos []byte) ([]byte, error) {
	if len(datos) < 1 || datos[0] != marcaSobre {
		return nil, errSinDPAPI
	}
	out := make([]byte, len(datos)-1)
	for i, b := range datos[1:] {
		out[i] = b ^ 0x5A
	}
	return out, nil
}
