package nucleo

// «My servers» (12-10, rama rx/SRV): varios servidores guardados en el mismo PC, cada uno con su propio mundo.
//
// Dónde vive cada servidor (todo bajo la carpeta de datos del lanzador, %LOCALAPPDATA%\YO2Online):
//
//	servidores\<id>\anfitrion\   identidad.key + sala.txt (+ tor\): SU peer ID, SU código de sala y SU fila en la lista
//	servidores\<id>\servidor\    YO2_STATE = state.json (+ .sqlite, admin_*.json, copias, repeticiones) y YO2_LOGDIR = logs\
//
// El servidor que ya existía antes de «My servers» (el «de antes», Legado) se queda EXACTAMENTE donde estaba, con su
// identidad y sus datos (mismo código de sala, nada se mueve ni se copia):
//
//	anfitrion\                   la identidad de siempre (la misma que OpenServer)
//	estado y logs                los de siempre: con el paquete, servidor\<5730+offset> (sala.Paquete.DirServidor); en
//	                             desarrollo (run.py del repo), el state.json y logs\ de tools\yo2emu (no se pasa YO2_STATE)
//
// Solo uno en marcha a la vez por PC (comparten los puertos del juego). Las reglas (Vanilla/Enhanced) se eligen al crear y
// no se cambian nunca: el mundo de un servidor se hizo con unas reglas. Borrar mueve su carpeta a la papelera (nunca la del
// repo); nunca el que está en marcha.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"yo2online/juego"
	"yo2online/sala"
)

// ServidorGuardado: un servidor de «My servers» tal como va en lanzador.json.
type ServidorGuardado struct {
	ID   string       `json:"id"`   // 16 hex (nombre de su carpeta); no es secreto ni sale en la red
	Form FormServidor `json:"form"` // sin contraseña (solo su clave derivada, en Clave)
	// Clave: la contraseña del modo «Password» como clave derivada protegida con DPAPI, ligada a SU identidad (claves.go).
	Clave *ClaveGuardada `json:"clave_servidor,omitempty"`
	// Legado: el servidor de antes de «My servers»: identidad en anfitrion\ y estado donde siempre (ver arriba).
	Legado bool      `json:"legado,omitempty"`
	Creado time.Time `json:"creado"`
	Usado  time.Time `json:"usado,omitempty"` // último arranque
}

var reIDServidor = regexp.MustCompile(`^[0-9a-f]{16}$`)

