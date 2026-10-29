# Building and running from source

The game client is a Windows program (`yo2.exe` Ver.080912). The usual player package is **Windows 10/11 x64**. The
server is portable Python. On **macOS** the same launcher can be built as an `.app` that starts the Windows client
inside CrossOver.

## Requirements

| Tool | Version | For |
|---|---|---|
| Python | **3.12** (standard library only) | the server (`tools/yo2emu/run.py`), extractor, tests |
| Node.js | **≥ 22.13** (uses `node:sqlite`) | the duel engine process (`duelcore`) |
| pnpm | 10.x | installs `ocgcore-wasm` and `tsx` for the engine (`pnpm-lock.yaml` is pinned) |
| Go | **1.27.1** (see `tools/yo2emu/launcher/go.mod`) | the launcher |
| Wails CLI | **2.16.0** (`go install github.com/wailsapp/wails/v2/cmd/wails@v2.16.0`) | the launcher window |
| WebView2 runtime | any recent | to *run* the launcher on Windows (ships with Windows 10/11) |
| CrossOver | current | to *run* the launcher on macOS (opens `yo2.exe` in a bottle) |
| zig | current (`brew install zig`) | optional on macOS: embed the `version.dll` game hook |
| w64devkit **x86** (32-bit, i686) | gcc 16.x tested | optional on Windows: the `version.dll` game hook |
| Your game client | `yo2.exe` **Ver.080912** + its `merge.dat` | data extraction and playing |

Optional Python packages: `sqlcipher3` (encrypted SQLite state; plain SQLite is used without it), `frida` 17.15.5 (only
to let `run.py` start the client itself in development mode), `Pillow` + `numpy` (only for `launcher/app/graficos.py`).

## 1. Third-party card data (`_vendor/`)

The engine reads the ProjectIgnis data **unmodified** from `_vendor/` (or from the folder in `YO2_VENDOR`). They are
not in this repository; see [`_vendor/README.md`](_vendor/README.md):

```
git clone https://github.com/ProjectIgnis/CardScripts.git _vendor/CardScripts
git -C _vendor/CardScripts checkout 37f270dc813a12d123707ae255f2bda7922999c4      # the revision we test with
curl -L -o _vendor/cards.cdb    https://raw.githubusercontent.com/ProjectIgnis/BabelCDB/master/cards.cdb
curl -L -o _vendor/strings.conf https://raw.githubusercontent.com/ProjectIgnis/Distribution/master/config/strings.conf
```

Newer revisions normally work; if a duel behaves oddly, go back to the tested ones (hashes in `_vendor/README.md`).

## 2. Duel engine (Node)

```
pnpm install
```

This installs `ocgcore-wasm` 0.1.2 and `tsx` into `apps/server/node_modules`. `tools/yo2emu/duelcore/duelcore.mts`
imports the five engine files in `apps/server/src/duel/` and is started by the server with `node --import tsx`.

For a self-contained engine (what the launcher package uses) bundle it into one file with the esbuild that pnpm
installed:

```
python tools/yo2emu/paquete/bundle_duelcore.py --salida build/duelcore.mjs
python tools/yo2emu/paquete/verificar_duelcore.py      # optional: same duels with tsx and with the bundle
```

## 3. Game data from your client

The server needs ~0.9 MB of data from the client's `merge.dat` (card ids and names, pack contents, structure decks,
NPC deck recipes, profile bits, title names). Extract it from **your** copy and build the server tables from it;
nothing is downloaded:

```
python tools/yo2emu/paquete/extraer_juego.py --juego "C:\Games\Yu-Gi-Oh ONLINE 2" --salida _original/extracted/merge2008/data
python tools/yo2emu/paquete/generar_tablas.py --datos _original/extracted/merge2008/data --vendor _vendor --juego "C:\Games\Yu-Gi-Oh ONLINE 2"
```

`extraer_juego.py` refuses any client that is not Ver.080912 and checks every file against known SHA-256 hashes.
`generar_tablas.py` builds, next to the server code, the tables that come from the client (the launcher runs both
scripts every time it hosts, through `paquete/primer_arranque.py`):

