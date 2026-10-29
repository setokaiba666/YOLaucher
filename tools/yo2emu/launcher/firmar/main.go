//go:build windows

// firmar: firma de editor de las versiones de JoinServer.exe (ver sala/firma.go).
//
//	firmar -generar                  crea la clave del editor (solo si no existe) y enseña la pública para sala/firma.go
//	(también vale YO2OnlineLauncher.exe: se firma con su identidad, sala.NombreLanzadorApp, y nadie puede instalar uno en lugar del otro)
//	firmar [-version N] <JoinServer.exe>  escribe <exe>.firma (sin -version, se la pregunta al exe: JoinServer.exe -version)
//	firmar -comprobar <JoinServer.exe>    comprueba <exe>.firma con la clave pública incrustada en este firmar.exe
//	firmar -paquete <carpeta>             firma la VERSIÓN del paquete del Discord (sala/paquete_man.go): lee
//	                                      <carpeta>\paquete.manifiesto.json (lo escribe empaquetar.py), comprueba que la
//	                                      carpeta es EXACTAMENTE eso (ficheros, tamaños, sha256, .firma del exe) y
//	                                      escribe <carpeta>\paquete.json firmado (y borra el manifiesto sin firmar)
//	firmar -comprobar -paquete <carpeta>  comprueba paquete.json y la carpeta, sin firmar nada
//	firmar -red [-version N] [-salida F] <nodos.txt>  firma la lista de nodos de arranque/relays (sala/red_nodos.go;
//	                                      NOTES-red.md §13) -> F (por defecto <nodos>.firmada.json). Va dentro del
//	                                      manifiesto del paquete (empaquetar.py --firmar la pide sola)
//	firmar -comprobar -red <lista.json>   comprueba una lista de nodos firmada
//
// La clave PRIVADA está siempre en %LOCALAPPDATA%\YO2Online\firma\editor-ed25519.key (no hay opción para ponerla en
// otro sitio: así no acaba en el repo ni en launcher\). Haz una copia en un USB: si se pierde, las versiones nuevas
// habrá que llevarlas a mano a cada invitado (y repartir un JoinServer con otra clave pública).
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/windows"
	"yo2online/sala"
)

func rutaClave() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), "YO2Online", "firma", "editor-ed25519.key")
}

func main() {
	generar := flag.Bool("generar", false, "crear la clave del editor (si no existe) y mostrar la pública")
	version := flag.Int64("version", 0, "versión del exe (por defecto se le pregunta con -version)")
	comprobar := flag.Bool("comprobar", false, "comprobar <exe>.firma en vez de firmar")
	paquete := flag.Bool("paquete", false, "el argumento es la carpeta del paquete: firmar (o comprobar) su manifiesto")
	red := flag.Bool("red", false, "el argumento es la lista de nodos de arranque (texto): firmarla (o comprobar la firmada)")
	salida := flag.String("salida", "", "con -red: dónde se escribe la lista firmada")
	flag.Parse()
	windows.SetConsoleOutputCP(65001)
	if os.Getenv("LOCALAPPDATA") == "" {
		fallo(errors.New("falta LOCALAPPDATA"))
	}
	switch {
	case *generar:
		fallo(generarClave())
	case flag.NArg() != 1:
		fmt.Println("Uso: firmar -generar | firmar [-version N] <JoinServer.exe> | firmar -comprobar <JoinServer.exe> | firmar [-comprobar] -paquete <carpeta> | firmar -red [-version N] [-salida F] <nodos.txt> | firmar -comprobar -red <lista.json>")
		os.Exit(2)
	case *red && *comprobar:
		fallo(comprobarRed(flag.Arg(0)))
	case *red:
		fallo(firmarRed(flag.Arg(0), *version, *salida))
	case *paquete && *comprobar:
		fallo(comprobarPaquete(flag.Arg(0)))
	case *paquete:
		fallo(firmarPaquete(flag.Arg(0)))
	case *comprobar:
		fallo(comprobarFirma(flag.Arg(0)))
	default:
		fallo(firmar(flag.Arg(0), *version))
	}
}

func fallo(err error) {
	if err != nil {
		fmt.Println("ERROR:", err)
		os.Exit(1)
	}
}

