// YO2OnlineLauncher: la ventana «Yu-Gi-Oh! Online 2 Launcher» (Wails v2). Todo lo que hace está en el paquete nucleo (que no
// sabe nada de Wails y se prueba sin ventana); esto solo crea la ventana, enlaza el HTML (frontend\dist) con los métodos
// de nucleo y reenvía sus eventos.
//
// Compilar: app\build.ps1 (deja launcher\dist-app\YO2OnlineLauncher.exe; antes del 12-10 se llamaba YO2Launcher.exe y pisaba
// el Yo2Launcher.exe de Konami en la carpeta del cliente).
package main

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	wopts "github.com/wailsapp/wails/v2/pkg/options/windows"
	"github.com/wailsapp/wails/v2/pkg/runtime"
	"yo2online/app/nucleo"
	"yo2online/sala"
)

//go:embed all:frontend/dist
var assets embed.FS

// versionLanzador: la pone app\build.ps1 (-ldflags "-X main.versionLanzador=AAAAMMDDhhmmss", UTC). "0" = compilado a mano.
var versionLanzador = "0"

func miVersion() int64 {
	v, _ := strconv.ParseInt(versionLanzador, 10, 64)
	return v
}

const titulo = "Yu-Gi-Oh! Online 2 Launcher"

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "-version" || os.Args[1] == "--version") {
		fmt.Println(miVersion()) // lo usa firmar.exe, igual que con JoinServer.exe
		return
	}
	// Tras una autoactualización, el exe nuevo espera a que el viejo se cierre (si no, el candado de «una sola ventana»
	// lo tomaría por una segunda copia y se iría).
	if p, err := strconv.Atoi(os.Getenv(nucleo.EnvPadrePID)); err == nil && p > 0 {
		os.Unsetenv(nucleo.EnvPadrePID)
		esperarProceso(p, 90*time.Second)
	}

	exe, _ := os.Executable()
	exeDir := filepath.Dir(exe)
	datos := dirDatos()
	offset, _ := strconv.Atoi(os.Getenv("YO2_PORT_OFFSET"))
	if err := os.MkdirAll(datos, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, "No puedo crear", datos, err)
		os.Exit(1)
	}
	podarLogs(datos, 15)
	logPath := filepath.Join(datos, "yo2-lanzador-"+time.Now().Format("20060102-150405")+".log")
	lf, err := os.Create(logPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "No puedo crear el log", logPath, err)
		os.Exit(1)
	}
	defer lf.Close()
	logger := log.New(lf, "", log.LstdFlags|log.Lmicroseconds)
	log.SetOutput(sala.FiltroLog(lf)) // lo que escriben las librerías (mDNS...) va al log, sin ruido
	logf := func(f string, a ...any) { logger.Printf(f, a...) }
	logf("YO2OnlineLauncher versión %d (%s), pid %d, %s", miVersion(), nucleo.VersionTexto(miVersion()), os.Getpid(), exe)
	if sala.ChocaConKonami(exe) { // un lanzador viejo (YO2Launcher.exe) actualizado: sigue con su nombre
		logf("AVISO: este exe se llama como el lanzador de Konami (%s); si está en la carpeta del juego lo ha sustituido. "+
			"El nombre bueno es %s (cualquier carpeta vale).", sala.ExeKonami, sala.ExeLanzadorApp)
	}

	if aplicarPaqueteAlArrancar(exe, offset, logf) { // arranque_paquete.go: versión del paquete preparada
		return
	}
	if len(os.Args) > 1 && os.Args[1] == argSoloAplicar { // arranque_paquete.go: solo aplicar (sin ventana)
		return
	}

	app := &App{logf: logf}
	app.Nucleo = nucleo.Nuevo(nucleo.Config{
		Datos: datos, ExeDir: exeDir, Version: miVersion(), Offset: offset, Logf: logf, Log: sala.FiltroLog(lf),
		Emitir:       app.emitir,
		AbrirCarpeta: abrirCarpeta,
		Portapapeles: func(t string) error {
			if app.ctx == nil {
				return fmt.Errorf("la ventana aún no está lista")
			}
			return runtime.ClipboardSetText(app.ctx, t)
		},
		Salir: func() {
			if app.ctx != nil {
				runtime.Quit(app.ctx)
			}
		},
		WebView2: versionWebView,
	})

	err = wails.Run(&options.App{
		Title:            titulo,
		Width:            1160,
		Height:           680,
		MinWidth:         900,
		MinHeight:        600,
		Frameless:        true,
		BackgroundColour: &options.RGBA{R: 8, G: 12, B: 24, A: 255},
		AssetServer:      &assetserver.Options{Assets: assets},
		Bind:             []interface{}{app},
		OnStartup:        app.startup,
		OnBeforeClose:    func(ctx context.Context) bool { return app.AntesDeCerrar() },
		OnShutdown: func(ctx context.Context) {
			logf("cerrando")
			app.Cerrar()
		},
		SingleInstanceLock: &options.SingleInstanceLock{
			UniqueId: idUnico(),
			OnSecondInstanceLaunch: func(options.SecondInstanceData) {
				// Un segundo doble clic en el exe: traer la ventana de la primera.
				if app.ctx != nil {
					runtime.WindowUnminimise(app.ctx)
					runtime.WindowShow(app.ctx)
				}
			},
		},
		Windows: &wopts.Options{
			WebviewUserDataPath: filepath.Join(datos, "webview"),
			DisablePinchZoom:    true,
		},
		Mac: opcionesMac(),
	})
	if err != nil {
		logf("wails: %v", err)
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}

