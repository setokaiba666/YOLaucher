package sala

// API del lanzador nuevo «Yu-Gi-Oh! Online 2 Launcher» (ventana Wails; maqueta en launcher/maqueta/lanzador.html del
// árbol principal). Todo lo de red está aquí; la ventana solo llama a esto y pinta.
//
//	l := sala.NuevoLanzador(datos, logf, logw)       // datos = %LOCALAPPDATA%\YO2Online
//	defer l.Cerrar()
//	l.BuscarServidores(ctx, func(s sala.ServidorVisto) { ... })      // pestaña Jugar (lista.go)
//	con, err := l.Entrar(ctx, sala.Destino{Servidor: &s}, opc, func(e sala.EventoEntrar) { ... })
//	con, err := l.Entrar(ctx, sala.Destino{Codigo: "YO2-..."}, opc, ev)  // «Entrar con código» (privados)
//	err := l.CrearServidor(ctx, sala.OpcServidor{...})                 // pestaña Crear servidor
//	l.EstadoServidor(); l.PararServidor(); l.Estado()
//
// Reutiliza lo de JoinServer/OpenServer: Invitado (P2P + Tor, reconexión), Entrar/Servir (saludo v3, T1, T2),
// NodoP2P (DHT, relay, hole punching), ServicioOnion, Catalogo (autoactualización).

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hashicorp/yamux"
	"github.com/ipfs/go-cid"
	"github.com/libp2p/go-libp2p/core/peer"
)

type Lanzador struct {
	Datos           string // %LOCALAPPDATA%\YO2Online (identidad del invitado, caché de la lista, carpeta del anfitrión)
	Logf            func(string, ...any)
	LogW            io.Writer
	VersionLanzador int64 // va en la ficha del servidor propio
	// Paquete: lo que reparte el servidor propio de la versión firmada del paquete (paquete_red.go; nil = nada). Su
	// versión va en la ficha y se atiende por ProtoActualiza (lista) y por el puerto 3 del túnel (Join).
	Paquete *ServidorPaquete

	// Red: plantilla de los nodos P2P. Vacía = DHT pública de IPFS. Las pruebas ponen Arranque/SoloLoopback/MinPares.
	Red OpcP2P

	// Límites de la búsqueda (0 = por defecto): proveedores de la lista que se miran (300), fichas pedidas a la vez
	// (16), plazo por ficha (10 s) y de la búsqueda entera (90 s).
	MaxProveedores int
	MaxConsultas   int
	EsperaFicha    time.Duration
	EsperaBusqueda time.Duration

	puertoFijo atomic.Int32 // «Fixed port» de Ajustes → Red para el servidor propio (0 = aleatorio; puerto.go)

	mu       sync.Mutex
	nodo     *NodoP2P
	srv      *servidorLocal
	favOnce  sync.Once
	favs     map[string]bool
	buscando atomic.Int32
	vistos   atomic.Int32
}

func NuevoLanzador(datos string, logf func(string, ...any), logw io.Writer) *Lanzador {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	if logw == nil {
		logw = io.Discard
	}
	return &Lanzador{Datos: datos, Logf: logf, LogW: logw}
}

func (l *Lanzador) logf(f string, a ...any) { l.Logf(f, a...) }

// PonerPuertoFijo: el puerto fijo del servidor propio (0 = aleatorio). Vale para el PRÓXIMO arranque del servidor.
func (l *Lanzador) PonerPuertoFijo(p int) { l.puertoFijo.Store(int32(max(0, p))) }

// PuertoFijo: el de PonerPuertoFijo.
func (l *Lanzador) PuertoFijo() int { return int(l.puertoFijo.Load()) }

func (l *Lanzador) limites() (maxProv, maxCons int, espera time.Duration) {
	maxProv, maxCons, espera = l.MaxProveedores, l.MaxConsultas, l.EsperaFicha
	if maxProv <= 0 {
		maxProv = 300
	}
	if maxCons <= 0 {
		maxCons = 16
	}
	if espera <= 0 {
		espera = 10 * time.Second
	}
	return
}

func (l *Lanzador) esperaBusqueda() time.Duration {
	if l.EsperaBusqueda > 0 {
		return l.EsperaBusqueda
	}
	return 90 * time.Second
}

// nodoLista: el nodo P2P del lanzador (identidad aleatoria; se crea la primera vez y sirve para buscar y para entrar).
func (l *Lanzador) nodoLista() (*NodoP2P, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.nodo != nil {
		return l.nodo, nil
	}
	o := l.Red
	o.Clave, o.Anfitr, o.Logf, o.Diag = nil, false, l.Logf, nil
	o.Puerto, o.Relays = 0, nil // el puerto fijo y los relays fijos son del servidor propio, no del nodo de la lista
	n, err := NuevoNodo(o)
	if err != nil {
		return nil, fmt.Errorf("no arranca la red P2P: %w", err)
	}
	l.nodo = n
	return n, nil
}

func (l *Lanzador) idPropio() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.srv != nil && l.srv.publica != nil {
		return l.srv.sala.PeerID().String()
	}
	return ""
}