| Table (under `tools/yo2emu/`) | What | Built from |
|---|---|---|
| `research/ia/cards_yo2.csv` | YO2 card id ↔ ProjectIgnis passcode, names, stats, 2008 limits | card tables + `cards.cdb` |
| `research/ia/npc_decks.json` | deck recipe and prize bits of each NPC | `cardpack/100NN.ydc`, `yo2pbinmap.dat` |
| `research/ia/npc_profiles.json` | NPC AI profile (level, aggressiveness…) | `npc_decks.json` + `cards.cdb` (`brain.py`) |
| `research/persistencia/valid_cards.json` | card ids the client can draw | `data/mini` and `data/card` pictures listed in `merge.dat` |
| `research/persistencia/titles.json` | title table (id, profile bit, name) | the table inside `yo2.exe` + the client's text file |

With an unmodified Ver.080912 client the output is byte-identical to the tables the project is developed with
(`--comprobar <folder>` compares). `titles.json` needs `--juego` (it comes from `yo2.exe`). `npc_ids.json` (NPC names,
sex and portrait ids) is the project's own table and ships with the code. In package mode the server refuses to start
if any of the generated tables is missing. `_original/extracted/merge2008/data` is the server's default data folder;
any other folder works with `YO2_JUEGO_DATOS=<folder>`.

## 4. Running the server

Development mode (paths relative to the checkout):

```
cd tools/yo2emu
set YO2_NO_CLIENT=1          # do not try to start yo2.exe with Frida (use the launcher's "Play here" instead)
python run.py 0              # 0 = no time limit. Ctrl+C stops it.
```

