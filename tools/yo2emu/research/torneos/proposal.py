"""Torneos (CHAMPIONSHIP), eventos (0x5000 EVENTINFO) y millas — emulador de YO2 (cliente Ver.080912).

Todo sale del cliente (descompilado + desensamblado); direcciones y confianza (C/P/D) en NOTES.md. Resumen:

  Evento     S->C 0x5000 EVENTINFO s32 serie, s32 mundo (6 = todos, si no = b1 de la sala), s32 estado
             (impar = empieza, par = acaba; 0xb/0xc = CHAMPIONSHIP: icono de evento 6 -> «¿Ir?» -> CTourMng)
  Cuadro     el cliente (al crear CTourMng y al entrar en la escena 0xd) pide SIN datos, en este orden:
             0x5115 STATUS, 0x5113 TIMER, 0x5107 REQENTRY, 0x5100 INFO, 0x5114 PCOUNTER, 0x5111 LIST
             -> 0x5115 (u32 serie, u32 día, u32 estado 0-9, u32 fase, u32, u32 ronda, u8 subtítulo)
             -> 0x5113 (u32 segundos)          -> 0x5108 (u32 x3) + 0x5109 n x 41 B + 0x5110
             -> 0x5100 (u32, 100 B nombre, u32 día, s32, s32, u32 máx, u32, u32)   [con él: escena 0xd]
             -> 0x5114 (u32 serie, u32 día, u32 vivos, u32 inscritos, u32 público)
             -> 0x5111 x 4 trozos (u32, u32, u32, u32 fase, u8 trozo 0-3; páginas de 8 x (u32 pid, u16 bits))
  Avisos     0x5101 START, 0x5102 END, 0x5103 LOTTERY (u32 fase, u8 1 empieza/0 acaba), 0x5104 LOTRESULT (u8 1
             dentro / 2 fuera), 0x5105 PHASE (u32 fase, u8), 0x5106 ROUND (u32, u32 ronda, u8), 0x5112 ENDRESULT
             (u32, 100 B, u32 día, u32, u32, 16 B campeón, u32, 16 B subcampeón), 0x5120 CHKDUELSTART (u8 1-3:
             «el otro no respondió a tiempo»)
  Duelo      sin petición: 0x43a0 AUTODUELSTART (último u8 = 3) + 0x4306 a los dos; el duelo es el PvP de
             pvp_match (reserva {pidA, pidB}); al acabar el cliente vuelve SOLO a la escena 0xd si su sala es tipo 8.
  Millas     0x5200/0x5202 no los manda nunca este cliente (sin llamadores): no hay nada que contestar. El saldo MP
             (+0xac) se gasta en la tienda (run.py 0x5208/0x5210).

Uso (run.py): TO = load_research("torneos"); TO.install(globals()); LP.HANDLERS.update(TO.HANDLERS);
y en on_conn_closed: TO.on_close(c). Ver NOTES.md §Integración. Todo apagado por defecto (banderas).
"""
import os
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.normpath(os.path.join(HERE, "..", ".."))
if EMU not in sys.path:
    sys.path.insert(0, EMU)
import proto  # noqa: E402

W = proto.W

# ---------------------------------------------------------------- ids (C: protocol.json + manejadores 006c0930..)
M_EVENTINFO = 0x5000
M_INFO, M_START, M_END, M_LOTTERY, M_LOTRESULT, M_PHASE, M_ROUND = 0x5100, 0x5101, 0x5102, 0x5103, 0x5104, 0x5105, 0x5106
M_REQENTRY, M_ENTRY_START, M_ENTRY_DATA, M_ENTRY_END = 0x5107, 0x5108, 0x5109, 0x5110
M_LIST, M_ENDRESULT, M_TIMER, M_PCOUNTER, M_STATUS = 0x5111, 0x5112, 0x5113, 0x5114, 0x5115
M_SENTRY_START, M_SENTRY_DATA, M_SENTRY_END, M_SLIST, M_CHKDUELSTART = 0x5116, 0x5117, 0x5118, 0x5119, 0x5120
M_AUTODUELSTART, M_DUELSERVER = 0x43a0, 0x4306
M_NPCINFOLIST_REQ = 0x4230      # solo se observa (devuelve False): el cliente ya está en el mapa