// Cerrar: para el servidor propio (si hay) y la red.
func (l *Lanzador) Cerrar() {
	l.PararServidor()
	l.mu.Lock()
	n := l.nodo
	l.nodo = nil
	l.mu.Unlock()
	if n != nil {
		n.Close()
	}
}

// EstadoLanzador: barra de estado y menú lateral de la maqueta.
type EstadoLanzador struct {
	RedP2P           string // "sin arrancar" | "conectando" | "conectada"
	Pares            int    // pares de la DHT en la tabla de rutas
	Buscando         bool
	ServidoresVistos int // fichas válidas en la última búsqueda (o la que está en marcha)
	Servidor         EstadoServidor
}

func (l *Lanzador) Estado() EstadoLanzador {
	e := EstadoLanzador{RedP2P: "sin arrancar", Buscando: l.buscando.Load() > 0, ServidoresVistos: int(l.vistos.Load())}
	l.mu.Lock()
	n := l.nodo
	l.mu.Unlock()
	if n != nil {
		e.Pares = n.DHT.RoutingTable().Size()
		e.RedP2P = "conectando"
		if e.Pares > 0 {
			e.RedP2P = "conectada"
		}
	}
	e.Servidor = l.EstadoServidor()
	return e
}

// ---------------------------------------------------------------- entrar (invitado)

// Paso: los de la capa «Entrando…» de la maqueta, en orden.
type Paso int

const (
	PasoBuscando Paso = iota // Buscando al anfitrión
	PasoTunel                // Abriendo túnel cifrado
	PasoVersion              // Comprobando versión del juego
	PasoJuego                // Abriendo Yu-Gi-Oh! Online 2
)

var textoPaso = [...]string{"Buscando al anfitrión", "Abriendo túnel cifrado", "Comprobando versión del juego",
	"Abriendo Yu-Gi-Oh! Online 2"}

func (p Paso) String() string { return textoPaso[p] }

const (
	PasoVa    = "va"    // empieza
	PasoYa    = "ya"    // hecho
	PasoFallo = "fallo" // ha fallado (Err dice por qué); no hay más eventos
)

type EventoEntrar struct {
	Paso    Paso
	Texto   string // el texto del paso (Paso.String())
	Estado  string // PasoVa | PasoYa | PasoFallo
	Detalle string // p. ej. «P2P directo (red local, QUIC/UDP), 3 ms»
	Via     Msg    // PasoTunel/PasoYa: el camino, con clave para la ventana (Detalle es lo mismo en texto)
	Ms      int    // PasoTunel/PasoYa: latencia medida (0 = no medida)
	Err     error
}

// Destino: un servidor de la lista (público, sin código) o un código de sala (privado).
type Destino struct {
	Servidor *ServidorVisto
	Codigo   string
	// Direccion: «Join by address» (direccion.go): host:puerto o multiaddr del anfitrión. Con Codigo, se entra con el
	// código; sin él, se pide la ficha en esa dirección (Public / Password, con Credencial). Solo directo, sin DHT.
	Direccion string
	// Credencial: la contraseña (o la clave guardada) de un servidor de la lista con contraseña. Con código no hace falta.
	Credencial *Credencial
}

type OpcEntrar struct {
	Modo        string        // "auto" (P2P y, si no hay conexión directa, Tor), "p2p" o "tor"; "" = auto
	EsperaP2P   time.Duration // 0 = 30 s
	SinMDNS     bool
	Escuchar    string // IP de los puertos locales del juego ("" = 127.0.0.1)
	OffsetLocal int    // se suma a los puertos locales (pruebas)
	SinTuneles  bool   // no abrir los puertos locales (solo comprobar)
	// ComprobarVersion: lo de JoinServer (autoactualización por el túnel, mazos...). nil = nada; siempre se comprueba
	// además que la versión del juego de la ficha es la nuestra y que el servidor del juego contesta.
	ComprobarVersion func(ctx context.Context, c *Conexion) error
	// AbrirJuego: lanzar yo2.exe con el gancho (lo de JoinServer). nil = no se abre (pruebas).
	AbrirJuego func(ctx context.Context, c *Conexion) error
	Aviso      func(string) // avisos sueltos (reconexiones, progreso de Tor...); nil = solo al log
	AvisoK     func(Msg)    // la ventana: los avisos que tienen clave van aquí en vez de a Aviso (nil = a Aviso)
}

// Conexion: una partida en marcha (el túnel con el anfitrión y los puertos locales del juego).
type Conexion struct {
	Via      string        // «P2P directo (...)» o «Tor (.onion)»
	ViaMsg   Msg           // lo mismo, con clave para la ventana
	Huella   string        // del anfitrión
	Latencia time.Duration // ida y vuelta por el túnel (media de 5)
	Publica  bool          // entró por la lista (sin código)

	inv *Invitado
	lns []net.Listener
}

// Invitado: el de siempre (para la autoactualización de JoinServer: autoactualizar(inv, ...)).
func (c *Conexion) Invitado() *Invitado { return c.inv }

// Sesion: la sesión del túnel (reconecta si se cayó).
func (c *Conexion) Sesion(ctx context.Context) (*yamux.Session, error) { return c.inv.Sesion(ctx) }

