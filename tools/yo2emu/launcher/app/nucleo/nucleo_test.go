package nucleo

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"yo2online/internal/pruebas"
	"yo2online/juego"
	"yo2online/sala"
)

func TestMain(m *testing.M) {
	// Proceso de mentira que hace de run.py: duerme hasta que lo matan.
	if os.Getenv("NUCLEO_TEST_DUERME") == "1" {
		time.Sleep(10 * time.Minute)
		return
	}
	os.Exit(m.Run())
}

func nuevoDePrueba(t *testing.T) (*Nucleo, string) {
	t.Helper()
	datos := pruebas.TempDir(t)
	var eventos []string
	var mu sync.Mutex
	n := Nuevo(Config{Datos: datos, ExeDir: datos, Version: 20261011120000,
		Emitir: func(ev string, d any) { mu.Lock(); eventos = append(eventos, ev); mu.Unlock() }})
	t.Cleanup(n.Cerrar)
	return n, datos
}

// ---------------------------------------------------------------- ajustes

func TestAjustesPersistenYTolerantes(t *testing.T) {
	d := pruebas.TempDir(t)
	al := abrirAlmacen(d)
	if a := al.leer(); !a.Actualizar || len(a.Servidores) != 0 || a.CarpetaJuego != "" || a.V != formatoAjustes {
		t.Fatalf("valores por defecto: %+v", a)
	}
	f := FormServidor{Nombre: "Duelos", Descripcion: "hola", Max: 12, Publica: false, Reglas: sala.ReglasMejorado, Cuentas: sala.CuentasCerrada, NivelInicial: 5,
		PuntosDP: 500, DuelosGratis: true}
	sg := ServidorGuardado{ID: "0123456789abcdef", Form: f.Normalizado()}
	if err := al.cambiar(func(a *Ajustes) {
		a.Actualizar = false
		a.CarpetaJuego = `C:\x`
		a.MostrarOcultos = true
		a.Servidores = []ServidorGuardado{sg}
	}); err != nil {
		t.Fatal(err)
	}
	b := abrirAlmacen(d).leer() // otro arranque
	// (un formulario de antes del 12-10, sin «modo», se lee con el modo que sale de «publica»: false = Hidden)
	if b.Actualizar || b.CarpetaJuego != `C:\x` || !b.MostrarOcultos || len(b.Servidores) != 1 || b.Servidores[0].Form != f.Normalizado() ||
		b.Servidores[0].Form.Modo != ModoOculto {
		t.Fatalf("no se recordó: %+v", b)
	}
	// un fichero roto o de otra versión no impide arrancar
	os.WriteFile(filepath.Join(d, ficheroAjustes), []byte("{no es json"), 0o600)
	if a := abrirAlmacen(d).leer(); !a.Actualizar {
		t.Fatal("fichero roto: no volvió a los valores por defecto")
	}
	// valores raros dentro de un fichero válido se corrigen (Reglas/Cuentas) y se tiran ids que no valen como carpeta
	os.WriteFile(filepath.Join(d, ficheroAjustes), []byte(`{"v":2,"actualizar":true,"servidores":[{"id":"0123456789abcdef","form":{"nombre":"x","max":5,"reglas":"inventada","cuentas":"x"}},{"id":"../../x","form":{"nombre":"malo"}}]}`), 0o600)
	a := abrirAlmacen(d).leer()
	if len(a.Servidores) != 1 {
		t.Fatalf("servidores: %+v", a.Servidores)
	}
	if g := a.Servidores[0].Form; g.Reglas != sala.ReglasVanilla || g.Cuentas != sala.CuentasLibre || g.Max != 5 {
		t.Fatalf("no se normalizó: %+v", g)
	}
}

