// Package juego: abrir Yu-Gi-Oh! Online 2 enganchado al túnel (DLL propia, dll.go; o frida-inject + redirect_hook.js de
// respaldo), encontrar su carpeta y autoactualizar el lanzador por el túnel. Lo usan JoinServer (consola) y
// YO2OnlineLauncher (ventana): antes vivía en el paquete main de JoinServer.
//
// Nunca se toca el fichero hosts ni el registro: el gancho redirige las conexiones del juego a 127.0.0.1 en el propio
// proceso del juego.
package juego

import (
	_ "embed"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"yo2online/sala"
)

//go:embed redirect_hook.js
var hookJS []byte

// Modo diagnóstico (yo2-diag.txt junto a JoinServer.exe / YO2OnlineLauncher.exe o a yo2.exe): se añade diag_hook.js detrás de
// redirect_hook.js (registro interno del juego, asertos, salidas, excepciones y ficheros, por console.log al log).
//
//go:embed diag_hook.js
var diagJS []byte

// Añadido NUESTRO (no 1:1, pedido por el owner el 07-10): aviso 5092 al pulsar MOVE con sala de duelo propia. Va
// siempre detrás de redirect_hook.js pero no hace nada salvo que exista yo2-aviso-sala.txt junto a yo2.exe o
// YO2_AVISO_SALA=1 (=0 lo apaga). Ver aviso_sala_hook.js.
//
//go:embed aviso_sala_hook.js
var avisoSalaJS []byte

// RutaPorDefecto: dónde instala Konami el juego (var para que las pruebas puedan quitarla).
var RutaPorDefecto = `C:\Program Files (x86)\KONAMI\Yu-Gi-Oh! ONLINE 2`

// FicheroCarpeta: dentro de la carpeta de datos, la carpeta del juego que se eligió una vez.
const FicheroCarpeta = "carpeta-juego.txt"

// VersionEsperada: la única versión del cliente que soportan los servidores (yo2.exe Ver.080912).
const VersionEsperada = sala.VersionJuegoCliente

// Existe: el fichero o carpeta existe.
func Existe(p string) bool { _, err := os.Stat(p); return err == nil }

// NormalizarCarpeta: quita comillas y espacios y, si es la ruta de yo2.exe, devuelve su carpeta.
func NormalizarCarpeta(c string) string {
	c = strings.Trim(strings.TrimSpace(c), `"`)
	if strings.EqualFold(filepath.Base(c), "yo2.exe") {
		c = filepath.Dir(c)
	}
	return c
}

// CarpetaValida: ¿hay un yo2.exe en esa carpeta?
func CarpetaValida(c string) bool { return c != "" && Existe(filepath.Join(c, "yo2.exe")) }

// BuscarCarpeta: la carpeta del juego. La pedida (flag/ajuste) manda si vale: es una elección explícita. Si no, entre
// junto al exe, la guardada en <datos>\carpeta-juego.txt y la ruta de instalación de Konami (en ese orden) gana la
// primera cuyo yo2.exe es de la versión que usan los servidores (VersionEsperada); si ninguna lo es, la primera que
// tenga un yo2.exe. Así un yo2.exe viejo de la ruta de Konami (p. ej. el cliente de 2006, Ver.060419) no tapa al cliente
// bueno (Ver.080912) que está en otra de las carpetas. "" = no está en ninguna.
func BuscarCarpeta(pedida, exeDir, datos string) string {
	if CarpetaValida(pedida) {
		return pedida
	}
	cands := []string{exeDir}
	if b, err := os.ReadFile(filepath.Join(datos, FicheroCarpeta)); err == nil {
		cands = append(cands, strings.TrimSpace(string(b)))
	}
	cands = append(cands, RutaPorDefecto)
	primera := ""
	for _, c := range cands {
		if !CarpetaValida(c) {
			continue
		}
		if v, err := VersionJuego(c); err == nil && v == VersionEsperada {
			return c
		}
		if primera == "" {
			primera = c
		}
	}
	return primera
}

// GuardarCarpeta: la recuerda en <datos>\carpeta-juego.txt (la usan JoinServer y la ventana).
func GuardarCarpeta(datos, c string) error {
	return os.WriteFile(filepath.Join(datos, FicheroCarpeta), []byte(c), 0o600)
}

// ErrSinVersion: yo2.exe no lleva la cadena «Ver.AAMMDD.NN».
var ErrSinVersion = errors.New("yo2.exe no dice qué versión es")

var reVersion = regexp.MustCompile(`Ver\.(\d{6})\.\d\d`)

