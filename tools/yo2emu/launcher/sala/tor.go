package sala

// Vía Tor (respaldo): tor.exe del Tor Expert Bundle oficial, INCRUSTADO en el binario y extraído a %LOCALAPPDATA%.
//   - Anfitrión: servicio .onion v3 con SU clave ed25519 (la de identidad.key; la dirección .onion es su clave pública,
//     que va en el código de sala). Se escribe hs_ed25519_secret_key a mano: sin puerto de control. HiddenServicePort 5730 -> 127.0.0.1:<puerto del conector>.
//   - Invitado: SocksPort local; marca <onion>:5730 por SOCKS5.
// __OwningControllerProcess: tor.exe se cierra solo si muere el conector/lanzador.

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/proxy"
)

type Tor struct {
	cmd   *exec.Cmd
	Socks string
	listo chan struct{}
	fallo chan error
	Onion string

	// Diagnóstico (lo rellena el lector de la salida de tor.exe)
	mu        sync.Mutex
	pct       int    // último «Bootstrapped N%»
	fase      string // su descripción («Loading relay descriptors»...)
	problema  string // último aviso de tor sobre la conexión (red bloqueada, reloj...)
	subidas   int    // anfitrión: descriptores del .onion subidos con éxito a los HSDir
	publicado chan struct{}
}

var reBootstrap = regexp.MustCompile(`Bootstrapped (\d+)% \(([^)]*)\): (.*)`)

// Avisos de tor que NO son problemas de conexión (salen siempre con nuestra configuración).
var ruidoTor = []string{"NON-ANONYMOUS", "non-anonymous", "Bad element", "is relative", "sensitive information"}