func TestOcultosPorPeerIDYFavoritos(t *testing.T) {
	n, datos := nuevoDePrueba(t)
	s1, _, _ := sala.CargarAnfitrion(pruebas.TempDir(t), false, false)
	s2, _, _ := sala.CargarAnfitrion(pruebas.TempDir(t), false, false)
	id1, id2 := s1.PeerID().String(), s2.PeerID().String()

	if err := n.Ocultar("no-es-un-peer-id", true); err == nil {
		t.Fatal("se guardó un identificador inventado")
	}
	if err := n.Ocultar(id1, true); err != nil {
		t.Fatal(err)
	}
	n.Ocultar(id1, true) // repetir no duplica
	n.Ocultar(id2, true)
	if got := n.Ajustes().Ocultos; len(got) != 2 {
		t.Fatalf("ocultos: %v", got)
	}
	// se ve en la lista (la página decide si lo enseña) y sobrevive a reabrir
	n.mu.Lock()
	n.filas[id1] = sala.ServidorVisto{ID: id1, Ficha: sala.Ficha{Nombre: "Mismo nombre", Max: 10}}
	n.filas[id2] = sala.ServidorVisto{ID: id2, Ficha: sala.Ficha{Nombre: "Mismo nombre", Max: 10}}
	n.mu.Unlock()
	l := n.Servidores()
	if len(l.Filas) != 2 || !l.Filas[0].Oculto || !l.Filas[1].Oculto {
		t.Fatalf("lista: %+v", l.Filas)
	}
	if IDCorto(id1) == IDCorto(id2) || strings.HasPrefix(IDCorto(id1), "12D3") {
		t.Fatalf("el ID corto no distingue servidores con el mismo nombre: %q %q", IDCorto(id1), IDCorto(id2))
	}
	n2 := Nuevo(Config{Datos: datos, ExeDir: datos})
	defer n2.Cerrar()
	if !n2.Ajustes().EsOculto(id1) {
		t.Fatal("el oculto no sobrevive a reabrir")
	}
	n2.Ocultar(id1, false)
	if n2.Ajustes().EsOculto(id1) || !n2.Ajustes().EsOculto(id2) {
		t.Fatal("mostrar un oculto quitó el otro o no quitó éste")
	}
	n2.RestaurarOcultos()
	if len(n2.Ajustes().Ocultos) != 0 {
		t.Fatal("RestaurarOcultos")
	}

	// favoritos: por peer ID, en la capa de sala
	if err := n.Favorito("nombre-no-id", true); err == nil {
		t.Fatal("favorito por nombre")
	}
	if err := n.Favorito(id1, true); err != nil {
		t.Fatal(err)
	}
	if f := n.Servidores().Filas; !(f[0].ID == id1 && f[0].Favorito) && !(f[1].ID == id1 && f[1].Favorito) {
		t.Fatalf("favorito no reflejado: %+v", f)
	}
	n3 := Nuevo(Config{Datos: datos, ExeDir: datos})
	defer n3.Cerrar()
	if fs := n3.l.Favoritos(); len(fs) != 1 || fs[0] != id1 {
		t.Fatalf("favoritos tras reabrir: %v", fs)
	}
}

func TestElegirCarpetaJuego(t *testing.T) {
	n, datos := nuevoDePrueba(t)
	juegoDir := pruebas.TempDir(t)
	if r := n.ElegirCarpetaJuego(juegoDir); r.OK {
		t.Fatal("aceptó una carpeta sin yo2.exe")
	}
	os.WriteFile(filepath.Join(juegoDir, "yo2.exe"), []byte("MZ....Ver.080912.00...."), 0o644)
	r := n.ElegirCarpetaJuego(`"` + filepath.Join(juegoDir, "yo2.exe") + `"`) // con comillas y la ruta del exe
	if !r.OK || r.Carpeta != juegoDir || r.Version != "080912" || !r.VersionOK {
		t.Fatalf("resultado: %+v", r)
	}
	if n.Ajustes().CarpetaJuego != juegoDir {
		t.Fatal("no se guardó en los ajustes")
	}
	if b, _ := os.ReadFile(filepath.Join(datos, "carpeta-juego.txt")); string(b) != juegoDir {
		t.Fatalf("JoinServer no la vería: %q", b)
	}
	if e := n.Estado(); !e.JuegoOK || e.CarpetaJuego != juegoDir || e.VersionJuego != "080912" {
		t.Fatalf("estado: %+v", e)
	}
	// otra versión: se guarda pero avisa
	os.WriteFile(filepath.Join(juegoDir, "yo2.exe"), []byte("MZ Ver.060419.00"), 0o644)
	if r := n.ElegirCarpetaJuego(juegoDir); !r.OK || r.VersionOK || r.Msg.K != "msg.carpeta.otra_version" || r.Msg.P["version"] != "060419" {
		t.Fatalf("versión distinta: %+v", r)
	}
}