Useful variables (all optional): `YO2_PORT_OFFSET` (adds to every port: a second instance for tests),
`YO2_STATE` / `YO2_LOGDIR` (state file and log folder), `YO2_STORAGE=sqlite`, `YO2_PERFIL=vanilla|enhanced`,
`YO2_JUEGO_DATOS`, `YO2_VENDOR`, `YO2_NODE`, `YO2_DUELCORE` (a `.mjs` bundle instead of the TypeScript source).
**Package mode** (`YO2_PAQUETE=1`, what the launcher uses) keeps state and logs under `%LOCALAPPDATA%\YO2Online\servidor\`
and refuses to start if the game data or the rules engine are missing. Every flag is listed in
[docs/FLAGS.md](docs/FLAGS.md) (generated source: `tools/yo2emu/yo2srv/BANDERAS.md`); paths and package mode in `tools/yo2emu/paquete/RUTAS.md`.

Ports (all + `YO2_PORT_OFFSET`): gate 5730, account/lobby 5731-5732, rooms 5740-5750 and 5800-5876, duel 5760,
HTTP patch lists 8730, admin console 5799 (127.0.0.1 only, token in `%LOCALAPPDATA%\YO2Online\consola_<port>.txt`),
management API 8791 (127.0.0.1 only, token in `gestion_<port>.txt`).

## 5. The launcher

### Windows

```
cd tools/yo2emu/launcher
powershell -ExecutionPolicy Bypass -File fetch_deps.ps1       # frida-inject.exe and tor.exe (official, SHA-256 pinned)
powershell -ExecutionPolicy Bypass -File juego\dll\build_dll.ps1   # optional: version.dll hook (w64devkit x86)
powershell -ExecutionPolicy Bypass -File app\build.ps1         # -> dist-app\YO2OnlineLauncher.exe
```

- `frida-inject.exe` and `tor.exe` are **embedded** into the exe at build time (`go:embed`), so the build fails until
  `fetch_deps.ps1` has put them in `juego\` and `sala\`. The script downloads only from github.com/frida and
  torproject.org and checks pinned SHA-256 hashes; it never runs what it downloads.
- Without the DLL (`juego\dllbin\yo2net-version.dll`) the launcher still builds and uses Frida to hook the game.
- The window uses some graphics cut from the game's own UI. They are not in the repository; without them the launcher
  works with plain colours. To generate them from your client (needs `Pillow` and `numpy`):

  ```
  python tools/kcej_dat.py extract "<game folder>\merge.dat" _original/extracted/merge2008 data\common\window data\menu data\shop
  python -I tools/yo2emu/launcher/app/graficos.py _original/extracted/merge2008/data
  ```
- The launcher looks for `run.py` next to it, one or two folders up (so `dist-app\` inside a checkout can host), or for a
  package layout (`server\`, `python\`, `node\`, `engine\`, `vendor\` next to the exe).

### macOS

Rebuild whenever the Go, Wails or `app/frontend/dist` sources change:

```
brew install go zig
go install github.com/wailsapp/wails/v2/cmd/wails@v2.16.0
export PATH="$(go env GOPATH)/bin:$PATH"
cd tools/yo2emu/launcher
./app/build.sh                                                 # -> dist-app/YO2OnlineLauncher.app
```

- CrossOver must be installed, with `yo2.exe` **Ver.080912** in a bottle. In the launcher, *Settings → Game folder*
  is that bottle path (the folder that contains `yo2.exe`), not the macOS `.app`.
- `zig` cross-compiles the 32-bit `version.dll` hook so the app can attach to the client. Without zig the `.app`
  still builds, but it cannot hook `yo2.exe`.
- `frida-inject.exe` is a Windows tool and is not used on macOS; the DLL hook is required.
- Open the rebuilt app from `tools/yo2emu/launcher/dist-app/YO2OnlineLauncher.app` (replace any copy you were running).

### Signing your own builds

Self-updates and server packages are accepted only when signed by the **publisher key** whose public half is compiled
into `launcher/sala/firma.go` (`ClaveEditorHex`). If you distribute your own builds, create your own key and replace it:

```
go build -o firmar.exe ./firmar
firmar.exe -generar          # creates %LOCALAPPDATA%\YO2Online\firma\editor-ed25519.key and prints the public key
```

Put the printed public key in `ClaveEditorHex` (or build with `-ldflags "-X yo2online/sala.claveEditorHex=<hex>"`), then
`app\build.ps1 -Firmar`. Never commit the private key. The package layout and its signed manifest are described in
[docs/NETWORK.md](docs/NETWORK.md#signed-updates).

## 6. A self-contained package (what players download)

`tools/yo2emu/paquete/empaquetar.py` builds the folder/zip that runs on a PC with only the game client: the launcher,
an embeddable Python, `node.exe`, the bundled engine, `_vendor/` and the server files the server actually loads (a
whitelist). No file from the game goes in: the launcher extracts the data and builds the tables on the host.

First put the two official archives in `tools/yo2emu/_descargas/`: `python-3.12.10-embed-amd64.zip` (python.org) and
`node-v22.23.3-win-x64.zip` (nodejs.org). The script checks their SHA-256 and never downloads anything. Then:

```
copy tools\yo2emu\paquete\empaquetar.local.example.json tools\yo2emu\paquete\empaquetar.local.json
python tools\yo2emu\paquete\empaquetar.py --lanzador tools\yo2emu\launcher\dist-app\YO2OnlineLauncher.exe
python tools\yo2emu\paquete\empaquetar.py --lanzador tools\yo2emu\launcher\dist-app\YO2OnlineLauncher.exe --firmar
```

The last form produces a signed version (it needs your publisher key, see *Signing your own builds*).

`empaquetar.local.json` (never committed) holds your personal rules: strings replaced in the copied `.py` files (for
example your character name in comments) and patterns that stop the build if they appear (your Windows user folder,
your names). Without it only the IP scan runs. The output goes to `tools/yo2emu/paquete/_build/`.

## Tests

| Command (from `tools/yo2emu` unless noted) | What |
|---|---|
| `python -m yo2srv.tests` | ~250 unit tests of the server package, no ports |
| `python storage/test_storage.py` | state store (uses a synthetic state when there is no `state.json`) |
| `python fake_client.py pvp --offset 300` | starts a test instance and plays a full PvP duel with two scripted clients (also `npc`, `pvpmatch`, `salas`, `dpcoste`…) |
| `python duelcore/test_bucle.py 20` | 20 engine duels + reproducibility (needs `_vendor/`) |
| `go vet ./... && go test ./...` (in `launcher/`) | launcher: P2P on 127.0.0.1, tunnel, signatures, packages, UI messages |

Pick an offset that keeps every port free (e.g. 300 → 6030…). `paridad/bateria.py` is the full regression battery used
during development; many of its steps replay private recordings and protocol tests that are **not** in this repository
(`paridad/_fixtures/`, logs of real sessions, `research/*/test_*.py`), so it does not run out of the box. Contributions
of synthetic fixtures are welcome.

## Known limitations of a fresh checkout

- Until `generar_tablas.py` has run (section 3), NPCs have no deck of their own, only the default title exists and every
  card id is accepted. The launcher's package mode always runs it before starting the server.
- Pre-installed decks: the launcher copies 15 decks into each character's profile folder. The 7 popular 2008 tournament
  decks are the project's own lists (`launcher/juego/mazos/`); the 8 official Structure Decks are built at run time from
  each player's own `merge.dat` (`launcher/juego/mazos_cliente.go`, checked against SHA-256 hashes). With a client that
  is not the unmodified Ver.080912 those 8 are skipped.
