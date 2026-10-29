// Package nucleo: la lógica de la ventana «Yu-Gi-Oh! Online 2 Launcher» sin nada de Wails (se prueba sin ventana).
// La ventana (paquete main de launcher\app) enlaza los métodos exportados de Nucleo con el HTML y reenvía los eventos.
//
// Encima de sala.Lanzador (red P2P/Tor, lista pública, túneles) y del paquete juego (abrir yo2.exe, autoactualizar):
//
//	Buscar / Servidores            pestaña Jugar: la lista (caché al momento, luego en vivo)
//	Entrar / EntrarConCodigo       pasos reales de sala.Entrar; autoactualización y juego incluidos
//	MisServidores / CrearMiServidor / EditarMiServidor / BorrarMiServidor / ArrancarMiServidor   «My servers»
//	                               (misservidores.go: varios servidores guardados, cada uno con su mundo)
//	PararServidor                  el que está en marcha (run.py + P2P + lista pública), con parada ordenada
//	Estado                         barra de estado, «Conectado a X», panel «Tu servidor»
package nucleo

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"yo2online/juego"
	"yo2online/sala"
)

// Config: lo que la ventana pone al crear el núcleo.
type Config struct {
	Datos   string // %LOCALAPPDATA%\YO2Online
	ExeDir  string // carpeta de este exe (YO2OnlineLauncher.exe, o el viejo YO2Launcher.exe)
	Exe     string // ruta de este exe ("" = la del proceso): es lo que se reparte y lo que se reemplaza al actualizar
	Version int64  // versión de este exe (la pone build.ps1)
	Offset  int    // YO2_PORT_OFFSET del servidor propio (0 salvo pruebas)

	Logf func(string, ...any)
	Log  io.Writer // salida de la red (libp2p, tor, frida)
	// Emitir manda un evento a la página (nil = nada).
	Emitir func(evento string, datos any)

	AbrirCarpeta func(dir string) error
	Portapapeles func(texto string) error
	Salir        func()                 // cierra la ventana de verdad
	WebView2     func() (string, error) // versión del runtime de WebView2
	Lanzador     *sala.Lanzador         // pruebas: otra red; nil = la de siempre
	// Solo pruebas: retocan las opciones antes de entrar / crear (puertos con desplazamiento, sin mDNS, sin abrir el juego).
	AjustarEntrada  func(*sala.OpcEntrar)
	AjustarServidor func(*sala.OpcServidor)
	Puertos         func() []int // pruebas: puertos a mirar en «Comprobar instalación»; nil = los del juego
	Gestion         *Gestion     // pruebas: otro cliente de la API de gestión (servidor falso); nil = el de siempre
	// Papelera: mandar una carpeta a la papelera (Delete de «My servers»). nil = la papelera de Windows. Pruebas: otra.
	Papelera func(dir string) error
	// RunPyAjeno: pruebas: un servidor NUEVO puede ponerse delante de un run.py que ya escuchaba (el de mentira). Fuera de
	// las pruebas solo el servidor de antes puede (uno nuevo mezclaría su identidad con el mundo de otro).
	RunPyAjeno bool
}

// Nucleo: el estado de la ventana.
type Nucleo struct {
	cfg Config
	l   *sala.Lanzador
	aj  *almacen
	cl  *almacenClaves // «Remember password» de servidores con contraseña (claves.go)
	jl  *juego.Lanzador

	mu        sync.Mutex
	filas     map[string]sala.ServidorVisto
	buscando  bool
	cancelBus context.CancelFunc
	genBus    int
	ultEmit   time.Time
	emitPend  bool

	entrando *entrada
	ultimo   destinoUI

	con        *sala.Conexion
	conNombre  string
	conDesde   time.Time
	partida    *juego.Partida
	juegoAbier bool
	juegoLocal bool

	srvID    string   // «My servers»: el servidor guardado que está en marcha (misservidores.go)
	gest     *Gestion // cliente de la API de gestión del run.py propio (gestion.go); se crea al usarlo
	parando  bool
	cerrando bool
	// cierre: la página es quien pregunta «¿cerrar?». Si no contesta (se ha colgado), un segundo intento de cerrar la deja cerrar.
	cierreEmitido time.Time
	cierreVisto   bool
	retomar       *Retomar
	avisoSrv      Msg
	cacheOS       struct {
		t    time.Time
		vivo bool
	}
	cacheJuego struct {
		t       time.Time
		carpeta string
	}
	paq *actPaquete // actualización del paquete entero (paquete_act.go)
	// red: nodos de arranque y relays en uso (red.go)
	red sala.RedEfectiva
}

type entrada struct {
	cancel context.CancelFunc
	nombre string
}

type destinoUI struct {
	ID     string // peer ID de un servidor de la lista
	Codigo string // código de sala (privado)
	Dir    string // «Join by address»: host:puerto del anfitrión (red.go); con Codigo, cred o nada
	Nombre string
	sv     *sala.ServidorVisto // la ficha con la que se entra (la lista puede cambiar mientras tanto)
	// servidor con contraseña: la credencial (escrita ahora o guardada), si se recuerda al entrar y si venía de lo guardado
	cred     *sala.Credencial
	recordar bool
	guardada bool
}

var errReiniciando = errors.New("reiniciando con la versión nueva")

// Nuevo crea el núcleo. Lee los ajustes y lo que dejó una autoactualización anterior (entorno), pero no hace nada de red
// hasta Listo().
func Nuevo(cfg Config) *Nucleo {
	if cfg.Logf == nil {
		cfg.Logf = func(string, ...any) {}
	}
	if cfg.Log == nil {
		cfg.Log = io.Discard
	}
	if cfg.Exe == "" {
		cfg.Exe = juego.RutaPropia()
	}
	n := &Nucleo{cfg: cfg, filas: map[string]sala.ServidorVisto{}}
	n.aj = abrirAlmacen(cfg.Datos)
	n.cl = abrirClaves(cfg.Datos)
	n.l = cfg.Lanzador
	if n.l == nil {
		n.l = sala.NuevoLanzador(cfg.Datos, cfg.Logf, cfg.Log)
	}
	n.l.VersionLanzador = cfg.Version
	n.iniciarPaquete() // paquete_act.go: lo que se reparte al hospedar y lo ya descargado
	// red.go: puerto fijo y nodos de arranque (firmados, del usuario, de fábrica)
	n.iniciarRed(cfg.Lanzador == nil)
	n.jl = &juego.Lanzador{Datos: cfg.Datos, Logf: cfg.Logf, Log: cfg.Log, Oculto: true}
	n.retomar = leerRetomarProceso()
	return n
}

func (n *Nucleo) logf(f string, a ...any) { n.cfg.Logf(f, a...) }

func (n *Nucleo) emit(ev string, d any) {
	if n.cfg.Emitir != nil {
		n.cfg.Emitir(ev, d)
	}
}

// aviso: un mensaje corto para el usuario (la página lo traduce y lo enseña como aviso emergente) y al log.
func (n *Nucleo) aviso(m Msg) {
	n.logf("[aviso] %s", m.Es)
	n.emit("aviso", m)
}

// raw: un texto que no se traduce (ruta, nombre de fichero, texto de otro programa) como mensaje.
func raw(s string) Msg { return sala.M("msg.raw", s, "texto", s) }

// ---------------------------------------------------------------- inicio

// VersionTexto: AAAAMMDDhhmmss -> «2026.10.11» ("dev" si se compiló a mano).
func VersionTexto(v int64) string {
	s := strconv.FormatInt(v, 10)
	if len(s) != 14 {
		return "dev"
	}
	return s[0:4] + "." + s[4:6] + "." + s[6:8]
}

// Inicio: lo que la página necesita al abrirse.
type Inicio struct {
	Version    string
	Reanudando bool // acabamos de reiniciarnos por una actualización y vamos a volver a entrar en el servidor de antes
	Ajustes    Ajustes
	Estado     Estado
}

// Inicio: se pide una vez al cargar la página.
func (n *Nucleo) Inicio() Inicio {
	n.mu.Lock()
	rean := n.retomar != nil
	n.mu.Unlock()
	return Inicio{Version: VersionTexto(n.cfg.Version), Reanudando: rean, Ajustes: n.aj.leer().paraPagina(), Estado: n.Estado()}
}