// ---------------------------------------------------------------- crear servidor: formulario -> entorno

func TestEntornoRunPy(t *testing.T) {
	mapa := func(f FormServidor) map[string]string {
		m := map[string]string{}
		for _, kv := range f.EntornoRunPy() {
			k, v, _ := strings.Cut(kv, "=")
			if _, dup := m[k]; dup {
				t.Fatalf("variable repetida: %s", k)
			}
			m[k] = v
		}
		return m
	}
	base := FormPorDefecto()
	base.Nombre = "x"
	m := mapa(base)
	want := map[string]string{"YO2_STORAGE": "sqlite", "YO2_ROOM_PER_LANG": "1", "YO2_PERFIL": "vanilla", "YO2_LOGIN_ESTRICTO": "0",
		"YO2_MAX_CUENTAS": "90", "YO2_NO_CLIENT": "1", "YO2_START_LEVEL": "1",
		"YO2_DP_INICIO": "10"} // 10 DP y sin YO2_DP_COSTE: como el original
	for k, v := range want {
		if m[k] != v {
			t.Errorf("%s = %q, quería %q", k, m[k], v)
		}
	}
	if len(m) != len(want) {
		t.Errorf("variables de más: %v", m)
	}
	mj := base
	mj.Reglas, mj.Cuentas, mj.Max = sala.ReglasMejorado, sala.CuentasCerrada, 8
	m = mapa(mj)
	if m["YO2_PERFIL"] != "enhanced" || m["YO2_LOGIN_ESTRICTO"] != "1" || m["YO2_MAX_CUENTAS"] != "64" {
		t.Errorf("mejorado/cerradas/8: %v", m)
	}
	mj.NivelInicial = 5
	if mapa(mj)["YO2_START_LEVEL"] != "5" {
		t.Errorf("nivel inicial 5: %v", mapa(mj))
	}
	mj.NivelInicial = 0 // formulario guardado antes de existir el campo: como el original
	if mapa(mj)["YO2_START_LEVEL"] != "1" {
		t.Errorf("nivel inicial sin poner: %v", mapa(mj))
	}
	// 12-10: puntos Duelpass de inicio y duelos gratis
	mj.PuntosDP = 500
	if m := mapa(mj); m["YO2_DP_INICIO"] != "500" || m["YO2_DP_COSTE"] != "" || m["YO2_DP_INICIO_CUENTA"] != "" {
		t.Errorf("500 DP: %v", m)
	}
	mj.PuntosDP = 0 // formulario guardado antes de existir el campo: como el original
	if mapa(mj)["YO2_DP_INICIO"] != "10" {
		t.Errorf("DP sin poner: %v", mapa(mj))
	}
	gratis := mj
	gratis.DuelosGratis = true
	if m := mapa(gratis); m["YO2_DP_COSTE"] != "0" || m["YO2_DP_INICIO_CUENTA"] != "0" || m["YO2_DP_INICIO"] != "10" {
		t.Errorf("duelos gratis: %v", m)
	}
	mj.Max = 1000
	if mapa(mj)["YO2_MAX_CUENTAS"] != "3000" {
		t.Errorf("max cuentas con 1000 jugadores: %v", mapa(mj))
	}
	// opciones de sala
	o := mj.OpcServidor(7)
	if o.Offset != 7 || o.Reglas != sala.ReglasMejorado || o.Cuentas != sala.CuentasCerrada || o.Max != 1000 || len(o.Env) != 8 {
		t.Errorf("OpcServidor: %+v", o)
	}
	o = FormServidor{Nombre: "  hola  ", Descripcion: " d ", Max: 5, Reglas: sala.ReglasVanilla, Cuentas: sala.CuentasLibre}.OpcServidor(0)
	if o.Nombre != "hola" || o.Descripcion != "d" {
		t.Errorf("no se recortó: %q %q", o.Nombre, o.Descripcion)
	}
}