func generarClave() error {
	p := rutaClave()
	if _, err := os.Stat(p); err == nil {
		priv, err := leerClave()
		if err != nil {
			return err
		}
		fmt.Println("La clave ya existe (no se toca):", p)
		fmt.Println("Pública:", hex.EncodeToString(priv.Public().(ed25519.PublicKey)))
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	seed := make([]byte, ed25519.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return err
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(seed); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	pub := ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey)
	fmt.Println("Clave del editor creada:", p)
	fmt.Println("HAZ UNA COPIA en un USB (sin ella no se pueden firmar versiones nuevas).")
	fmt.Println("Pública (va en sala/firma.go, ClaveEditorHex):", hex.EncodeToString(pub))
	return nil
}

func leerClave() (ed25519.PrivateKey, error) {
	b, err := os.ReadFile(rutaClave())
	if err != nil {
		return nil, fmt.Errorf("no hay clave del editor en este PC (%v): crea una con firmar -generar", err)
	}
	if len(b) != ed25519.SeedSize {
		return nil, errors.New("la clave del editor está dañada (no tiene 32 bytes)")
	}
	return ed25519.NewKeyFromSeed(b), nil
}

// versionDe: le pregunta al exe su versión (JoinServer.exe -version imprime solo el número).
func versionDe(exe string) (int64, error) {
	out, err := exec.Command(exe, "-version").Output()
	if err != nil {
		return 0, fmt.Errorf("no puedo preguntarle la versión a %s: %w", exe, err)
	}
	v, err := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
	if err != nil || v <= 0 {
		return 0, fmt.Errorf("%s no tiene versión (compílalo con build.ps1): %q", exe, strings.TrimSpace(string(out)))
	}
	return v, nil
}

// nombreDe: qué se firma según el nombre del fichero (YO2OnlineLauncher.exe, o el viejo YO2Launcher.exe = el lanzador
// con ventana, que se firma con su identidad sala.NombreLanzadorApp; el resto, JoinServer.exe).
func nombreDe(exe string) string {
	if b := filepath.Base(exe); strings.EqualFold(b, sala.ExeLanzadorApp) || strings.EqualFold(b, sala.NombreLanzadorApp) {
		return sala.NombreLanzadorApp
	}
	return sala.NombreLanzador
}

func firmar(exe string, version int64) error {
	priv, err := leerClave()
	if err != nil {
		return err
	}
	if pubIncrustada, err := sala.ClaveEditor(); err != nil || !pubIncrustada.Equal(priv.Public()) {
		return errors.New("la clave de este PC no coincide con la pública de sala/firma.go: los invitados rechazarían la firma")
	}
	if version == 0 {
		if version, err = versionDe(exe); err != nil {
			return err
		}
	}
	st, err := os.Stat(exe)
	if err != nil {
		return err
	}
	sha, err := sala.ShaFichero(exe)
	if err != nil {
		return err
	}
	nombre := nombreDe(exe)
	f := sala.FirmaLanzador{V: sala.FirmaVersionV, Nombre: nombre, Version: version, SHA256: sha, Tam: st.Size()}
	f.Firma = base64.StdEncoding.EncodeToString(ed25519.Sign(priv, f.Mensaje()))
	if err := f.VerificarPara(nombre); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(f, "", "  ")
	if err := os.WriteFile(exe+sala.ExtFirma, b, 0o644); err != nil {
		return err
	}
	fmt.Printf("Firmado: %s (versión %d, sha256 %s…)\n", exe+sala.ExtFirma, version, sha[:12])
	return nil
}

func comprobarFirma(exe string) error {
	f, err := sala.LeerFirma(exe + sala.ExtFirma)
	if err != nil {
		return err
	}
	if err := f.VerificarPara(nombreDe(exe)); err != nil {
		return err
	}
	sha, err := sala.ShaFichero(exe)
	if err != nil {
		return err
	}
	if sha != f.SHA256 {
		return fmt.Errorf("la firma es válida pero es de OTRO exe (sha256 %s… firmado, %s… el fichero)", f.SHA256[:12], sha[:12])
	}
	fmt.Printf("OK: %s firmado por el editor, versión %d\n", exe, f.Version)
	return nil
}

// firmarPaquete: paquete.json firmado para la carpeta del paquete (ver la cabecera).
func firmarPaquete(dir string) error {
	priv, err := leerClave()
	if err != nil {
		return err
	}
	if pub, err := sala.ClaveEditor(); err != nil || !pub.Equal(priv.Public()) {
		return errors.New("la clave de este PC no coincide con la pública de sala/firma.go: los lanzadores rechazarían el paquete")
	}
	raw, err := os.ReadFile(filepath.Join(dir, sala.NombrePaqueteSinFir))
	if err != nil {
		return fmt.Errorf("falta el manifiesto sin firmar (lo escribe empaquetar.py): %w", err)
	}
	var m sala.ManifiestoPaquete
	if err := json.Unmarshal(raw, &m); err != nil {
		return fmt.Errorf("manifiesto mal formado: %w", err)
	}
	if err := m.Validar(); err != nil {
		return err
	}
	if err := sala.ComprobarArbol(dir, &m); err != nil {
		return fmt.Errorf("la carpeta no es lo que dice el manifiesto: %w", err)
	}
	out, err := sala.FirmarManifiesto(priv, raw)
	if err != nil {
		return err
	}
	if _, err := sala.VerificarPaquete(out); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, sala.NombrePaquete), out, 0o644); err != nil {
		return err
	}
	os.Remove(filepath.Join(dir, sala.NombrePaqueteSinFir))
	fmt.Printf("Paquete firmado: %s (versión %d, lanzador %d, %d ficheros)\n", filepath.Join(dir, sala.NombrePaquete),
		m.Version, m.VersionLanzador, len(m.Ficheros))
	return nil
}