# Estado del servidor de torneo (0x5115 3er u32 -> +0x36d4; textos de depuración de FUN_0099b4e0) — C
ST_NONE, ST_STANDBY, ST_WAIT_START, ST_WAIT_ENTRY, ST_ENTRY, ST_MATCHING = 0, 1, 2, 3, 4, 5
ST_OPEN, ST_PHASE, ST_ROUND, ST_END = 6, 7, 8, 9
# Subtítulo (0x5115 u8 -> +0x36c4, FUN_0099c220): 0/1 «ronda N», 2 cuartos, 3 semis, 4 final — C
SUB_ROUND, SUB_QUARTER, SUB_SEMI, SUB_FINAL = 1, 2, 3, 4
FINAL_DAY = 0x11                # +0x366c: 1-16 = torneo preliminar «día N», 0x11 = FINAL (本戦) — C
# Eventos (s32 estado de 0x5000; FUN_00a800a0 y FUN_006bf310) — C
EV_CHAMPIONSHIP_ON, EV_CHAMPIONSHIP_OFF = 0xb, 0xc
EVENT_NAMES = {1: "GUNSLINGER", 2: "fin GUNSLINGER", 3: "GUNSLINGER", 4: "fin GUNSLINGER", 5: "Sala TRIAL",
               6: "fin DUELIST TRIAL", 7: "ANNIVERSARY TRIAL", 8: "fin ANNIVERSARY TRIAL", 9: "GUNSLINGER",
               10: "fin GUNSLINGER", 0xb: "CHAMPIONSHIP", 0xc: "fin CHAMPIONSHIP", 0xd: "GUNSLINGER",
               0xe: "fin GUNSLINGER", 0xf: "DUELIST TRIAL", 0x10: "fin DUELIST TRIAL", 0x11: "EVOLUTION MATCH",
               0x12: "fin del evento", 0x15: "ULTIMATE MATCH", 0x16: "fin del evento"}
EVENT_WORLD_ALL = 6             # C: el 2º s32 vale si es 6 o == +0x9ec (b1 de la sala, idioma); P: 6 = «todos»

PAGE_PLAYERS = 8                # C: 8 x (u32, u16) por página de 0x48 B
LIST_CHUNKS = 4                 # C: +0x72 solo con trozo > 2 -> 0..3 (20 páginas por trozo, 73 en total)
MAX_ENTRIES = 0x200             # C: tabla de inscritos +0x4b68, 0x200 x 0x48 B
ENTRY_PER_MSG = 24              # 41 B por inscrito; 24 x 41 = 984 <= 1024 B
DUEL_MODE = 3                   # C (agente duelo): 0/1 sin cronómetro; 3 = duelo suelto (lo que usan las salas)