func TestValidarFormulario(t *testing.T) {
	ok := FormPorDefecto()
	ok.Nombre = "Mi servidor"
	if err := ok.Validar(); err != nil {
		t.Fatal(err)
	}
	casos := map[string]func(*FormServidor){
		"nombre vacío": func(f *FormServidor) { f.Nombre = "   " },
		"nombre largo": func(f *FormServidor) { f.Nombre = strings.Repeat("a", 41) },
		"descr larga":  func(f *FormServidor) { f.Descripcion = strings.Repeat("a", 201) },
		"max 1":        func(f *FormServidor) { f.Max = 1 },
		"max enorme":   func(f *FormServidor) { f.Max = sala.MaxJugadores + 1 },
		"reglas":       func(f *FormServidor) { f.Reglas = "otra" },
		"cuentas":      func(f *FormServidor) { f.Cuentas = "otra" },
		"nivel 0":      func(f *FormServidor) { f.NivelInicial = 0 },
		"nivel 100":    func(f *FormServidor) { f.NivelInicial = 100 },
		"dp negativo":  func(f *FormServidor) { f.PuntosDP = -5 },
		"dp enorme":    func(f *FormServidor) { f.PuntosDP = puntosDPMax + 1 },
	}
	for nombre, mod := range casos {
		f := ok
		mod(&f)
		if f.Validar() == nil {
			t.Errorf("%s: no dio error", nombre)
		}
	}
}

// ---------------------------------------------------------------- parar run.py con orden

// consolaFalsa imita yo2srv/admin/consola.py: primera línea «AUTH <token>»; cada línea siguiente se contesta «ok».
type consolaFalsa struct {
	ln      net.Listener
	lineas  chan string
	cierra  func() // qué pasa cuando llega la orden (p. ej. «el proceso muere»)
	rechaza bool
}

func nuevaConsola(t *testing.T, token string) (*consolaFalsa, int) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	c := &consolaFalsa{ln: ln, lineas: make(chan string, 8)}
	go func() {
		for {
			cn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer cn.Close()
				r := bufio.NewReader(cn)
				primera, _ := r.ReadString('\n')
				if strings.TrimSpace(primera) != "AUTH "+token {
					c.lineas <- "RECHAZADO"
					return // como consola.py: cierra sin contestar
				}
				for {
					l, err := r.ReadString('\n')
					if err != nil {
						return
					}
					c.lineas <- strings.TrimSpace(l)
					cn.Write([]byte("ok\n"))
					if c.cierra != nil {
						c.cierra()
					}
				}
			}()
		}
	}()
	return c, ln.Addr().(*net.TCPAddr).Port
}

// procesoDuerme arranca un proceso de verdad (esta misma prueba en modo «duerme») para poder comprobar si lo matan.
func procesoDuerme(t *testing.T) (*os.Process, chan struct{}) {
	cmd := exec.Command(os.Args[0], "-test.run=NoExiste")
	cmd.Env = append(os.Environ(), "NUCLEO_TEST_DUERME=1")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	hecho := make(chan struct{})
	go func() { cmd.Wait(); close(hecho) }()
	t.Cleanup(func() { cmd.Process.Kill() })
	return cmd.Process, hecho
}

