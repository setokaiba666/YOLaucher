package nucleo

// «Crear servidor»: del formulario a las opciones de sala.OpcServidor y al entorno de run.py.

import (
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"

	"yo2online/sala"
)

// FormServidor: el formulario de la pestaña «Crear servidor» (y lo que se recuerda entre sesiones).
type FormServidor struct {
	Nombre      string `json:"nombre"`
	Descripcion string `json:"descripcion"`
	Max         int    `json:"max"`
	Publica     bool   `json:"publica"` // = Modo != ModoOculto (lo de antes del 12-10: true = público, false = solo código)
	// Modo: «Who can join» (12-10): ModoPublico, ModoClave (en la lista, con contraseña) u ModoOculto (solo con el código).
	Modo string `json:"modo"`
	// Contrasena: SOLO de la página a Go (la que escribe el anfitrión para ponerla o cambiarla; vacía = la de antes). Nunca
	// se guarda ni vuelve a la página: ArrancarServidor la convierte en clave derivada (claves.go) y la borra.
	Contrasena string `json:"contrasena,omitempty"`
	// TieneClave: SOLO de Go a la página: hay una contraseña guardada (la página dice «déjala vacía para no cambiarla»).
	TieneClave bool   `json:"tiene_clave"`
	Reglas     string `json:"reglas"` // sala.ReglasVanilla | sala.ReglasMejorado
	// (12-10: sin «idioma»: el servidor ya no tiene idioma en la interfaz; uno guardado de antes se ignora al leerlo y la
	// ficha lleva "multi" para los lanzadores viejos)
	Cuentas string `json:"cuentas"` // sala.CuentasLibre | sala.CuentasCerrada
	// NivelInicial: LV de los personajes nuevos (YO2_START_LEVEL). 1 = como el juego original (con LV 1 el cliente solo deja
	// entrar en las Salas de Principiantes); 2 o más = los nuevos entran en todas las salas. 0 (formulario de antes) = 1.
	NivelInicial int `json:"nivel_inicial"`
	// PuntosDP: puntos DUELPASS de los personajes nuevos (YO2_DP_INICIO). 10 = como el juego original (Konami regalaba 10 al
	// crear la cuenta; cada duelo cuesta 1 y el cliente no deja retar con menos de 1, o de 3 en match). 0 (formulario de
	// antes) = 10.
	PuntosDP int `json:"dp_inicio"`
	// DuelosGratis: los duelos no gastan puntos DUELPASS (YO2_DP_COSTE=0). No es del original (por eso va apagado). Además
	// cada personaje nuevo recibe sus puntos de inicio (YO2_DP_INICIO_CUENTA=0; en el original solo el primero de cada
	// cuenta): el cliente sigue pidiendo 1 (o 3) para retar en una sala, y así nadie empieza con 0.
	DuelosGratis  bool          `json:"duelos_gratis"`
	Torneos       *bool         `json:"torneos,omitempty"`
	Eventos       *bool         `json:"eventos,omitempty"`
	RelojTorneo   *bool         `json:"reloj_torneo,omitempty"`
	DuelpassAuth  *bool         `json:"duelpass_auth,omitempty"`
	RegalosInicio RegalosInicio `json:"regalos_inicio"`
}

type RegalosInicio struct {
	MP   int    `json:"mp"`
	DPP  int    `json:"dpp"`
	Baul string `json:"baul"` // all3, deck o empty
}

// Modos de «Who can join».
const (
	ModoPublico = "publico" // en la lista, entra cualquiera
	ModoClave   = "clave"   // en la lista con candado: hace falta la contraseña (o el código)
	ModoOculto  = "oculto"  // no sale en la lista: solo con el código de sala (lo que antes era «Private»)
)

// NivelesIniciales: los que ofrece el formulario (el servidor admite 1-99).
var NivelesIniciales = []int{1, 2, 5, 10}

const nivelMax = 99

// PuntosDPIniciales: los que ofrece el formulario (el servidor admite 0 o más; aquí 1-puntosDPMax).
var PuntosDPIniciales = []int{10, 100, 500, 1000}

const (
	puntosDPOriginal = 10
	puntosDPMax      = 1000000
)

// FormPorDefecto: lo que sale la primera vez.
func FormPorDefecto() FormServidor {
	return FormServidor{Max: 30, Publica: true, Modo: ModoPublico, Reglas: sala.ReglasVanilla, Cuentas: sala.CuentasLibre, NivelInicial: 1,
		PuntosDP: puntosDPOriginal, RegalosInicio: RegalosInicio{Baul: "empty"}}
}