# ---------------------------------------------------------------- banderas (1:1: lo no visto en el cliente, APAGADO)
# 07-10: TOURNAMENT_ON, EVENTS_ON y AUTO_CLOCK activados por defecto con el OK del owner; YO2_TOURNAMENT=0,
# YO2_EVENTS=0 y YO2_AUTO_CLOCK=0 los apagan (pruebas que suponían el valor viejo)
_ON = lambda k, d="1": os.environ.get(k, d) not in ("", "0")
TOURNAMENT_ON = _ON("YO2_TOURNAMENT")   # contestar la ráfaga 0x51xx con datos (False = auto_replies como antes)
EVENTS_ON = _ON("YO2_EVENTS")           # mandar EVENTINFO al entrar en el mapa (con CURRENT_EVENT)
CURRENT_EVENT = None            # (serie, mundo, estado) p. ej. (1, 6, 0xb). D: calendario del oficial desconocido
EVENT_DELAY = 1.5               # s tras 0x4230 (el cliente ya tiene el menú del mapa). D
AUTO_CLOCK = _ON("YO2_AUTO_CLOCK")      # hilo que avanza el torneo solo, arrancado en install (si no: tick() a mano / admin)
TOUR_NAME = "CHAMPIONSHIP"      # D: los 100 B de 0x5100 (nombre del torneo del oficial: sin dato)
TOUR_DAY = 1                    # D: preliminar del «día 1» (1-16). 0x11 = final
MAX_PLAYERS = 8                 # D: una página. El oficial llenaba hasta 73 páginas (C: capacidad del cliente)
MIN_PLAYERS = 2
ENTRY_SECONDS = 120             # D: inscripción abierta antes del sorteo
STEP_SECONDS = 15               # D: pausa entre sorteo, anuncio, rondas
AUTO_ENTRY = True               # D: estar en el cuadro durante la inscripción = inscrito (el oficial: reserva web + sorteo)
BIT80_ALIVE = True              # D: bit 0x80 de la página = sigue vivo (C: el bit existe; su lector en la escena no se vio)
NOSHOW_ADVANCE_SEAT0 = True     # D: si no se presenta ninguno, pasa el asiento menor
REPAIR_ON_DRAW = True           # P/D: el cliente repite piedra-papel-tijera por el MISMO socket (pvp_match no): se re-empareja
PRIZE_TP = 0                    # D: puntos de torneo (+0x124) al campeón; 0 = nada (premios del oficial desconocidos)
SEND_PAUSE = 0.03

NS = {}
LOCK = threading.RLock()
WATCH = {}                      # pid -> conexión que está en el cuadro (mandó la ráfaga)
TOUR = None                     # torneo en curso (Tour) o None
PAUSED = False                  # control del anfitrión; no requiere reiniciar run.py
_CLOCK = None
CLOCK = time.time               # reloj del torneo (las pruebas lo cambian)


def install(ns):
    """Globals VIVOS de run.py: HUB, PVP, CTX, load_state/save_state, STATE_LOCK, PP, duel_parts, duel_ip_for,
    DUEL_PORT, PUBLIC_IP, log."""
    global NS
    NS = ns if isinstance(ns, dict) else dict(ns)
    if AUTO_CLOCK:
        start_clock()
    _log(f"[torneos] instalado (torneo={'sí' if TOURNAMENT_ON else 'no'}, eventos={'sí' if EVENTS_ON else 'no'})")


def _log(*a):
    (NS.get("log") or print)(*a)


def _sess(c):
    return getattr(c, "sess", None)


def _fix(text, n):
    raw = str(text or "").encode("latin1", "replace")[:n - 1]
    return raw + bytes(n - len(raw))


def _send(c, mid, data=b""):
    if c is None or getattr(c, "closed", False):
        return
    try:
        (c.post if hasattr(c, "post") else c.send)(mid, data, SEND_PAUSE)
    except Exception as e:                                       # noqa: BLE001
        _log(f"[torneos] 0x{mid:04x}: {e!r}")


# ---------------------------------------------------------------- mensajes (formatos C)
def eventinfo(serial, world, state):
    return W().u32(serial).u32(world).u32(state).bytes()


def info_msg(t):
    if t is None:
        return W().u32(0).bytes() + bytes(100) + W().u32(0).u32(0).u32(0).u32(0).u32(0).u32(0).bytes()
    return (W().u32(t.serial).bytes() + _fix(t.name, 100) +
            W().u32(t.day).u32(0).u32(0).u32(t.max_players).u32(t.total_rounds()).u32(0).bytes())


def status_msg(t):
    if t is None:
        return W().u32(0).u32(0).u32(ST_STANDBY).u32(0).u32(0).u32(0).u8(0).bytes()
    return W().u32(t.serial).u32(t.day).u32(t.state).u32(t.phase).u32(t.phase).u32(t.round).u8(t.subtitle()).bytes()


def timer_msg(t):
    return W().u32(t.seconds_left() if t is not None else 0).bytes()


def pcounter_msg(t):
    if t is None:
        return W().u32(0).u32(0).u32(0).u32(0).u32(0).bytes()
    return W().u32(t.serial).u32(t.day).u32(len(t.alive())).u32(len(t.seats)).u32(t.audience()).bytes()