func ficheroToken(t *testing.T, puerto int, token string) string {
	datos := pruebas.TempDir(t) // hace de %LOCALAPPDATA%\YO2Online
	if err := os.WriteFile(filepath.Join(datos, "consola_"+strconv.Itoa(puerto)+".txt"), []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
	return datos
}

func TestPararRunPyConOrden(t *testing.T) {
	const token = "token-secreto-de-prueba"
	con, puerto := nuevaConsola(t, token)
	datos := ficheroToken(t, puerto, token)
	proc, hecho := procesoDuerme(t)
	// «run.py» termina a los 300 ms de recibir DEADLINE = 1, como cuando su bucle principal acaba
	con.cierra = func() { time.AfterFunc(300*time.Millisecond, func() { proc.Kill() }) }
	var log strings.Builder
	r := PararRunPy(ParadaOpc{Offset: puerto - 5799, Datos: datos, Proc: proc, Hecho: hecho, Espera: 5 * time.Second,
		Logf: func(f string, a ...any) { log.WriteString(strings.TrimSpace(f)) }})
	if !r.Limpia || r.Forzada {
		t.Fatalf("debía cerrarse con orden: %+v", r)
	}
	if l := <-con.lineas; l != "DEADLINE = 1" {
		t.Fatalf("la consola recibió %q", l)
	}
	if strings.Contains(r.Mensaje+log.String(), token) {
		t.Fatal("el token salió en un mensaje o en el log")
	}
	if r.Tarda > 3*time.Second {
		t.Fatalf("tardó %v", r.Tarda)
	}
}

func TestPararRunPyForzadoSiNoSeCierra(t *testing.T) {
	const token = "otro-token"
	con, puerto := nuevaConsola(t, token)
	datos := ficheroToken(t, puerto, token)
	proc, hecho := procesoDuerme(t)
	// la consola contesta «ok» pero «run.py» no termina (algo lo cuelga): a los 600 ms se mata
	var log strings.Builder
	r := PararRunPy(ParadaOpc{Offset: puerto - 5799, Datos: datos, Proc: proc, Hecho: hecho, Espera: 600 * time.Millisecond,
		Logf: func(f string, a ...any) { log.WriteString(f) }})
	if !r.Forzada || r.Limpia || !strings.Contains(r.Mensaje, "a la fuerza") {
		t.Fatalf("debía forzarse y decirlo: %+v", r)
	}
	<-con.lineas
	select {
	case <-hecho:
	case <-time.After(5 * time.Second):
		t.Fatal("el proceso sigue vivo tras forzar")
	}
}

func TestPararRunPyTokenMalo(t *testing.T) {
	con, puerto := nuevaConsola(t, "el-bueno")
	datos := ficheroToken(t, puerto, "uno-viejo")
	proc, hecho := procesoDuerme(t)
	r := PararRunPy(ParadaOpc{Offset: puerto - 5799, Datos: datos, Proc: proc, Hecho: hecho, Espera: 400 * time.Millisecond})
	if l := <-con.lineas; l != "RECHAZADO" {
		t.Fatalf("la consola debía rechazar el token: %q", l)
	}
	if !r.Forzada || !strings.Contains(r.Mensaje, "Motivo") || len(r.Partes) != 2 || r.Partes[1].K != "msg.parar.motivo" {
		t.Fatalf("sin consola hay que forzar y explicar por qué: %+v", r)
	}
	if strings.Contains(r.Mensaje, "uno-viejo") || strings.Contains(r.Mensaje, "el-bueno") {
		t.Fatal("el token salió en el mensaje")
	}
}

func TestPararRunPySinFicheroDeToken(t *testing.T) {
	proc, hecho := procesoDuerme(t)
	r := PararRunPy(ParadaOpc{Offset: 40000, Datos: pruebas.TempDir(t), Proc: proc, Hecho: hecho, Espera: 300 * time.Millisecond})
	if !r.Forzada || !strings.Contains(r.Mensaje, "token") {
		t.Fatalf("%+v", r)
	}
}

func TestPararRunPyAjenoNoSeToca(t *testing.T) {
	r := PararRunPy(ParadaOpc{Datos: pruebas.TempDir(t)})
	if r.Forzada || r.Limpia || !strings.Contains(r.Mensaje, "no lo arrancó") {
		t.Fatalf("%+v", r)
	}
}

// ---------------------------------------------------------------- reiniciar tras una actualización

func TestRetomarTrasActualizar(t *testing.T) {
	s, _, err := sala.CargarAnfitrion(pruebas.TempDir(t), false, false)
	if err != nil {
		t.Fatal(err)
	}
	id, codigo := s.PeerID().String(), s.Codigo()

	// servidor público: va el peer ID; nada secreto
	envPub := Retomar{Publico: id}.Entorno()
	if len(envPub) != 1 || strings.Contains(strings.Join(envPub, " "), codigo) {
		t.Fatalf("entorno público: %v", envPub)
	}
	vars := map[string]string{}
	for _, kv := range envPub {
		k, v, _ := strings.Cut(kv, "=")
		vars[k] = v
	}
	borradas := []string{}
	r := LeerRetomar(func(k string) string { return vars[k] }, func(k string) { borradas = append(borradas, k) })
	if r == nil || r.Publico != id || r.Codigo != "" {
		t.Fatalf("público: %+v", r)
	}
	if len(borradas) != 4 { // las cuatro variables (con la de «Join by address») se borran SIEMPRE (no las heredan frida ni el juego)
		t.Fatalf("se borraron %v", borradas)
	}

	// público con contraseña: va la clave derivada (nunca la contraseña) y vuelve igual; basura = sin clave
	cred := sala.CredencialGuardada(1, make([]byte, sala.LenSalClave), bytes.Repeat([]byte{7}, sala.LenClave))
	vars = map[string]string{}
	for _, kv := range (Retomar{Publico: id, Clave: cred.Texto()}).Entorno() {
		k, v, _ := strings.Cut(kv, "=")
		vars[k] = v
	}
	if r = LeerRetomar(func(k string) string { return vars[k] }, func(string) {}); r == nil || r.Publico != id || r.Clave != cred.Texto() {
		t.Fatalf("público con contraseña: %+v", r)
	}
	vars[EnvRetomarClave] = "v1:basura"
	if r = LeerRetomar(func(k string) string { return vars[k] }, func(string) {}); r == nil || r.Clave != "" {
		t.Fatalf("clave rota: %+v", r)
	}

	// privado: el código va por el entorno
	vars = map[string]string{}
	for _, kv := range (Retomar{Codigo: codigo}).Entorno() {
		k, v, _ := strings.Cut(kv, "=")
		vars[k] = v
	}
	r = LeerRetomar(func(k string) string { return vars[k] }, func(string) {})
	if r == nil || r.Codigo != codigo || r.Publico != "" {
		t.Fatalf("privado: %+v", r)
	}

	// basura o inventado: arranque normal
	for _, v := range []map[string]string{
		{},
		{EnvRetomar: "pub:no-es-un-id"},
		{EnvRetomar: "cod", EnvRetomarCodigo: "YO2-AAAA"},
		{EnvRetomar: "cod"},
		{EnvRetomar: "otra-cosa"},
	} {
		vv := v
		if r := LeerRetomar(func(k string) string { return vv[k] }, func(string) {}); r != nil {
			t.Errorf("%v: %+v", v, r)
		}
	}
	if (Retomar{}).Entorno() != nil {
		t.Error("sin destino no debe haber entorno")
	}
}

func TestEntornoRealSeBorra(t *testing.T) {
	s, _, _ := sala.CargarAnfitrion(pruebas.TempDir(t), false, false)
	t.Setenv(EnvRetomar, "pub:"+s.PeerID().String())
	t.Setenv(EnvRetomarCodigo, "x")
	if r := leerRetomarProceso(); r == nil || r.Publico == "" {
		t.Fatalf("%+v", r)
	}
	if os.Getenv(EnvRetomar) != "" || os.Getenv(EnvRetomarCodigo) != "" {
		t.Fatal("el entorno sigue ahí")
	}
}

// ---------------------------------------------------------------- cerrar, errores, comprobaciones

func TestAntesDeCerrar(t *testing.T) {
	var evs []string
	var mu sync.Mutex
	datos := pruebas.TempDir(t)
	n := Nuevo(Config{Datos: datos, ExeDir: datos, Emitir: func(ev string, d any) { mu.Lock(); evs = append(evs, ev); mu.Unlock() }})
	defer n.Cerrar()
	if n.AntesDeCerrar() {
		t.Fatal("sin nada en marcha debe poder cerrar")
	}
	n.mu.Lock()
	n.con = &sala.Conexion{} // «estoy conectado»
	n.conNombre = "X"
	n.mu.Unlock()
	if !n.AntesDeCerrar() {
		t.Fatal("conectado: debe pedir confirmación")
	}
	mu.Lock()
	ok := len(evs) == 1 && evs[0] == "cerrar"
	mu.Unlock()
	if !ok {
		t.Fatalf("eventos: %v", evs)
	}
	// la página no contesta (colgada): un segundo intento de cerrar, pasados unos segundos, la deja cerrar
	n.mu.Lock()
	n.cierreEmitido = time.Now().Add(-6 * time.Second)
	n.mu.Unlock()
	if n.AntesDeCerrar() {
		t.Fatal("con la página colgada no se puede quedar la ventana sin poder cerrarse")
	}
	// pero si la página contestó (está enseñando la pregunta), no se salta
	n.mu.Lock()
	n.cierreEmitido = time.Now().Add(-6 * time.Second)
	n.mu.Unlock()
	n.CierreVisto()
	if !n.AntesDeCerrar() {
		t.Fatal("con la pregunta en pantalla debe seguir preguntando")
	}
	n.CancelarCierre()
	n.mu.Lock()
	n.con = nil
	n.cerrando = true // el usuario ya confirmó
	n.mu.Unlock()
	if n.AntesDeCerrar() {
		t.Fatal("ya confirmado: no debe volver a preguntar")
	}
}

func TestEntrarPideCarpetaDelJuego(t *testing.T) {
	n, _ := nuevoDePrueba(t)
	n.cfg.ExeDir = pruebas.TempDir(t)
	viejo := juego.RutaPorDefecto
	juego.RutaPorDefecto = "" // como si Konami no lo hubiera instalado en este PC
	t.Cleanup(func() { juego.RutaPorDefecto = viejo })
	if n.carpetaJuego() != "" {
		t.Fatal("encontró un juego que no existe")
	}
	err := n.EntrarConCodigo("esto no es un codigo")
	if codigoDe(err) != "msg.err.codigo_formato" {
		t.Fatalf("código inválido: %v", err)
	}
	s, _, _ := sala.CargarAnfitrion(pruebas.TempDir(t), false, false)
	if err := n.EntrarConCodigo(s.Codigo()); codigoDe(err) != "msg.err.sin_juego" || !errors.Is(err, juego.ErrCarpeta) {
		t.Fatalf("sin juego: %v", err)
	}
}

// codigoDe: la clave del mensaje de un error con clave ("" si no lo es).
func codigoDe(err error) string {
	var e *ErrUI
	if errors.As(err, &e) {
		return e.K
	}
	return ""
}

func TestTraducirError(t *testing.T) {
	casos := []struct {
		err  error
		k    string
		ajus bool
	}{
		{sala.ErrLleno, "msg.err.lleno", false},
		{sala.ErrVersionDistint, "msg.err.otra_version", false},
		{sala.ErrImpostor, "msg.err.impostor", false},
		{fmt.Errorf("x: %w", sala.ErrPuertoLocal), "msg.err.puerto_ocupado", false},
		{fmt.Errorf("x: %w", sala.ErrJuegoNoContesta), "msg.err.runpy_no_contesta", false},
		{fmt.Errorf("x: %w", juego.ErrLanzar), "msg.err.no_abre_juego", false},
		{fmt.Errorf("%w: CrossOver: no está", juego.ErrLanzar), "msg.err.crossover", true},
		{&sala.ErrSinCamino{P2P: errors.New("a"), Tor: errors.New("b")}, "msg.err.sin_camino", false},
		{context.Canceled, "msg.err.cancelado", false},
		// respaldo: errores sin tipo (texto de otra versión)
		{errString("quien contesta NO es el anfitrión"), "msg.err.impostor", false},
		{errString("el puerto local 5731 está ocupado (¿otro lanzador abierto?)"), "msg.err.puerto_ocupado", false},
		{errString("- P2P: nada\n- Tor: nada"), "msg.err.sin_camino", false},
		{errString("algo raro\nsegunda línea"), "msg.err.entrar_generico", false},
	}
	for _, c := range casos {
		m, tec, ajus := traducirError(c.err)
		if m.K != c.k || ajus != c.ajus || tec == "" {
			t.Errorf("%v -> %q (ajustes %v)", c.err, m.K, ajus)
		}
	}
	// lo que ya viene con clave (abrirJuego) se respeta y su texto técnico es el de la causa
	if m, tec, _ := traducirError(errJuego(fmt.Errorf("%w: boom", juego.ErrLanzar))); m.K != "msg.err.no_abre_juego" || !strings.Contains(tec, "boom") {
		t.Errorf("errJuego: %q %q", m.K, tec)
	}
	// el texto que llega a la página es el JSON del mensaje
	if s := errM("msg.err.ya_conectado", "x", "nombre", "A").Error(); s != `{"k":"msg.err.ya_conectado","p":{"nombre":"A"}}` {
		t.Errorf("JSON del error: %s", s)
	}
	if _, _, ajus := traducirError(juego.ErrCarpeta); !ajus {
		t.Error("la falta de carpeta debe mandar a Ajustes")
	}
}

type errString string

func (e errString) Error() string { return string(e) }

func TestVersionTexto(t *testing.T) {
	if VersionTexto(20261011120000) != "2026.10.11" || VersionTexto(0) != "dev" {
		t.Fatal(VersionTexto(20261011120000), VersionTexto(0))
	}
}

func TestComprobarPuertos(t *testing.T) {
	libre, _ := net.Listen("tcp", "127.0.0.1:0")
	pLibre := libre.Addr().(*net.TCPAddr).Port
	libre.Close()
	ocupado, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ocupado.Close()
	pOcupado := ocupado.Addr().(*net.TCPAddr).Port

	if o := ocupadosDe([]int{pLibre}); len(o) != 0 {
		t.Fatalf("libre marcado ocupado: %v", o)
	}
	if o := ocupadosDe([]int{pLibre, pOcupado}); len(o) != 1 || o[0] != pOcupado {
		t.Fatalf("ocupados: %v", o)
	}
	// y el informe usa los puertos que se le dan (no los reales del juego)
	n, _ := nuevoDePrueba(t)
	n.cfg.Puertos = func() []int { return []int{pOcupado} }
	n.cfg.WebView2 = func() (string, error) { return "1.2.3", nil }
	var puertos *Chequeo
	rep := n.Comprobar()
	for i := range rep {
		if rep[i].Nombre.K == "msg.chk.puertos" {
			puertos = &rep[i]
		}
	}
	if puertos == nil || puertos.Estado != "aviso" {
		t.Fatalf("informe: %+v", rep)
	}
	n.cfg.Puertos = func() []int { return []int{pLibre} }
	for _, c := range n.Comprobar() {
		if c.Nombre.K == "msg.chk.puertos" && c.Estado != "ok" {
			t.Fatalf("libres: %+v", c)
		}
		if c.Nombre.Es == "WebView2" && c.Estado != "ok" {
			t.Fatalf("webview2: %+v", c)
		}
	}
}

// Con YO2_LANZADOR_DATOS (pruebas) la carpeta de datos del lanzador no es la de run.py: el token está en
// %LOCALAPPDATA%\YO2Online (Local) y la parada tiene que encontrarlo igual (fallo visto en la prueba en vivo del 11-10).
func TestPararRunPyTokenEnLaCarpetaDeRunPyNoEnDatos(t *testing.T) {
	const token = "token-en-local"
	con, puerto := nuevaConsola(t, token)
	local := ficheroToken(t, puerto, token) // la carpeta de run.py
	datosLanzador := pruebas.TempDir(t)     // la del lanzador: vacía
	proc, hecho := procesoDuerme(t)
	con.cierra = func() { time.AfterFunc(200*time.Millisecond, func() { proc.Kill() }) }
	r := PararRunPy(ParadaOpc{Offset: puerto - 5799, Datos: datosLanzador, Local: local, Proc: proc, Hecho: hecho, Espera: 5 * time.Second})
	if !r.Limpia || r.Forzada {
		t.Fatalf("debía cerrarse con orden usando el token de la carpeta de run.py: %+v", r)
	}
	if l := <-con.lineas; l != "DEADLINE = 1" {
		t.Fatalf("la consola recibió %q", l)
	}
}

func TestDirsTokenOrden(t *testing.T) {
	got := dirsToken(ParadaOpc{Datos: `D:\datos`, Local: `C:\local`})
	if len(got) != 2 || got[0] != `C:\local` || got[1] != `D:\datos` {
		t.Fatalf("primero la de run.py, luego la del lanzador: %v", got)
	}
	if got := dirsToken(ParadaOpc{Datos: `C:\x`, Local: `C:\x`}); len(got) != 1 {
		t.Fatalf("sin repetir: %v", got)
	}
}

func TestTemaDeLaVentana(t *testing.T) {
	for _, ok := range []string{"system", "light", "dark"} {
		if !TemaValido(ok) {
			t.Errorf("%q debería valer", ok)
		}
	}
	for _, mal := range []string{"", "Dark", "azul", "../x"} {
		if TemaValido(mal) {
			t.Errorf("%q no debería valer", mal)
		}
	}
	d := pruebas.TempDir(t)
	al := abrirAlmacen(d)
	if al.leer().Tema != "" {
		t.Fatal("por defecto no hay tema guardado (= el del sistema)")
	}
	if err := al.cambiar(func(a *Ajustes) { a.Tema = "dark" }); err != nil {
		t.Fatal(err)
	}
	if got := abrirAlmacen(d).leer().Tema; got != "dark" {
		t.Fatalf("el tema no se recordó: %q", got)
	}
}