// Dial abre una conexión al puerto LÓGICO p del servidor del juego (5730...).
func (c *Conexion) Dial(p int) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	ses, err := c.inv.Sesion(ctx)
	if err != nil {
		return nil, err
	}
	return AbrirPuerto(ses, p)
}

// Cerrar: cierra los puertos locales y el túnel.
func (c *Conexion) Cerrar() {
	for _, l := range c.lns {
		l.Close()
	}
	c.inv.Cerrar()
}

// AbrirTuneles: escucha en <ip>:<puerto+offset> cada puerto del juego y lo reenvía por el túnel (como JoinServer).
func (c *Conexion) AbrirTuneles(ip string, offset int, logf func(string, ...any)) error {
	for _, p := range Puertos() {
		ln, err := net.Listen("tcp", net.JoinHostPort(ip, strconv.Itoa(p+offset)))
		if err != nil {
			for _, l := range c.lns {
				l.Close()
			}
			c.lns = nil
			return fmt.Errorf("el puerto local %d está ocupado (¿otro lanzador abierto?): %w: %w", p+offset, ErrPuertoLocal, err)
		}
		c.lns = append(c.lns, ln)
		go func() {
			for {
				lc, err := ln.Accept()
				if err != nil {
					return
				}
				go func() {
					defer lc.Close()
					r, err := c.Dial(p)
					if err != nil {
						logf("dial %d: %v", p, err)
						return
					}
					defer r.Close()
					Unir(lc, r)
				}()
			}
		}()
	}
	return nil
}

var (
	ErrLleno          = errors.New("el servidor está lleno")
	ErrVersionDistint = errors.New("el servidor es de otra versión del juego")
	// ErrPuertoLocal: un puerto local del juego ya está en uso (otro lanzador, o el servidor propio).
	ErrPuertoLocal = errors.New("el puerto local del juego está ocupado")
	// ErrJuegoNoContesta: el anfitrión está conectado pero su servidor del juego (run.py) no responde.
	ErrJuegoNoContesta = errors.New("el servidor del juego del anfitrión no contesta")
)

