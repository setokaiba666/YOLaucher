package nucleo

// Cliente de la API de gestión de run.py (yo2srv/admin/gestion.py): la pestaña «Manage» de «Host a server». Habla con
// http://127.0.0.1:<8791+offset> con el token de %LOCALAPPDATA%\YO2Online\gestion_<puerto>.txt (el que run.py escribe en cada
// arranque; con YO2_LANZADOR_DATOS se mira también la carpeta de datos del lanzador, igual que el token de la consola de
// parada). Solo se usa mientras el servidor propio de este lanzador está en marcha (Nucleo.gestion).
//
// Reglas: el token y las contraseñas nunca van al log ni a un mensaje de error. Los errores del servidor traen una clave
// estable ({"error": "no_conectado", ...}) que aquí se convierte en un mensaje con clave para la ventana (msg.gestion.*);
// la ventana los escribe en inglés. Las acciones que cambian algo dejan UNA línea en el log (ruta y resultado, sin cuerpo).

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// PuertoGestion: el de la API de gestión de run.py (8791 + YO2_PORT_OFFSET; YO2_GESTION_PORT lo cambia).
func PuertoGestion(offset int) int {
	if v, err := strconv.Atoi(os.Getenv("YO2_GESTION_PORT")); err == nil && v > 0 {
		return v
	}
	return 8791 + offset
}

// Gestion: el cliente. El valor cero no sirve: usar NuevaGestion.
type Gestion struct {
	Offset int
	Datos  string // carpeta de datos del lanzador (segundo sitio donde se busca el token)
	Local  string // donde run.py deja gestion_<puerto>.txt: %LOCALAPPDATA%\YO2Online (vacío = se saca del entorno)
	Base   string // «http://127.0.0.1:puerto»; vacío = el de PuertoGestion (las pruebas ponen un servidor falso)
	Client *http.Client
	Logf   func(string, ...any)

	mu  sync.Mutex
	tok string
}

func NuevaGestion(offset int, datos string, logf func(string, ...any)) *Gestion {
	return &Gestion{Offset: offset, Datos: datos, Logf: logf}
}

func (g *Gestion) logf(f string, a ...any) {
	if g.Logf != nil {
		g.Logf(f, a...)
	}
}

func (g *Gestion) base() string {
	if g.Base != "" {
		return strings.TrimRight(g.Base, "/")
	}
	return "http://" + net.JoinHostPort("127.0.0.1", strconv.Itoa(PuertoGestion(g.Offset)))
}

func (g *Gestion) puerto() int {
	if g.Base != "" {
		if i := strings.LastIndex(g.Base, ":"); i >= 0 {
			if p, err := strconv.Atoi(strings.TrimRight(g.Base[i+1:], "/")); err == nil {
				return p
			}
		}
	}
	return PuertoGestion(g.Offset)
}

