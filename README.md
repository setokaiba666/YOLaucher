# YO2 Online — a server emulator and launcher for Yu-Gi-Oh! Online 2

## Download

Ready-to-use Windows package: get the latest `YO2OnlineLauncher-*.zip` from the [**Releases**](../../releases/latest) page (unzip anywhere and run `YO2OnlineLauncher.exe`). The same page has the source code as `yo2online-source.zip`.

A fan-made, non-commercial reimplementation of the **game server** of *Yu-Gi-Oh! Online 2: Duel Evolution* (the 2008
client, `yo2.exe` **Ver.080912**), plus a desktop **launcher** that lets anyone join or host a community server without
any central infrastructure.

The original servers were shut down years ago. This project speaks the client's own network protocol, so the unmodified
2008 client can log in, walk around the lobbies, open packs, build decks, trade and duel again — against other players
or against the NPCs.

> **You need your own copy of the game client (`yo2.exe` Ver.080912).** No file from the original game is included in
> this repository, and none is downloaded by it. The server reads the few data files it needs (card ids, pack lists,
> structure decks, NPC deck recipes, titles) from *your* client every time you host, and the launcher builds the
> pre-installed Structure Decks from each player's own client.

> Code comments, server log messages and the generated flag list (`yo2srv/BANDERAS.md`) are mostly in **Spanish**. The
> documentation (including the protocol notes in each `research/<area>/NOTES.md` and [docs/FLAGS.md](docs/FLAGS.md))
> and everything a player sees — the launcher and its texts — are in English.

## What works

- Login gate, accounts and characters (created on first login), language rooms, lobby and map areas, chat (general,
  regional, whisper, team), friends, teams, mail.
