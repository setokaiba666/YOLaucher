package sala

// run.py (el servidor del juego) visto desde el anfitrión: ¿escucha?, ¿dónde está?, ¿cuántos juegan?
// (Escucha/BuscarRunPy/PythonExe vivían en anfitrion/main.go; ahora los usan OpenServer y el lanzador nuevo.)

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Escucha: ¿hay algo aceptando conexiones TCP en addr? (1 s como mucho)
func Escucha(addr string) bool {
	c, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

// BuscarRunPy: la ruta dada, o run.py junto al exe y hasta ocho carpetas más arriba (el .app de macOS queda hondo).
// También vale tools/yo2emu/run.py si el exe está dentro del repositorio. "" = no está.
func BuscarRunPy(flagRuta, exeDir string) string {
	var cands []string
	if flagRuta != "" {
		cands = append(cands, flagRuta)
	}
	dir := exeDir
	for i := 0; i < 8 && dir != "" && dir != string(filepath.Separator); i++ {
		cands = append(cands, filepath.Join(dir, "run.py"), filepath.Join(dir, "tools", "yo2emu", "run.py"))
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	for _, c := range cands {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			p, _ := filepath.Abs(c)
			return p
		}
	}
	return ""
}

func PythonExe() string {
	if p := buscarPython(); p != "" {
		return p
	}
	return "python"
}

// HayPython: hay un intérprete de verdad (python3, python o py). Una app de macOS abierta desde el Finder no hereda
// el PATH del shell, así que también se miran las rutas fijas de Homebrew y del framework.
func HayPython() bool { return buscarPython() != "" }

func buscarPython() string {
	var cands []string
	visto := map[string]bool{}
	add := func(p string) {
		if p == "" || visto[p] {
			return
		}
		visto[p] = true
		cands = append(cands, p)
	}
	for _, nombre := range []string{"python3", "python", "py"} {
		if x, err := exec.LookPath(nombre); err == nil {
			add(x)
		}
	}
	for _, p := range []string{
		"/opt/homebrew/bin/python3",
		"/usr/local/bin/python3",
		"/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
		"/usr/bin/python3",
	} {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			add(p)
		}
	}
	primero := ""
	for _, p := range cands {
		if primero == "" {
			primero = p
		}
		// El servidor se desarrolla con 3.12. 3.9 de /usr/bin no vale si hay otro más nuevo.
		if mayor, menor, ok := versionPython(p); ok && (mayor > 3 || (mayor == 3 && menor >= 12)) {
			return p
		}
	}
	return primero
}

func versionPython(exe string) (mayor, menor int, ok bool) {
	out, err := exec.Command(exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])").Output()
	if err != nil {
		return 0, 0, false
	}
	f := strings.Fields(string(out))
	if len(f) != 2 {
		return 0, 0, false
	}
	mayor, err1 := strconv.Atoi(f[0])
	menor, err2 := strconv.Atoi(f[1])
	if err1 != nil || err2 != nil {
		return 0, 0, false
	}
	return mayor, menor, true
}

// ---------------------------------------------------------------- estado del juego para la ficha

// DatosJuego: lo que run.py cuenta de sí mismo para la ficha pública (solo números y textos públicos).
type DatosJuego struct {
	Jugadores    int      `json:"jugadores"`               // conectados ahora (también el owner si juega en local)
	Duelos       int      `json:"duelos"`                  // duelos en curso; -1 = no se sabe
	Perfil       string   `json:"perfil,omitempty"`        // "vanilla" | "enhanced" (YO2_PERFIL con que arrancó)
	VersionJuego string   `json:"version_juego,omitempty"` // "080912"
	Salas        []string `json:"salas,omitempty"`         // p. ej. ["Mundial","ES","EN"] con salas por idioma
	Cuentas      string   `json:"cuentas,omitempty"`       // "libre" | "cerradas"
}

// FuenteEstado: de dónde saca el anfitrión los jugadores y duelos de ahora.
type FuenteEstado interface {
	Leer(ctx context.Context) (DatosJuego, error)
}

// EstadoPanel: GET http://127.0.0.1:<puerto>/publico, la ruta de solo lectura y SIN token que run.py da con lo poco que
// es público (jugadores, duelos, perfil...). Hay dos sitios que la sirven: la API de gestión de yo2srv
// (yo2srv/admin/gestion.py, 8791 + offset: va en todos los paquetes) y el panel privado del owner (8790 + offset). Se
// prueba primero la de gestión (Antes) y luego el panel (URL): así sirve también con un run.py anterior a la API de gestión.
type EstadoPanel struct {
	URL    string   // el panel (8790 + offset): el último recurso
	Antes  []string // lo que se prueba antes de URL (la API de gestión)
	Client *http.Client
}

func puertoEntorno(nombre string, defecto int) int {
	if v, err := strconv.Atoi(os.Getenv(nombre)); err == nil && v > 0 {
		return v
	}
	return defecto
}

// NuevoEstadoPanel: la instancia de run.py con ese YO2_PORT_OFFSET (YO2_GESTION_PORT y YO2_ADMIN_PANEL_PORT cambian los puertos).
func NuevoEstadoPanel(offset int) *EstadoPanel {
	return &EstadoPanel{
		URL:   "http://127.0.0.1:" + strconv.Itoa(puertoEntorno("YO2_ADMIN_PANEL_PORT", 8790+offset)) + "/publico",
		Antes: []string{"http://127.0.0.1:" + strconv.Itoa(puertoEntorno("YO2_GESTION_PORT", 8791+offset)) + "/publico"},
	}
}

func (e *EstadoPanel) Leer(ctx context.Context) (DatosJuego, error) {
	var d DatosJuego
	var err error
	for _, u := range append(append([]string{}, e.Antes...), e.URL) {
		if u == "" {
			continue
		}
		if d, err = e.leerDe(ctx, u); err == nil {
			return d, nil
		}
	}
	if err == nil {
		err = errors.New("sin dirección del estado de run.py")
	}
	return d, err
}

func (e *EstadoPanel) leerDe(ctx context.Context, url string) (DatosJuego, error) {
	var d DatosJuego
	ctx, c := context.WithTimeout(ctx, 2*time.Second)
	defer c()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return d, err
	}
	cl := e.Client
	if cl == nil {
		cl = &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	}
	r, err := cl.Do(req)
	if err != nil {
		return d, err
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return d, fmt.Errorf("run.py contesta %d en %s (¿sin la ruta /publico todavía?)", r.StatusCode, url)
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 8<<10)).Decode(&d); err != nil {
		return d, fmt.Errorf("estado de run.py mal formado: %w", err)
	}
	if d.Jugadores < 0 || d.Jugadores > MaxJugadores || d.Duelos < -1 || d.Duelos > MaxJugadores {
		return d, errors.New("estado de run.py con números fuera de rango")
	}
	return d, nil
}

// EstadoFijo: para pruebas (o para quien no tenga panel): siempre lo mismo.
type EstadoFijo struct {
	D   DatosJuego
	Err error
}

func (e EstadoFijo) Leer(context.Context) (DatosJuego, error) { return e.D, e.Err }