// Entrar conecta con un servidor (de la lista o por código) contando los pasos por ev (se llama en orden, desde la
// goroutine de Entrar). Si sale bien, la Conexion queda abierta hasta Cerrar. Cancelar ctx = botón «Cancelar».
func (l *Lanzador) Entrar(ctx context.Context, d Destino, o OpcEntrar, ev func(EventoEntrar)) (*Conexion, error) {
	if ev == nil {
		ev = func(EventoEntrar) {}
	}
	paso := PasoBuscando
	emitir := func(p Paso, estado, detalle string, err error) {
		ev(EventoEntrar{Paso: p, Texto: p.String(), Estado: estado, Detalle: detalle, Err: err})
	}
	emitirTunel := func(detalle string, via Msg, ms int) {
		ev(EventoEntrar{Paso: PasoTunel, Texto: PasoTunel.String(), Estado: PasoYa, Detalle: detalle, Via: via, Ms: ms})
	}
	fallar := func(err error) (*Conexion, error) {
		emitir(paso, PasoFallo, err.Error(), err)
		return nil, err
	}
	emitir(PasoBuscando, PasoVa, "", nil)

	var s *Sala
	var pista *peer.AddrInfo
	var dir *Direccion
	if d.Direccion != "" {
		var err error
		if dir, err = ParseDireccion(d.Direccion); err != nil {
			return fallar(err)
		}
		if d.Codigo == "" { // Public / Password: la ficha de quien contesta en esa dirección (como si viniera de la lista)
			sv, err := l.ServidorEnDireccion(ctx, dir)
			if err != nil {
				return fallar(err)
			}
			d.Servidor = sv
		}
	}
	switch {
	case d.Servidor != nil:
		sv := d.Servidor
		if sv.firmada == nil {
			return fallar(errors.New("ese servidor no viene de la lista"))
		}
		if sv.Lleno() && sv.Estado == EstadoVivo {
			return fallar(ErrLleno)
		}
		var err error
		if s, err = sv.firmada.Sala(); err != nil {
			return fallar(err)
		}
		if d.Credencial != nil {
			s = s.ConCredencial(d.Credencial) // también si la ficha ya no dice «password»: el anfitrión contesta igual
		} else if sv.Ficha.Contrasena {
			return fallar(&ErrRechazado{Motivo: rechazoPideClave}) // ni se intenta: la ventana pide la contraseña
		}
		pista = &peer.AddrInfo{ID: sv.firmada.ID, Addrs: sv.addrs}
	case d.Codigo != "":
		var err error
		if s, err = Parse(d.Codigo); err != nil {
			return fallar(err)
		}
	default:
		return fallar(errors.New("falta el servidor o el código"))
	}
	if dir != nil {
		if dir.Info.ID != "" && dir.Info.ID != s.PeerID() {
			return fallar(ErrDirOtro)
		}
		pista = nil
	}
	clave, _, err := CargarClaveInvitado(l.Datos)
	if err != nil {
		return fallar(fmt.Errorf("identidad de este equipo: %w", err))
	}
	modo := o.Modo
	if modo == "" {
		modo = "auto"
	}
	if dir != nil {
		modo = "p2p" // por dirección: solo directo (con código, «Join with code» sigue teniendo Tor de respaldo)
	}
	espera := o.EsperaP2P
	if espera <= 0 {
		espera = 30 * time.Second
	}
	aviso := o.Aviso
	if aviso == nil {
		aviso = func(m string) { l.logf("%s", m) }
	}
	inv := &Invitado{Sala: s, Clave: clave, BinDir: l.Datos, Dir: filepath.Join(l.Datos, "tor-invitado"), Modo: modo,
		EsperaP2P: espera, SinMDNS: o.SinMDNS, Logf: l.Logf, LogW: l.LogW, Aviso: aviso, AvisoK: o.AvisoK, Pista: pista, Direccion: dir}
	if modo != "tor" {
		if inv.Nodo, err = l.nodoLista(); err != nil {
			return fallar(err)
		}
	}
	var encontrado sync.Once
	pasarATunel := func() {
		encontrado.Do(func() {
			emitir(PasoBuscando, PasoYa, "", nil)
			paso = PasoTunel
			emitir(PasoTunel, PasoVa, "", nil)
		})
	}
	// Progreso llega desde las goroutines de P2P/Tor: se pasa a esta por un canal para que ev vaya en orden.
	hallado := make(chan struct{}, 2)
	inv.Progreso = func(string) {
		select {
		case hallado <- struct{}{}:
		default:
		}
	}
	porDir := ""
	if dir != nil {
		porDir = ", por dirección " + dir.Texto
	}
	l.logf("[entrar] servidor %s (%s%s)", s.Huella(), map[bool]string{true: "público, de la lista", false: "privado, con código"}[s.publica], porDir)
	fin := make(chan error, 1)
	go func() { fin <- inv.Conectar(ctx) }()
esperar:
	for {
		select {
		case <-hallado:
			pasarATunel()
		case err = <-fin:
			break esperar
		}
	}
	if err != nil {
		inv.Cerrar()
		return fallar(err)
	}
	pasarATunel()
	c := &Conexion{Via: inv.Via, ViaMsg: inv.ViaMsg, Huella: s.Huella(), Publica: s.publica, inv: inv}
	det, ms := inv.Via, 0
	ses, errSes := inv.Sesion(ctx)
	var rz *ErrRechazado
	if errors.As(errSes, &rz) {
		c.Cerrar()
		return fallar(errSes)
	}
	if errSes == nil {
		if _, med, err := MedirEco(ses, 5); err == nil {
			c.Latencia = med
			ms = int(max(1, med.Milliseconds()))
			det += fmt.Sprintf(", %d ms", ms)
		} else if r := inv.esperarRechazo(); r != nil {
			// el anfitrión dijo que no DESPUÉS del saludo (lleno, sin sitio): mensaje concreto y se acaba aquí
			c.Cerrar()
			return fallar(r)
		}
	}
	emitirTunel(det, inv.ViaMsg, ms)

	paso = PasoVersion
	emitir(PasoVersion, PasoVa, "", nil)
	if d.Servidor != nil && !d.Servidor.Compatible {
		c.Cerrar()
		return fallar(fmt.Errorf("%w (Ver.%s; este cliente es Ver.%s)", ErrVersionDistint, d.Servidor.Ficha.VersionJuego, VersionJuegoCliente))
	}
	if o.ComprobarVersion != nil {
		if err := o.ComprobarVersion(ctx, c); err != nil {
			c.Cerrar()
			return fallar(err)
		}
	}
	if err := comprobarJuego(c); err != nil {
		if r := inv.esperarRechazo(); r != nil {
			err = r
		}
		c.Cerrar()
		return fallar(err)
	}
	emitir(PasoVersion, PasoYa, "", nil)

	paso = PasoJuego
	emitir(PasoJuego, PasoVa, "", nil)
	if !o.SinTuneles {
		ip := o.Escuchar
		if ip == "" {
			ip = "127.0.0.1"
		}
		if err := c.AbrirTuneles(ip, o.OffsetLocal, l.Logf); err != nil {
			c.Cerrar()
			return fallar(err)
		}
	}
	if o.AbrirJuego != nil {
		if err := o.AbrirJuego(ctx, c); err != nil {
			c.Cerrar()
			return fallar(err)
		}
	}
	emitir(PasoJuego, PasoYa, "", nil)
	return c, nil
}

// comprobarJuego: que el servidor del juego (run.py) contesta detrás del anfitrión (lo mismo que «Servidor accesible»
// de JoinServer: la puerta 5730 o manda algo o se queda esperando; si cierra, run.py no está).
func comprobarJuego(c *Conexion) error {
	cn, err := c.Dial(5730)
	if err != nil {
		return err
	}
	defer cn.Close()
	cn.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, err = cn.Read(make([]byte, 1))
	var ne net.Error
	if err == nil || (errors.As(err, &ne) && ne.Timeout()) {
		return nil
	}
	return fmt.Errorf("el anfitrión está conectado pero su servidor del juego no contesta (%v): ¿tiene run.py arrancado?: %w", err, ErrJuegoNoContesta)
}