def entry_msgs(t):
    """0x5108 (3 x u32, el cliente los lee y no los guarda: C) + 0x5109 en trozos + 0x5110 (pone +0x70)."""
    rows = []
    for s in (t.seats if t is not None else [])[:MAX_ENTRIES]:
        rows.append(W().u32(s["pid"]).bytes() + _fix(s["name"], 16) +
                    bytes([s["wins"] & 0xff, t.entry_status(s)]) + bytes(4) + bytes(16))
    out = [(M_ENTRY_START, W().u32(len(rows)).u32(0).u32(0).bytes())]
    for i in range(0, len(rows), ENTRY_PER_MSG):
        out.append((M_ENTRY_DATA, b"".join(rows[i:i + ENTRY_PER_MSG])))
    out.append((M_ENTRY_END, b""))
    return out


def page_bytes(t, page):
    """Página de 8 del cuadro: u8 nº de jugadores, u8 (sin lector: 0), 8 x (u32 pid, u16 bits en el byte BAJO)."""
    seats = t.seats[page * PAGE_PLAYERS:(page + 1) * PAGE_PLAYERS] if t is not None else []
    w = W().u8(len(seats)).u8(0)
    for i in range(PAGE_PLAYERS):
        if i < len(seats):
            s = seats[i]
            bits = (s["wins"] & 0x3f) | (0x40 if s["playing"] else 0) | (0x80 if (BIT80_ALIVE and s["alive"]) else 0)
            w.u32(s["pid"]).u16(bits)
        else:
            w.u32(0).u16(0)
    return w.bytes()


def list_msgs(t):
    """0x5111 en 4 trozos: trozo 0 borra las 73 páginas, el 3 avisa (+0x72). 20 páginas por trozo (0x5a0 B)."""
    pages = (len(t.seats) + PAGE_PLAYERS - 1) // PAGE_PLAYERS if t is not None else 0
    phase = t.phase if t is not None else 0
    out = []
    for chunk in range(LIST_CHUNKS):
        body = b"".join(page_bytes(t, p) for p in range(chunk * 20, min(pages, chunk * 20 + 20)))
        out.append((M_LIST, W().u32(0).u32(0).u32(0).u32(phase).u8(chunk).bytes() + body))
    return out


def endresult_msg(t):
    champ = t.by_pid(t.champion) if t.champion else None
    second = t.by_pid(t.runner_up) if t.runner_up else None
    return (W().u32(t.serial).bytes() + _fix(t.name, 100) + W().u32(t.day).u32(len(t.seats)).u32(t.total_rounds())
            .bytes() + _fix(champ["name"] if champ else "", 16) + W().u32(0).bytes() +
            _fix(second["name"] if second else "", 16))


def autoduelstart(rival_pid, rival_name, parts16):
    return (W().u32(rival_pid).s(rival_name[:15], 16).u8(0).u16(0).u16(0).bytes() +
            bytes(parts16 or b"")[:16].ljust(16, b"\0") + bytes([DUEL_MODE]))


# ---------------------------------------------------------------- torneo
class Tour:
    """Un torneo de eliminación en una página (MAX_PLAYERS <= 8 por defecto). Asientos en orden de inscripción;
    ronda r: los vivos por orden de asiento, de dos en dos (impar: pasa sin jugar, D)."""

    def __init__(self, serial=1, day=TOUR_DAY, name=TOUR_NAME, max_players=MAX_PLAYERS, now=None):
        self.serial, self.day, self.name, self.max_players = serial, day, name, max_players
        self.state, self.phase, self.round = ST_ENTRY, 1, 0
        self.seats = []               # {"pid","name","wins","alive","playing","sess"}
        self.matches = {}             # id(PvpMatch) -> dict
        self.deadline = (now if now is not None else CLOCK()) + ENTRY_SECONDS
        self.champion = self.runner_up = None
        self.history = []

    # -- consultas
    def by_pid(self, pid):
        return next((s for s in self.seats if s["pid"] == pid), None)

    def alive(self):
        return [s for s in self.seats if s["alive"]]

    def total_rounds(self):
        n, r = max(len(self.seats), 2), 0
        while (1 << r) < n:
            r += 1
        return r

    def subtitle(self):
        if self.state < ST_ROUND:
            return 0
        n = len(self.alive())
        return SUB_FINAL if n <= 2 else SUB_SEMI if n <= 4 else SUB_QUARTER if n <= 8 else SUB_ROUND

    def seconds_left(self, now=None):
        if self.deadline is None:
            return 0
        return max(0, int(self.deadline - (now if now is not None else CLOCK())))

    def audience(self):
        with LOCK:
            return sum(1 for p in WATCH if self.by_pid(p) is None)

    def entry_status(self, s):
        return 1 if s["playing"] else 0         # P: +0x16 == 1 = tiene duelo (la escena lo mira en estado 7)