func nuevoIDServidor() string {
	b := make([]byte, 8)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// nombreDeAntes: el nombre del servidor de antes si su formulario no tenía (p. ej. solo se usó OpenServer).
const nombreDeAntes = "My server"

// servidorDeAntes: el servidor que ya existía en este PC antes de «My servers», si hay rastro de él: un formulario con
// nombre en el lanzador.json de antes, la identidad de anfitrión de siempre (anfitrion\identidad.key, también la de
// OpenServer) o el estado del paquete (servidor\<puerto>\state.json). Si no hay nada, no se inventa ninguno.
func servidorDeAntes(f *FormServidor, clave *ClaveGuardada, datos string) (ServidorGuardado, bool) {
	hayForm := f != nil && strings.TrimSpace(f.Nombre) != ""
	_, errId := os.Stat(filepath.Join(datos, "anfitrion", "identidad.key"))
	estados, _ := filepath.Glob(filepath.Join(datos, "servidor", "*", "state.json*"))
	if !hayForm && errId != nil && len(estados) == 0 {
		return ServidorGuardado{}, false
	}
	form := FormPorDefecto()
	if f != nil {
		form = f.Normalizado()
	}
	form.Nombre = strings.TrimSpace(form.Nombre)
	if form.Nombre == "" {
		form.Nombre = nombreDeAntes
	}
	form.Contrasena, form.TieneClave = "", false
	return ServidorGuardado{ID: nuevoIDServidor(), Form: form, Clave: clave, Legado: true, Creado: time.Now()}, true
}

// limpiarServidores: lo leído de lanzador.json sin entradas raras (id que no vale como carpeta, repetidas, dos «de antes»).
func limpiarServidores(ss []ServidorGuardado) []ServidorGuardado {
	vistos := map[string]bool{}
	legado := false
	var r []ServidorGuardado
	for _, s := range ss {
		if !reIDServidor.MatchString(s.ID) || vistos[s.ID] || (s.Legado && legado) {
			continue
		}
		vistos[s.ID] = true
		legado = legado || s.Legado
		s.Form = s.Form.Normalizado()
		s.Form.Contrasena, s.Form.TieneClave = "", false
		r = append(r, s)
	}
	return r
}

// ---------------------------------------------------------------- carpetas

// dirPropio: la carpeta de un servidor nuevo (servidores\<id>). "" para el de antes (no tiene una propia).
func (n *Nucleo) dirPropio(sg ServidorGuardado) string {
	if sg.Legado {
		return ""
	}
	return filepath.Join(n.cfg.Datos, "servidores", sg.ID)
}

// dirAnfitrion: la carpeta de su identidad (peer ID, código de sala, .onion).
func (n *Nucleo) dirAnfitrion(sg ServidorGuardado) string {
	if sg.Legado {
		return filepath.Join(n.cfg.Datos, "anfitrion")
	}
	return filepath.Join(n.dirPropio(sg), "anfitrion")
}

// dirDatos: la carpeta de su estado y sus logs (YO2_STATE/YO2_LOGDIR). "" = la de siempre (el de antes).
func (n *Nucleo) dirDatos(sg ServidorGuardado) string {
	if sg.Legado {
		return ""
	}
	return filepath.Join(n.dirPropio(sg), "servidor")
}

// carpetasAlBorrar: lo que Delete manda a la papelera. Un servidor nuevo: su carpeta entera (identidad incluida). El de
// antes: solo su estado del paquete (servidor\<puerto>), si lo hay; su identidad (anfitrion\, compartida con OpenServer)
// se queda, y en desarrollo NUNCA se toca nada del repo.
func (n *Nucleo) carpetasAlBorrar(sg ServidorGuardado) []string {
	if !sg.Legado {
		return []string{n.dirPropio(sg)}
	}
	if pq := sala.BuscarPaquete(n.cfg.ExeDir); pq != nil {
		d := pq.DirServidor(n.cfg.Datos, n.cfg.Offset)
		if st, err := os.Stat(d); err == nil && st.IsDir() {
			return []string{d}
		}
	}
	return nil
}

// peerDe: el peer ID de la identidad de esa carpeta (la crea si no existe: un servidor nuevo la estrena al crearse).
func peerDe(dir string) (*sala.Sala, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	s, _, err := sala.CargarAnfitrion(dir, false, false)
	return s, err
}

// ---------------------------------------------------------------- lo que ve la página

// MiServidor: una tarjeta de «My servers».
type MiServidor struct {
	ID          string
	Form        FormServidor // sin contraseña; TieneClave = hay una guardada para su identidad
	Legado      bool
	EnMarcha    bool
	Jugadores   int // en marcha: los de ahora
	Max         int
	CreadoHaceS int
	UsadoHaceS  int    // -1 = nunca arrancado desde «My servers»
	BorraDatos  bool   // Delete manda a la papelera su mundo (false: el de antes en desarrollo, cuyos datos son los del repo)
	Huella      string // XXXX-XXXX de su identidad (la que ven los jugadores en el detalle de la lista)
}

func (n *Nucleo) miServidor(sg ServidorGuardado, enMarchaID string, e sala.EstadoServidor) MiServidor {
	m := MiServidor{ID: sg.ID, Form: sg.Form, Legado: sg.Legado, Max: sg.Form.Max, CreadoHaceS: int(time.Since(sg.Creado).Seconds()),
		UsadoHaceS: -1, BorraDatos: len(n.carpetasAlBorrar(sg)) > 0}
	if !sg.Usado.IsZero() {
		m.UsadoHaceS = int(time.Since(sg.Usado).Seconds())
	}
	if s, _, err := sala.CargarAnfitrion(n.dirAnfitrion(sg), false, false); err == nil && existeFichero(filepath.Join(n.dirAnfitrion(sg), "identidad.key")) {
		m.Huella = s.Huella()
		m.Form.TieneClave = sg.Clave.claveServidor(s.PeerID().String()) != nil
	}
	if enMarchaID == sg.ID && e.EnMarcha {
		m.EnMarcha, m.Jugadores = true, e.Jugadores
		if e.Max > 0 {
			m.Max = e.Max
		}
	}
	m.Form.Contrasena = ""
	return m
}

func existeFichero(p string) bool { st, err := os.Stat(p); return err == nil && st.Mode().IsRegular() }

// MisServidores: las tarjetas de «My servers» (en el orden en que se crearon; el de antes, el primero).
func (n *Nucleo) MisServidores() []MiServidor {
	e := n.l.EstadoServidor()
	n.mu.Lock()
	id := n.srvID
	n.mu.Unlock()
	ss := n.aj.leer().Servidores
	r := make([]MiServidor, 0, len(ss))
	for _, sg := range ss {
		r = append(r, n.miServidor(sg, id, e))
	}
	return r
}

// FormNuevo: el formulario de «New server» (los valores por defecto).
func (n *Nucleo) FormNuevo() FormServidor { return FormPorDefecto() }

// FormServidor: el formulario del servidor en marcha (o del primero de la lista; o el de por defecto si no hay ninguno).
func (n *Nucleo) FormServidor() FormServidor {
	n.mu.Lock()
	id := n.srvID
	n.mu.Unlock()
	ss := n.aj.leer().Servidores
	for _, sg := range ss {
		if sg.ID == id {
			return n.miServidor(sg, "", sala.EstadoServidor{}).Form
		}
	}
	if len(ss) > 0 {
		return n.miServidor(ss[0], "", sala.EstadoServidor{}).Form
	}
	return FormPorDefecto()
}

// ---------------------------------------------------------------- crear, editar, borrar

func errNoExiste() error {
	return errM("msg.srv.no_existe", "Ese servidor ya no está en «My servers».")
}

// nombreLibre: dos servidores no pueden llamarse igual (sin contar mayúsculas): Delete pide escribir el nombre.
func nombreLibre(ss []ServidorGuardado, nombre, salvo string) error {
	for _, s := range ss {
		if s.ID != salvo && strings.EqualFold(strings.TrimSpace(s.Form.Nombre), strings.TrimSpace(nombre)) {
			return errM("msg.srv.nombre_repetido", fmt.Sprintf("Ya tienes un servidor llamado «%s»: elige otro nombre.", strings.TrimSpace(nombre)),
				"nombre", strings.TrimSpace(nombre))
		}
	}
	return nil
}

// derivarClave: la contraseña nueva del modo «Password» como clave del servidor (sal nueva) y su copia protegida para
// lanzador.json, ligadas a la identidad s.
func derivarClave(s *sala.Sala, contrasena string) (*sala.ClaveServidor, *ClaveGuardada, error) {
	cs, err := sala.NuevaClaveServidor(contrasena, s.Pub())
	if err != nil {
		return nil, nil, errMc(err, "msg.form.clave_rara", "La contraseña lleva caracteres que no valen (de control o invisibles).")
	}
	g, err := guardarClave(cs.Ver, cs.Sal, cs.Clave, s.PeerID().String())
	return cs, g, err
}

// CrearMiServidor: «New server». Crea su carpeta y su identidad NUEVA (su propio código de sala y su peer ID: su propia
// fila en la lista); no lo arranca.
func (n *Nucleo) CrearMiServidor(f FormServidor) (MiServidor, error) {
	f = f.Normalizado()
	f.Nombre = strings.TrimSpace(f.Nombre)
	f.TieneClave = false // uno nuevo no tiene contraseña guardada: en modo «Password» hay que escribirla
	if err := f.Validar(); err != nil {
		return MiServidor{}, err
	}
	if err := nombreLibre(n.aj.leer().Servidores, f.Nombre, ""); err != nil {
		return MiServidor{}, err
	}
	sg := ServidorGuardado{ID: nuevoIDServidor(), Creado: time.Now()}
	dir := n.dirPropio(sg)
	fallo := func(err error) (MiServidor, error) {
		os.RemoveAll(dir) // recién creada por nosotros: vacía o con la identidad que se acaba de generar
		return MiServidor{}, errGen(err)
	}
	if err := os.MkdirAll(filepath.Join(n.dirDatos(sg), "logs"), 0o700); err != nil {
		return fallo(err)
	}
	s, err := peerDe(n.dirAnfitrion(sg))
	if err != nil {
		return fallo(err)
	}
	if f.Modo == ModoClave {
		_, g, err := derivarClave(s, f.Contrasena)
		if err != nil {
			return fallo(err)
		}
		sg.Clave = g
	}
	f.Contrasena = ""
	sg.Form = f
	if err := n.aj.cambiar(func(a *Ajustes) { a.Servidores = append(a.Servidores, sg) }); err != nil {
		return fallo(err)
	}
	n.logf("[servidores] creado %s «%s» (%s, %s; anfitrión %s)", sg.ID, f.Nombre, f.Reglas, f.Modo, s.Huella())
	return n.miServidor(sg, "", sala.EstadoServidor{}), nil
}

func (n *Nucleo) guardado(id string) (ServidorGuardado, bool) {
	for _, s := range n.aj.leer().Servidores {
		if s.ID == id {
			return s, true
		}
	}
	return ServidorGuardado{}, false
}

func (n *Nucleo) enMarchaID() string {
	if !n.l.EstadoServidor().EnMarcha {
		return ""
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.srvID
}

// EditarMiServidor: «Edit». Todo menos las reglas (fijas desde que se creó). Con el servidor parado. Contraseña vacía en
// modo «Password» = la guardada.
func (n *Nucleo) EditarMiServidor(id string, f FormServidor) error {
	sg, ok := n.guardado(id)
	if !ok {
		return errNoExiste()
	}
	if n.enMarchaID() == id {
		return errM("msg.srv.editar_en_marcha", "Para cambiar este servidor, páralo antes.")
	}
	f = f.Normalizado()
	f.Nombre = strings.TrimSpace(f.Nombre)
	if f.Reglas != sg.Form.Reglas {
		return errM("msg.srv.reglas_fijas", "Las reglas de un servidor no se pueden cambiar. Crea otro servidor para probar las otras.")
	}
	s, err := peerDe(n.dirAnfitrion(sg))
	if err != nil {
		return errGen(err)
	}
	f.TieneClave = sg.Clave.claveServidor(s.PeerID().String()) != nil
	if err := f.Validar(); err != nil {
		return err
	}
	if err := nombreLibre(n.aj.leer().Servidores, f.Nombre, id); err != nil {
		return err
	}
	var nueva *ClaveGuardada
	if f.Modo == ModoClave && f.Contrasena != "" {
		if _, nueva, err = derivarClave(s, f.Contrasena); err != nil {
			return errGen(err)
		}
	}
	f.Contrasena, f.TieneClave = "", false
	return errGen(n.aj.cambiar(func(a *Ajustes) {
		for i := range a.Servidores {
			if a.Servidores[i].ID == id {
				a.Servidores[i].Form = f
				if nueva != nil {
					a.Servidores[i].Clave = nueva
				}
			}
		}
	}))
}

// BorrarMiServidor: «Delete». Hay que escribir su nombre; nunca el que está en marcha. Su carpeta va a la papelera (si
// Windows no deja, se aparta a servidores\_borrados); si no se puede ni eso, no se borra nada.
func (n *Nucleo) BorrarMiServidor(id, confirmacion string) error {
	sg, ok := n.guardado(id)
	if !ok {
		return errNoExiste()
	}
	if strings.TrimSpace(confirmacion) != strings.TrimSpace(sg.Form.Nombre) {
		return errM("msg.srv.borrar_nombre", "Escribe el nombre exacto del servidor para borrarlo.")
	}
	if n.enMarchaID() == id {
		return errM("msg.srv.borrar_en_marcha", "No se puede borrar el servidor que está en marcha: páralo antes.")
	}
	for _, d := range n.carpetasAlBorrar(sg) {
		if err := n.aPapelera(d); err != nil {
			return errMc(err, "msg.srv.borrar_fallo", "No pude mover la carpeta del servidor a la papelera: "+err.Error(), "error", err.Error())
		}
		n.logf("[servidores] %s «%s»: %s a la papelera", sg.ID, sg.Form.Nombre, d)
	}
	n.logf("[servidores] borrado %s «%s»", sg.ID, sg.Form.Nombre)
	return errGen(n.aj.cambiar(func(a *Ajustes) {
		r := a.Servidores[:0]
		for _, s := range a.Servidores {
			if s.ID != id {
				r = append(r, s)
			}
		}
		a.Servidores = r
	}))
}

// aPapelera: la papelera de Windows (Config.Papelera en las pruebas); si falla, se aparta a servidores\_borrados\.
func (n *Nucleo) aPapelera(dir string) error {
	f := n.cfg.Papelera
	if f == nil {
		f = moverAPapelera
	}
	err := f(dir)
	if err == nil {
		return nil
	}
	n.logf("[servidores] la papelera no acepta %s (%v): se aparta a servidores\\_borrados", dir, err)
	dst := filepath.Join(n.cfg.Datos, "servidores", "_borrados", filepath.Base(dir)+"-"+time.Now().Format("20060102-150405"))
	if e := os.MkdirAll(filepath.Dir(dst), 0o700); e != nil {
		return err
	}
	if e := os.Rename(dir, dst); e != nil {
		return fmt.Errorf("%v; tampoco se puede apartar: %w", err, e)
	}
	return nil
}

// ---------------------------------------------------------------- arrancar

// ArrancarMiServidor: «Start» en una tarjeta. Solo uno a la vez: si hay otro en marcha, error (la página pregunta antes
// si se para el que está).
func (n *Nucleo) ArrancarMiServidor(id string) error { return n.arrancarGuardado(id, nil) }

// ArrancarServidor: lo de antes de «My servers» (un formulario y «Start»), para las pruebas y una página vieja: arranca el
// servidor guardado con ese nombre (aplicándole el formulario; las reglas no cambian) o, si no hay, uno nuevo.
func (n *Nucleo) ArrancarServidor(f FormServidor) error {
	f = f.Normalizado()
	if err := f.Validar(); err != nil {
		return err
	}
	if n.l.EstadoServidor().EnMarcha {
		return errM("msg.err.srv_ya_en_marcha", "Tu servidor ya está en marcha.")
	}
	id := ""
	for _, s := range n.aj.leer().Servidores {
		if strings.EqualFold(strings.TrimSpace(s.Form.Nombre), strings.TrimSpace(f.Nombre)) {
			id = s.ID
		}
	}
	if id == "" {
		m, err := n.CrearMiServidor(f)
		if err != nil {
			return err
		}
		id = m.ID
	} else if err := n.EditarMiServidor(id, f); err != nil {
		return err
	}
	return n.arrancarGuardado(id, nil)
}

func (n *Nucleo) arrancarGuardado(id string, clave *sala.ClaveServidor) error {
	sg, ok := n.guardado(id)
	if !ok {
		return errNoExiste()
	}
	f := sg.Form.Normalizado()
	if juego.ProcesoActivo("OpenServer.exe") {
		return errM("msg.err.openserver_abierto", "OpenServer.exe está abierto en este PC y usa la misma identidad de anfitrión: ciérralo antes de crear el servidor desde aquí (si no, los dos se pisarían).")
	}
	if e := n.l.EstadoServidor(); e.EnMarcha {
		n.mu.Lock()
		cur := n.srvID
		n.mu.Unlock()
		if cur == id {
			return errM("msg.err.srv_ya_en_marcha", "Tu servidor ya está en marcha.")
		}
		nom := ""
		if c, ok := n.guardado(cur); ok {
			nom = c.Form.Nombre
		}
		return errM("msg.srv.otro_en_marcha", fmt.Sprintf("Ya está en marcha «%s»: solo puede haber un servidor a la vez en este PC (comparten los puertos del juego). Páralo antes.", nom),
			"nombre", nom)
	}
	gate := net.JoinHostPort("127.0.0.1", strconv.Itoa(5730+n.cfg.Offset))
	runpy := ""
	var pq *sala.Paquete // P5: servidor desde el paquete del Discord (server\ junto al exe; nucleo/paquete.go)
	var aviso Msg
	if sala.Escucha(gate) {
		// un run.py que no arrancó este lanzador: tiene SU mundo (el de quien lo arrancó). Solo el servidor de antes, que
		// es el que se usaba así (run.py a mano y el lanzador encima), puede ponerse delante; uno nuevo mezclaría mundos.
		if !sg.Legado && !n.cfg.RunPyAjeno {
			return errM("msg.srv.runpy_ajeno_nuevo", fmt.Sprintf("Ya hay un servidor del juego (run.py) en marcha en este PC que no arrancó este lanzador. «%s» tiene su propio mundo: cierra ese run.py antes de arrancarlo.", f.Nombre),
				"nombre", f.Nombre)
		}
		aviso = sala.M("msg.srv.runpy_ajeno", "Ya había un run.py en marcha en este PC: se usa ese. Las reglas y las cuentas del formulario no se le aplican (siguen siendo las suyas) y «Parar» no lo cierra.")
	} else {
		runpy = os.Getenv("YO2_RUNPY")
		if runpy == "" {
			runpy = sala.BuscarRunPy("", n.cfg.ExeDir)
		}
		var errPq error
		if pq, runpy, errPq = n.paqueteSiFalta(runpy); errPq != nil {
			return errPq
		}
		if runpy == "" {
			return errM("msg.err.no_runpy", "No encuentro run.py (el servidor del juego). YO2OnlineLauncher.exe tiene que estar dentro de la carpeta tools\\yo2emu del proyecto, junto a run.py.")
		}
		if pq == nil && !sala.HayPython() {
			return errM("msg.err.no_python", "No encuentro Python en este PC, y run.py lo necesita para arrancar.")
		}
	}
	o := f.OpcServidor(n.cfg.Offset)
	o.DirAnfitrion, o.DirDatos = n.dirAnfitrion(sg), n.dirDatos(sg)
	s, err := peerDe(o.DirAnfitrion)
	if err != nil {
		return errGen(err)
	}
	if f.Modo == ModoClave {
		if clave == nil {
			clave = sg.Clave.claveServidor(s.PeerID().String())
		}
		if clave == nil {
			return errM("msg.srv.clave_falta", "Este servidor pide contraseña y no hay ninguna guardada: pulsa «Edit» y escríbela.")
		}
		o.Clave = clave
	}
	logsRunPy := filepath.Join(n.cfg.Datos, "logs") // el de antes: donde siempre
	if o.DirDatos != "" {
		// su propio mundo: estado y logs en su carpeta (en el paquete lo pone pq.Ajustar con o.DirDatos)
		logsRunPy = filepath.Join(o.DirDatos, "logs")
		if err := os.MkdirAll(logsRunPy, 0o700); err != nil {
			return errGen(err)
		}
		if pq == nil {
			o.Env = append(o.Env, "YO2_STATE="+filepath.Join(o.DirDatos, "state.json"), "YO2_LOGDIR="+logsRunPy)
		}
		if err := escribirRegalosInicio(o.DirDatos, f.RegalosInicio); err != nil {
			return errGen(err)
		}
	}
	o.RunPy = runpy
	pq.Ajustar(&o, n.cfg.Datos) // P5: nil = nada (desarrollo)
	o.Catalogo = &sala.Catalogo{Logf: n.cfg.Logf, Lanzador: buscarJoinServer(n.cfg.ExeDir), LanzadorApp: n.cfg.Exe}
	o.Aviso = func(m string) { n.logf("[servidor] %s", m) }
	if runpy != "" {
		if err := os.MkdirAll(logsRunPy, 0o700); err == nil {
			podarLogs(filepath.Join(logsRunPy, "run-py-*.log"), 10)
			if lf, err := os.Create(filepath.Join(logsRunPy, "run-py-"+time.Now().Format("20060102-150405")+".log")); err == nil {
				o.SalidaRunPy = lf
				// el proceso hijo hereda su propia copia del manejador al arrancar: la nuestra se cierra al volver de CrearServidor
				defer lf.Close()
			}
		}
	}
	if n.cfg.AjustarServidor != nil {
		n.cfg.AjustarServidor(&o)
	}
	n.logf("[servidores] arrancando %s «%s» (%s; anfitrión %s; datos %s)", sg.ID, f.Nombre, f.Reglas, s.Huella(),
		map[bool]string{true: "los de siempre", false: o.DirDatos}[o.DirDatos == ""])
	if err := n.l.CrearServidor(context.Background(), o); err != nil {
		return errGen(err)
	}
	n.mu.Lock()
	n.srvID, n.avisoSrv = id, aviso
	n.mu.Unlock()
	n.aj.cambiar(func(a *Ajustes) {
		for i := range a.Servidores {
			if a.Servidores[i].ID == id {
				a.Servidores[i].Usado = time.Now()
			}
		}
	})
	go func() { // por qué no se reparte la autoactualización (como hace OpenServer al arrancar), en el log
		m := o.Catalogo.Manifiesto()
		if o.Catalogo.ProblemaLanzadorApp != "" {
			n.logf("[servidor] autoactualización: NO se reparte el lanzador: %s", o.Catalogo.ProblemaLanzadorApp)
		} else if m.LanzadorApp() != nil {
			n.logf("[servidor] autoactualización: se reparte el lanzador versión %d", m.LanzadorApp().Firma.Version)
		}
	}()
	return nil
}

func escribirRegalosInicio(dir string, r RegalosInicio) error {
	if r.MP == 0 && r.DPP == 0 && (r.Baul == "" || r.Baul == "empty") {
		// El fichero es opcional: eliminarlo conserva exactamente el comportamiento
		// histórico y evita dejar configuración obsoleta en servidores antiguos.
		_ = os.Remove(filepath.Join(dir, "admin_regalos.json"))
		return nil
	}
	b, err := json.MarshalIndent(map[string]any{"mp": r.MP, "dpp": r.DPP, "baul": r.Baul}, "", "  ")
	if err != nil {
		return err
	}
	p := filepath.Join(dir, "admin_regalos.json")
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}