// Progreso: «69% (Loading relay descriptors)» + el último problema que haya contado tor, si lo hay.
func (t *Tor) Progreso() string {
	if t == nil {
		return "sin arrancar"
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	m := fmt.Sprintf("%d%% (%s)", t.pct, t.fase)
	if t.fase == "" {
		m = "arrancando"
	}
	if t.problema != "" {
		m += "; último aviso de tor: " + t.problema
	}
	return m
}

// Diagnostico: el progreso + qué significa. Ojo: quedarse en 45-75 % («Loading relay descriptors») NO es un bloqueo:
// es la primera descarga del directorio de la red (~20 MB; el 06-10 tardó 7 min en casa del owner). Bloqueo = no pasa
// del 5-15 % (no consigue hablar con ningún relay).
func (t *Tor) Diagnostico() string {
	if t == nil {
		return "Tor sin arrancar"
	}
	t.mu.Lock()
	pct := t.pct
	t.mu.Unlock()
	p := t.Progreso()
	switch {
	case pct == 0:
		return p + ". tor.exe no avanza: ¿lo ha bloqueado el antivirus?"
	case pct < 20:
		return p + ". No consigue hablar con ningún relay de Tor: esta red parece BLOQUEAR Tor (pasa en algunas redes " +
			"de empresa, universidad o wifi pública)"
	case pct < 80:
		return p + ". NO está bloqueado: está descargando el directorio de la red Tor (la primera vez tarda varios " +
			"minutos; las siguientes, segundos, porque queda guardado)"
	default:
		return p + ". Conectado a la red Tor pero aún montando circuitos"
	}
}

// DiagnosticoMsg: lo mismo que Diagnostico(), como mensaje con clave para la ventana. El avance («45% (Loading relay
// descriptors)») es texto de tor.exe y va tal cual en el parámetro «progreso»; el último aviso de tor queda en el log.
func (t *Tor) DiagnosticoMsg() Msg {
	if t == nil {
		return M("msg.tor.diag_sin_arrancar", t.Diagnostico())
	}
	t.mu.Lock()
	pct, fase := t.pct, t.fase
	t.mu.Unlock()
	progreso := "—"
	if fase != "" {
		progreso = fmt.Sprintf("%d%% (%s)", pct, fase)
	}
	es := t.Diagnostico()
	switch {
	case pct == 0:
		return M("msg.tor.diag_0", es, "progreso", progreso)
	case pct < 20:
		return M("msg.tor.diag_bloqueado", es, "progreso", progreso)
	case pct < 80:
		return M("msg.tor.diag_descargando", es, "progreso", progreso)
	}
	return M("msg.tor.diag_circuitos", es, "progreso", progreso)
}

// Publicado: se cierra cuando el descriptor del .onion se ha subido al menos a un HSDir (solo anfitrión).
func (t *Tor) Publicado() <-chan struct{} { return t.publicado }

// leerLinea interpreta una línea de tor.exe; devuelve si va al log (las [info] solo se usan para el diagnóstico).
func (t *Tor) leerLinea(l string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if m := reBootstrap.FindStringSubmatch(l); m != nil {
		t.pct, _ = strconv.Atoi(m[1])
		t.fase = m[3]
	}
	if strings.Contains(l, "[warn]") {
		ruido := false
		for _, r := range ruidoTor {
			if strings.Contains(l, r) {
				ruido = true
			}
		}
		if !ruido {
			if i := strings.Index(l, "[warn] "); i >= 0 {
				t.problema = l[i+7:]
				if len(t.problema) > 200 {
					t.problema = t.problema[:200] + "..."
				}
			}
		}
	}
	if strings.Contains(l, "[info]") {
		if strings.Contains(l, "HS descriptor stored successfully") && strings.Contains(l, "finished with status 200") {
			t.subidas++
			if t.subidas == 1 && t.publicado != nil {
				close(t.publicado)
				return true
			}
			return false
		}
		// Fallos al subir el descriptor: se quedan en el log
		return strings.Contains(l, "upload_hsdesc") && !strings.Contains(l, "status 200")
	}
	return true
}

func extraerTor(dir string) (string, error) {
	if len(torBin) == 0 {
		if p, err := exec.LookPath("tor"); err == nil {
			return p, nil
		}
		return "", errors.New("no hay tor (en macOS: brew install tor)")
	}
	p := filepath.Join(dir, "tor.exe")
	if st, err := os.Stat(p); err != nil || st.Size() != int64(len(torBin)) {
		if err := os.WriteFile(p, torBin, 0o700); err != nil {
			return "", err
		}
	}
	return p, nil
}

func puertoLibre() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

// arrancarTor: dir = carpeta de datos propia (anfitrión e invitado usan carpetas distintas).
func arrancarTor(binDir, dir string, args []string, logw io.Writer) (*Tor, error) {
	exe, err := extraerTor(binDir)
	if err != nil {
		return nil, fmt.Errorf("no puedo extraer tor.exe: %w", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	vacio := filepath.Join(dir, "torrc-vacio")
	os.WriteFile(vacio, nil, 0o600)
	base := []string{
		"-f", vacio, "--ignore-missing-torrc",
		"--DataDirectory", filepath.Join(dir, "datos"),
		"--__OwningControllerProcess", strconv.Itoa(os.Getpid()),
		// notice + [rend]info: así se ve cuándo el .onion queda publicado (las [info] se filtran en leerLinea)
		"--Log", "notice [rend]info stdout",
		"--ClientOnly", "1",
		"--GeoIPFile", "", "--GeoIPv6File", "", // no van en el Expert Bundle incrustado (quita dos avisos)
	}
	t := &Tor{listo: make(chan struct{}), fallo: make(chan error, 1), publicado: make(chan struct{})}
	t.cmd = exec.Command(exe, append(base, args...)...)
	OcultarVentana(t.cmd)
	out, err := t.cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	t.cmd.Stderr = logw
	if err := t.cmd.Start(); err != nil {
		return nil, fmt.Errorf("no puedo arrancar tor.exe (¿antivirus?): %w", err)
	}
	go func() {
		sc := bufio.NewScanner(out)
		hecho := false
		for sc.Scan() {
			l := sc.Text()
			if t.leerLinea(l) {
				fmt.Fprintln(logw, "[tor] "+l)
			}
			if !hecho && strings.Contains(l, "Bootstrapped 100%") {
				hecho = true
				close(t.listo)
			}
		}
		err := t.cmd.Wait()
		if !hecho {
			t.fallo <- fmt.Errorf("tor.exe terminó antes de conectar: %v", err)
		}
	}()
	return t, nil
}

func (t *Tor) EsperarListo(ctx context.Context) error {
	select {
	case <-t.listo:
		return nil
	case err := <-t.fallo:
		return err
	case <-ctx.Done():
		return fmt.Errorf("Tor no termina de conectar: %s", t.Diagnostico())
	}
}

func (t *Tor) Cerrar() {
	if t != nil && t.cmd != nil && t.cmd.Process != nil {
		t.cmd.Process.Kill()
	}
}

// ---------------------------------------------------------------- anfitrión

// ServicioOnion arranca tor con el .onion de la sala apuntando a un listener local que atiende con a.Servir.
func ServicioOnion(binDir, dir string, s *Sala, a *Anfitrion, logw io.Writer) (*Tor, net.Listener, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, nil, err
	}
	onion, sk := s.Onion(), s.OnionSecreto()
	hs := filepath.Join(dir, "hs1-"+onion[:16]) // hs1 = modo de un salto (Tor no deja reutilizar un dir anónimo)
	if err := os.MkdirAll(hs, 0o700); err != nil {
		ln.Close()
		return nil, nil, err
	}
	cab := make([]byte, 32)
	copy(cab, "== ed25519v1-secret: type0 ==")
	if err := os.WriteFile(filepath.Join(hs, "hs_ed25519_secret_key"), append(cab, sk...), 0o600); err != nil {
		ln.Close()
		return nil, nil, err
	}
	os.Remove(filepath.Join(hs, "hs_ed25519_public_key")) // que Tor la regenere a partir de la secreta
	os.Remove(filepath.Join(hs, "hostname"))
	t, err := arrancarTor(binDir, dir, []string{
		"--SocksPort", "0",
		// Servicio de UN salto: el anfitrión no necesita anonimato y así el camino es de 4 saltos en vez de 6
		// (menos latencia y menos circuitos que fallen). El invitado sigue siendo anónimo.
		"--HiddenServiceNonAnonymousMode", "1",
		"--HiddenServiceSingleHopMode", "1",
		"--HiddenServiceDir", hs,
		"--HiddenServicePort", "5730 " + ln.Addr().String(),
	}, logw)
	if err != nil {
		ln.Close()
		return nil, nil, err
	}
	t.Onion = onion + ".onion"
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go a.Servir(c, "Tor")
		}
	}()
	return t, ln, nil
}