// idiomaFicha: lo que va en el campo «idioma» de la ficha firmada. El lanzador ya no pregunta ni enseña idioma (12-10: es
// un juego de cartas, se escribe poco); el campo sigue en el protocolo para los lanzadores viejos, que enseñan «MULTI».
const idiomaFicha = "multi"

// Normalizado: valores raros (de un fichero viejo o a mano) vuelven a los de por defecto.
func (f FormServidor) Normalizado() FormServidor {
	d := FormPorDefecto()
	if f.Modo != ModoPublico && f.Modo != ModoClave && f.Modo != ModoOculto {
		f.Modo = ModoOculto // formulario de antes del 12-10: sale de «publica»
		if f.Publica {
			f.Modo = ModoPublico
		}
	}
	f.Publica = f.Modo != ModoOculto
	if f.Reglas != sala.ReglasVanilla && f.Reglas != sala.ReglasMejorado {
		f.Reglas = d.Reglas
	}
	if f.Cuentas != sala.CuentasLibre && f.Cuentas != sala.CuentasCerrada {
		f.Cuentas = d.Cuentas
	}
	if f.Max <= 0 {
		f.Max = d.Max
	}
	if f.NivelInicial < 1 || f.NivelInicial > nivelMax {
		f.NivelInicial = d.NivelInicial
	}
	if f.PuntosDP < 1 || f.PuntosDP > puntosDPMax {
		f.PuntosDP = d.PuntosDP
	}
	if f.RegalosInicio.MP < 0 || f.RegalosInicio.MP > 1000000 {
		f.RegalosInicio.MP = 0
	}
	if f.RegalosInicio.DPP < 0 || f.RegalosInicio.DPP > 1000000 {
		f.RegalosInicio.DPP = 0
	}
	if f.RegalosInicio.Baul != "all3" && f.RegalosInicio.Baul != "deck" && f.RegalosInicio.Baul != "empty" {
		f.RegalosInicio.Baul = "empty"
	}
	return f
}

// Validar: errores con clave (ErrUI) para enseñar en el formulario; el texto en español va en Msg.Es.
func (f FormServidor) Validar() error {
	n := strings.TrimSpace(f.Nombre)
	if n == "" {
		return errM("msg.form.nombre_vacio", "Pon un nombre al servidor.")
	}
	if utf8.RuneCountInString(n) > 40 {
		return errM("msg.form.nombre_largo", "El nombre es demasiado largo (máximo 40 letras).", "max", 40)
	}
	if utf8.RuneCountInString(f.Descripcion) > 200 {
		return errM("msg.form.desc_larga", "La descripción es demasiado larga (máximo 200 letras).", "max", 200)
	}
	if f.Max < 2 || f.Max > sala.MaxJugadores {
		return errM("msg.form.max_rango", fmt.Sprintf("El máximo de jugadores tiene que estar entre 2 y %d.", sala.MaxJugadores),
			"min", 2, "max", sala.MaxJugadores)
	}
	if f.Reglas != sala.ReglasVanilla && f.Reglas != sala.ReglasMejorado {
		return errM("msg.form.reglas", "Elige las reglas: Vanilla o Mejorado.")
	}
	if f.Modo == ModoClave {
		if err := validarContrasenaForm(f.Contrasena, f.TieneClave); err != nil {
			return err
		}
	}
	if f.Cuentas != sala.CuentasLibre && f.Cuentas != sala.CuentasCerrada {
		return errM("msg.form.cuentas", "Elige cómo se crean las cuentas.")
	}
	if f.NivelInicial < 1 || f.NivelInicial > nivelMax {
		return errM("msg.form.nivel", fmt.Sprintf("El nivel inicial tiene que estar entre 1 y %d.", nivelMax), "max", nivelMax)
	}
	if f.PuntosDP != 0 && (f.PuntosDP < 1 || f.PuntosDP > puntosDPMax) { // 0 = formulario de antes: 10
		return errM("msg.form.dp", fmt.Sprintf("Los puntos Duelpass iniciales tienen que estar entre 1 y %d.", puntosDPMax),
			"max", puntosDPMax)
	}
	if f.RegalosInicio.MP < 0 || f.RegalosInicio.MP > 1000000 || f.RegalosInicio.DPP < 0 || f.RegalosInicio.DPP > 1000000 {
		return errM("msg.form.regalos", "Los regalos iniciales deben estar entre 0 y 1.000.000.")
	}
	if f.RegalosInicio.Baul != "" && f.RegalosInicio.Baul != "all3" && f.RegalosInicio.Baul != "deck" && f.RegalosInicio.Baul != "empty" {
		return errM("msg.form.regalos_baul", "El modo del baúl inicial no es válido.")
	}
	return nil
}