def _watchers():
    with LOCK:
        return list(WATCH.items())


def _to_all(msgs):
    for _pid, c in _watchers():
        for mid, data in msgs:
            _send(c, mid, data)


def _to_pid(pid, msgs):
    with LOCK:
        c = WATCH.get(pid)
    if c is None:
        hub = NS.get("HUB")
        s = hub.by_pid(pid) if hub is not None else None
        c = getattr(s, "lobby", None)
    for mid, data in msgs:
        _send(c, mid, data)


def _refresh_all(t):
    """Lo que la escena repinta: estado, temporizador, contador, inscritos y cuadro."""
    _to_all([(M_STATUS, status_msg(t)), (M_TIMER, timer_msg(t)), (M_PCOUNTER, pcounter_msg(t))]
            + entry_msgs(t) + list_msgs(t))


# ---------------------------------------------------------------- arranque / avance (admin, reloj o pruebas)
def start_tournament(serial=1, day=TOUR_DAY, name=TOUR_NAME, max_players=MAX_PLAYERS, now=None):
    """Abre la inscripción (estado 4). Los que ya estén en el cuadro se inscriben (AUTO_ENTRY)."""
    global TOUR, PAUSED
    with LOCK:
        TOUR = Tour(serial, day, name, max_players, now)
        PAUSED = False
        if AUTO_ENTRY:
            for pid, c in list(WATCH.items()):
                _enter(TOUR, _sess(c), pid)
    _log(f"[torneos] inscripción abierta: {name} día {day} (máx. {max_players}, {ENTRY_SECONDS} s)")
    _to_all([(M_START, b""), (M_INFO, info_msg(TOUR))])
    _refresh_all(TOUR)
    return TOUR


def pause_tournament():
    """Pausa el avance automático/manual sin cerrar la inscripción ni el cuadro."""
    global PAUSED
    with LOCK:
        PAUSED = True
    _log("[torneos] pausado por administración")
    return TOUR


def resume_tournament():
    """Reanuda el reloj; el siguiente tick continúa desde el estado actual."""
    global PAUSED
    with LOCK:
        PAUSED = False
    _log("[torneos] reanudado por administración")
    return TOUR


def finish_tournament():
    """Cierra el torneo actual sin declarar campeón ni reiniciar el servidor."""
    global PAUSED
    t = TOUR
    if t is None:
        return None
    with LOCK:
        PAUSED = False
        _finish(t, CLOCK(), cancelled=True)
    return t


def _enter(t, s, pid):
    if t is None or t.state != ST_ENTRY or t.by_pid(pid) is not None or len(t.seats) >= t.max_players:
        return False
    t.seats.append({"pid": pid, "name": getattr(s, "name", "") or f"Duelista {pid}", "wins": 0, "alive": True,
                    "playing": False, "sess": s})
    return True


def tick(now=None):
    """Avanza el torneo si ha vencido el plazo actual. Devuelve el estado."""
    t = TOUR
    if t is None:
        return None
    if PAUSED:
        return t.state
    now = now if now is not None else CLOCK()
    with LOCK:
        if t.deadline is None or now < t.deadline:
            return t.state
        if t.state == ST_ENTRY:
            _draw(t, now)
        elif t.state in (ST_MATCHING, ST_OPEN, ST_PHASE):
            _next_round(t, now)
        elif t.state == ST_ROUND and not _pending(t):
            _next_round(t, now)
    return t.state