// ---------------------------------------------------------------- crear servidor (anfitrión)

type OpcServidor struct {
	Nombre      string
	Descripcion string
	Max         int    // jugadores; también el máximo de invitados con túnel a la vez (0 = 30)
	Publica     bool   // sale en la lista pública (si no, solo con el código privado, como OpenServer)
	Reglas      string // ReglasVanilla | ReglasMejorado (YO2_PERFIL vanilla/enhanced si este lanzador arranca run.py)
	Idioma      string // "es", "en", "ja"... o "multi"
	Cuentas     string // CuentasLibre | CuentasCerrada (run.py aún no lo aplica: NOTES-red.md §8.5)
	Salas       []string
	Offset      int    // YO2_PORT_OFFSET de run.py
	Modo        string // "auto" (P2P + Tor), "p2p" o "tor"; "" = auto
	SinMDNS     bool
	RunPy       string       // run.py que se arranca si no escucha ("" = no se arranca: lo hace quien llama)
	Python      string       // python.exe con que se arranca run.py ("" = PythonExe(): el del sistema). P5: el del paquete
	Estado      FuenteEstado // de dónde salen jugadores y duelos (nil = NuevoEstadoPanel(Offset))
	Catalogo    *Catalogo    // autoactualización de los invitados (nil = nada)
	Aviso       func(string) // avisos para la ventana (nil = solo al log)
	// Env: variables de entorno de run.py además de YO2_PERFIL/YO2_PORT_OFFSET (YO2_LOGIN_ESTRICTO, YO2_STORAGE...).
	// Solo se usan si este lanzador arranca run.py; si ya había uno escuchando, ese manda (RunPyPropio lo dice).
	Env []string
	// SalidaRunPy: dónde va lo que imprime run.py (nil = LogW). run.py tiene su propio log: así el del lanzador no se llena.
	SalidaRunPy io.Writer
	// Clave: contraseña para entrar desde la lista (solo con Publica; contrasena.go). nil = sin contraseña. El código
	// privado entra sin ella.
	Clave *ClaveServidor
	// DirAnfitrion: carpeta de la identidad del anfitrión (identidad.key, sala.txt y tor\): de ella salen el peer ID, el
	// código de sala y el .onion. "" = <Datos>\anfitrion (la de siempre, la misma que OpenServer). «My servers» (12-10):
	// cada servidor guardado tiene la suya, así cada uno tiene su código y su fila en la lista.
	DirAnfitrion string
	// DirDatos: carpeta del estado y los logs de run.py de ESTE servidor (YO2_STATE = <DirDatos>\state.json, YO2_LOGDIR =
	// <DirDatos>\logs). "" = lo de siempre (desarrollo: el state.json del repo; paquete: <datos>\servidor\<puerto>).
	// La pone quien llama en Env (desarrollo) o Paquete.Ajustar (paquete).
	DirDatos string
}

// EstadoServidor: el panel «Tu servidor» de la maqueta.
type EstadoServidor struct {
	EnMarcha   bool
	Desde      time.Time
	Huella     string
	PeerID     string // identidad del servidor en la red (la que usan favoritos y ocultos de quien lo ve)
	Publica    bool
	ConClave   bool   // público con contraseña
	JuegoOK    bool   // run.py escucha
	Juego      string // texto
	P2POK      bool   // anunciado en la DHT
	P2P        string // texto («anunciado», «buscando la DHT»...) + lo que se sabe de la red
	ListadoOK  bool   // la última ronda de anuncios en la lista pública salió bien
	Listado    string
	UltimoAnun time.Time // último anuncio correcto (lista pública incluida)
	TorOK      bool
	Tor        string
	Invitados  int // invitados con túnel ahora
	Jugadores  int // los de la ficha
	Duelos     int // -1 = no se sabe
	Max        int
	FuenteJug  string // "run.py" | "túneles" (si run.py no da su estado)
	// RunPyPropio: este lanzador arrancó run.py (y por tanto puede pararlo con orden). false = ya estaba en marcha.
	RunPyPropio bool

	// Lo mismo que Juego, P2P, Listado y Tor, con clave para la ventana (los textos siguen siendo los de la consola).
	JuegoM, P2PM, ListadoM, TorM Msg

	// Acceso: puerto, si se llega desde Internet y por qué dirección (puerto.go; nil = sin P2P).
	Acceso *AccesoRed
}

type servidorLocal struct {
	o       OpcServidor
	sala    *Sala
	publica *Sala
	a       *Anfitrion
	nodo    *NodoP2P
	tor     *Tor
	lnTor   net.Listener
	py      *exec.Cmd
	pyHecho chan struct{} // se cierra cuando run.py (el nuestro) termina
	cancel  context.CancelFunc
	desde   time.Time

	paquete         *ServidorPaquete
	versionLanzador int64

	mu       sync.Mutex
	juego    DatosJuego
	juegoErr error
	juegoOK  bool
	p2pOK    bool
	p2pM     Msg
	listaErr error
	ultimoAn time.Time
	torOK    bool
	torM     Msg
}