// App: lo que se enlaza con el HTML. Los métodos exportados de Nucleo (Estado, Servidores, Entrar...) se ven desde JS
// como window.go.main.App.<Método>; los de ventana (Minimizar...) están aquí.
type App struct {
	*nucleo.Nucleo
	ctx  context.Context
	logf func(string, ...any)
}

func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
	if arranquePrueba != nil { // solo en el exe de pruebas (prueba_ctl.go, etiqueta «prueba»)
		arranquePrueba(a)
	}
}

// arranquePrueba: lo pone prueba_ctl.go en el exe de pruebas de extremo a extremo; nil en el que se reparte.
var arranquePrueba func(*App)

func (a *App) emitir(ev string, d any) {
	if a.ctx != nil {
		runtime.EventsEmit(a.ctx, ev, d)
	}
}

// Minimizar / Maximizar (alterna) / CerrarVentana: los botones de la barra de título propia.
func (a *App) Minimizar() { runtime.WindowMinimise(a.ctx) }
func (a *App) Maximizar() { runtime.WindowToggleMaximise(a.ctx) }

// CerrarVentana: la X. Si hay una conexión o el servidor en marcha pregunta antes (evento «cerrar»).
func (a *App) CerrarVentana() {
	if a.AntesDeCerrar() {
		return
	}
	runtime.Quit(a.ctx)
}

// ExaminarCarpetaJuego: el selector de carpetas de Windows (título en inglés; los botones los pone Windows, en su idioma);
// guarda lo elegido.
func (a *App) ExaminarCarpetaJuego() (nucleo.ResultadoCarpeta, error) {
	dir, err := runtime.OpenDirectoryDialog(a.ctx, runtime.OpenDialogOptions{Title: "Game folder (the one with yo2.exe)"})
	if err != nil {
		return nucleo.ResultadoCarpeta{}, nucleo.ErrGenerico(err)
	}
	if dir == "" { // canceló
		return nucleo.ResultadoCarpeta{Cancelado: true}, nil
	}
	return a.Nucleo.ElegirCarpetaJuego(dir), nil
}

// ErrorJS: la página cuenta sus errores de JavaScript para que queden en el log.
func (a *App) ErrorJS(msg string) {
	if len(msg) > 2000 {
		msg = msg[:2000]
	}
	a.logf("[js] %s", msg)
}

// idUnico: el candado de «una sola ventana». Con YO2_LANZADOR_DATOS (solo pruebas: otra carpeta de datos) el candado es
// otro, así se puede probar un exe compilado con la ventana de siempre abierta (si no, el de prueba se cerraría al momento
// y traería al frente la ventana de verdad).
func idUnico() string {
	const id = "yo2launcher-3f6a2b1e-8c0d-4a57-9e41-5d0b7c9a1f22"
	if d := os.Getenv("YO2_LANZADOR_DATOS"); d != "" {
		h := sha256.Sum256([]byte(strings.ToLower(filepath.Clean(d))))
		return id + "-prueba-" + hex.EncodeToString(h[:6])
	}
	return id
}

// podarLogs deja los n logs más nuevos del lanzador (yo2-lanzador-*.log) en la carpeta de datos.
func podarLogs(datos string, n int) {
	fs, _ := filepath.Glob(filepath.Join(datos, "yo2-lanzador-*.log"))
	sort.Strings(fs) // el nombre lleva la fecha
	for i := 0; i+n < len(fs); i++ {
		os.Remove(fs[i])
	}
}