// Listo: la página ya escucha los eventos. Arranca la búsqueda y, si venimos de una actualización, vuelve a entrar donde
// estábamos (por peer ID o con el código, sin pedirlo otra vez).
func (n *Nucleo) Listo() {
	n.mu.Lock()
	r := n.retomar
	n.retomar = nil
	n.mu.Unlock()
	if r != nil {
		go n.reanudar(*r)
	}
	n.Buscar()
	go n.autoBuscar()
}

// BuscarCadaAuto: cada cuánto se vuelve a buscar en segundo plano (12-10: el owner no quiere tener que pulsar Refresh).
var BuscarCadaAuto = 45 * time.Second

// autoBuscar: búsqueda en segundo plano cada BuscarCadaAuto, SIN vaciar la lista (las filas se actualizan en su sitio y
// las nuevas aparecen solas) y sin el aviso «searching…». Si ya hay una búsqueda en marcha (p. ej. Refresh), se salta.
func (n *Nucleo) autoBuscar() {
	t := time.NewTicker(BuscarCadaAuto)
	defer t.Stop()
	for range t.C {
		n.mu.Lock()
		ocupado := n.buscando
		n.mu.Unlock()
		if !ocupado {
			n.buscar(false)
		}
	}
}

func (n *Nucleo) reanudar(r Retomar) {
	n.logf("[retomar] tras actualizar: volviendo a entrar (%s)", map[bool]string{true: "servidor público", false: "con código"}[r.Publico != ""])
	switch {
	case r.Dir != "": // «Join by address»: otra vez a la misma dirección (sin DHT), con el código o la clave derivada
		d := destinoUI{Dir: r.Dir, Nombre: r.Dir, Codigo: r.Codigo}
		if r.Clave != "" {
			d.cred, _ = sala.CredencialDeTexto(r.Clave)
		}
		if err := n.iniciarEntrada(d); err != nil {
			n.aviso(MsgDe(err))
		}
	case r.Publico != "":
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		sv, err := n.l.ConsultarServidor(ctx, r.Publico)
		if err != nil {
			n.logf("[retomar] el servidor no contesta: %v", err)
			n.aviso(sala.M("msg.reanudar.sin_respuesta", "El lanzador se ha actualizado, pero el servidor en el que estabas no contesta. Elígelo otra vez en la lista."))
			return
		}
		d := destinoUI{ID: sv.ID, Nombre: sv.Ficha.Nombre, sv: sv}
		if r.Clave != "" { // con contraseña: la clave derivada que traía el proceso anterior (nunca la contraseña)
			d.cred, _ = sala.CredencialDeTexto(r.Clave)
		} else if sv.Ficha.Contrasena {
			d.cred, d.guardada = n.cl.credencial(sv.ID), true
		}
		if err := n.iniciarEntrada(d); err != nil {
			n.aviso(MsgDe(err))
		}
	case r.Codigo != "":
		// Nombre vacío = «servidor privado» (la página lo escribe en el idioma de la interfaz)
		if err := n.iniciarEntrada(destinoUI{Codigo: r.Codigo}); err != nil {
			n.aviso(MsgDe(err))
		}
	}
}

// ---------------------------------------------------------------- ajustes

// Ajustes: los de ahora.
func (n *Nucleo) Ajustes() Ajustes { return n.aj.leer().paraPagina() }

// PonerActualizar: autoactualización del lanzador por el túnel, sí o no.
func (n *Nucleo) PonerActualizar(si bool) error {
	return errGen(n.aj.cambiar(func(a *Ajustes) { a.Actualizar = si }))
}

// PonerMostrarOcultos: enseñar también los servidores ocultos en la lista.
func (n *Nucleo) PonerMostrarOcultos(si bool) error {
	err := n.aj.cambiar(func(a *Ajustes) { a.MostrarOcultos = si })
	n.emitirLista(true)
	return errGen(err)
}

// PonerTema: aspecto de la ventana: "system" (el de Windows), "light" o "dark". Lo que llega de la página no se guarda sin mirar.
func (n *Nucleo) PonerTema(tema string) error {
	if !TemaValido(tema) {
		return errM("msg.err.tema_invalido", "tema no válido")
	}
	return errGen(n.aj.cambiar(func(a *Ajustes) { a.Tema = tema }))
}

// PonerVentana: Ajustes → Display → «Window size»: "" (Original), "1.5", "2" o "fit" (juego/ventana.go). Vale desde el próximo
// arranque del juego. Lo que llega de la página no se guarda sin mirar.
func (n *Nucleo) PonerVentana(v string) error {
	c, ok := juego.NormalizarVentana(v)
	if !ok {
		return errM("msg.err.ventana_invalida", "tamaño de ventana no válido")
	}
	return errGen(n.aj.cambiar(func(a *Ajustes) { a.Ventana = c }))
}

// RestaurarOcultos: vuelve a enseñar todos los servidores que se ocultaron.
func (n *Nucleo) RestaurarOcultos() error {
	err := n.aj.cambiar(func(a *Ajustes) { a.Ocultos = nil })
	n.emitirLista(true)
	return errGen(err)
}

// ResultadoCarpeta: qué pasó al elegir la carpeta del juego.
type ResultadoCarpeta struct {
	OK        bool
	Carpeta   string
	Version   string
	Msg       Msg  // aviso o error para enseñar (vacío si todo bien)
	Cancelado bool // el usuario cerró el selector sin elegir
	VersionOK bool
}

// msgSinVersion: por qué no se supo la versión de yo2.exe.
func msgSinVersion(err error) Msg {
	if errors.Is(err, juego.ErrSinVersion) {
		return sala.M("msg.carpeta.sin_version_desconocida", "Carpeta guardada, pero no pude saber la versión de yo2.exe: "+err.Error())
	}
	return sala.M("msg.carpeta.sin_version", "Carpeta guardada, pero no pude saber la versión de yo2.exe: "+err.Error(), "error", err.Error())
}

// ElegirCarpetaJuego: valida y guarda la carpeta del juego (la que contiene yo2.exe; vale también la ruta de yo2.exe).
func (n *Nucleo) ElegirCarpetaJuego(c string) ResultadoCarpeta {
	c = juego.NormalizarCarpeta(c)
	if !juego.CarpetaValida(c) {
		return ResultadoCarpeta{Msg: sala.M("msg.carpeta.sin_exe", "Ahí no está yo2.exe. Elige la carpeta del juego (la que tiene yo2.exe dentro).")}
	}
	if err := n.aj.cambiar(func(a *Ajustes) { a.CarpetaJuego = c }); err != nil {
		return ResultadoCarpeta{Msg: sala.M("msg.carpeta.no_guarda", "No pude guardar el ajuste: "+err.Error(), "error", err.Error())}
	}
	juego.GuardarCarpeta(n.cfg.Datos, c) // JoinServer la lee de aquí
	n.mu.Lock()
	n.cacheJuego.t = time.Time{}
	n.mu.Unlock()
	r := ResultadoCarpeta{OK: true, Carpeta: c}
	if v, err := juego.VersionJuego(c); err == nil {
		r.Version, r.VersionOK = v, v == juego.VersionEsperada
		if !r.VersionOK {
			r.Msg = sala.M("msg.carpeta.otra_version", fmt.Sprintf("Carpeta guardada, pero ese yo2.exe es Ver.%s y los servidores usan Ver.%s.", v, juego.VersionEsperada),
				"version", v, "esperada", juego.VersionEsperada)
		}
	} else {
		r.Msg = msgSinVersion(err)
	}
	return r
}

// DetectarCarpetaJuego: olvida la carpeta elegida a mano y vuelve a buscarla (junto al lanzador, guardada, instalación).
func (n *Nucleo) DetectarCarpetaJuego() ResultadoCarpeta {
	n.aj.cambiar(func(a *Ajustes) { a.CarpetaJuego = "" })
	n.mu.Lock()
	n.cacheJuego.t = time.Time{}
	n.mu.Unlock()
	c := n.carpetaJuego()
	if c == "" {
		return ResultadoCarpeta{Msg: sala.M("msg.carpeta.no_encuentro", "No encuentro el juego. Pulsa «Cambiar» y elige la carpeta que tiene yo2.exe.")}
	}
	r := ResultadoCarpeta{OK: true, Carpeta: c}
	if v, err := juego.VersionJuego(c); err == nil {
		r.Version, r.VersionOK = v, v == juego.VersionEsperada
	}
	return r
}