func comprobarPaquete(dir string) error {
	pv, err := sala.LeerPaquete(filepath.Join(dir, sala.NombrePaquete))
	if err != nil {
		return err
	}
	if err := sala.ComprobarArbol(dir, pv.Man); err != nil {
		return err
	}
	fmt.Printf("OK: %s firmado por el editor, versión %d (lanzador %d), %d ficheros\n", dir, pv.Man.Version,
		pv.Man.VersionLanzador, len(pv.Man.Ficheros))
	return nil
}

// firmarRed: la lista de nodos de arranque y relays firmada por el editor (sala/red_nodos.go). El texto: una línea por
// nodo, «arranque|bootstrap <multiaddr>» o «relay <multiaddr>»; una línea que no vale PARA (no se firma a medias).
func firmarRed(txt string, version int64, salida string) error {
	priv, err := leerClave()
	if err != nil {
		return err
	}
	if pub, err := sala.ClaveEditor(); err != nil || !pub.Equal(priv.Public()) {
		return errors.New("la clave de este PC no coincide con la pública de sala/firma.go: los lanzadores rechazarían la lista")
	}
	b, err := os.ReadFile(txt)
	if err != nil {
		return err
	}
	arranque, relays, malas := sala.LeerNodosTexto(b)
	if len(malas) > 0 {
		return fmt.Errorf("%s tiene líneas que no valen: %v", txt, malas)
	}
	l, err := sala.FirmarListaRed(priv, sala.ListaRed{Version: version, Arranque: arranque, Relays: relays})
	if err != nil {
		return err
	}
	if err := l.Verificar(); err != nil {
		return err
	}
	out, err := json.MarshalIndent(l, "", " ")
	if err != nil {
		return err
	}
	if salida == "" {
		salida = strings.TrimSuffix(txt, filepath.Ext(txt)) + ".firmada.json"
	}
	if err := os.WriteFile(salida, out, 0o644); err != nil {
		return err
	}
	fmt.Printf("Lista de nodos firmada: %s (versión %d, %d de arranque, %d relays)\n", salida, l.Version, len(l.Arranque), len(l.Relays))
	return nil
}

func comprobarRed(p string) error {
	b, err := os.ReadFile(p)
	if err != nil {
		return err
	}
	var l sala.ListaRed
	if err := json.Unmarshal(b, &l); err != nil {
		return err
	}
	if err := l.Verificar(); err != nil {
		return err
	}
	fmt.Printf("OK: lista de nodos firmada por el editor, versión %d (%d de arranque, %d relays)\n", l.Version, len(l.Arranque), len(l.Relays))
	return nil
}