// HostnameTor: lo que Tor escribió en hostname (para comprobar que la derivación de la clave coincide).
func HostnameTor(dir string, s *Sala) string {
	onion := s.Onion()
	b, _ := os.ReadFile(filepath.Join(dir, "hs1-"+onion[:16], "hostname"))
	return strings.TrimSpace(string(b))
}

// ---------------------------------------------------------------- invitado

func ClienteTor(binDir, dir string, logw io.Writer) (*Tor, error) {
	p, err := puertoLibre()
	if err != nil {
		return nil, err
	}
	// ExtendedErrors: tor dice POR QUÉ no llega al .onion (códigos 0xF0-0xF7, ver explicarSocks)
	t, err := arrancarTor(binDir, dir, []string{"--SocksPort", "127.0.0.1:" + strconv.Itoa(p) + " ExtendedErrors"}, logw)
	if err != nil {
		return nil, err
	}
	t.Socks = "127.0.0.1:" + strconv.Itoa(p)
	return t, nil
}

// DialOnion reintenta hasta ctx: el descriptor del .onion puede tardar en publicarse tras arrancar el anfitrión.
func (t *Tor) DialOnion(ctx context.Context, s *Sala, logf func(string, ...any)) (net.Conn, error) {
	if err := t.EsperarListo(ctx); err != nil {
		return nil, err
	}
	onion := s.Onion()
	d, err := proxy.SOCKS5("tcp", t.Socks, nil, &net.Dialer{Timeout: 10 * time.Second})
	if err != nil {
		return nil, err
	}
	cd := d.(proxy.ContextDialer)
	var last error
	for ctx.Err() == nil {
		c2, cancel := context.WithTimeout(ctx, 60*time.Second)
		c, err := cd.DialContext(c2, "tcp", onion+".onion:5730")
		cancel()
		if err == nil {
			return c, nil
		}
		last = explicarSocks(err)
		logf("[tor] dial onion: %v", last)
		select {
		case <-ctx.Done():
		case <-time.After(3 * time.Second):
		}
	}
	if last == nil {
		last = ctx.Err()
	}
	return nil, fmt.Errorf("Tor sin éxito: %w", last)
}

var reCodigoSocks = regexp.MustCompile(`unknown code: (\d+)`)

// explicarSocks traduce los códigos extendidos de tor (SocksPort ... ExtendedErrors) a algo que se entienda.
func explicarSocks(err error) error {
	m := reCodigoSocks.FindStringSubmatch(err.Error())
	if m == nil {
		if strings.Contains(err.Error(), "host unreachable") || strings.Contains(err.Error(), "TTL expired") {
			return fmt.Errorf("%w (tor no consigue llegar al .onion)", err)
		}
		return err
	}
	n, _ := strconv.Atoi(m[1])
	porque := map[int]string{
		0xF0: "el .onion de la sala no está publicado: OpenServer cerrado, recién abierto (espera 1-2 min) o su Tor aún no ha conectado",
		0xF1: "el descriptor del .onion no es válido",
		0xF2: "el anfitrión no responde por sus puntos de introducción (¿OpenServer cerrado hace poco?)",
		0xF3: "falló el punto de encuentro de Tor (reintentando)",
		0xF6: "dirección .onion inválida",
		0xF7: "se agotó el tiempo con el punto de introducción (la red Tor va lenta; reintentando)",
	}[n]
	if porque == "" {
		return err
	}
	return fmt.Errorf("%s [código 0x%X]", porque, n)
}