- Shop, booster packs, structure decks, Duelpass points and codes, trunk and deck editor, card trades.
- **Duels with real rules**: the server is authoritative and runs the
  [EDOPro/ProjectIgnis ocgcore](https://github.com/edo9300/ygopro-core) engine (through
  [ocgcore-wasm](https://github.com/n1xx1/ocgcore-wasm)); a translator turns engine messages into the client's own
  duel messages. PvP (single and best-of-3 match, turn timer, surrender, spectators) and NPC duels with a scripted AI.
- Two rule profiles: **vanilla** (behave like the 2008 game as far as we know) and **enhanced** (optional fixes).
- Replays, duel logs, moderation tools (kick, ban, accounts, announcements, backups) from the launcher's *Manage* tab.
- The launcher: public server list over the IPFS DHT, private servers with a room code, password-protected servers,
  direct "join by address", LAN discovery, Tor fallback, signed self-updates, several saved servers per PC.

Known gaps: some cards and rulings differ from the 2008 game, a few NPC decks and event features are incomplete, and
parts of the original protocol are still unknown. See *Project status* below.

## Playing (with the launcher)

1. Get the launcher (build it yourself — [BUILDING.md](BUILDING.md) — or use a package someone you trust gives you).
   On Windows that is `YO2OnlineLauncher.exe`; on macOS, `YO2OnlineLauncher.app` (see below).
2. Run it. If Windows SmartScreen warns you, choose *More info → Run anyway* (the build is not code-signed).
3. *Settings → Game folder*: pick the folder that contains your `yo2.exe` **Ver.080912**. Other client versions do not
   work. Do **not** put the launcher inside the game folder over Konami's own `Yo2Launcher.exe`. On macOS that folder
   is inside a [CrossOver](https://www.codeweavers.com/crossover) bottle.
4. *Play*: pick a server and press **Join** (or **Join with code** for a hidden server, or **Join by address**). The
   launcher opens the game for you. Log in with any username and password: the account is created the first time
   (unless the host only allows accounts it creates).

The launcher keeps the connection open while you play. "Direct P2P" is a direct connection between the two PCs; "Tor"
is the slower fallback when the routers in between do not allow a direct one.

### Rebuilding the launcher on macOS

The game client is still Windows `yo2.exe`; the macOS launcher opens it through CrossOver. From a checkout:

```
brew install go zig
go install github.com/wailsapp/wails/v2/cmd/wails@v2.16.0
export PATH="$(go env GOPATH)/bin:$PATH"
cd tools/yo2emu/launcher
./app/build.sh
```

That writes `tools/yo2emu/launcher/dist-app/YO2OnlineLauncher.app`. `zig` is needed to embed the `version.dll` game
hook; without it the app still builds but cannot attach to the client. Full Windows and macOS build notes are in
[BUILDING.md](BUILDING.md#the-launcher).

## Hosting a server

From the launcher: *Host a server → New server*, fill in the form (name, who can join: Public / Password / Hidden,
rules, accounts, starting level, Duelpass points…) and press **Start**. No router ports or VPN are needed; a fixed port
(*Settings → Network*) helps players behind strict NATs connect directly.

Hosting from source needs Python 3.12, Node ≥ 22.13 and the third-party card data (`_vendor/`). The short version:

```
pnpm install                                                       # duel engine dependencies (apps/server)
python tools/yo2emu/paquete/extraer_juego.py --juego "<your game folder>" --salida _original/extracted/merge2008/data
python tools/yo2emu/paquete/generar_tablas.py --datos _original/extracted/merge2008/data --vendor _vendor --juego "<your game folder>"
set YO2_NO_CLIENT=1
python tools/yo2emu/run.py 0                                       # 0 = run until stopped
```

Then point a client at `127.0.0.1` with the launcher (*Play here* when hosting from the launcher, which also starts
`run.py` for you). Full details, environment variables and the "package mode" used by the launcher are in
[BUILDING.md](BUILDING.md) and `tools/yo2emu/paquete/RUTAS.md`.

Your server's accounts, logs and backups stay on your PC (`%LOCALAPPDATA%\YO2Online\` when run from the launcher).

## Architecture in ten lines

1. `yo2.exe` (unmodified) talks the original TCP protocol; a tiny hook (`version.dll` proxy, or Frida as a fallback)
   redirects its connections to `127.0.0.1`.
2. On the player's PC the **launcher** (Go + Wails, `tools/yo2emu/launcher/`) carries those connections through an
   encrypted **libp2p** tunnel (QUIC/TCP, hole punching, relays) or **Tor** to the host.
3. On the host, `tools/yo2emu/run.py` starts the **server** (`yo2srv/`: network, accounts, lobby, shop, world, duel
   ports, console, management API) and loads the protocol modules in `research/*/` (persistence, lobby, trades, teams…).
4. Duels are authoritative: `ocg_duel.py` drives **duelcore** (`duelcore/duelcore.mts`, a Node process running
   ocgcore-wasm) and `research/traductor/translator.py` maps engine messages ↔ the client's `0x6202` duel messages.
5. NPC opponents use a heuristic "brain" (`research/ia/brain.py`) on top of the engine's legal moves.
6. State lives in JSON or SQLite (`storage/`, append-only change chain with snapshots and verification).
7. Card data comes from ProjectIgnis (`_vendor/`); game-specific tables are extracted from the host's own client.
8. Public servers announce a signed "server card" on the public **IPFS DHT** — there is no central server list.
9. Hosts are identified by an ed25519 key (peer ID = .onion address = room code public key); joining proves possession
   of the room secret or password (Argon2id) and the host proves its identity.
10. Launcher and server package updates are signed by the publisher and spread peer to peer between launchers.

More: [docs/NETWORK.md](docs/NETWORK.md) (networking and security), [docs/FLAGS.md](docs/FLAGS.md) (every feature
flag), the `NOTES.md` file of each `research/<area>/` folder (protocol notes).

## Repository layout

| Path | What |
|---|---|
| `tools/yo2emu/run.py`, `yo2srv/` | The server (Python 3.12, standard library only) and its unit tests (`python -m yo2srv.tests`) |
| `tools/yo2emu/research/` | Protocol modules loaded by the server, one folder per area, each with its `NOTES.md` |
| `tools/yo2emu/duelcore/`, `apps/server/src/duel/` | Duel engine process (Node/TypeScript over ocgcore-wasm) |
| `tools/yo2emu/storage/` | JSON/SQLite state store |
| `tools/yo2emu/paquete/` | Game-data extractor, table generator, engine bundler, package builder (`empaquetar.py`), package docs |
| `tools/yo2emu/launcher/` | Launcher: `sala/` (P2P, tunnel, list, updates), `juego/` (game hook, DLL), `app/` (Wails UI), `firmar/` (signing tool) |
| `tools/yo2emu/fake_client.py`, `paridad/` | Scripted fake clients and the regression/parity battery |
| `tools/yo2emu/ghidra/` | Ghidra scripts used to study the client |
| `tools/kcej_dat.py`, `tools/build_cards.py` | Reader for the client's `.dat` archives; card mapping helpers |

## Project status

Work in progress. The vanilla profile aims at 1:1 behaviour with the 2008 servers; anything that is
known to differ, or that we invented because the original behaviour is unknown, is behind a flag
([docs/FLAGS.md](docs/FLAGS.md)). Expect rough edges: some regression tests in `paridad/` and in `research/*/` need private
recordings that are not part of this repository (see [BUILDING.md](BUILDING.md#tests)).

## License

**TODO** — not chosen yet (see [LICENSE](LICENSE)). Third-party components keep their own licenses (see *Credits*).

## Credits

- **EDOPro / ProjectIgnis** — the [ocgcore](https://github.com/edo9300/ygopro-core) rules engine,
  [CardScripts](https://github.com/ProjectIgnis/CardScripts) and [BabelCDB](https://github.com/ProjectIgnis/BabelCDB)
  card database (AGPL-3.0), used unmodified at run time.
- **ocgcore-wasm** by n1xx1 — ocgcore compiled to WebAssembly (MIT).
- **libp2p** ([go-libp2p](https://github.com/libp2p/go-libp2p), go-libp2p-kad-dht) and the public **IPFS** bootstrap
  network; **yamux** (HashiCorp).
- **Tor** (The Tor Project) — the Tor Expert Bundle is embedded as a fallback transport.
- **Wails** — the launcher's desktop window; **Frida** — fallback game hook.
- **Python**, **Node.js**, **esbuild**, **tsx**, **Go**; **w64devkit** to build the hook DLL.
- Fonts: Rubik and Nunito Sans (SIL Open Font License, `launcher/app/frontend/dist/fonts/`).
- Everyone who kept notes, videos and screenshots of the original game alive.