def _draw(t, now):
    """Sorteo (LOTTERY): con <= MAX_PLAYERS entran todos (el oficial elegía por sorteo: D)."""
    if len(t.seats) < MIN_PLAYERS:
        _log(f"[torneos] sin jugadores suficientes ({len(t.seats)}): se cierra")
        _finish(t, now, cancelled=True)
        return
    t.state, t.phase, t.deadline = ST_MATCHING, 2, now + STEP_SECONDS
    _to_all([(M_LOTTERY, W().u32(t.phase).u8(1).bytes())])
    for pid, _c in _watchers():
        _to_pid(pid, [(M_LOTRESULT, bytes([1 if t.by_pid(pid) is not None else 2]))])
    _to_all([(M_LOTTERY, W().u32(t.phase).u8(0).bytes())])
    t.phase = 3
    _refresh_all(t)


def _pending(t):
    return any(not m["done"] for m in t.matches.values())


def _next_round(t, now):
    alive = t.alive()
    if t.round > 0:
        _to_all([(M_ROUND, W().u32(t.phase).u32(t.round).u8(0).bytes())])
    if len(alive) <= 1:
        _finish(t, now)
        return
    if t.state != ST_ROUND:
        t.state, t.phase = ST_PHASE, 4
        _to_all([(M_PHASE, W().u32(t.phase).u8(1).bytes())])
    t.round += 1
    t.state, t.deadline = ST_ROUND, None
    _to_all([(M_ROUND, W().u32(t.phase).u32(t.round).u8(1).bytes())])
    pairs = [alive[i:i + 2] for i in range(0, len(alive), 2)]
    for p in pairs:
        if len(p) == 1:                                       # D: pasa sin jugar
            p[0]["wins"] += 1
            t.history.append((t.round, p[0]["pid"], None, "bye"))
    _refresh_all(t)
    for p in pairs:
        if len(p) == 2:
            _pair(t, p[0], p[1])


def _pair(t, a, b):
    """Reserva el PvP y manda 0x43a0 + 0x4306 a los dos (sin petición, C)."""
    pvp = NS.get("PVP")
    sa, sb = a.get("sess"), b.get("sess")
    hub = NS.get("HUB")
    if hub is not None:
        sa = hub.by_pid(a["pid"]) or sa
        sb = hub.by_pid(b["pid"]) or sb
    rec = {"a": a["pid"], "b": b["pid"], "done": False, "winner": None, "m": None}
    a["playing"] = b["playing"] = True
    if pvp is not None and sa is not None and sb is not None:
        m = pvp.reserve(sa, sb)
        rec["m"] = m
        _hook_match(t, m, rec)
        key = id(m)
    else:
        key = ("sin-pvp", a["pid"], b["pid"], t.round)
    t.matches[key] = rec
    port = NS.get("DUEL_PORT", 5760)
    ipf = NS.get("duel_ip_for") or (lambda _c: NS.get("PUBLIC_IP", "127.0.0.1"))
    for me, ot, so in ((a, b, sb), (b, a, sa)):
        with LOCK:
            c = WATCH.get(me["pid"])
        _to_pid(me["pid"], [(M_AUTODUELSTART, autoduelstart(ot["pid"], ot["name"], _parts(so))),
                            (M_DUELSERVER, W().s(ipf(c), 15).u16(port).bytes())])
    _log(f"[torneos] ronda {t.round}: {a['name']} contra {b['name']}")
    _refresh_all(t)
    return rec


def _parts(s):
    """16 B de avatar de s (duel_parts de run.py en la vista de su cuenta)."""
    dp, ctx = NS.get("duel_parts"), NS.get("CTX")
    if dp is None or s is None:
        return bytes(16)
    old = getattr(ctx, "sess", None) if ctx is not None else None
    try:
        if ctx is not None:
            ctx.sess = s
        return dp(None)
    except Exception:                                            # noqa: BLE001
        return bytes(16)
    finally:
        if ctx is not None:
            ctx.sess = old