func (g *Gestion) cliente() *http.Client {
	if g.Client != nil {
		return g.Client
	}
	return &http.Client{
		Timeout:       10 * time.Second,
		Transport:     &http.Transport{Proxy: nil, DisableKeepAlives: true}, // 127.0.0.1: nunca por un proxy
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// leerToken lee el token de run.py (la carpeta de run.py primero, luego la de datos del lanzador). ok=false si no existe
// o no es un token razonable. No lo guarda en ningún sitio más que en la memoria de g.
func (g *Gestion) leerToken() (string, bool) {
	local := g.Local
	if local == "" {
		if la := os.Getenv("LOCALAPPDATA"); la != "" {
			local = filepath.Join(la, "YO2Online")
		}
	}
	nombre := fmt.Sprintf("gestion_%d.txt", g.puerto())
	for _, d := range []string{local, g.Datos} {
		if d == "" {
			continue
		}
		b, err := os.ReadFile(filepath.Join(d, nombre))
		if err != nil {
			continue
		}
		t := strings.TrimSpace(string(b))
		if t == "" || len(t) > 200 || strings.ContainsAny(t, "\r\n \t") {
			continue
		}
		return t, true
	}
	return "", false
}

func (g *Gestion) token(renovar bool) (string, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.tok != "" && !renovar {
		return g.tok, true
	}
	t, ok := g.leerToken()
	g.tok = t
	return t, ok
}

// Olvidar descarta el token en memoria (el servidor propio se paró o se arrancó otro).
func (g *Gestion) Olvidar() {
	g.mu.Lock()
	g.tok = ""
	g.mu.Unlock()
}

// errGestion: un error del servidor (clave estable) como mensaje con clave para la ventana.
func errGestion(status int, clave, mensaje string) error {
	switch clave {
	case "no_conectado":
		return errM("msg.gestion.no_conectado", "Ese jugador ya no está conectado.")
	case "dueno":
		return errM("msg.gestion.dueno", "La cuenta del dueño no se puede expulsar, vetar ni borrar.")
	case "no_existe":
		return errM("msg.gestion.no_existe", "Esa cuenta ya no existe.")
	case "cuenta_invalida", "ambigua":
		return errM("msg.gestion.cuenta_invalida", "No se reconoce esa cuenta.")
	case "no_vetada":
		return errM("msg.gestion.no_vetada", "Esa cuenta no está vetada.")
	case "aviso_vacio":
		return errM("msg.gestion.aviso_vacio", "Escribe el aviso.")
	case "entrada":
		return errM("msg.gestion.entrada", "Datos no válidos: usuario y contraseña de 1 a 16 caracteres, avisos y motivos de hasta 200.")
	case "ya_existe", "usuario_repetido":
		return errM("msg.gestion.ya_existe", "Ya hay una cuenta con ese usuario (o con esa combinación de usuario y contraseña).")
	case "tope":
		return errM("msg.gestion.tope", "El servidor ya tiene el máximo de cuentas.")
	case "usuario_otro":
		return errM("msg.gestion.usuario_otro", "Ese no es el usuario de esa cuenta.")
	case "sin_confirmar":
		return errM("msg.gestion.sin_confirmar", "Falta la confirmación.")
	case "conectada":
		return errM("msg.gestion.conectada", "Esa cuenta está conectada: expúlsala antes.")
	case "con_personajes":
		return errM("msg.gestion.con_personajes", "Esa cuenta tiene personajes: solo se borran cuentas vacías.")
	case "sin_cifrado":
		return errM("msg.gestion.sin_cifrado", "Al servidor le falta el módulo de cuentas.")
	case "bloqueado":
		return errM("msg.gestion.bloqueado", "Demasiados intentos con token malo: espera un minuto.")
	case "nivel":
		return errM("msg.gestion.nivel", fmt.Sprintf("El nivel va de 1 a %d.", nivelMax), "max", nivelMax)
	case "puntos":
		return errM("msg.gestion.puntos", fmt.Sprintf("Los puntos van de 0 a %d (al sumar, también negativos).", puntosMax), "max", puntosMax)
	case "modo":
		return errM("msg.gestion.modo", "Elige poner o sumar.")
	case "hueco":
		return errM("msg.gestion.hueco", "Esa cuenta tiene varios personajes: elige cuál.")
	case "sin_personaje":
		return errM("msg.gestion.sin_personaje", "Ese personaje ya no existe.")
	case "sin_token":
		return errM("msg.gestion.token_malo", "run.py no acepta el token (¿se reinició el servidor?). Vuelve a abrir la pestaña.")
	case "no_duelpass":
		return errM("msg.gestion.no_duelpass", "El servidor no tiene instalado el módulo DUELPASS.")
	case "no_torneos":
		return errM("msg.gestion.no_torneos", "El servidor no tiene instalado el módulo de torneos.")
	case "no_eventos":
		return errM("msg.gestion.no_eventos", "El servidor no tiene instalado el motor de eventos.")
	case "evento_regla":
		return errM("msg.gestion.evento_regla", mensaje)
	case "duelpass_no_existe":
		return errM("msg.gestion.duelpass_no_existe", "Ese código DUELPASS no existe.")
	case "duelpass_carta", "duelpass_mazo":
		return errM("msg.gestion.duelpass_recompensa", "La carta o el starter deck no es válido.")
	case "duelpass_entrada", "torneo_entrada":
		return errM("msg.gestion.entrada", "Los datos de la operación no son válidos.")
	}
	return errM("msg.gestion.error", fmt.Sprintf("La API de gestión contestó %d (%s).", status, clave), "codigo", status, "clave", clave)
}

var (
	errSinToken = func() error {
		return errM("msg.gestion.sin_token", "No encuentro el token de la API de gestión: este run.py es anterior a la pestaña «Manage» o aún no ha arrancado.")
	}
	errNoResp = func(causa error) error {
		return errMc(causa, "msg.gestion.no_responde", "La API de gestión de run.py no contesta: "+causa.Error(), "error", causa.Error())
	}
	errSinServer = func() error { return errM("msg.gestion.sin_servidor", "Tu servidor no está en marcha.") }
)

// llamar hace una petición; si out != nil decodifica en él la respuesta. Un 401 se reintenta una vez con el token releído.
func (g *Gestion) llamar(metodo, ruta string, cuerpo any, out any) error {
	var datos []byte
	if cuerpo != nil {
		var err error
		if datos, err = json.Marshal(cuerpo); err != nil {
			return errGen(err)
		}
	}
	renovar := false
	for intento := 0; ; intento++ {
		tok, ok := g.token(renovar)
		if !ok {
			return errSinToken()
		}
		var rd io.Reader
		if datos != nil {
			rd = bytes.NewReader(datos)
		}
		req, err := http.NewRequest(metodo, g.base()+ruta, rd)
		if err != nil {
			return errGen(err)
		}
		req.Header.Set("Authorization", "Bearer "+tok)
		if datos != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := g.cliente().Do(req)
		if err != nil {
			return errNoResp(err)
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if resp.StatusCode == 401 && intento == 0 {
			renovar = true // run.py se reinició y tiene otro token
			continue
		}
		if resp.StatusCode/100 != 2 {
			var e struct {
				Error   string `json:"error"`
				Mensaje string `json:"mensaje"`
			}
			json.Unmarshal(body, &e)
			g.logf("[gestion] %s %s -> %d %s", metodo, ruta, resp.StatusCode, e.Error)
			return errGestion(resp.StatusCode, e.Error, e.Mensaje)
		}
		if metodo != "GET" {
			g.logf("[gestion] %s %s -> ok", metodo, ruta)
		}
		if out != nil {
			if err := json.Unmarshal(body, out); err != nil {
				return errMc(err, "msg.gestion.respuesta_mala", "La API de gestión contestó algo que no se entiende.")
			}
		}
		return nil
	}
}

// ---------------------------------------------------------------- datos (lo que ve la ventana; json en minúscula)

type JugadorG struct {
	Corto  string `json:"corto"`
	Nombre string `json:"nombre"`
	Dueno  bool   `json:"dueno"`
	Estado string `json:"estado"` // duelo | sala | mapa | menu
	Mundo  string `json:"mundo"`  // Domino | Academia | ""
	Zona   string `json:"zona"`
	DesdeS int    `json:"desde_s"` // -1 = no se sabe
	Vetado bool   `json:"vetado"`
	Hueco  int    `json:"hueco"` // personaje con que está conectado (0-3); -1 = aún no ha elegido
	Nivel  int    `json:"nivel"` // su LV; -1 = no se sabe (sin perfil todavía, run.py anterior)
	DP     int    `json:"dp"`    // sus puntos DUELPASS; -1 = no se sabe (sin perfil todavía, run.py anterior al 12-10)
	MP     int    `json:"mp"`    // sus MP (tienda); -1 = no se sabe
}

// FichaG: un personaje de una cuenta (pestaña Accounts).
type FichaG struct {
	Hueco  int    `json:"hueco"`
	Nombre string `json:"nombre"`
	Nivel  int    `json:"nivel"` // -1 = no se sabe (personaje sin perfil todavía)
	DP     int    `json:"dp"`    // puntos DUELPASS; -1 = no se sabe
	MP     int    `json:"mp"`    // -1 = no se sabe
}

// PuntosG: lo que contesta /gestion/dp (antes = solo lo que se tocó).
type PuntosG struct {
	Antes     map[string]int `json:"antes"`
	Ahora     map[string]int `json:"ahora"` // {"dp": n, "mp": n}
	AlMomento bool           `json:"al_momento"`
	EnLinea   bool           `json:"en_linea"`
}

// puntosMax: tope de DP y MP en run.py (moderacion.POINTS_MAX).
const puntosMax = 9999999

// NivelG: lo que contesta /gestion/nivel.
type NivelG struct {
	Antes     int  `json:"antes"`
	Ahora     int  `json:"ahora"`
	AlMomento bool `json:"al_momento"` // el jugador estaba conectado con ese personaje y fuera de un duelo: lo ve ya
	EnLinea   bool `json:"en_linea"`
}

type VetoG struct {
	Corto   string   `json:"corto"`
	Nombres []string `json:"nombres"`
	Motivo  string   `json:"motivo"`
	Fecha   string   `json:"fecha"`
}

type CuentaG struct {
	Corto      string   `json:"corto"`
	Usuario    string   `json:"usuario"` // vacío = cuenta anterior a la gestión (el usuario no está guardado)
	Dueno      bool     `json:"dueno"`
	Personajes []string `json:"personajes"`
	Fichas     []FichaG `json:"fichas"` // los mismos personajes con hueco y nivel (run.py anterior al 12-10: vacío)
	EnLinea    bool     `json:"en_linea"`
	Vetado     bool     `json:"vetado"`
}

type CuentasG struct {
	Estricto bool      `json:"estricto"` // true = «Only accounts I create»; false = registro abierto
	Max      int       `json:"max"`      // largo máximo de usuario y contraseña
	Cuentas  []CuentaG `json:"cuentas"`
}

type CopiaG struct {
	Fichero string `json:"fichero"`
	KB      int    `json:"kb"`
	Fecha   string `json:"fecha"`
}

type DuelpassG struct {
	Codigo string `json:"codigo"`
	Estado string `json:"estado"`
	DPP    int    `json:"dpp"`
	Carta  int    `json:"carta"`
	Mazo   int    `json:"mazo"`
	Creado string `json:"creado"`
	Canje  any    `json:"canje"`
	Nota   string `json:"nota"`
}

type DuelpassCartaG struct {
	ID     int    `json:"id"`
	Nombre string `json:"nombre"`
}

type DuelpassMazoG struct {
	ID     int    `json:"id"`
	Nombre string `json:"nombre"`
	Bit    int    `json:"bit"`
}

type EventoTorneoG struct {
	Evento      int            `json:"evento"`
	Familia     string         `json:"familia"`
	Nombre      string         `json:"nombre"`
	Cuadro      bool           `json:"cuadro"`
	Modalidad   string         `json:"modalidad"`
	Contador    string         `json:"contador"`
	Descripcion string         `json:"descripcion"`
	Confirmado  bool           `json:"confirmado"`
	Activo      bool           `json:"activo"`
	Milestones  []any          `json:"milestones"`
	Recompensas map[string]int `json:"recompensas"`
}

type SesionEventoG struct {
	Familia  string                    `json:"familia"`
	Serial   int                       `json:"serial"`
	Estado   string                    `json:"estado"`
	Inicio   int64                     `json:"inicio"`
	Fin      *int64                    `json:"fin"`
	Progreso map[string]map[string]int `json:"progreso"`
}

type EventoActivoG struct {
	Serial int    `json:"serial"`
	World  int    `json:"world"`
	Estado int    `json:"estado"`
	Nombre string `json:"nombre"`
}

type RegalosG struct {
	MP   int    `json:"mp"`
	DPP  int    `json:"dpp"`
	Baul string `json:"baul"`
}

type TorneoG struct {
	Activo       bool           `json:"activo"`
	Serial       int            `json:"serial"`
	Dia          int            `json:"dia"`
	Nombre       string         `json:"nombre"`
	MaxJugadores int            `json:"max_jugadores"`
	Estado       int            `json:"estado"` // -1 = sin cuadro
	Evento       *EventoActivoG `json:"evento"`
	Sesion       *SesionEventoG `json:"sesion"`
	Pausado      bool           `json:"pausado"`
	Fase         int            `json:"fase"`
	Ronda        int            `json:"ronda"`
	Inscritos    int            `json:"inscritos"`
	Vivos        int            `json:"vivos"`
	Espectadores int            `json:"espectadores"`
	Segundos     int            `json:"segundos"`
	Campeon      any            `json:"campeon"`
	Subcampeon   any            `json:"subcampeon"`
}

// oMenos1: un número que puede venir como null (o no venir: run.py anterior) -> -1.
func oMenos1(p *int) int {
	if p == nil {
		return -1
	}
	return *p
}

func (g *Gestion) Jugadores() ([]JugadorG, error) {
	var r struct {
		Jugadores []struct {
			JugadorG
			DesdeS *int `json:"desde_s"`
			Hueco  *int `json:"hueco"`
			Nivel  *int `json:"nivel"`
			DP     *int `json:"dp"`
			MP     *int `json:"mp"`
		} `json:"jugadores"`
	}
	if err := g.llamar("GET", "/gestion/jugadores", nil, &r); err != nil {
		return nil, err
	}
	out := make([]JugadorG, 0, len(r.Jugadores))
	for _, j := range r.Jugadores {
		x := j.JugadorG
		x.DesdeS, x.Hueco, x.Nivel = oMenos1(j.DesdeS), oMenos1(j.Hueco), oMenos1(j.Nivel)
		x.DP, x.MP = oMenos1(j.DP), oMenos1(j.MP)
		out = append(out, x)
	}
	return out, nil
}

// Nivel pone el LV (1-99) de un personaje. hueco < 0 = el que tiene conectado (o el único de la cuenta).
func (g *Gestion) Nivel(corto string, hueco, nivel int) (NivelG, error) {
	c := map[string]any{"acct": corto, "nivel": nivel}
	if hueco >= 0 {
		c["hueco"] = hueco
	}
	var r NivelG
	err := g.llamar("POST", "/gestion/nivel", c, &r)
	return r, err
}

// Puntos pone (modo «poner») o suma (modo «sumar», puede ser negativo) los puntos DUELPASS y/o MP de un personaje.
// nil = no tocar. hueco < 0 = el que tiene conectado (o el único de la cuenta).
func (g *Gestion) Puntos(corto string, hueco int, dp, mp *int, modo string) (PuntosG, error) {
	c := map[string]any{"acct": corto, "modo": modo}
	if hueco >= 0 {
		c["hueco"] = hueco
	}
	if dp != nil {
		c["dp"] = *dp
	}
	if mp != nil {
		c["mp"] = *mp
	}
	var r PuntosG
	err := g.llamar("POST", "/gestion/dp", c, &r)
	return r, err
}

func (g *Gestion) Expulsar(corto string) error {
	return g.llamar("POST", "/gestion/expulsar", map[string]any{"acct": corto}, nil)
}

func (g *Gestion) Vetar(corto, motivo string) error {
	return g.llamar("POST", "/gestion/vetar", map[string]any{"acct": corto, "motivo": motivo}, nil)
}

func (g *Gestion) Vetados() ([]VetoG, error) {
	var r struct {
		Vetados []VetoG `json:"vetados"`
	}
	if err := g.llamar("GET", "/gestion/vetados", nil, &r); err != nil {
		return nil, err
	}
	if r.Vetados == nil {
		r.Vetados = []VetoG{}
	}
	return r.Vetados, nil
}

func (g *Gestion) QuitarVeto(corto string) error {
	return g.llamar("POST", "/gestion/quitar_veto", map[string]any{"acct": corto}, nil)
}

func (g *Gestion) Cuentas() (CuentasG, error) {
	var r struct {
		CuentasG
		Cuentas []struct {
			CuentaG
			Fichas []struct {
				FichaG
				Nivel *int `json:"nivel"`
				DP    *int `json:"dp"`
				MP    *int `json:"mp"`
			} `json:"fichas"`
		} `json:"cuentas"`
	}
	if err := g.llamar("GET", "/gestion/cuentas", nil, &r); err != nil {
		return CuentasG{}, err
	}
	out := r.CuentasG
	out.Cuentas = make([]CuentaG, 0, len(r.Cuentas))
	for _, c := range r.Cuentas {
		x := c.CuentaG
		if x.Personajes == nil {
			x.Personajes = []string{}
		}
		x.Fichas = make([]FichaG, 0, len(c.Fichas))
		for _, f := range c.Fichas {
			y := f.FichaG
			y.Nivel, y.DP, y.MP = oMenos1(f.Nivel), oMenos1(f.DP), oMenos1(f.MP)
			x.Fichas = append(x.Fichas, y)
		}
		out.Cuentas = append(out.Cuentas, x)
	}
	return out, nil
}

func (g *Gestion) CrearCuenta(usuario, clave string) error {
	return g.llamar("POST", "/gestion/crear", map[string]any{"usuario": usuario, "clave": clave}, nil)
}

func (g *Gestion) CambiarClave(corto, clave, usuario string) error {
	c := map[string]any{"acct": corto, "clave": clave}
	if usuario != "" {
		c["usuario"] = usuario
	}
	return g.llamar("POST", "/gestion/cambiar_clave", c, nil)
}

func (g *Gestion) BorrarCuenta(corto string) error {
	return g.llamar("POST", "/gestion/borrar", map[string]any{"acct": corto, "confirmar": true}, nil)
}

// Aviso manda un aviso de chat a todos los conectados; devuelve a cuántos.
func (g *Gestion) Aviso(texto string) (int, error) {
	var r struct {
		EnviadoA int `json:"enviado_a"`
	}
	if err := g.llamar("POST", "/gestion/aviso", map[string]any{"texto": texto}, &r); err != nil {
		return 0, err
	}
	return r.EnviadoA, nil
}

func (g *Gestion) Copia() (string, error) {
	var r struct {
		Fichero string `json:"fichero"`
	}
	if err := g.llamar("POST", "/gestion/copia", map[string]any{}, &r); err != nil {
		return "", err
	}
	return r.Fichero, nil
}

func (g *Gestion) Copias() ([]CopiaG, error) {
	var r struct {
		Copias []CopiaG `json:"copias"`
	}
	if err := g.llamar("GET", "/gestion/copias", nil, &r); err != nil {
		return nil, err
	}
	if r.Copias == nil {
		r.Copias = []CopiaG{}
	}
	return r.Copias, nil
}

func (g *Gestion) Duelpass() ([]DuelpassG, error) {
	var r struct {
		Codigos []DuelpassG `json:"codigos"`
	}
	if err := g.llamar("GET", "/gestion/duelpass", nil, &r); err != nil {
		return nil, err
	}
	if r.Codigos == nil {
		r.Codigos = []DuelpassG{}
	}
	return r.Codigos, nil
}

func (g *Gestion) DuelpassCartas() ([]DuelpassCartaG, error) {
	var r struct {
		Cartas []DuelpassCartaG `json:"cartas"`
	}
	err := g.llamar("GET", "/gestion/duelpass/cartas", nil, &r)
	return r.Cartas, err
}

func (g *Gestion) DuelpassMazos() ([]DuelpassMazoG, error) {
	var r struct {
		Mazos []DuelpassMazoG `json:"mazos"`
	}
	err := g.llamar("GET", "/gestion/duelpass/mazos", nil, &r)
	return r.Mazos, err
}

func (g *Gestion) CrearDuelpass(n, dpp, carta, mazo int, nota string) ([]string, error) {
	var r struct {
		Codigos []string `json:"codigos"`
	}
	err := g.llamar("POST", "/gestion/duelpass/crear",
		map[string]any{"n": n, "dpp": dpp, "carta": carta, "mazo": mazo, "nota": nota}, &r)
	return r.Codigos, err
}

func (g *Gestion) AnularDuelpass(codigo string) error {
	return g.llamar("POST", "/gestion/duelpass/anular", map[string]any{"codigo": codigo}, nil)
}

func (g *Gestion) RegalosInicio() (RegalosG, error) {
	var r struct {
		Regalos RegalosG `json:"regalos"`
	}
	err := g.llamar("GET", "/gestion/duelpass/inicio", nil, &r)
	return r.Regalos, err
}

func (g *Gestion) GuardarRegalosInicio(r RegalosG) (RegalosG, error) {
	var out struct {
		Regalos RegalosG `json:"regalos"`
	}
	err := g.llamar("POST", "/gestion/duelpass/inicio", map[string]any{"mp": r.MP, "dpp": r.DPP, "baul": r.Baul}, &out)
	return out.Regalos, err
}

func (g *Gestion) Torneo() (TorneoG, error) {
	var r TorneoG
	err := g.llamar("GET", "/gestion/torneo", nil, &r)
	return r, err
}

func (g *Gestion) TorneoOpciones() ([]EventoTorneoG, error) {
	var r struct {
		Eventos []EventoTorneoG `json:"eventos"`
	}
	err := g.llamar("GET", "/gestion/torneo/opciones", nil, &r)
	return r.Eventos, err
}

func (g *Gestion) AccionTorneo(accion, nombre string, evento, serial, dia, maxJugadores int) (TorneoG, error) {
	c := map[string]any{"accion": accion}
	if nombre != "" {
		c["nombre"] = nombre
	}
	if evento != 0 {
		c["evento"] = evento
	}
	if serial != 0 {
		c["serial"] = serial
	}
	if dia != 0 {
		c["dia"] = dia
	}
	if maxJugadores != 0 {
		c["max_jugadores"] = maxJugadores
	}
	var r struct {
		Torneo TorneoG `json:"torneo"`
	}
	err := g.llamar("POST", "/gestion/torneo", c, &r)
	return r.Torneo, err
}

func (g *Gestion) GuardarReglaEvento(familia string, activo bool, recompensas map[string]int) (EventoTorneoG, error) {
	var r struct {
		Regla EventoTorneoG `json:"regla"`
	}
	err := g.llamar("POST", "/gestion/eventos/regla",
		map[string]any{"familia": familia, "activo": activo, "recompensas": recompensas}, &r)
	return r.Regla, err
}

// ---------------------------------------------------------------- métodos de la ventana (Nucleo)

// gestion: el cliente de la API de gestión del servidor que ESTE lanzador tiene en marcha (el run.py puede haberlo arrancado
// él o ser uno que ya había: en los dos casos es el servidor que el anfitrión está sirviendo, y la API es de 127.0.0.1).
func (n *Nucleo) gestion() (*Gestion, error) {
	if !n.l.EstadoServidor().EnMarcha {
		return nil, errSinServer()
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.gest == nil {
		if n.cfg.Gestion != nil {
			n.gest = n.cfg.Gestion
		} else {
			n.gest = NuevaGestion(n.cfg.Offset, n.cfg.Datos, n.cfg.Logf)
		}
	}
	return n.gest, nil
}

// con ejecuta f con el cliente; si el servidor no está, el error de siempre.
func (n *Nucleo) conGestion(f func(g *Gestion) error) error {
	g, err := n.gestion()
	if err != nil {
		return err
	}
	return f(g)
}

func (n *Nucleo) GestionJugadores() (r []JugadorG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Jugadores(); return })
	return
}
func (n *Nucleo) GestionExpulsar(corto string) error {
	return n.conGestion(func(g *Gestion) error { return g.Expulsar(corto) })
}
func (n *Nucleo) GestionVetar(corto, motivo string) error {
	return n.conGestion(func(g *Gestion) error { return g.Vetar(corto, motivo) })
}
func (n *Nucleo) GestionVetados() (r []VetoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Vetados(); return })
	return
}
func (n *Nucleo) GestionQuitarVeto(corto string) error {
	return n.conGestion(func(g *Gestion) error { return g.QuitarVeto(corto) })
}
func (n *Nucleo) GestionCuentas() (r CuentasG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Cuentas(); return })
	return
}
func (n *Nucleo) GestionCrearCuenta(usuario, clave string) error {
	return n.conGestion(func(g *Gestion) error { return g.CrearCuenta(usuario, clave) })
}
func (n *Nucleo) GestionCambiarClave(corto, clave, usuario string) error {
	return n.conGestion(func(g *Gestion) error { return g.CambiarClave(corto, clave, usuario) })
}
func (n *Nucleo) GestionBorrarCuenta(corto string) error {
	return n.conGestion(func(g *Gestion) error { return g.BorrarCuenta(corto) })
}
func (n *Nucleo) GestionNivel(corto string, hueco, nivel int) (r NivelG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Nivel(corto, hueco, nivel); return })
	return
}