// validarContrasenaForm: la contraseña del formulario (vacía vale solo si ya hay una guardada).
func validarContrasenaForm(c string, hay bool) error {
	if c == "" {
		if hay {
			return nil
		}
		return errM("msg.form.clave_vacia", "Escribe una contraseña para tu servidor.")
	}
	if n := utf8.RuneCountInString(c); n < sala.MinContrasena {
		return errM("msg.form.clave_corta", fmt.Sprintf("La contraseña tiene que tener al menos %d letras.", sala.MinContrasena), "min", sala.MinContrasena)
	} else if n > sala.MaxContrasena {
		return errM("msg.form.clave_larga", fmt.Sprintf("La contraseña puede tener como mucho %d letras.", sala.MaxContrasena), "max", sala.MaxContrasena)
	}
	if err := sala.ValidarContrasena(c); err != nil {
		return errMc(err, "msg.form.clave_rara", "La contraseña lleva caracteres que no valen (de control o invisibles).")
	}
	return nil
}

// MaxCuentas: tope de cuentas guardadas de run.py (YO2_MAX_CUENTAS): lo normal (64) o 3 veces los jugadores si es más.
func (f FormServidor) MaxCuentas() int { return max(64, 3*f.Max) }

// EntornoRunPy: las variables de entorno con que se arranca run.py para este formulario. Reproduce las de «Arrancar
// servidor YO2.cmd» del owner (YO2_STORAGE=sqlite, YO2_ROOM_PER_LANG=1) y añade las del formulario:
//
//	Reglas Vanilla/Mejorado        -> YO2_PERFIL=vanilla|enhanced
//	Cuentas «Registro libre»       -> YO2_LOGIN_ESTRICTO=0 (el primer login crea la cuenta)
//	Cuentas «Solo las que yo cree» -> YO2_LOGIN_ESTRICTO=1 (una cuenta que no existe recibe -240; se crean desde el panel)
//	                               -> YO2_MAX_CUENTAS=max(64, 3 x jugadores)
//	«Starting level» N             -> YO2_START_LEVEL=N (1 = como el original)
//	«Duelpass points for new players» N -> YO2_DP_INICIO=N (10 = como el original)
//	«Free duels»                   -> YO2_DP_COSTE=0 y YO2_DP_INICIO_CUENTA=0 (apagado = como el original: 1 DP por duelo y
//	                                  los puntos de inicio solo para el primer personaje de cada cuenta)
//
// Además YO2_NO_CLIENT=1: run.py NO abre su propio yo2.exe (la ventana abre el juego con «Jugar aquí»; si no, saldrían dos).
func (f FormServidor) EntornoRunPy() []string {
	perfil := "vanilla"
	if f.Reglas == sala.ReglasMejorado {
		perfil = "enhanced"
	}
	estricto := "0"
	if f.Cuentas == sala.CuentasCerrada {
		estricto = "1"
	}
	n := f.Normalizado()
	env := []string{
		"YO2_STORAGE=sqlite",
		"YO2_ROOM_PER_LANG=1",
		"YO2_PERFIL=" + perfil,
		"YO2_LOGIN_ESTRICTO=" + estricto,
		"YO2_MAX_CUENTAS=" + strconv.Itoa(f.MaxCuentas()),
		"YO2_START_LEVEL=" + strconv.Itoa(n.NivelInicial),
		"YO2_DP_INICIO=" + strconv.Itoa(n.PuntosDP),
		"YO2_NO_CLIENT=1",
	}
	if n.DuelosGratis {
		env = append(env, "YO2_DP_COSTE=0", "YO2_DP_INICIO_CUENTA=0")
	}
	if n.Torneos != nil {
		env = append(env, "YO2_TOURNAMENT="+boolEnv(*n.Torneos))
	}
	if n.Eventos != nil {
		env = append(env, "YO2_EVENTS="+boolEnv(*n.Eventos))
	}
	if n.RelojTorneo != nil {
		env = append(env, "YO2_AUTO_CLOCK="+boolEnv(*n.RelojTorneo))
	}
	if n.DuelpassAuth != nil {
		env = append(env, "YO2_DPAUTH="+boolEnv(*n.DuelpassAuth))
	}
	return env
}

func boolEnv(v bool) string {
	if v {
		return "1"
	}
	return "0"
}

// OpcServidor: el formulario como opciones de sala.CrearServidor (sin las partes de la máquina: run.py, catálogo, avisos).
func (f FormServidor) OpcServidor(offset int) sala.OpcServidor {
	return sala.OpcServidor{Nombre: strings.TrimSpace(f.Nombre), Descripcion: strings.TrimSpace(f.Descripcion), Max: f.Max,
		Publica: f.Publica, Reglas: f.Reglas, Idioma: idiomaFicha, Cuentas: f.Cuentas, Offset: offset, Env: f.EntornoRunPy()}
}