def _hook_match(t, m, rec):
    """Sin tocar pvp_match: se envuelven _finish (resultado por cliente) y _close_match (fin) de ESTA partida."""
    orig_finish, orig_close = m._finish, m._close_match

    def finish(k, wv, *a, **kw):
        try:
            if wv == 0:
                rec["winner"] = m.sess[k].pid
            elif wv is None and rec["winner"] is None:
                rec["winner"] = "draw"
        except Exception:                                        # noqa: BLE001
            pass
        return orig_finish(k, wv, *a, **kw)

    def close():
        try:
            return orig_close()
        finally:
            present = [m.sess[k].pid for k in (0, 1) if m.c[k] is not None]
            report(rec, rec["winner"], present)

    m._finish, m._close_match = finish, close


def report(rec, winner, present=()):
    """Fin de una partida del torneo. winner = pid, "draw" o None (no empezó: present = los que conectaron)."""
    t = TOUR
    if t is None or rec.get("done"):
        return
    with LOCK:
        a, b = t.by_pid(rec["a"]), t.by_pid(rec["b"])
        if winner == "draw" and REPAIR_ON_DRAW:
            rec["done"] = True
            _log(f"[torneos] empate {a['name']} - {b['name']}: se repite")
            _pair(t, a, b)
            return
        if winner in (None, "draw"):
            here = [p for p in present if p in (rec["a"], rec["b"])]
            if len(here) == 1:
                winner = here[0]
                _to_pid(winner, [(M_CHKDUELSTART, bytes([1]))])  # «el otro no respondió a tiempo» (C texto; P uso)
            elif NOSHOW_ADVANCE_SEAT0:
                winner = rec["a"] if t.seats.index(a) < t.seats.index(b) else rec["b"]
        rec["done"], rec["winner"] = True, winner
        w, l_ = (a, b) if winner == rec["a"] else (b, a)
        w["wins"] += 1
        l_["alive"] = False
        a["playing"] = b["playing"] = False
        t.history.append((t.round, w["pid"], l_["pid"], "ok"))
        _log(f"[torneos] ronda {t.round}: gana {w['name']}")
        if len(t.alive()) == 1:
            t.runner_up = l_["pid"]
        if not _pending(t):
            t.deadline = CLOCK() + STEP_SECONDS
        _refresh_all(t)


def _finish(t, now, cancelled=False):
    global TOUR
    alive = t.alive()
    if t.round > 0:
        _to_all([(M_PHASE, W().u32(t.phase).u8(0).bytes())])
    t.state, t.deadline = ST_END, None          # fase: se queda (5/6 = consola de la FINAL: solo día 0x11)
    if not cancelled and len(alive) == 1:
        t.champion = alive[0]["pid"]
        _award(t)
        _to_all([(M_ENDRESULT, endresult_msg(t))])
    _to_all([(M_END, b"")])
    _refresh_all(t)
    _log(f"[torneos] fin: campeón {alive[0]['name'] if (alive and not cancelled) else '-'}")


def _award(t):
    ER = NS.get("ER")
    if not PRIZE_TP and ER is None:
        return
    ctx, PP = NS.get("CTX"), NS.get("PP")
    if PP is None or "load_state" not in NS:
        return
    for pid, placement in ((t.runner_up, "subcampeon"), (t.champion, "campeon")):
        row = t.by_pid(pid) if pid is not None else None
        s = row.get("sess") if row else None
        if s is None:
            continue
        old = getattr(ctx, "sess", None)
        try:
            ctx.sess = s
            lk = NS.get("STATE_LOCK") or threading.RLock()
            with lk:
                st = NS["load_state"]()
                p = PP.profile(st, s.slot if getattr(s, "slot", None) is not None else PP.active_slot(st))
                if placement == "campeon" and PRIZE_TP:
                    r = p.setdefault("rec", {})
                    r["tp"] = int(r.get("tp", 0) or 0) + int(PRIZE_TP)
                if ER is not None and hasattr(ER, "championship_award"):
                    ER.championship_award(p, placement)
                NS["save_state"](st)
        finally:
            ctx.sess = old