// GestionPuntos: DP y/o MP de un personaje desde la página (null = no tocar; modo «poner» o «sumar»).
func (n *Nucleo) GestionPuntos(corto string, hueco int, dp, mp *int, modo string) (r PuntosG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Puntos(corto, hueco, dp, mp, modo); return })
	return
}
func (n *Nucleo) GestionAviso(texto string) (r int, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Aviso(texto); return })
	return
}
func (n *Nucleo) GestionCopia() (r string, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Copia(); return })
	return
}
func (n *Nucleo) GestionCopias() (r []CopiaG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Copias(); return })
	return
}

func (n *Nucleo) GestionDuelpass() (r []DuelpassG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Duelpass(); return })
	return
}
func (n *Nucleo) GestionDuelpassCartas() (r []DuelpassCartaG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.DuelpassCartas(); return })
	return
}
func (n *Nucleo) GestionDuelpassMazos() (r []DuelpassMazoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.DuelpassMazos(); return })
	return
}
func (n *Nucleo) GestionCrearDuelpass(cantidad, dpp, carta, mazo int, nota string) (r []string, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.CrearDuelpass(cantidad, dpp, carta, mazo, nota); return })
	return
}
func (n *Nucleo) GestionAnularDuelpass(codigo string) error {
	return n.conGestion(func(g *Gestion) error { return g.AnularDuelpass(codigo) })
}
func (n *Nucleo) GestionRegalosInicio() (r RegalosG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.RegalosInicio(); return })
	return
}
func (n *Nucleo) GestionGuardarRegalosInicio(r RegalosG) (out RegalosG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { out, e = g.GuardarRegalosInicio(r); return })
	return
}
func (n *Nucleo) GestionTorneo() (r TorneoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.Torneo(); return })
	return
}
func (n *Nucleo) GestionTorneoOpciones() (r []EventoTorneoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) { r, e = g.TorneoOpciones(); return })
	return
}
func (n *Nucleo) GestionAccionTorneo(accion, nombre string, evento, serial, dia, maxJugadores int) (r TorneoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) {
		r, e = g.AccionTorneo(accion, nombre, evento, serial, dia, maxJugadores)
		return
	})
	return
}
func (n *Nucleo) GestionGuardarReglaEvento(familia string, activo bool, recompensas map[string]int) (r EventoTorneoG, err error) {
	err = n.conGestion(func(g *Gestion) (e error) {
		r, e = g.GuardarReglaEvento(familia, activo, recompensas)
		return
	})
	return
}