// CrearServidor arranca el servidor propio: identidad y código de %LOCALAPPDATA%\YO2Online\anfitrion (los mismos que
// OpenServer: no abrir los dos a la vez), run.py si hace falta, P2P (+ lista pública si Publica) y Tor. Vuelve
// enseguida; el estado se ve con EstadoServidor.
func (l *Lanzador) CrearServidor(ctx context.Context, o OpcServidor) error {
	l.mu.Lock()
	if l.srv != nil {
		l.mu.Unlock()
		return errors.New("el servidor ya está en marcha")
	}
	l.mu.Unlock()
	if o.Max <= 0 {
		o.Max = 30
	}
	o.Max = min(o.Max, MaxJugadores)
	if o.Modo == "" {
		o.Modo = "auto"
	}
	aviso := o.Aviso
	if aviso == nil {
		aviso = func(m string) { l.logf("%s", m) }
	}
	dir := o.DirAnfitrion
	if dir == "" {
		dir = filepath.Join(l.Datos, "anfitrion")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	s, _, err := CargarAnfitrion(dir, false, false)
	if err != nil {
		return fmt.Errorf("identidad del anfitrión: %w", err)
	}
	ctx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	sv := &servidorLocal{o: o, sala: s, cancel: cancel, desde: time.Now(), paquete: l.Paquete, versionLanzador: l.VersionLanzador, p2pM: M("msg.p2p.arrancando", "arrancando"), torM: M("msg.tor.arrancando", "arrancando")}
	if o.Publica {
		sv.publica = s.Publica()
	}
	if o.Estado == nil {
		o.Estado = NuevoEstadoPanel(o.Offset)
		sv.o.Estado = o.Estado
	}
	modoTxt := map[bool]string{true: "PÚBLICO", false: "privado (solo con código)"}[o.Publica]
	if o.Publica && o.Clave.Valida() {
		modoTxt = "PÚBLICO con contraseña"
	}
	l.logf("[servidor] anfitrión %s (peer %s), %s", s.Huella(), s.PeerID(), modoTxt)

	// run.py
	gate := net.JoinHostPort("127.0.0.1", strconv.Itoa(5730+o.Offset))
	if !Escucha(gate) && o.RunPy != "" {
		aviso("Arrancando run.py: " + o.RunPy)
		py := exec.Command(pythonDe(o), o.RunPy, "0") // 0 = sin hora de apagado
		py.Dir = filepath.Dir(o.RunPy)
		salida := o.SalidaRunPy
		if salida == nil {
			salida = l.LogW
		}
		py.Stdout, py.Stderr = salida, salida
		OcultarVentana(py) // el lanzador no tiene consola: sin esto Windows abriría una ventana negra para python
		perfil := "vanilla"
		if o.Reglas == ReglasMejorado {
			perfil = "enhanced"
		}
		py.Env = append(os.Environ(), "YO2_PERFIL="+perfil)
		if o.Offset != 0 {
			py.Env = append(py.Env, "YO2_PORT_OFFSET="+strconv.Itoa(o.Offset))
		}
		py.Env = append(py.Env, o.Env...)
		if err := py.Start(); err != nil {
			aviso("No pude arrancar run.py: " + err.Error())
		} else {
			sv.py = py
			sv.pyHecho = make(chan struct{})
			go func() { py.Wait(); close(sv.pyHecho) }()
		}
	}

	sv.a = &Anfitrion{Sala: s, Publica: sv.publica, MaxInvitados: o.Max, Offset: o.Offset, Logf: l.Logf, LogW: l.LogW,
		Aviso: aviso, Catalogo: o.Catalogo, Paquete: l.Paquete}
	if sv.publica != nil && o.Clave.Valida() {
		sv.a.Clave = o.Clave
	}
	go sv.vigilarJuego(ctx, gate)

	if o.Modo != "tor" {
		ro := l.Red
		ro.Clave, ro.Anfitr, ro.Logf, ro.Diag = s.ClaveP2P(), true, l.Logf, func(m string) { l.logf("%s", m) }
		ro.Puerto = l.PuertoFijo()
		n, err := NuevoNodo(ro)
		if err != nil {
			sv.ponP2P(false, M("msg.p2p.no_disponible", "no disponible: "+err.Error(), "error", err.Error()))
		} else {
			sv.nodo = n
			claves := []cid.Cid{s.Encuentro()}
			if sv.publica != nil {
				claves = append(claves, sv.publica.Encuentro(), CIDLista())
				fs := &ServidorFichas{Sala: sv.publica, Ficha: sv.ficha, Logf: l.Logf}
				n.H.SetStreamHandler(ProtoFicha, fs.Atender)
			}
			if l.Paquete != nil { // el paquete firmado es público: se reparte también sin código (paquete_red.go)
				n.H.SetStreamHandler(ProtoActualiza, l.Paquete.AtenderLibp2p)
			}
			sv.ponP2P(false, M("msg.p2p.buscando_dht", "buscando la DHT"))
			nombres := []string{s.NombreMDNS()}
			if sv.publica != nil {
				nombres = append(nombres, sv.publica.NombreMDNS())
			}
			n.Anunciar(ctx, sv.a, Anuncio{Claves: claves, SinMDNS: o.SinMDNS, NombresMDNS: nombres,
				Ronda: func(err error) {
					sv.mu.Lock()
					sv.listaErr = err
					if err == nil {
						sv.ultimoAn = time.Now()
					}
					sv.mu.Unlock()
					switch {
					case err == nil:
						sv.ponP2P(true, M("msg.p2p.anunciado", "anunciado"))
					case errors.Is(err, ErrSinDHT):
						sv.ponP2P(sv.p2pOK, M("msg.p2p.error_dht", "error al anunciar: "+err.Error()))
					default:
						sv.ponP2P(sv.p2pOK, M("msg.p2p.error_anunciar", "error al anunciar: "+err.Error(), "error", err.Error()))
					}
				}})
		}
	} else {
		sv.ponP2P(false, M("msg.p2p.desactivado", "desactivado"))
	}
	if o.Modo != "p2p" {
		t, ln, err := ServicioOnion(l.Datos, filepath.Join(dir, "tor"), s, sv.a, l.LogW)
		if err != nil {
			sv.ponTor(false, M("msg.tor.no_disponible", "no disponible: "+err.Error(), "error", err.Error()))
		} else {
			sv.tor, sv.lnTor = t, ln
			go sv.vigilarTor(ctx)
		}
	} else {
		sv.ponTor(false, M("msg.tor.desactivado", "desactivado"))
	}
	l.mu.Lock()
	l.srv = sv
	l.mu.Unlock()
	return nil
}

func (sv *servidorLocal) ponP2P(ok bool, m Msg) {
	sv.mu.Lock()
	sv.p2pOK, sv.p2pM = ok, m
	sv.mu.Unlock()
}

func (sv *servidorLocal) ponTor(ok bool, m Msg) {
	sv.mu.Lock()
	sv.torOK, sv.torM = ok, m
	sv.mu.Unlock()
}

// vigilarJuego: cada 10 s, ¿escucha run.py? y su estado (jugadores, duelos) para la ficha. La ficha se firma con lo
// guardado aquí: pedir fichas nunca llega a run.py (no se puede usar para cargarlo).
func (sv *servidorLocal) vigilarJuego(ctx context.Context, gate string) {
	for {
		ok := Escucha(gate)
		d, err := sv.o.Estado.Leer(ctx)
		sv.mu.Lock()
		sv.juegoOK, sv.juego, sv.juegoErr = ok, d, err
		sv.mu.Unlock()
		select {
		case <-ctx.Done():
			return
		case <-time.After(10 * time.Second):
		}
	}
}

func (sv *servidorLocal) vigilarTor(ctx context.Context) {
	t0 := time.Now()
	for {
		c2, c := context.WithTimeout(ctx, time.Minute)
		err := sv.tor.EsperarListo(c2)
		c()
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			return
		}
		if c2.Err() == nil {
			sv.ponTor(false, M("msg.tor.error", err.Error(), "error", err.Error()))
			return
		}
		sv.ponTor(false, M("msg.tor.conectando", "conectando: "+sv.tor.Diagnostico(), "diag", sv.tor.DiagnosticoMsg()))
	}
	sv.ponTor(false, M("msg.tor.publicando", fmt.Sprintf("conectado (%v); publicando el .onion", time.Since(t0).Round(time.Second)),
		"seg", int(time.Since(t0).Seconds())))
	select {
	case <-sv.tor.Publicado():
		sv.ponTor(true, M("msg.tor.publicado", ".onion publicado"))
	case <-ctx.Done():
	}
}