# ---------------------------------------------------------------- reloj
def start_clock(period=1.0):
    global _CLOCK
    if _CLOCK is not None and _CLOCK.is_alive():
        return

    def run():
        while AUTO_CLOCK:
            try:
                tick()
            except Exception as e:                               # noqa: BLE001
                _log(f"[torneos] reloj: {e!r}")
            time.sleep(period)
    _CLOCK = threading.Thread(target=run, name="torneos-reloj", daemon=True)
    _CLOCK.start()


# ---------------------------------------------------------------- eventos
def push_event(state=None, world=EVENT_WORLD_ALL, serial=1, conns=None):
    """EVENTINFO a todos los lobbies (o a conns). state None = CURRENT_EVENT."""
    global CURRENT_EVENT
    if state is not None:
        CURRENT_EVENT = (serial, world, state)
    if CURRENT_EVENT is None:
        return 0
    data = eventinfo(*CURRENT_EVENT)
    if conns is None:
        hub = NS.get("HUB")
        conns = [s.lobby for s in (list(hub.sessions.values()) if hub is not None else []) if s.lobby is not None]
    for c in conns:
        _send(c, M_EVENTINFO, data)
    return len(conns)


def h_npclist(c, msg_id, param, data):
    """0x4230: el mapa está montado. Solo se observa (False: contesta run.py); EVENTINFO va después, por la cola."""
    if EVENTS_ON and CURRENT_EVENT is not None:
        threading.Timer(EVENT_DELAY, lambda: _send(c, M_EVENTINFO, eventinfo(*CURRENT_EVENT))).start()
    return False


# ---------------------------------------------------------------- ráfaga del cuadro
def _watch(c):
    s = _sess(c)
    pid = getattr(s, "pid", None)
    if pid is None:
        return None
    with LOCK:
        WATCH[pid] = c
        if AUTO_ENTRY:
            _enter(TOUR, s, pid)
    return pid


def h_tour(c, msg_id, param, data):
    if not TOURNAMENT_ON:
        return False
    t = TOUR
    if msg_id == M_STATUS:
        _watch(c)
        c.send(M_STATUS, status_msg(t))
    elif msg_id == M_TIMER:
        c.send(M_TIMER, timer_msg(t))
    elif msg_id == M_REQENTRY:
        _watch(c)
        for mid, d in entry_msgs(t):
            c.send(mid, d)
    elif msg_id == M_INFO:
        c.send(M_INFO, info_msg(t))
    elif msg_id == M_PCOUNTER:
        c.send(M_PCOUNTER, pcounter_msg(t))
    elif msg_id == M_LIST:
        for mid, d in list_msgs(t):
            c.send(mid, d)
    elif msg_id == M_ENDRESULT:                      # C: sin llamadores en este cliente
        if t is not None and t.champion:
            c.send(M_ENDRESULT, endresult_msg(t))
        else:
            return False
    else:
        return False
    return True


def on_close(c):
    """El lobby del que miraba el cuadro se cierra: deja de recibir avisos (sigue inscrito: si no se presenta a su
    duelo, pierde por no presentado)."""
    with LOCK:
        for pid, cc in list(WATCH.items()):
            if cc is c:
                WATCH.pop(pid, None)


def status():
    t = TOUR
    if t is None:
        return "sin torneo"
    return (f"{t.name} día {t.day}: estado {t.state} fase {t.phase} ronda {t.round}, "
            f"{len(t.alive())}/{len(t.seats)} vivos, {len(WATCH)} mirando, {t.seconds_left()} s")


HANDLERS = {M_STATUS: h_tour, M_TIMER: h_tour, M_REQENTRY: h_tour, M_INFO: h_tour, M_PCOUNTER: h_tour,
            M_LIST: h_tour, M_ENDRESULT: h_tour, M_NPCINFOLIST_REQ: h_npclist}


def handle(c, msg_id, param, data):
    h = HANDLERS.get(msg_id)
    return bool(h and h(c, msg_id, param, data))