// carpetaJuego: la carpeta con yo2.exe (la de Ajustes si vale; si no, la que se detecta). "" = no está. Con caché de 5 s
// (la página pregunta cada segundo).
func (n *Nucleo) carpetaJuego() string {
	n.mu.Lock()
	if time.Since(n.cacheJuego.t) < 5*time.Second {
		c := n.cacheJuego.carpeta
		n.mu.Unlock()
		return c
	}
	n.mu.Unlock()
	c := juego.BuscarCarpeta(n.aj.leer().CarpetaJuego, n.cfg.ExeDir, n.cfg.Datos)
	n.mu.Lock()
	n.cacheJuego.t, n.cacheJuego.carpeta = time.Now(), c
	n.mu.Unlock()
	return c
}

// AbrirLogs: abre la carpeta con los logs (la de datos) en el Explorador.
func (n *Nucleo) AbrirLogs() error {
	if n.cfg.AbrirCarpeta == nil {
		return errM("msg.err.sin_carpeta", "no se puede abrir la carpeta desde aquí")
	}
	return errGen(n.cfg.AbrirCarpeta(n.cfg.Datos))
}

// ---------------------------------------------------------------- estado

// InfoConexion: «Conectado a X».
type InfoConexion struct {
	Nombre       string // vacío = servidor privado (entrado con código)
	Via          Msg
	PingMs       int
	Publica      bool
	JuegoAbierto bool
	DesdeS       int
}

// Estado: todo lo que pintan la barra de estado y los paneles; la página lo pide cada segundo.
type Estado struct {
	JuegoOK      bool
	CarpetaJuego string
	VersionJuego string
	Actualizar   bool
	Version      string
	RedP2P       string
	Pares        int
	Buscando     bool
	Vistos       int
	Entrando     bool
	EntrandoA    string // vacío = servidor privado
	Conexion     *InfoConexion
	JugandoLocal bool // el juego abierto desde «Jugar aquí» (en el servidor propio)
	Servidor     EstadoSrv
	Parando      bool
	OpenServer   bool // el OpenServer.exe de siempre está abierto (usa la misma identidad de anfitrión)
}

func (n *Nucleo) Estado() Estado {
	e := Estado{Version: VersionTexto(n.cfg.Version), Actualizar: n.aj.leer().Actualizar}
	if c := n.carpetaJuego(); c != "" {
		e.JuegoOK, e.CarpetaJuego = true, c
		e.VersionJuego, _ = juego.VersionJuego(c)
	}
	le := n.l.Estado()
	e.RedP2P, e.Pares, e.Buscando, e.Vistos = le.RedP2P, le.Pares, le.Buscando, le.ServidoresVistos
	e.Servidor = n.estadoServidor(le.Servidor)
	n.mu.Lock()
	defer n.mu.Unlock()
	if time.Since(n.cacheOS.t) > 3*time.Second {
		n.cacheOS.t, n.cacheOS.vivo = time.Now(), juego.ProcesoActivo("OpenServer.exe")
	}
	e.OpenServer = n.cacheOS.vivo
	if n.entrando != nil {
		e.Entrando, e.EntrandoA = true, n.entrando.nombre
	}
	if n.con != nil {
		e.Conexion = &InfoConexion{Nombre: n.conNombre, Via: n.con.ViaMsg, PingMs: int(n.con.Latencia.Milliseconds()),
			Publica: n.con.Publica, JuegoAbierto: n.juegoAbier && !n.juegoLocal, DesdeS: int(time.Since(n.conDesde).Seconds())}
	}
	e.JugandoLocal = n.juegoAbier && n.juegoLocal
	e.Parando = n.parando
	return e
}

// ---------------------------------------------------------------- lista de servidores

// Fila: un servidor de la lista, listo para pintar.
type Fila struct {
	ID          string
	IDCorto     string // 8 letras del peer ID (sin el prefijo común «12D3KooW»): distingue servidores con el mismo nombre
	Huella      string // XXXX-XXXX de la clave pública del anfitrión
	Nombre      string
	Descripcion string
	Reglas      string
	Cuentas     string
	Jugadores   int
	Max         int
	EnDuelo     int // jugadores en duelo ahora (-1 = el anfitrión no lo cuenta)
	PingMs      int // -1 = no medido
	PorRelay    bool
	Estado      string // cache | vivo | sin-respuesta | privado
	VistoHace   int    // segundos
	Version     string // del juego, según la ficha
	Compatible  bool
	Salas       []string
	Favorito    bool
	Propio      bool
	Lleno       bool
	Oculto      bool
	Contrasena  bool // pide contraseña para entrar (candado)
	ClaveGuard  bool // este equipo tiene su contraseña guardada («Remember password»)
}

// IDCorto: el peer ID sin el prefijo que todos comparten, recortado a 8 letras.
func IDCorto(id string) string {
	id = strings.TrimPrefix(id, "12D3KooW")
	if len(id) > 8 {
		id = id[:8]
	}
	return id
}

func (n *Nucleo) fila(sv sala.ServidorVisto, ocultos map[string]bool) Fila {
	return Fila{ID: sv.ID, IDCorto: IDCorto(sv.ID), Huella: sv.Huella, Nombre: sv.Ficha.Nombre, Descripcion: sv.Ficha.Descripcion,
		Reglas: sv.Ficha.Reglas, Cuentas: sv.Ficha.Cuentas, Jugadores: sv.Ficha.Jugadores, Max: sv.Ficha.Max,
		EnDuelo: sv.Ficha.Duelos, PingMs: sv.PingMs, PorRelay: sv.PorRelay, Estado: sv.Estado,
		VistoHace: int(time.Since(sv.Visto).Seconds()), Version: sv.Ficha.VersionJuego, Compatible: sv.Compatible,
		Salas: sv.Ficha.Salas, Favorito: sv.Favorito, Propio: sv.Propio, Lleno: sv.Lleno(), Oculto: ocultos[sv.ID],
		Contrasena: sv.Ficha.Contrasena, ClaveGuard: sv.Ficha.Contrasena && n.cl.tiene(sv.ID)}
}

// Lista: lo que se manda a la página en cada actualización de la lista.
type Lista struct {
	Filas    []Fila
	Buscando bool
	Vivos    int // servidores que han contestado en esta búsqueda
	EnCache  int // servidores de la última vez que aún se están comprobando («Checking…»)
}