// ficha: la de ahora (la firma ServidorFichas en cada petición).
func (sv *servidorLocal) ficha() Ficha {
	sv.mu.Lock()
	d, err := sv.juego, sv.juegoErr
	sv.mu.Unlock()
	f := Ficha{Nombre: sv.o.Nombre, Descripcion: sv.o.Descripcion, Idioma: sv.o.Idioma, Reglas: sv.o.Reglas,
		Cuentas: sv.o.Cuentas, Max: sv.o.Max, Duelos: -1, VersionJuego: VersionJuegoCliente, Salas: sv.o.Salas}
	if err == nil {
		f.Jugadores, f.Duelos = d.Jugadores, d.Duelos
		switch d.Perfil { // lo que de verdad corre manda sobre lo que se eligió
		case "enhanced":
			f.Reglas = ReglasMejorado
		case "vanilla":
			f.Reglas = ReglasVanilla
		}
		if d.VersionJuego != "" {
			f.VersionJuego = d.VersionJuego
		}
		if len(d.Salas) > 0 {
			f.Salas = d.Salas
		}
		if d.Cuentas != "" {
			f.Cuentas = d.Cuentas
		}
	} else {
		f.Jugadores = sv.a.Invitados()
	}
	if len(f.Salas) == 0 {
		f.Salas = []string{"Mundial"}
	}
	f.VersionLanzador = sv.versionLanzador
	f.VersionPaquete = sv.paquete.Version()
	f.Contrasena = sv.a.Clave != nil
	return f
}