// VersionJuego: la versión del cliente que hay en esa carpeta, leída del propio yo2.exe («Ver.080912.00» → "080912").
func VersionJuego(carpeta string) (string, error) {
	b, err := os.ReadFile(filepath.Join(carpeta, "yo2.exe"))
	if err != nil {
		return "", err
	}
	m := reVersion.FindSubmatch(b)
	if m == nil {
		return "", ErrSinVersion
	}
	return string(m[1]), nil
}

// ExisteDiag: yo2-diag.txt junto al lanzador o junto a yo2.exe activa el modo diagnóstico sin parámetros.
func ExisteDiag(carpetaJuego string) bool {
	dirs := []string{carpetaJuego}
	if exe, err := os.Executable(); err == nil {
		dirs = append(dirs, filepath.Dir(exe))
	}
	for _, d := range dirs {
		if d != "" && Existe(filepath.Join(d, "yo2-diag.txt")) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- lanzar el juego

// Lanzador: lo que hace falta para abrir el juego.
type Lanzador struct {
	Datos string // %LOCALAPPDATA%\YO2Online: aquí se extraen frida-inject.exe y el gancho
	Logf  func(string, ...any)
	Log   io.Writer // salida de frida-inject (nil = descartada)
	// Ventana: tamaño de la ventana del juego (ventana.go): "" = Original, "1.5", "2" o "fit". Solo con el gancho por DLL.
	Ventana string
	// Oculto: no abrir la ventana negra de frida-inject (la ventana del lanzador no tiene consola; JoinServer sí y la comparte).
	Oculto bool
}

func (l *Lanzador) logf(f string, a ...any) {
	if l.Logf != nil {
		l.Logf(f, a...)
	}
}

// Partida: un juego abierto.
type Partida struct {
	// Fin recibe UN mensaje cuando el juego se cierra: «Juego cerrado.» o por qué no llegó a arrancar.
	Fin <-chan string
	// FinMsg: lo mismo que Fin, con clave para la ventana (se envía justo antes que Fin; con búfer, no hace falta leerlo).
	FinMsg <-chan sala.Msg
	Cmd    *exec.Cmd
}

// Matar cierra frida-inject (y con él el juego) o, con el gancho por DLL, el propio yo2.exe.
func (p *Partida) Matar() {
	if p != nil && p.Cmd != nil && p.Cmd.Process != nil {
		p.Cmd.Process.Kill()
	}
}

// Extraer deja frida-inject.exe y el gancho en la carpeta de datos y devuelve sus rutas. offset: el YO2_PORT_OFFSET del
// servidor propio al jugar en este PC (solo pruebas; 0 = los puertos de siempre), ver redirect_hook.js.
func (l *Lanzador) Extraer(diag bool, offset int) (frida, hook string, err error) {
	frida = filepath.Join(l.Datos, "frida-inject.exe")
	if st, err := os.Stat(frida); err != nil || st.Size() != int64(len(fridaBin)) {
		if err := os.WriteFile(frida, fridaBin, 0o700); err != nil {
			return "", "", err
		}
	}
	hook = filepath.Join(l.Datos, "redirect_hook.js")
	js := []byte(fmt.Sprintf("const YO2_OFFSET = %d;\n", offset))
	js = append(append(append(js, hookJS...), 10), avisoSalaJS...) // 10 = salto de línea
	if diag {
		js = append(append(js, 10), diagJS...)
	}
	if err := os.WriteFile(hook, js, 0o600); err != nil {
		return "", "", err
	}
	return frida, hook, nil
}

// ErrCarpeta: no hay yo2.exe en la carpeta pedida.
var ErrCarpeta = errors.New("no encuentro yo2.exe: elige la carpeta del juego en Ajustes")

// ErrPreparar / ErrLanzar: no se pudieron sacar los ficheros del lanzador / no arrancó frida-inject (suele ser el antivirus).
var (
	ErrPreparar = errors.New("no puedo preparar los ficheros del lanzador")
	ErrLanzar   = errors.New("no pude lanzar el juego")
)

// Lanzar abre yo2.exe de esa carpeta con el gancho que manda todo su tráfico a 127.0.0.1 (donde escuchan los túneles
// o run.py). Vuelve en cuanto el juego (o frida-inject) está en marcha; cuando el juego se cierra, p.Fin recibe el mensaje.
func (l *Lanzador) Lanzar(carpeta string, diag bool) (*Partida, error) {
	return l.LanzarCon(carpeta, diag, 0)
}

// LanzarCon: Lanzar con el YO2_PORT_OFFSET del servidor propio («Jugar aquí» en pruebas): el juego va a 5730+offset y
// al HTTP 8730+offset. 0 = los puertos de siempre (jugar de verdad y por túnel).
func (l *Lanzador) LanzarCon(carpeta string, diag bool, offset int) (*Partida, error) {
	if !CarpetaValida(carpeta) {
		return nil, ErrCarpeta
	}
	if diag {
		l.logf("MODO DIAGNOSTICO: se carga diag_hook.js detras de redirect_hook.js")
	}
	modo, motivo := l.ElegirGancho(carpeta, diag)
	dll := DLLIncrustada()
	if soloDLL() && modo != GanchoDLL {
		// frida-inject.exe es de Windows. En macOS el juego se abre dentro de CrossOver y solo vale la DLL.
		modo, motivo = GanchoDLL, "CrossOver ("+motivo+")"
	}
	if modo == GanchoDLL {
		if len(dll) == 0 {
			if soloDLL() {
				return nil, fmt.Errorf("%w: CrossOver necesita la DLL de red y este lanzador no la lleva", ErrLanzar)
			}
			modo, motivo = GanchoFrida, "este lanzador no lleva la DLL"
		} else if que, err := ColocarDLL(carpeta, dll); err != nil {
			if soloDLL() {
				return nil, fmt.Errorf("%w: CrossOver: no puedo dejar %s en la carpeta del juego (%v)", ErrLanzar, NombreDLL, err)
			}
			l.logf("gancho: no puedo dejar %s en %s (%v): se usa frida", NombreDLL, carpeta, err)
			modo, motivo = GanchoFrida, "sin DLL en la carpeta del juego"
		} else {
			l.logf("gancho: %s %s", NombreDLL, que)
		}
	}
	l.logf("gancho: %s (%s)%s", modo, motivo, map[bool]string{true: fmt.Sprintf(", offset %d", offset), false: ""}[offset != 0])

	var cmd *exec.Cmd
	var exe, tmp, logDLL string
	if modo == GanchoDLL {
		// yo2.exe a secas: carga la version.dll de su carpeta y esta, con YO2ONLINE_NET=1, pone los ganchos.
		exe = filepath.Join(carpeta, "yo2.exe")
		logDLL = filepath.Join(l.Datos, FicheroLogDLL)
		os.Remove(logDLL)
		// El manifiesto de yo2.exe pide administrador; así arranca como usuario normal (basta en el entorno del hijo).
		// En macOS comandoJuego abre este mismo exe con el wine de CrossOver.
		env := append(os.Environ(), "__COMPAT_LAYER=RunAsInvoker", "YO2ONLINE_NET=1", "YO2ONLINE_NET_LOG="+logDLL,
			"YO2ONLINE_OFFSET="+strconv.Itoa(offset))
		// Tamaño de ventana (ventana.go). Una YO2ONLINE_WINDOW heredada del entorno no cuenta: manda el ajuste del lanzador.
		env = append(env, EnvVentana+"=")
		if ev := entornoVentana(l.Ventana); ev != nil {
			env = append(env, ev...) // el último valor gana
			l.logf("ventana: tamaño %q (ventana ampliada de la DLL)", l.Ventana)
		}
		var err error
		cmd, err = comandoJuego(exe, env)
		if err != nil {
			return nil, fmt.Errorf("%w: %w", ErrLanzar, err)
		}
	} else {
		fi, hook, err := l.Extraer(diag, offset)
		if err != nil {
			return nil, fmt.Errorf("%w en %s: %w", ErrPreparar, l.Datos, err)
		}
		exe = fi
		cmd = exec.Command(fi, "-f", filepath.Join(carpeta, "yo2.exe"), "-s", hook)
		// frida-inject saca sus ayudantes a %TEMP%\frida-<hash> y, si el juego se cierra de golpe, se quedan ahí para
		// siempre. Con un TEMP propio dentro de la carpeta de datos se vacía al arrancar y al cerrar.
		tmp = filepath.Join(l.Datos, "tmp")
		limpiarTmp(tmp)
		os.MkdirAll(tmp, 0o700)
		// El manifiesto de yo2.exe pide administrador; así arranca como usuario normal (lo heredan frida-inject y el juego).
		// YO2ONLINE_NET=0: si se quedó nuestra version.dll en la carpeta, que no enganche también.
		cmd.Env = append(os.Environ(), "__COMPAT_LAYER=RunAsInvoker", "TEMP="+tmp, "TMP="+tmp, "YO2ONLINE_NET=0", EnvVentana+"=")
		if entornoVentana(l.Ventana) != nil {
			l.logf("ventana: el tamaño %q hace falta el gancho por DLL; con frida se ignora", l.Ventana)
		}
		if l.Log != nil {
			cmd.Stdout, cmd.Stderr = l.Log, l.Log
		}
		if l.Oculto {
			sala.OcultarVentana(cmd)
		}
	}
	cmd.Dir = carpeta
	// Mazos premontados (mazos.go) en los perfiles que ya existen, antes de abrir el juego; los perfiles nuevos los
	// recoge la pasada de cada 5 s de abajo (el editor rehace la lista al abrir «Load»).
	if n := InstalarMazosLog(carpeta, l.logf); n > 0 {
		l.logf("mazos premontados copiados: %d", n)
	}
	if err := cmd.Start(); err != nil {
		if modo == GanchoDLL {
			return nil, fmt.Errorf("%w: %w", ErrLanzar, err)
		}
		return nil, fmt.Errorf("%w: %w (¿el antivirus ha bloqueado %s?)", ErrLanzar, err, exe)
	}
	fin := make(chan string, 1)
	finMsg := make(chan sala.Msg, 1)
	var visto atomic.Bool // se ha visto yo2.exe vivo al menos una vez
	// El cliente graba cada duelo en profiles\<usuario>\duellogs\ y, si esa carpeta no existe, se cierra al acabar un
	// duelo entre jugadores (06-10, portátil). Los perfiles nuevos se crean durante la partida: se repasa cada pocos
	// segundos mientras el juego esté abierto (y ahí mismo se les copian los mazos premontados, mazos.go).
	parar := make(chan struct{})
	go func() {
		for {
			AsegurarDuellogs(carpeta)
			if n := InstalarMazosLog(carpeta, l.logf); n > 0 {
				l.logf("mazos premontados copiados: %d", n)
			}
			select {
			case <-parar:
				return
			case <-time.After(5 * time.Second):
			}
		}
	}()
	go func() {
		err := cmd.Wait()
		l.logf("%s termino: %v", filepath.Base(exe), err)
		// frida-inject sigue vivo mientras el juego vive; por si acaso, se espera también a que no quede yo2.exe
		time.Sleep(2 * time.Second)
		for ProcesoActivo("yo2.exe") {
			time.Sleep(3 * time.Second)
		}
		close(parar)
		if tmp != "" {
			limpiarTmp(tmp)
		}
		if modo == GanchoDLL {
			// Lo que apuntó la DLL, al log. Sin «ganchos OK» (o sin registro: no llegó a cargarse), la próxima vez frida.
			ok, hay := l.volcarLogDLL(logDLL)
			if !ok {
				l.logf("gancho: la DLL no confirmó los ganchos (registro: %v); la próxima vez se usa frida", hay)
				if err := marcarFalloDLL(l.Datos, dll); err != nil {
					l.logf("gancho: no pude apuntar el fallo: %v", err)
				}
			}
		}
		if err != nil && !visto.Load() {
			finMsg <- sala.M("msg.juego.no_arranco", fmt.Sprintf("El juego no llego a arrancar (%v).", err), "error", err.Error())
			fin <- fmt.Sprintf("El juego no llego a arrancar (%v).", err)
			return
		}
		finMsg <- sala.M("msg.juego.cerrado", "Juego cerrado.")
		fin <- "Juego cerrado."
	}()
	go func() {
		for i := 0; i < 60; i++ {
			if ProcesoActivo("yo2.exe") {
				visto.Store(true)
				return
			}
			time.Sleep(time.Second)
		}
	}()
	return &Partida{Fin: fin, FinMsg: finMsg, Cmd: cmd}, nil
}

// AsegurarDuellogs crea profiles\<usuario>\duellogs\favorites en cada perfil que no la tenga (solo crea carpetas).
func AsegurarDuellogs(juego string) {
	ents, _ := os.ReadDir(filepath.Join(juego, "profiles"))
	for _, e := range ents {
		if e.IsDir() {
			os.MkdirAll(filepath.Join(juego, "profiles", e.Name(), "duellogs", "favorites"), 0o755)
		}
	}
}

// limpiarTmp vacía la carpeta temporal propia del lanzador (solo esa: nunca el %TEMP% del sistema). Lo que siga en uso
// (otro juego abierto) no se puede borrar en Windows y se queda para la próxima vez.
func limpiarTmp(dir string) {
	if filepath.Base(dir) != "tmp" {
		return
	}
	ents, _ := os.ReadDir(dir)
	for _, e := range ents {
		os.RemoveAll(filepath.Join(dir, e.Name()))
	}
}