// Servidores: la lista de ahora (sin ordenar: la página filtra y ordena).
func (n *Nucleo) Servidores() Lista {
	oc := map[string]bool{}
	for _, id := range n.aj.leer().Ocultos {
		oc[id] = true
	}
	propio := n.l.ServidorPublicoEnMarcha() // antes de n.mu: así no se anidan los dos candados
	var vivo sala.EstadoServidor
	if propio {
		vivo = n.l.EstadoServidor() // 12-10: la fila propia con los números de AHORA (la ficha de la lista es de la búsqueda)
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	crudas := make([]sala.ServidorVisto, 0, len(n.filas))
	for _, sv := range n.filas {
		crudas = append(crudas, sv)
	}
	visibles := vistaLista(crudas, n.buscando, propio)
	l := Lista{Buscando: n.buscando, Filas: make([]Fila, 0, len(visibles))}
	for _, sv := range visibles {
		if sv.Propio && vivo.EnMarcha {
			sv.Ficha.Jugadores, sv.Ficha.Duelos = vivo.Jugadores, vivo.Duelos
			if vivo.Max > 0 {
				sv.Ficha.Max = vivo.Max
			}
		}
		l.Filas = append(l.Filas, n.fila(sv, oc))
		switch sv.Estado {
		case sala.EstadoVivo:
			l.Vivos++
		case sala.EstadoCache:
			l.EnCache++
		}
	}
	return l
}

// emitirLista manda la lista a la página; sin ya=true, como mucho cada 250 ms (la búsqueda trae fichas de golpe).
func (n *Nucleo) emitirLista(ya bool) {
	n.mu.Lock()
	if !ya && time.Since(n.ultEmit) < 250*time.Millisecond {
		if !n.emitPend {
			n.emitPend = true
			time.AfterFunc(260*time.Millisecond, func() {
				n.mu.Lock()
				n.emitPend = false
				n.mu.Unlock()
				n.emitirLista(true)
			})
		}
		n.mu.Unlock()
		return
	}
	n.ultEmit = time.Now()
	n.mu.Unlock()
	n.emit("lista", n.Servidores())
}

// Buscar: (re)lanza la búsqueda de servidores públicos. Lo guardado de la última vez sale al momento, marcado «cache».
func (n *Nucleo) Buscar() { n.buscar(true) }

// buscar: completa = la del botón Refresh y la del arranque (vacía la lista y enseña «searching…»); si no, la de fondo de
// autoBuscar (deja las filas como están y las va poniendo al día).
func (n *Nucleo) buscar(completa bool) {
	n.mu.Lock()
	if n.cancelBus != nil {
		n.cancelBus()
	}
	ctx, cancel := context.WithCancel(context.Background())
	n.cancelBus = cancel
	n.genBus++
	gen := n.genBus
	if completa {
		n.buscando = true
		n.filas = map[string]sala.ServidorVisto{}
	}
	n.mu.Unlock()
	n.emitirLista(true)
	go func() {
		err := n.l.BuscarServidores(ctx, func(sv sala.ServidorVisto) {
			n.mu.Lock()
			if n.genBus == gen {
				n.filas[sv.ID] = sv
			}
			n.mu.Unlock()
			n.emitirLista(false)
			n.verPaquete(sv) // paquete_act.go: ¿reparte una versión firmada más nueva?
		})
		n.mu.Lock()
		if n.genBus == gen {
			n.buscando = false
		}
		n.mu.Unlock()
		if err != nil && ctx.Err() == nil {
			n.logf("[lista] %v", err)
			n.emit("lista_error", sala.M("msg.lista.error", "No se pudo buscar servidores: "+err.Error(), "error", err.Error()))
		}
		n.emitirLista(true)
	}()
}

// marcarPrivado: un servidor de la lista acaba de decir que ahora es privado (rechazó la entrada pública). 12-10: se queda
// en la lista como «Private now» (antes se quitaba sin más y el jugador no sabía qué había pasado).
func (n *Nucleo) marcarPrivado(id string) {
	n.mu.Lock()
	if sv, ok := n.filas[id]; ok {
		sv.Estado, sv.PingMs, sv.PorRelay = sala.EstadoPrivado, -1, false
		n.filas[id] = sv
	}
	n.mu.Unlock()
	n.emitirLista(true)
}

// marcarConClave: el servidor pidió contraseña aunque su ficha (de antes) no lo decía: la fila pasa a llevar candado.
func (n *Nucleo) marcarConClave(id string) {
	n.mu.Lock()
	sv, ok := n.filas[id]
	if ok && !sv.Ficha.Contrasena {
		sv.Ficha.Contrasena = true
		n.filas[id] = sv
	}
	n.mu.Unlock()
	if ok {
		n.emitirLista(true)
	}
}

// Favorito marca o quita un servidor de favoritos (por peer ID).
func (n *Nucleo) Favorito(id string, si bool) error {
	if err := n.l.MarcarFavorito(id, si); err != nil {
		return errGen(err)
	}
	n.mu.Lock()
	if sv, ok := n.filas[id]; ok {
		sv.Favorito = si
		n.filas[id] = sv
	}
	n.mu.Unlock()
	n.emitirLista(true)
	return nil
}

// Ocultar (si=true) o vuelve a enseñar (si=false) un servidor de la lista. Se guarda por peer ID en este equipo.
func (n *Nucleo) Ocultar(id string, si bool) error {
	if err := n.aj.ponerOculto(id, si); err != nil {
		return errGen(err)
	}
	n.emitirLista(true)
	return nil
}

// ---------------------------------------------------------------- entrar

// EventoEntrada: lo que ve la capa «Entrando…».
type EventoEntrada struct {
	Fase         string // inicio | paso | aviso | listo | error | cancelado | reiniciando
	Servidor     string // vacío = servidor privado (con código)
	Paso         int    // 0..3 (Buscando, Túnel, Versión, Juego): la página pone el texto de cada paso
	Estado       string // va | ya | fallo
	Aviso        Msg    // fase aviso
	Error        Msg    // mensaje para el usuario (fase error)
	Tecnico      string // el error tal cual (en español, es técnico), para el desplegable «detalles»
	IrAjustes    bool   // el error se arregla en Ajustes (falta la carpeta del juego)
	SinReintento bool   // el anfitrión rechazó a propósito (privado, lleno...): reintentar no sirve, no se enseña «Retry»
	PideClave    bool   // contraseña mala o que falta: la página vuelve a abrir el diálogo de la contraseña
	ServidorID   string // fase error: el peer ID (para el diálogo de la contraseña); vacío = con código
	Direccion    string // fase error: «Join by address» (la página vuelve a abrir ese diálogo si pide contraseña)
	Via          Msg    // fases paso (túnel hecho) y listo: el camino
	Ms           int    // fase paso (túnel hecho): latencia en ms (0 = no medida)
}

// EntrarEn entra en un servidor de la lista por su peer ID. Vuelve enseguida: el progreso llega por el evento «entrar».
func (n *Nucleo) Entrar(id string) error {
	n.mu.Lock()
	sv, ok := n.filas[id]
	n.mu.Unlock()
	if !ok {
		return errM("msg.err.ya_no_esta", "Ese servidor ya no está en la lista. Pulsa «Actualizar».")
	}
	if sv.Propio {
		return errM("msg.err.es_tuyo", "Ese es tu servidor: usa «Jugar aquí».")
	}
	d := destinoUI{ID: id, Nombre: sv.Ficha.Nombre, sv: &sv}
	if sv.Ficha.Contrasena {
		if d.cred = n.cl.credencial(id); d.cred == nil {
			// la página abre el diálogo de la contraseña y vuelve con EntrarConClave
			return errM("msg.err.pide_clave", "Este servidor pide contraseña.")
		}
		d.guardada, d.recordar = true, true
	}
	return n.iniciarEntrada(d)
}

// EntrarConClave entra en un servidor de la lista con contraseña (el diálogo «Password»). recordar = «Remember
// password»: si sale bien se guarda la clave derivada (DPAPI); si no se marca, se olvida la que hubiera.
func (n *Nucleo) EntrarConClave(id, contrasena string, recordar bool) error {
	n.mu.Lock()
	sv, ok := n.filas[id]
	n.mu.Unlock()
	if !ok {
		return errM("msg.err.ya_no_esta", "Ese servidor ya no está en la lista. Pulsa «Actualizar».")
	}
	if sv.Propio {
		return errM("msg.err.es_tuyo", "Ese es tu servidor: usa «Jugar aquí».")
	}
	if contrasena == "" {
		return errM("msg.err.clave_vacia", "Escribe la contraseña del servidor.")
	}
	if utf8.RuneCountInString(contrasena) > sala.MaxContrasena {
		return errM("msg.err.clave_mala", "Contraseña incorrecta.") // ningún servidor admite una así
	}
	return n.iniciarEntrada(destinoUI{ID: id, Nombre: sv.Ficha.Nombre, sv: &sv, cred: sala.NuevaCredencial(contrasena), recordar: recordar})
}

// OlvidarClave borra la contraseña guardada de un servidor (detalle de la lista).
func (n *Nucleo) OlvidarClave(id string) error {
	if err := n.cl.olvidar(id); err != nil {
		return errGen(err)
	}
	n.emitirLista(true)
	return nil
}

// EntrarConCodigo entra en un servidor privado con su código de sala.
func (n *Nucleo) EntrarConCodigo(codigo string) error {
	if err := validarCodigo(codigo); err != nil { // red.go
		return err
	}
	return n.iniciarEntrada(destinoUI{Codigo: codigo}) // Nombre vacío = «servidor privado»
}

// Reintentar repite la última entrada (botón de la capa de error).
func (n *Nucleo) Reintentar() error {
	n.mu.Lock()
	d := n.ultimo
	n.mu.Unlock()
	if d.ID == "" && d.Codigo == "" && d.Dir == "" {
		return errM("msg.err.nada_que_reintentar", "No hay nada que reintentar.")
	}
	return n.iniciarEntrada(d)
}

// CancelarEntrada corta una entrada en marcha (botón «Cancelar»).
func (n *Nucleo) CancelarEntrada() {
	n.mu.Lock()
	e := n.entrando
	n.mu.Unlock()
	if e != nil {
		e.cancel()
	}
}

func (n *Nucleo) iniciarEntrada(d destinoUI) error {
	n.mu.Lock()
	switch {
	case n.cerrando:
		n.mu.Unlock()
		return errM("msg.err.cerrando", "El lanzador se está cerrando.")
	case n.entrando != nil:
		n.mu.Unlock()
		return errM("msg.err.ya_entrando", "Ya se está entrando en un servidor.")
	case n.con != nil:
		nom := n.conNombre
		n.mu.Unlock()
		if nom == "" { // entrado con código: no hay nombre
			return errM("msg.err.ya_conectado_privado", "Ya estás conectado a un servidor privado. Desconecta antes de entrar en otro.")
		}
		return errM("msg.err.ya_conectado", fmt.Sprintf("Ya estás conectado a «%s». Desconecta antes de entrar en otro.", nom), "nombre", nom)
	case n.juegoAbier:
		n.mu.Unlock()
		return errM("msg.err.juego_abierto", "Ya hay un juego abierto desde este lanzador: ciérralo antes de entrar en otro servidor.")
	}
	n.mu.Unlock()
	if n.carpetaJuego() == "" {
		return errMc(juego.ErrCarpeta, "msg.err.sin_juego", "No encuentro yo2.exe. Elige la carpeta del juego en Ajustes.")
	}
	ctx, cancel := context.WithCancel(context.Background())
	n.mu.Lock()
	n.entrando = &entrada{cancel: cancel, nombre: d.Nombre}
	n.ultimo = d
	n.mu.Unlock()
	go n.correrEntrada(ctx, d)
	return nil
}

func (n *Nucleo) correrEntrada(ctx context.Context, d destinoUI) {
	defer func() {
		n.mu.Lock()
		if n.entrando != nil {
			n.entrando.cancel()
			n.entrando = nil
		}
		n.mu.Unlock()
	}()
	var dest sala.Destino
	if d.ID != "" {
		sv := *d.sv
		dest.Servidor = &sv
		dest.Credencial = d.cred
	} else {
		dest.Codigo, dest.Direccion = d.Codigo, d.Dir
		if d.Dir != "" {
			dest.Credencial = d.cred
		}
	}
	n.emit("entrar", EventoEntrada{Fase: "inicio", Servidor: d.Nombre})
	retomar := Retomar{Publico: d.ID, Codigo: d.Codigo, Dir: d.Dir}
	opc := sala.OpcEntrar{
		Aviso: func(m string) {
			n.logf("[entrar] %s", m)
			n.emit("entrar", EventoEntrada{Fase: "aviso", Servidor: d.Nombre, Aviso: raw(m)})
		},
		AvisoK: func(m sala.Msg) {
			n.logf("[entrar] %s", m.Es)
			n.emit("entrar", EventoEntrada{Fase: "aviso", Servidor: d.Nombre, Aviso: m})
		},
		ComprobarVersion: func(ctx context.Context, c *sala.Conexion) error {
			r := retomar
			if d.cred != nil {
				r.Clave = d.cred.Texto() // ya hubo saludo: la clave derivada (si se reinicia por una versión nueva)
			}
			return n.comprobarVersion(c, d, r)
		},
		AbrirJuego: func(ctx context.Context, c *sala.Conexion) error { return n.abrirJuego(false) },
	}
	if n.cfg.AjustarEntrada != nil {
		n.cfg.AjustarEntrada(&opc)
	}
	c, err := n.l.Entrar(ctx, dest, opc, func(e sala.EventoEntrar) {
		if errors.Is(e.Err, errReiniciando) {
			return
		}
		n.emit("entrar", EventoEntrada{Fase: "paso", Servidor: d.Nombre, Paso: int(e.Paso), Estado: e.Estado, Via: e.Via, Ms: e.Ms})
	})
	// «entrando» se apaga ANTES de contar el resultado: el botón «Reintentar» de la página ya debe poder volver a entrar
	// (si fue el jugador quien canceló se mira ANTES: después de esto ctx siempre está cancelado y, hasta el 12-10, cualquier
	// fallo salía como «cancelado»: la capa se cerraba sin decir por qué y no se marcaba «Private now»)
	cancelado := ctx.Err() != nil
	n.mu.Lock()
	if n.entrando != nil {
		n.entrando.cancel()
		n.entrando = nil
	}
	n.mu.Unlock()
	switch {
	case errors.Is(err, errReiniciando):
		n.logf("[entrar] el lanzador se reinicia con la versión nueva")
		n.emit("entrar", EventoEntrada{Fase: "reiniciando", Servidor: d.Nombre})
		n.mu.Lock()
		n.cerrando = true // que el cierre no pregunte nada
		n.mu.Unlock()
		time.Sleep(400 * time.Millisecond)
		if n.cfg.Salir != nil {
			n.cfg.Salir()
		}
	case err != nil:
		msg, tec, ajustes := traducirError(err)
		if cancelado {
			n.emit("entrar", EventoEntrada{Fase: "cancelado", Servidor: d.Nombre})
			return
		}
		n.logf("[entrar] fallo: %v", err)
		var rz *sala.ErrRechazado
		definitivo := errors.As(err, &rz)
		if errors.Is(err, sala.ErrPrivado) && d.ID != "" {
			n.marcarPrivado(d.ID) // ya no es público: «Private now» (hace falta el código del anfitrión)
		}
		pide := (d.ID != "" || d.Dir != "") && (errors.Is(err, sala.ErrContrasena) || errors.Is(err, sala.ErrPideContrasena))
		if pide && d.ID != "" {
			if d.guardada { // lo guardado ya no vale (el anfitrión cambió la contraseña)
				n.cl.olvidar(d.ID)
			}
			n.marcarConClave(d.ID)
		}
		n.emit("entrar", EventoEntrada{Fase: "error", Servidor: d.Nombre, Error: msg, Tecnico: tec, IrAjustes: ajustes, SinReintento: definitivo,
			Estado: "fallo", PideClave: pide, ServidorID: d.ID, Direccion: d.Dir})
	default:
		if d.cred != nil && d.ID != "" && !d.guardada { // contraseña escrita ahora: «Remember password» o se olvida
			var err error
			if d.recordar {
				err = n.cl.recordar(d.ID, d.cred)
			} else {
				err = n.cl.olvidar(d.ID)
			}
			if err != nil {
				n.logf("[entrar] no pude guardar u olvidar la contraseña: %v", err)
			}
		}
		n.mu.Lock()
		n.con, n.conNombre, n.conDesde = c, d.Nombre, time.Now()
		n.mu.Unlock()
		n.logf("[entrar] conectado a %q por %s", d.Nombre, c.Via) // Nombre vacío = servidor privado
		n.emit("entrar", EventoEntrada{Fase: "listo", Servidor: d.Nombre, Via: c.ViaMsg})
	}
}

// traducirError: el error de sala/juego como mensaje con clave (la página lo traduce) + el texto técnico, que se queda
// tal cual (en español) para el desplegable «detalles técnicos». Las causas conocidas se reconocen por su error
// (errors.Is / errors.As); el reconocimiento por texto es solo el respaldo de errores sin tipo.
func traducirError(err error) (m Msg, tecnico string, irAjustes bool) {
	tecnico = err.Error()
	var ui *ErrUI
	if errors.As(err, &ui) { // ya viene con clave (p. ej. de abrirJuego)
		if ui.Causa != nil {
			tecnico = ui.Causa.Error()
		} else {
			tecnico = ui.Msg.Es
		}
		return ui.Msg, tecnico, errors.Is(err, juego.ErrCarpeta)
	}
	s := strings.ToLower(tecnico)
	var sinCamino *sala.ErrSinCamino
	var rechazado *sala.ErrRechazado
	switch {
	case errors.Is(err, context.Canceled):
		return sala.M("msg.err.cancelado", "Cancelado."), tecnico, false
	case errors.Is(err, sala.ErrDireccion), errors.Is(err, sala.ErrDirSinRespuesta), errors.Is(err, sala.ErrDirOculta), errors.Is(err, sala.ErrDirOtro):
		return msgDireccion(err), tecnico, false // red.go
	case errors.Is(err, sala.ErrPrivado):
		return sala.M("msg.err.privado", "Este servidor ahora es privado. Pide el código al anfitrión y usa «Join with code»."), tecnico, false
	case errors.Is(err, sala.ErrContrasena):
		return sala.M("msg.err.clave_mala", "Contraseña incorrecta."), tecnico, false
	case errors.Is(err, sala.ErrPideContrasena):
		return sala.M("msg.err.pide_clave", "Este servidor pide contraseña."), tecnico, false
	case errors.Is(err, sala.ErrIntentos):
		return sala.M("msg.err.clave_intentos", "Demasiadas contraseñas incorrectas. Espera un minuto e inténtalo otra vez."), tecnico, false
	case errors.Is(err, sala.ErrLleno):
		return sala.M("msg.err.lleno", "El servidor está lleno ahora mismo. Prueba dentro de un rato."), tecnico, false
	case errors.As(err, &rechazado):
		if rechazado.SinSitio() {
			return sala.M("msg.err.sin_sitio", "El anfitrión no puede aceptar otra conexión ahora mismo. Prueba dentro de un rato."), tecnico, false
		}
		return sala.M("msg.err.rechazado", "El anfitrión ha rechazado la entrada."), tecnico, false
	case errors.Is(err, sala.ErrVersionDistint):
		return sala.M("msg.err.otra_version", "Ese servidor usa otra versión del juego y tu yo2.exe no puede entrar en él."), tecnico, false
	case errors.Is(err, juego.ErrCarpeta):
		return sala.M("msg.err.sin_juego", "No encuentro yo2.exe. Elige la carpeta del juego en Ajustes."), tecnico, true
	case errors.Is(err, sala.ErrImpostor), strings.Contains(s, "no es el anfitrión"), strings.Contains(s, "firma"):
		return sala.M("msg.err.impostor", "El servidor que contesta no es quien dice ser, así que no se entra. Puede que alguien se esté haciendo pasar por él."), tecnico, false
	case errors.Is(err, sala.ErrPuertoLocal), strings.Contains(s, "ocupado"):
		return sala.M("msg.err.puerto_ocupado", "Un puerto del juego ya está en uso en este PC (¿otro lanzador o tu propio servidor?). Ciérralo e inténtalo otra vez."), tecnico, false
	case errors.Is(err, sala.ErrJuegoNoContesta), strings.Contains(s, "no contesta") && strings.Contains(s, "run.py"):
		return sala.M("msg.err.runpy_no_contesta", "Te conectaste al anfitrión, pero su servidor del juego no responde. Avísale: puede que le falte arrancar run.py."), tecnico, false
	case strings.Contains(s, "crossover"):
		return sala.M("msg.err.crossover", "No se pudo abrir el juego con CrossOver. En Ajustes elige la carpeta del juego dentro de la botella (la que tiene yo2.exe), o define YO2_CX_BOTTLE con el nombre de la botella."), tecnico, true
	case errors.Is(err, juego.ErrLanzar), errors.Is(err, juego.ErrPreparar), strings.Contains(s, "no pude lanzar el juego"), strings.Contains(s, "no puedo preparar los ficheros"):
		return sala.M("msg.err.no_abre_juego", "No se pudo abrir el juego. Si tienes antivirus, permite la carpeta %LOCALAPPDATA%\\YO2Online (ahí está frida-inject.exe)."), tecnico, false
	case errors.As(err, &sinCamino), strings.Contains(tecnico, "- P2P:") && strings.Contains(tecnico, "- Tor:"):
		return sala.M("msg.err.sin_camino", "No se pudo conectar con el servidor ni directo (P2P) ni por Tor. Puede que esté apagado o que tu red lo bloquee."), tecnico, false
	}
	return sala.M("msg.err.entrar_generico", "No se pudo entrar: "+firstLine(tecnico)), tecnico, false
}

// errJuego: un fallo al abrir yo2.exe como error con clave (el texto técnico queda en Causa).
func errJuego(err error) error {
	m, _, _ := traducirError(err)
	if m.K == "msg.err.entrar_generico" {
		m = sala.M("msg.err.generico", err.Error(), "error", err.Error())
	}
	return &ErrUI{Msg: m, Causa: err}
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return s
}

// comprobarVersion: la autoactualización del lanzador por el túnel recién abierto. Si se instala una versión nueva, la
// ventana se reinicia y vuelve a entrar sola (por eso se le pasa a qué estábamos entrando).
func (n *Nucleo) comprobarVersion(c *sala.Conexion, d destinoUI, r Retomar) error {
	if n.paqueteAlEntrar(c) { // paquete_act.go: el anfitrión reparte el paquete firmado (se baja en segundo plano)
		return nil
	}
	act := &juego.Actualizador{Datos: n.cfg.Datos, Logf: n.cfg.Logf, Version: n.cfg.Version, Tipo: sala.TipoLanzadorApp,
		Nombre: sala.NombreLanzadorApp, Exe: n.cfg.Exe}
	env := append(r.Entorno(), EnvPadrePID+"="+strconv.Itoa(os.Getpid()))
	rel, _ := act.Actualizar(c.Invitado(), nil, juego.OpcActualiza{
		Desactivada: !n.aj.leer().Actualizar,
		Aviso: func(m string) {
			n.logf("[actualiza] %s", m)
			n.emit("entrar", EventoEntrada{Fase: "aviso", Servidor: d.Nombre, Aviso: raw(m)})
		},
		AvisoMsg: func(m sala.Msg) {
			n.logf("[actualiza] %s", m.Es)
			n.emit("entrar", EventoEntrada{Fase: "aviso", Servidor: d.Nombre, Aviso: m})
		},
		Progreso: func(hecho, total int64) {
			pct := int(hecho * 100 / max(total, 1))
			n.emit("entrar", EventoEntrada{Fase: "aviso", Servidor: d.Nombre,
				Aviso: sala.M("msg.act.progreso", fmt.Sprintf("Descargando la versión nueva del lanzador: %d%%", pct), "pct", pct)})
		},
		Env:        env,
		EsperaHijo: 6 * time.Second,
	})
	if rel {
		return errReiniciando
	}
	return nil
}

// abrirJuego lanza yo2.exe con el gancho. local=true: «Jugar aquí» (en el servidor propio, sin túnel).
func (n *Nucleo) abrirJuego(local bool) error {
	carpeta := n.carpetaJuego()
	if carpeta == "" {
		return errMc(juego.ErrCarpeta, "msg.err.sin_juego", "No encuentro yo2.exe. Elige la carpeta del juego en Ajustes.")
	}
	n.mu.Lock()
	if n.juegoAbier {
		n.mu.Unlock()
		return errM("msg.err.juego_ya_abierto", "El juego ya está abierto desde este lanzador.")
	}
	n.mu.Unlock()
	off := 0 // «Jugar aquí» va al servidor propio, que en pruebas escucha con YO2_PORT_OFFSET; por túnel, puertos de siempre
	if local {
		off = n.cfg.Offset
	}
	n.jl.Ventana = n.aj.leer().Ventana // tamaño de ventana de Ajustes → Display (apagado por defecto)
	p, err := n.jl.LanzarCon(carpeta, juego.ExisteDiag(carpeta), off)
	if err != nil {
		return errJuego(err)
	}
	n.mu.Lock()
	n.partida, n.juegoAbier, n.juegoLocal = p, true, local
	n.mu.Unlock()
	go func() {
		var mm Msg
		if p.FinMsg != nil {
			mm = <-p.FinMsg // lo envía justo antes que Fin
		}
		m := <-p.Fin
		if mm.Vacio() {
			mm = raw(m)
		}
		n.logf("[juego] %s", m)
		n.mu.Lock()
		if n.partida == p {
			n.partida, n.juegoAbier, n.juegoLocal = nil, false, false
		}
		n.mu.Unlock()
		n.emit("juego", mm)
	}()
	return nil
}

// AbrirJuego vuelve a abrir el juego por la conexión que sigue abierta (cuando el juego se cerró pero seguimos conectados).
func (n *Nucleo) AbrirJuego() error {
	n.mu.Lock()
	ok := n.con != nil
	n.mu.Unlock()
	if !ok {
		return errM("msg.err.no_conectado", "No estás conectado a ningún servidor.")
	}
	return n.abrirJuego(false)
}

// Desconectar cierra el túnel (y el juego, que sin él no tiene a dónde conectarse).
func (n *Nucleo) Desconectar() {
	n.mu.Lock()
	c, p := n.con, n.partida
	n.con, n.conNombre = nil, ""
	n.mu.Unlock()
	if p != nil {
		p.Matar()
	}
	if c != nil {
		c.Cerrar()
		n.logf("[entrar] desconectado")
	}
}

// ---------------------------------------------------------------- crear servidor

// EstadoSrv: el panel «Tu servidor».
type EstadoSrv struct {
	EnMarcha    bool
	ID          string // el de «My servers» que está en marcha
	Nombre      string
	DesdeS      int
	Huella      string
	Publica     bool
	ConClave    bool // público con contraseña
	JuegoOK     bool
	Juego       Msg
	P2POK       bool
	P2P         Msg
	ListadoOK   bool
	Listado     Msg
	TorOK       bool
	Tor         Msg
	Jugadores   int
	EnDuelo     int // -1 = no se sabe
	Max         int
	Invitados   int
	FuenteJug   string
	RunPyPropio bool
	Aviso       Msg
	Acceso      *sala.AccesoRed // puerto, ¿accesible desde Internet?, dirección pública (red.go)
}

func (n *Nucleo) estadoServidor(e sala.EstadoServidor) EstadoSrv {
	s := EstadoSrv{Acceso: e.Acceso, EnMarcha: e.EnMarcha, Huella: e.Huella, Publica: e.Publica, ConClave: e.ConClave, JuegoOK: e.JuegoOK, Juego: e.JuegoM, P2POK: e.P2POK,
		P2P: e.P2PM, ListadoOK: e.ListadoOK, Listado: e.ListadoM, TorOK: e.TorOK, Tor: e.TorM, Jugadores: e.Jugadores,
		EnDuelo: e.Duelos, Max: e.Max, Invitados: e.Invitados, FuenteJug: e.FuenteJug, RunPyPropio: e.RunPyPropio}
	if !e.EnMarcha {
		s.EnDuelo = -1
		return s
	}
	s.DesdeS = int(time.Since(e.Desde).Seconds())
	n.mu.Lock()
	s.Aviso, s.ID = n.avisoSrv, n.srvID
	n.mu.Unlock()
	if sg, ok := n.guardado(s.ID); ok {
		s.Nombre = sg.Form.Nombre
	}
	return s
}

// buscarJoinServer: el JoinServer.exe que se reparte a los invitados que usan la consola.
func buscarJoinServer(exeDir string) string {
	for _, c := range []string{filepath.Join(exeDir, sala.NombreLanzador), filepath.Join(exeDir, "..", "dist", sala.NombreLanzador),
		filepath.Join(exeDir, "..", "..", "launcher", "dist", sala.NombreLanzador)} {
		if st, err := os.Stat(c); err == nil && st.Mode().IsRegular() {
			p, _ := filepath.Abs(c)
			return p
		}
	}
	return ""
}

// EstadoServidor: el panel «Tu servidor» (también va dentro de Estado()).
func (n *Nucleo) EstadoServidor() EstadoSrv { return n.estadoServidor(n.l.EstadoServidor()) }

// ResultadoParar: cómo acabó «Parar servidor».
type ResultadoParar struct {
	OK       bool
	Forzada  bool
	Mensaje  string // el texto en español de siempre (log y pruebas)
	Partes   []Msg  // lo mismo con claves: la página las traduce y las enseña seguidas
	Segundos float64
}

// PararServidor para el servidor con orden: primero run.py por su consola (solo si lo arrancó este lanzador), luego el
// túnel y los anuncios. Bloquea hasta ~35 s: la página muestra «Parando…» mientras tanto. La confirmación («hay N
// jugadores») la pide la página antes de llamar.
func (n *Nucleo) PararServidor() ResultadoParar {
	n.mu.Lock()
	if n.parando {
		n.mu.Unlock()
		m := sala.M("msg.parar.ya_parando", "Ya se está parando.")
		return ResultadoParar{Mensaje: m.Es, Partes: []Msg{m}}
	}
	n.parando = true
	n.mu.Unlock()
	defer func() {
		n.mu.Lock()
		n.parando = false
		n.mu.Unlock()
	}()
	t0 := time.Now()
	r := ResultadoParar{OK: true}
	proc, hecho := n.l.RunPyPropio()
	if proc != nil {
		res := PararRunPy(ParadaOpc{Offset: n.cfg.Offset, Datos: n.cfg.Datos, Proc: proc, Hecho: hecho, Logf: n.cfg.Logf})
		r.Forzada, r.Mensaje, r.Partes = res.Forzada, res.Mensaje, res.Partes
	} else if n.l.EstadoServidor().EnMarcha {
		m := sala.M("msg.parar.runpy_ajeno", "run.py no lo arrancó este lanzador, así que se deja en marcha.")
		r.Mensaje, r.Partes = m.Es, []Msg{m}
	}
	n.l.PararServidor()
	if r.Mensaje == "" {
		m := sala.M("msg.parar.parado", "Servidor parado.")
		r.Mensaje, r.Partes = m.Es, []Msg{m}
	} else {
		m := sala.M("msg.parar.fuera_lista", "El servidor ya no está en la lista ni acepta invitados.")
		r.Mensaje += " " + m.Es
		r.Partes = append(r.Partes, m)
	}
	n.mu.Lock()
	n.avisoSrv, n.srvID = Msg{}, ""
	n.mu.Unlock()
	r.Segundos = time.Since(t0).Seconds()
	n.logf("[servidor] parado: %s", r.Mensaje)
	return r
}

// JugarAqui abre el juego en este PC contra el servidor propio (sin túnel: run.py ya escucha en 127.0.0.1).
func (n *Nucleo) JugarAqui() error {
	if !n.l.EstadoServidor().EnMarcha {
		return errM("msg.err.srv_parado", "Tu servidor no está en marcha.")
	}
	if !n.l.EstadoServidor().JuegoOK {
		return errM("msg.err.runpy_no_escucha", "El servidor del juego (run.py) todavía no escucha. Espera unos segundos.")
	}
	n.mu.Lock()
	if n.con != nil {
		n.mu.Unlock()
		return errM("msg.err.conectado_otro", "Estás conectado a otro servidor: desconecta antes.")
	}
	n.mu.Unlock()
	return n.abrirJuego(true)
}

// CopiarCodigo copia el código de sala PRIVADO al portapapeles (nunca se enseña entero ni va al log). En modo «Hidden»
// es la única forma de entrar; en modo «Password» el código entra SIN contraseña (para dárselo a quien se quiera).
func (n *Nucleo) CopiarCodigo() error {
	e := n.l.EstadoServidor()
	if !e.EnMarcha {
		return errM("msg.err.srv_parado", "Tu servidor no está en marcha.")
	}
	if e.Publica && !e.ConClave {
		return errM("msg.err.srv_publico", "Tu servidor es público: no hace falta código.")
	}
	if n.cfg.Portapapeles == nil {
		return errM("msg.err.sin_portapapeles", "no se puede usar el portapapeles desde aquí")
	}
	c := n.l.CodigoPrivado()
	if c == "" {
		return errM("msg.err.sin_codigo", "No hay código.")
	}
	if err := n.cfg.Portapapeles(c); err != nil {
		return errM("msg.err.no_copia", "No pude copiar al portapapeles: "+err.Error(), "error", err.Error())
	}
	return nil
}

// ---------------------------------------------------------------- cerrar

// PideCierre: lo que la página enseña al cerrar con algo en marcha.
type PideCierre struct {
	Conectado bool
	Servidor  bool
	Jugadores int
	Juego     bool
}

// AntesDeCerrar lo llama la ventana cuando el usuario cierra: true = no cerrar todavía (se ha pedido confirmación a la
// página con el evento «cerrar»). Cerrar con una conexión abierta cortaría el túnel; con el servidor en marcha, lo apagaría.
func (n *Nucleo) AntesDeCerrar() (impedir bool) {
	n.mu.Lock()
	if n.cerrando {
		n.mu.Unlock()
		return false
	}
	p := PideCierre{Conectado: n.con != nil, Juego: n.juegoAbier}
	n.mu.Unlock()
	se := n.l.EstadoServidor()
	p.Servidor, p.Jugadores = se.EnMarcha, se.Jugadores
	if !p.Conectado && !p.Servidor {
		return false
	}
	n.mu.Lock()
	if !n.cierreEmitido.IsZero() && !n.cierreVisto && time.Since(n.cierreEmitido) > 5*time.Second {
		n.mu.Unlock()
		n.logf("[cerrar] la página no contesta a la pregunta de cerrar: se deja cerrar")
		return false
	}
	n.cierreEmitido, n.cierreVisto = time.Now(), false
	n.mu.Unlock()
	n.emit("cerrar", p)
	return true
}

// CierreVisto: la página avisa de que ha recibido la pregunta de cerrar (y la está enseñando).
func (n *Nucleo) CierreVisto() {
	n.mu.Lock()
	n.cierreVisto = true
	n.mu.Unlock()
}

// ConfirmarCierre: el usuario ha dicho «sí, salir». Para el servidor con orden, corta la conexión y cierra la ventana.
func (n *Nucleo) ConfirmarCierre() {
	n.mu.Lock()
	n.cerrando = true
	n.mu.Unlock()
	go func() {
		n.emit("cerrando", sala.M("msg.cerrando", "Cerrando…"))
		if n.l.EstadoServidor().EnMarcha {
			n.PararServidor()
		}
		n.Desconectar()
		n.Cerrar()
		if n.cfg.Salir != nil {
			n.cfg.Salir()
		}
	}()
}

// CancelarCierre: el usuario ha dicho «no, seguir».
func (n *Nucleo) CancelarCierre() {
	n.mu.Lock()
	n.cierreEmitido, n.cierreVisto = time.Time{}, false
	n.mu.Unlock()
}

// Cerrar libera la red (al terminar el proceso).
func (n *Nucleo) Cerrar() {
	n.mu.Lock()
	if n.cancelBus != nil {
		n.cancelBus()
	}
	n.mu.Unlock()
	n.l.Cerrar()
}

// ---------------------------------------------------------------- comprobar instalación

// Chequeo: una línea del informe de «Comprobar instalación».
type Chequeo struct {
	Nombre  Msg
	Estado  string // ok | aviso | error
	Detalle Msg
}

// Comprobar revisa lo necesario para jugar y para crear servidor: juego, versión, WebView2, puertos libres, Python/run.py.
func (n *Nucleo) Comprobar() []Chequeo {
	var r []Chequeo
	add := func(nombre Msg, estado string, detalle Msg) { r = append(r, Chequeo{nombre, estado, detalle}) }
	nJuego := sala.M("msg.chk.juego", "Juego")
	nVersion := sala.M("msg.chk.version", "Versión del juego")
	nPuertos := sala.M("msg.chk.puertos", "Puertos del juego")
	nPython := sala.M("msg.chk.python", "Python (para crear servidor)")
	nRunPy := sala.M("msg.chk.runpy", "run.py (para crear servidor)")

	c := juego.BuscarCarpeta(n.aj.leer().CarpetaJuego, n.cfg.ExeDir, n.cfg.Datos)
	if c == "" {
		add(nJuego, "error", sala.M("msg.chk.juego_no", "No encuentro yo2.exe. Elige la carpeta en Ajustes."))
	} else {
		add(nJuego, "ok", sala.M("msg.chk.juego_en", "yo2.exe en "+c, "ruta", c))
		if v, err := juego.VersionJuego(c); err != nil {
			if errors.Is(err, juego.ErrSinVersion) {
				add(nVersion, "aviso", sala.M("msg.chk.version_desconocida", err.Error()))
			} else {
				add(nVersion, "aviso", raw(err.Error()))
			}
		} else if v != juego.VersionEsperada {
			add(nVersion, "error", sala.M("msg.chk.version_otra", fmt.Sprintf("Es Ver.%s; los servidores usan Ver.%s.", v, juego.VersionEsperada),
				"version", v, "esperada", juego.VersionEsperada))
		} else {
			add(nVersion, "ok", raw("Ver."+v))
		}
	}
	if juego.RequiereCrossOver() {
		if err := juego.CrossOverListo(); err != nil {
			add(raw("CrossOver"), "error", raw(err.Error()))
		} else {
			add(raw("CrossOver"), "ok", raw("CrossOver"))
		}
	}
	if n.cfg.WebView2 != nil {
		nombre := raw("WebView2")
		if v, err := n.cfg.WebView2(); err != nil {
			add(nombre, "error", sala.M("msg.chk.webview_falta", "Falta el runtime de WebView2 de Microsoft: "+err.Error(), "error", err.Error()))
		} else {
			add(nombre, "ok", sala.M("msg.chk.webview_version", "Versión "+v, "version", v))
		}
	}
	// puertos del juego en este PC: los usa el túnel al entrar. Si es tu servidor quien los tiene, es lo normal.
	ports := sala.Puertos()
	if n.cfg.Puertos != nil {
		ports = n.cfg.Puertos()
	}
	ocupados := ocupadosDe(ports)
	propio := n.l.EstadoServidor().EnMarcha
	switch {
	case len(ocupados) == 0:
		add(nPuertos, "ok", sala.M("msg.chk.puertos_libres", fmt.Sprintf("Los %d puertos locales están libres.", len(ports)), "n", len(ports)))
	case propio:
		add(nPuertos, "ok", sala.M("msg.chk.puertos_propio", "Los usa tu servidor (es lo normal mientras está en marcha)."))
	default:
		add(nPuertos, "aviso", sala.M("msg.chk.puertos_ocupados", fmt.Sprintf("%d puerto(s) en uso (p. ej. %d): para entrar en un servidor ajeno hay que cerrar el programa que los usa (¿un run.py o JoinServer abierto?).",
			len(ocupados), ocupados[0]), "n", len(ocupados), "ejemplo", ocupados[0]))
	}
	if pq := sala.BuscarPaquete(n.cfg.ExeDir); pq != nil {
		add(nPython, "ok", raw(pq.Python)) // P5: el del paquete
	} else if sala.HayPython() {
		add(nPython, "ok", raw(filepath.Base(sala.PythonExe())))
	} else {
		add(nPython, "aviso", sala.M("msg.chk.python_no", "No está en el PATH. Solo hace falta para crear un servidor."))
	}
	if rp := n.runPyVisible(); rp == "" { // P5: también el del paquete
		add(nRunPy, "aviso", sala.M("msg.chk.runpy_no", "No está junto al lanzador. Solo hace falta para crear un servidor."))
	} else {
		add(nRunPy, "ok", raw(rp))
	}
	if juego.ProcesoActivo("OpenServer.exe") {
		add(raw("OpenServer.exe"), "aviso", sala.M("msg.chk.openserver_abierto", "Está abierto: no podrás crear un servidor desde aquí hasta cerrarlo."))
	}
	return r
}

// ocupadosDe: los puertos de la lista que no se pueden abrir en 127.0.0.1 (justo lo que hace el túnel al entrar: escuchar
// ahí). Se abren y se cierran al momento; no se conecta a nadie.
func ocupadosDe(ports []int) []int {
	var r []int
	for _, p := range ports {
		ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(p)))
		if err != nil {
			r = append(r, p)
			continue
		}
		ln.Close()
	}
	return r
}

// podarLogs deja los n ficheros más nuevos que casen con el patrón (el nombre lleva la fecha).
func podarLogs(patron string, n int) {
	fs, _ := filepath.Glob(patron)
	sort.Strings(fs)
	for i := 0; i+n < len(fs); i++ {
		os.Remove(fs[i])
	}
}