// EstadoServidor: el del servidor propio (EnMarcha=false si no hay).
func (l *Lanzador) EstadoServidor() EstadoServidor {
	l.mu.Lock()
	sv := l.srv
	l.mu.Unlock()
	if sv == nil {
		return EstadoServidor{Juego: "parado", P2P: "—", Listado: "—", Tor: "—", Duelos: -1,
			JuegoM: M("msg.juego.parado", "parado"), P2PM: M("msg.nada", "—"), ListadoM: M("msg.nada", "—"), TorM: M("msg.nada", "—")}
	}
	f := sv.ficha()
	sv.mu.Lock()
	defer sv.mu.Unlock()
	e := EstadoServidor{EnMarcha: true, Desde: sv.desde, Huella: sv.sala.Huella(), PeerID: sv.sala.PeerID().String(), Publica: sv.publica != nil,
		JuegoOK: sv.juegoOK, P2POK: sv.p2pOK, P2P: sv.p2pM.Es, P2PM: sv.p2pM, UltimoAnun: sv.ultimoAn, TorOK: sv.torOK, Tor: sv.torM.Es, TorM: sv.torM,
		Invitados: sv.a.Invitados(), Jugadores: f.Jugadores, Duelos: f.Duelos, Max: f.Max, FuenteJug: "run.py",
		RunPyPropio: sv.py != nil, ConClave: sv.a.Clave != nil}
	if sv.juegoErr != nil {
		e.FuenteJug = "túneles"
	}
	e.Juego, e.JuegoM = "escuchando", M("msg.juego.escuchando", "escuchando")
	if !sv.juegoOK {
		e.Juego = "run.py no escucha en el puerto " + strconv.Itoa(5730+sv.o.Offset)
		e.JuegoM = M("msg.juego.no_escucha", e.Juego, "puerto", 5730+sv.o.Offset)
	}
	if sv.nodo != nil {
		e.P2P += " (" + sv.nodo.Red() + ")"
		e.P2PM = e.P2PM.Con("red", sv.nodo.RedMsgs())
		a := sv.nodo.Acceso()
		e.Acceso = &a
	}
	switch {
	case sv.publica == nil:
		e.Listado, e.ListadoM = "no (servidor privado: solo con código)", M("msg.listado.privado", "no (servidor privado: solo con código)")
	case sv.listaErr == nil && !sv.ultimoAn.IsZero():
		e.ListadoOK, e.Listado, e.ListadoM = true, "sí", M("msg.listado.si", "sí")
	case sv.listaErr != nil:
		e.Listado = "error: " + sv.listaErr.Error()
		if errors.Is(sv.listaErr, ErrSinDHT) {
			e.ListadoM = M("msg.listado.error_dht", e.Listado)
		} else {
			e.ListadoM = M("msg.listado.error", e.Listado, "error", sv.listaErr.Error())
		}
	default:
		e.Listado, e.ListadoM = "anunciándose (el primer anuncio tarda ~1 min)", M("msg.listado.anunciando", "anunciándose (el primer anuncio tarda ~1 min)")
	}
	return e
}

// CodigoPrivado: el código de sala del servidor propio, para el botón «Copiar» (ES SECRETO: nunca al log).
func (l *Lanzador) CodigoPrivado() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.srv == nil {
		return ""
	}
	return l.srv.sala.Codigo()
}

// RunPyPropio: el run.py que arrancó CrearServidor (nil = no lo arrancó este lanzador: ya estaba en marcha, o no hay
// servidor) y un canal que se cierra cuando termina. Quien lo use para pararlo con orden (consola de run.py) solo
// puede tocar ESTE proceso: uno ajeno no es suyo.
func (l *Lanzador) RunPyPropio() (*os.Process, <-chan struct{}) {
	l.mu.Lock()
	sv := l.srv
	l.mu.Unlock()
	if sv == nil || sv.py == nil || sv.py.Process == nil {
		return nil, nil
	}
	return sv.py.Process, sv.pyHecho
}

// cerrarConTope ejecuta fn y sigue a los d aunque fn no vuelva. Close() de libp2p (DHT, mDNS) a veces no vuelve
// y la ventana se queda en «parando» para siempre.
func cerrarConTope(fn func(), d time.Duration) {
	hecho := make(chan struct{})
	go func() {
		defer close(hecho)
		fn()
	}()
	select {
	case <-hecho:
	case <-time.After(d):
	}
}

// PararServidor: deja de anunciarse y de atender invitados. run.py NO se cierra (como OpenServer): cortarlo de golpe
// puede perder datos; para pararlo con orden está la consola de run.py (RunPyPropio + el paquete de la ventana).
func (l *Lanzador) PararServidor() {
	l.mu.Lock()
	sv := l.srv
	l.srv = nil
	l.mu.Unlock()
	if sv == nil {
		return
	}
	sv.cancel()
	if sv.lnTor != nil {
		sv.lnTor.Close()
	}
	sv.tor.Cerrar()
	if sv.nodo != nil {
		cerrarConTope(sv.nodo.Close, 4*time.Second)
	}
	l.logf("[servidor] parado")
}
