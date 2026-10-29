"""Propuesta del agente 3 (lobby y salas): manejadores para los mensajes prioritarios del lobby de YO2.

NO está enchufado a run.py. Integración propuesta (2 líneas en run.py, la hace el integrador):

    # tras definir lobby_reply / HANDLERS en run.py:
    sys.path.insert(0, os.path.join(HERE, "research", "lobby")); import proposal as LP; LP.install(globals())
    # al principio de lobby_reply, justo después del «if msg_id in (0x3001, 0x3003)»:
    if LP.handle(c, msg_id, param, data): return

LP.handle devuelve False para lo que no maneja (y para REQCREATEROOM con rival != 0, que sigue yendo por el flujo
de NPC que ya tiene run.py), así que lo demás sigue igual. Evidencias (direcciones del exe) en NOTES.md.

Convenciones vistas en el cliente (ver NOTES.md):
- Casi todos los «...RESULT» son un s32 que acaba en la tabla de resultados CNetLobby+0x7e0+4*k; la tarea del lobby
  (FUN_006d1840) o la escena lo leen con FUN_006adfb0/FUN_006d3990 y 0 = éxito, cualquier otro valor = error.
  Excepciones: DUELROOMLISTSTART y NPCINFOLIST_START (>= 0 = nº de entradas), REQTRADERESULT (error solo si -493),
  TEAMLISTRESULT_BEGIN (s32 = nº de miembros), FRIENDNOTICE (1 = aceptado), SELECTCARDPACKRESULT (cualquier valor
  != -150 = ha llegado).
- Pids de «jugadores» falsos (NPC que hacen de jugador): FAKE_BASE + id de NPC (100001 = NPC 1), como ya se probó
  con «Yugi» 100001 en el log 20261006-010128.

Prueba: python proposal.py --test   (ast + tamaños de cada respuesta contra _original/ghidra/protocol.json + casos
de la ola 1)

Ola 1 (lote L6, 2026-10-06; huecos de research/audit/BACKLOG.md):
- B12: `_pid()`/`_me()` se calculan en cada llamada (my_pid()/my_name() de run.py si existen; si no, hueco activo
  de persistencia + 1, que es lo que manda PSELECTRESULT). `install` guarda el dict de run.py VIVO, no una copia.
- B11: `_duel_notice` apunta LOBBY_ROOM["c"] y llama a DF["h_autoduelstart"] (sala propia + reto de NPC).
- B13/B40: las cartas de 0x4803 se suman al baúl del perfil y se guardan antes de mandar; u8 «nueva» real.
- B58: `on_close(c)` (y 0x0003 lo llama en silencio): borra las salas de esa conexión, la oferta pendiente y
  LOBBY_ROOM["c"] si era ella. Los temporizadores no mandan por una conexión cerrada o sustituida.
- B60: si un manejador falla después de mandar algo, `handle` devuelve True (no hay respuesta doble).
- B63: las salas de NPC (invisibles en el cliente) ya no se anuncian (NPC_ROOMS = 0).
- B61: auto_overrides.json: 0x8303 -> 0x8304 de 13 B.
"""
import os, sys, json, struct, threading, random, time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.normpath(os.path.join(HERE, "..", ".."))
if EMU not in sys.path:
    sys.path.insert(0, EMU)
import proto

W = proto.W
FAKE_BASE = 100000            # pid de un NPC que hace de jugador = FAKE_BASE + id NPC (1-98)
# 10-10 (owner): «en el original ningún NPC te reta» -> APAGADO por defecto (None = nunca). Era un añadido nuestro de
# antes del PvP. YO2_NPC_CHALLENGE=1 lo enciende (perfil enhanced si se quiere), YO2_NPC_CHALLENGE_DELAY = segundos.
CHALLENGE_DELAY = (float(os.environ.get("YO2_NPC_CHALLENGE_DELAY") or 8.0)
                   if os.environ.get("YO2_NPC_CHALLENGE", "0") not in ("", "0") else None)
FRIEND_ACCEPT_DELAY = 1.5     # s hasta que la NPC acepta la amistad
# Salas de duelo «abiertas» por NPC famosas en cada sala de las puertas. B63: 0 = no se anuncian. El cliente solo
# enseña (y deja pulsar, FUN_006b69f0) las salas cuyo P1 es un jugador del bloque (BLOCKPLAYERLIST/0x3112), y las
# NPC no lo son: hasta que se metan como jugadores falsos del bloque, anunciarlas no sirve de nada.
NPC_ROOMS = 0
MAX_DATA = 1024

# --- Tanda «espectador» (2026-10-06; research/lobby/NOTES.md §7-§9). Tres flags NUEVOS, apagados por defecto; se
# encienden con la variable de entorno al arrancar run.py (o poniendo el valor a True).
_ON = lambda k, d="": os.environ.get(k, d) not in ("", "0")
# §7 0x5600 LIMITATIONLIST tras PSELECT: lista de prohibidas/limitadas de 2008 (research/ia/cards_yo2.csv, columna
# `limit` 0/1/2). Sin ella el cliente deja 3 copias de todo (FUN_0073c290 devuelve 3 si la carta no está).  C (cliente)
LIMIT_LIST = _ON("YO2_LIMIT_LIST", "1")   # 06-10: activado por defecto con el OK del owner (=0 lo apaga)
UNRELEASED = ()               # ids YO2 para 0x5601 UNRELEASELIST: la lista del oficial es desconocida (D) -> no se manda
# §8 ver duelos de otros: 0x4320 REQWATCH -> 0x4321 con el servidor de duelo; el resto en pvp_match (0x6700/0x6702).
WATCH = _ON("YO2_WATCH", "1")   # 06-10: activado por defecto con el OK del owner (=0 lo apaga)
# §9 la sala sobrevive al duelo: al volver al mapa (0x4210), P2 = 0, mstat el de creación, 0x3113 al bloque y estado
# 100 otra vez. Sin esto la sala queda «ocupada» con el rival y sigue bloqueando Menú/Mover.  P (servidor) / C (cliente)
ROOM_REOPEN = _ON("YO2_ROOM_REOPEN", "1")   # 06-10: activado por defecto con el OK del owner (=0 lo apaga)

# --- Tanda «mensajes que faltaban» (2026-10-06; research/lobby/NOTES.md §10). Flags NUEVOS; encendidos solo los 1:1 C.
# 0x42a0 RESETAUTOWAIT -> 0x42a2 s32 0 (tarea FUN_006d1840 casos 0x13/0x14, k=0x20; escena FUN_0087e810).  C
RESETAUTOWAIT = _ON("YO2_RESETAUTOWAIT", "1")
# 0x8203 REQUSBDPAUTH -> 0x8204 s32 ≠0 (no hay servidor USB de tipo 6; FUN_006c1710 solo lee más si s32 == 0).  C
USBDPAUTH_REFUSE = _ON("YO2_USBDPAUTH_REFUSE", "1")
# 0x8301 REQTPPRESENTINFO -> 0x8302 u16 carta premio TP (FUN_006c2800). Formato C; la carta del oficial D.
TPPRESENT = _ON("YO2_TPPRESENT", "")
TP_PRESENT_CARD = 0           # D: qué carta daba el oficial por ranking TP (sin flag: auto_replies manda lo mismo, 0)
# 0x8401 REQTPCARDGET (lista elegida en presentEntry.cpp) -> 0x8402 STARTUP_PRESENTGET_RESULT con esas cartas.
# Formato C; que el oficial devolviera la misma lista y el tope de cartas por regalo: P.
STARTUP_PRESENT = _ON("YO2_STARTUP_PRESENT", "1")   # 07-10: activado por defecto con el OK del owner (=0 lo apaga)

# --- Tanda «autoduelo» (2026-10-07; research/lobby/NOTES.md «Autoduelo»). «Menú de Duelo» › «Autoduelo» manda
# 0x4288 con mstat (3.er byte) = 1 (CNetLobby+0xa4, FUN_00a3a870; FUN_0087e810: tras el duelo, sala propia + mstat 1
# -> 0x42a0 RESETAUTOWAIT). El cliente cuenta a los demás con +0x130 = 0x66 como «Autoduelo» (FUN_00888970 ->
# texto 5086) y les pone el icono 3 (FUN_0088d170); con 0x66 la ficha NO ofrece «Retar» (FUN_0088add0: 100..0x65).
# AUTODUEL: sala con mstat 1 -> estado 0x66 para los demás (también al volver del duelo y con 0x42a0) y REQMATCH a
# esa sala -> 0x4301 −602 (0x11c9 «Este Jugador busca Autoduelos.»).  C (cliente) / P (que el oficial mandara 0x66
# y −602 justo ahí; −602/−604/−606 dan el mismo texto). =0 lo apaga.
AUTODUEL = _ON("YO2_AUTODUEL", "1")
AUTO_STATE = 0x66             # +0x130 «busca autoduelo» (cuenta 0x514 «Autoduelo» y icono 3)
R_AUTO = -602                 # 0x11c9 «Este Jugador busca Autoduelos.»
# AUTODUEL_MATCH: el servidor empareja a dos jugadores en autoduelo del mismo bloque: 0x43a0 AUTODUELSTART + 0x4306
# a los dos (formato C; el cliente lo trata en el mapa: FUN_00863ec0 k=0x1f -> estado 0x3d -> FUN_0087ca70, el mismo
# camino que el duelo directo contra NPC, probado en vivo) y reserva el PvP. Las REGLAS de emparejamiento del oficial
# (tipo 3/4/6, «nivel similar», bloque o servidor, orden) no están en el cliente: D. Estado en el duelo 0x69 (P: lo
# cuentan «Autoduelo» y «Duelo» a la vez). Solo duelo simple (dstyle 3, o 3 con 6): el byte de modo para match es D.
AUTODUEL_MATCH = _ON("YO2_AUTODUEL_MATCH", "1")   # 07-10: activado por defecto con el OK del owner (=0 lo apaga)
AUTO_DUEL_STATE = 0x69
AUTODUEL_LV_DIFF = 5          # D: «Buscar Jugadores de Nivel Similar» (+0xbc = 1) -> diferencia máxima de LV
AUTODUEL_MATCH_DELAY = 1.0    # s tras crear/rearmar la espera (el aviso k=0x1f se queda puesto hasta que lo lea)

NS = {}                       # globals() de run.py (install)
LOCK = threading.RLock()
MEM = {"rooms": {}, "next_room": 1000, "offer": None, "mail": [], "next_mail": 1, "friends": [],
       "black": [], "team": None, "challenges": {}}


# ---------------------------------------------------------------- utilidades
def install(ns):
    """Recibe los globals de run.py: usa load_state/save_state, NPCS, PLAYER_ID, DUEL, DUEL_PORT, LOBBY_PORT,
    shop_purchase, npc_parts, PP, DF, LOBBY_ROOM, CONNS, STATE_LOCK, my_slot/my_pid/my_name y log si existen.
    B12: se guarda el dict VIVO (no una copia), así que lo que run.py defina o cambie después de install (CONNS,
    my_pid, un DF recargado, PLAYER_ID) se ve al momento."""
    global NS
    NS = ns if isinstance(ns, dict) else dict(ns)
    try:
        _load()
    except Exception as e:     # load_state de run.py ahora lanza si state.json es ilegible: el lobby sigue sin correo
        _log(f"[lobby] no se pudo leer state.json['lobby'] al instalar: {e!r} (correo/amigos/equipo en memoria)")


def _log(*a):
    (NS.get("log") or print)(*a)


def _admin_ev(name, *a, **k):
    """Panel (research/admin/eventos.py, 07-10): evento para «Actividad» / «Chat en vivo». Sin panel no hace nada;
    nunca lanza."""
    try:
        m = sys.modules.get("yo2_eventos")
        f = getattr(m, name, None) if m is not None else None
        return f(*a, **k) if callable(f) else None
    except Exception:                                # noqa: BLE001
        return None


def _store():
    """Parte persistente (state.json["lobby"]) si run.py da load_state/save_state; si no, memoria."""
    if "load_state" in NS:
        st = NS["load_state"]()
        return st, st.setdefault("lobby", {})
    return None, MEM


_LOADED = [False]       # ¿se leyó state.json["lobby"]? Si no, _save no lo pisa con listas vacías


def _load():
    st, lob = _store()
    for k in ("mail", "next_mail", "friends", "black", "team"):
        if k in lob:
            MEM[k] = lob[k]
    _LOADED[0] = True


class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _state_lock():
    """Cerrojo de state.json de run.py (STATE_LOCK, lote L1) si existe: leer-modificar-guardar sin pisar a otro
    hilo. Sin él, como antes."""
    lk = NS.get("STATE_LOCK")
    return lk if lk is not None and hasattr(lk, "__enter__") else _NoLock()


def _save():
    if "save_state" not in NS:
        return
    if not _LOADED[0]:
        _log("[lobby] state.json['lobby'] no se llegó a leer: no se guarda encima (cambio solo en memoria)")
        return
    with _state_lock():
        st, lob = _store()
        for k in ("mail", "next_mail", "friends", "black", "team"):
            lob[k] = MEM[k]
        NS["save_state"](st)


def _call(name):
    """Valor de la función `name` de run.py (my_slot/my_pid/my_name, B12), o None si no existe o falla."""
    f = NS.get(name)
    if callable(f):
        try:
            return f()
        except Exception as e:
            _log(f"[lobby] {name}() falló: {e!r}")
    return None


def _slot(st=None):
    """Hueco del personaje activo (0-3): my_slot() de run.py, o el activo de persistencia, o 0."""
    v = _call("my_slot")
    if v is not None:
        return int(v)
    PP = NS.get("PP")
    if PP is not None and hasattr(PP, "active_slot"):
        try:
            return int(PP.active_slot(st if st is not None else NS["load_state"]()))
        except Exception:
            pass
    return 0


def _pid():
    """B12: pid del jugador, calculado en cada llamada (no el PLAYER_ID copiado al instalar). Con persistencia,
    PSELECTRESULT manda pid = hueco + 1 (persistencia.h_pselect), así que aquí también; sin ella, PLAYER_ID."""
    v = _call("my_pid")
    if v is not None:
        return int(v)
    if NS.get("PP") is not None and "load_state" in NS:
        return _slot() + 1
    return NS.get("PLAYER_ID", 1)


def _me():
    """B12: nombre del personaje activo (no siempre players["0"])."""
    v = _call("my_name")
    if v:
        return str(v)
    try:
        st = NS["load_state"]()
        return st.get("players", {}).get(str(_slot(st)), "Duelista") or "Duelista"
    except Exception:
        return "Duelista"


def _alive(c):
    """B58/H8: ¿se puede mandar por c desde un temporizador? No si ya se cerró (0x0003, on_close, o c.closed que
    ponga run.py) ni si run.py ya tiene otra conexión más nueva en ese puerto (CONNS): mandar por la vieja sube
    Conn.SEQ del puerto y descuadra la numeración de la nueva (FUN_0068bac0 exige seq exacto)."""
    if getattr(c, "lp_closed", False) or getattr(c, "closed", False):
        return False
    if callable(getattr(c, "stale", None)):   # PvP V01: run.py sabe cuál es la conexión actual de ESE cliente;
        return not c.stale()                  # CONNS[puerto] es la última de cualquiera (con dos, mal)
    conns = NS.get("CONNS")
    if isinstance(conns, dict) and getattr(c, "port", None) in conns and conns[c.port] is not c:
        return False
    return True


def _npcs():
    """[(id, nombre, punto, piezas)] de run.py (NPCS); en pruebas, dos de muestra."""
    return NS.get("NPCS") or [(51, "Yugi Muto", 209, bytes(16)), (54, "Seto Kaiba", 213, bytes(16))]


def _npc_by_pid(pid):
    """(id NPC, nombre) si pid es un jugador falso (FAKE_BASE + id) o un id de NPC directo; si no, None."""
    nid = pid - FAKE_BASE if pid > FAKE_BASE else pid if 1 <= pid <= 98 else None
    if nid is not None and nid == pid and pid == _pid():     # B12: el pid propio (hueco + 1 = 1..4) no es la NPC 1..4
        return None
    for n in _npcs():
        if n[0] == nid:
            return n[0], n[1]
    return None


def _name_of(pid):
    if pid == _pid():
        return _me()
    n = _npc_by_pid(pid)
    return n[1] if n else f"Jugador {pid}"


def _s(text, size):
    """Cadena de largo fijo (proto.W().s usa latin1: los nombres con acentos de npc_ids.json caben)."""
    return W().s(text.encode("latin1", "replace").decode("latin1"), size).bytes()


def _cstr(data, start):
    return data[start:].split(bytes(1))[0].decode("latin1", "replace")


def _later(delay, fn, *a):
    if delay is None:
        return
    if delay <= 0:
        return fn(*a)
    # PvP V02: el temporizador corre en otro hilo; lleva la sesión del que lo programó (CTX de run.py), para que
    # _pid()/_me()/load_state() sean los de ESE jugador y no los del dueño.
    ctx = NS.get("CTX")
    sess = getattr(ctx, "sess", None) if ctx is not None else None

    def run():                 # un fallo en el hilo del temporizador iba a stderr (consola oculta): al log
        if ctx is not None:
            ctx.sess = sess
        try:
            fn(*a)
        except Exception as e:
            _log(f"[lobby] error en temporizador {getattr(fn, '__name__', fn)}: {e!r}")
    t = threading.Timer(delay, run); t.daemon = True; t.start()


def _chunks(entries, size, start=b""):
    """Trocea entradas de `size` bytes en mensajes de <= 1024 B."""
    per = max(1, (MAX_DATA - len(start)) // size)
    return [b"".join(entries[i:i + per]) for i in range(0, len(entries), per)] or []


# ---------------------------------------------------------------- salas de duelo
def room_entry(r):
    """Entrada de sala, 19 B (FUN_006ba6a0 / copia FUN_006ba740; log «index roomid pid_P1 pid_P2 mstat dstyle wmode
    password»): u32 roomid, u32 pid P1 (dueño), u32 pid P2 (0 = libre), u8 mstat, u8 dstyle, u8 wmode,
    u8 contraseña (bool), 3×u8 (objeto +0x15..+0x17, por defecto 1). P1 = P2 = 0 en UPDATE_DUELROOMINFO = borrar."""
    return (W().u32(r["id"]).u32(r["p1"]).u32(r["p2"]).u8(r["mstat"]).u8(r["dstyle"]).u8(r["wmode"])
            .u8(1 if r["pw"] else 0).u8(r["x"][0]).u8(r["x"][1]).u8(r["x"][2]).bytes())


def _npc_rooms():
    """Salas abiertas por NPC famosas (valores por defecto del constructor FUN_006c3500: dstyle 3, resto 0/1)."""
    rooms = []
    famous = [n for n in _npcs() if 51 <= n[0] <= 94][:NPC_ROOMS]
    for i, n in enumerate(famous):
        rooms.append({"id": 1 + i, "p1": FAKE_BASE + n[0], "p2": 0, "mstat": 0, "dstyle": 3, "wmode": 0,
                      "pw": False, "x": (1, 1, 1), "npc": n[0]})
    return rooms


def _all_rooms():
    with LOCK:
        return _npc_rooms() + list(MEM["rooms"].values())


def h_roomlist(c, msg_id, param, data):
    """0x4280 REQDUELROOMLIST (s32 -1) -> 0x4282 START s32 nº (>= 0; < 0 = error, FUN_006ba410 y FUN_006d1840 caso
    0x48 JGE) + 0x4284 DUELROOMLIST (bucle de 19 B, FUN_006ba4f0) + 0x4286 END (pone +0x29=2)."""
    if c.port == NS.get("LOBBY_PORT", -1):        # My Room: no hay salas
        rooms = []
    else:
        rooms = _all_rooms()
    c.send(0x4282, W().u32(len(rooms)).bytes())
    for chunk in _chunks([room_entry(r) for r in rooms], 19):
        c.send(0x4284, chunk)
    c.send(0x4286)
    return True


def is_npc_rival(rival):
    """¿El u32 «rival» de 0x4288 es un reto directo a una NPC? Lo pone la escena desde su +0xf4 (FUN_0086c605 ->
    FUN_006b1bb0 -> FUN_006cc250). En TODOS los logs: hablar con una NPC = su id (1..98, el rango que el lobby toma
    por NPC); «Menú de Duelo» -> abrir sala = 0xffffffff (log 20261006-160049, l.4112 y 5866). Antes, cualquier valor
    != 0 era «reto a NPC»: la sala del menú arrancaba al momento un duelo contra «Rival 4294967295».  C (logs)"""
    return 1 <= rival <= 98


def h_createroom(c, msg_id, param, data):
    """0x4288 REQCREATEROOM, 28 B: u8 dstyle, u8 wmode, u8 mstat, u8 con contraseña, 16 B contraseña, 4×u8,
    u32 rival (FUN_006b1bb0/FUN_006cc250). rival != 0 = duelo directo contra NPC: lo sigue llevando run.py.
    Sala propia -> 0x428a s32 0 (0 = éxito, FUN_006b19b0 caso 3) + 0x3113 UPDATE_DUELROOMINFO con la sala y, al
    rato, una NPC la reta (MATCHOFFER 0x4302)."""
    rival = struct.unpack(">I", data[24:28])[0] if len(data) >= 28 else 0
    if is_npc_rival(rival):
        return False
    me = _pid()
    s = _sess(c)
    with LOCK:
        rid = MEM["next_room"]; MEM["next_room"] += 1
        r = {"id": rid, "p1": me, "p2": 0, "mstat": data[2] if len(data) > 2 else 0,
             "mstat0": data[2] if len(data) > 2 else 0,           # ROOM_REOPEN: el de creación (0 en todos los logs)
             "dstyle": data[0] if data else 3, "wmode": data[1] if len(data) > 1 else 0,
             "pw": bool(data[3]) if len(data) > 3 else False,
             "pwd": bytes(data[4:20]).ljust(16, bytes(1)),   # PvP: 0x4300 trae 16 B: se comparan enteros (en la prueba 2
             # llegaron 16 B sin forma de texto, «20 2c b9 62…»: posible resumen; cortar en el 1.er cero lo rompería)
             "x": tuple(data[21:24]) if len(data) >= 24 else (1, 1, 1), "conn": c,
             # Prueba 3: FUN_006cc250 manda tras la contraseña UN byte más (local_18, siempre 0 en los logs) y
             # luego +0xa5, +0xa6, +0xa7 de la escena; antes se guardaba data[20:23] (desplazado uno): la ficha
             # enseñaba Rendirse/Tiempo/Mirar distintos de lo elegido.  C (orden de envío)
             "block": getattr(s, "block", None)}                                       # PvP: a quién se anuncia
        old = [v for v in MEM["rooms"].values() if v["p1"] == me]
        MEM["rooms"] = {k: v for k, v in MEM["rooms"].items() if v["p1"] != me}   # una sala por jugador
        MEM["rooms"][rid] = r
    c.send(0x428a, W().u32(0).bytes())
    c.send(0x3113, room_entry(r))
    # PvP fase 2 (V05, P0-5): la sala, a TODO el bloque. El retador busca en su lista de salas (+0x9c4) la que tiene
    # P1 == pid pulsado (FUN_006b69f0); si no la tiene, da −495 sin mandar nada. La sala vieja que sustituye, borrada.
    for v in old:
        _block_post(v.get("block"), 0x3113, room_entry(dict(v, p1=0, p2=0)), skip=s)
    _block_post(r["block"], 0x3113, room_entry(r), skip=s)
    if _is_auto(r):                                   # AUTODUEL: «busca autoduelo» (0x66), sin «Retar» en la ficha
        _room_state(s, AUTO_STATE)
        if AUTODUEL_MATCH:
            _later(AUTODUEL_MATCH_DELAY, _auto_try_match, c)
    else:
        _room_state(s, ROOM_STATE)                    # «sala abierta, esperando»: la ficha ofrece retar
    _later(CHALLENGE_DELAY, _npc_challenge, c, rid)
    return True


def _npc_challenge(c, rid):
    """Una NPC famosa pide duelo en la sala del jugador: MATCHOFFER 0x4302 (FUN_006bae90): u32 pid, nombre16,
    u8, u8 (+0xa98), u16 (+0xb06), u16 (+0xb0a), 16 B (FUN_009bce70: piezas del muñeco). Pone +0x2d=2, que la
    escena lee con FUN_006d3990(0x11)."""
    with LOCK:
        r = MEM["rooms"].get(rid)
        if not r or r["p2"] or not _alive(c):          # B58: sala borrada (on_close) o conexión muerta
            return
        # PvP: con un reto de un jugador pendiente, o con más jugadores en el bloque (la sala es para ellos), la NPC
        # no reta. Solo en el bloque = como siempre.
        if MEM.setdefault("challenges", {}).get(rid) or _block_has_others(c):
            return
        n = random.choice([n for n in _npcs() if 51 <= n[0] <= 94] or _npcs())
        MEM["offer"] = {"room": rid, "npc": n[0]}
    parts = n[3] if len(n) > 3 and any(n[3]) else bytes.fromhex("0000203040608090b820000000000000")
    c.send(0x4302, W().u32(FAKE_BASE + n[0]).bytes() + _s(n[1], 16) + W().u8(0).u8(0).u16(0).u16(0).bytes()
           + parts[:16].ljust(16, bytes(1)))


def _duel_notice(c, npc_id):
    """Prepara el servidor de duelo (run.py: DUEL['rival'] lo usa PREPARATION_RESULT) y manda DUELSERVERNOTICE
    0x4306 (ip15 + u16 puerto, FUN_006bb730; pone +0x30=2, que espera FUN_006d1840 caso 0x4b).
    B11: igual que run.py en 0x4288 con rival: LOBBY_ROOM["c"] = esta conexión (DF.finish manda por ella
    0x4140/0x4237/0x4800, sin el cual los resultados no sacan «Siguiente») y DF["h_autoduelstart"] (sess["npc"] =
    esta NPC, no la del duelo anterior)."""
    if callable(NS.get("duel_begin")):               # V08: run.py lo apunta en la SESIÓN de c (y en los globales)
        try:
            NS["duel_begin"](c, npc_id)
        except Exception as e:
            _log(f"[lobby] duel_begin({npc_id}) falló: {e!r}")
        return c.send(0x4306, W().s(NS.get("PUBLIC_IP", "127.0.0.1"), 15).u16(NS.get("DUEL_PORT", 5760)).bytes())
    if "DUEL" in NS:
        NS["DUEL"]["rival"] = npc_id
    if isinstance(NS.get("LOBBY_ROOM"), dict):
        NS["LOBBY_ROOM"]["c"] = c
    DF = NS.get("DF")
    if DF is not None:
        try:
            DF["h_autoduelstart"](npc_id, 0)          # 0 = partida suelta
        except Exception as e:                        # sin esto el duelo sigue, solo sin ficha de NPC correcta
            _log(f"[lobby] h_autoduelstart({npc_id}) falló: {e!r}")
    c.send(0x4306, W().s(NS.get("PUBLIC_IP", "127.0.0.1"), 15).u16(NS.get("DUEL_PORT", 5760)).bytes())   # V03


def h_offerreply(c, msg_id, param, data):
    """0x4303 MATCHOFFERREPLY s32 (1 = acepta, FUN_006cc8e0; sin respuesta propia: FUN_006d1840 caso 0x12 pasa
    a 0x59 sin esperar). Acepta -> sala ocupada (0x3113 con P2) + DUELSERVERNOTICE."""
    ok = struct.unpack(">i", data[:4])[0] if len(data) >= 4 else 0
    ch = _challenge_to(c)
    if ch is not None:                                # PvP: contesta al reto de un jugador
        return _challenge_reply(c, ch, ok)
    with LOCK:
        off, MEM["offer"] = MEM["offer"], None
        r = MEM["rooms"].get(off["room"]) if off else None
    if not off or not r:
        return True
    if ok:
        r["p2"] = FAKE_BASE + off["npc"]; r["mstat"] = 1
        c.send(0x3113, room_entry(r))
        _duel_notice(c, off["npc"])
    else:
        _later(CHALLENGE_DELAY, _npc_challenge, c, r["id"])
    return True


def h_deleteroom(c, msg_id, param, data):
    """0x4290 REQDELETEROOM -> 0x4292 s32 0 (FUN_006b2fa0: 0 = éxito) + 0x3113 con P1 = P2 = 0 (FUN_006ba7b0 borra
    la sala con ese roomid)."""
    me = _pid()
    with LOCK:
        mine = [k for k, v in MEM["rooms"].items() if v["p1"] == me]
        gone = [MEM["rooms"].pop(k) for k in mine]
    if gone:
        _room_state(_sess(c), 0)
    with LOCK:
        MEM["offer"] = None
        chs = [MEM.setdefault("challenges", {}).pop(r["id"], None) for r in gone]
    c.send(0x4292, W().u32(0).bytes())
    for r in gone:
        c.send(0x3113, room_entry(dict(r, p1=0, p2=0)))
        _block_post(r.get("block"), 0x3113, room_entry(dict(r, p1=0, p2=0)), skip=_sess(c))   # PvP: a todo el bloque
    for ch in chs:                                    # PvP: el retador esperaba respuesta -> «el oponente ha salido»
        if ch is not None:
            ch["done"] = True
            _post(ch["from"], 0x4301, W().u32(R_GONE & 0xffffffff).bytes())
    return True


def h_reqmatch(c, msg_id, param, data):
    """0x4300 REQMATCH: u32 roomid, 16 B contraseña. El cliente ya comprobó que la sala está en su lista
    (FUN_006b69f0). -> 0x4301 REQMATCHRESULT s32: 0 = aceptado (FUN_0086d270 pasa a 0x3f y espera el servidor de
    duelo), otro = error (-494 tiene diálogo propio). Sala de NPC: acepta al momento + DUELSERVERNOTICE."""
    rid = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    room = next((r for r in _all_rooms() if r["id"] == rid), None)
    if room is not None and _human_room(room):        # PvP: sala de otro jugador -> reto (REQMATCHRESULT diferido)
        return _challenge(c, room, data[4:20])
    if not room or not room.get("npc"):
        c.send(0x4301, W().u32(1).bytes())
        return True
    c.send(0x4301, W().u32(0).bytes())
    _duel_notice(c, room["npc"])
    return True


def h_cancelmatch(c, msg_id, param, data):
    """0x4304 CANCELREQMATCH -> 0x4309 CANCELREQMATCHRESULT s32 0 (caso «default» del distribuidor, comprobado en el
    desensamblado: CMP EAX,0x4309 / JZ 006ac5d5 -> FUN_006bb470; resultado k=19, FUN_006b54b0: 0 = éxito).
    gen_auto mandaba 0x4305 (sin datos): desbloquea el indicador pero no rellena el resultado."""
    ch = _challenge_from(c)
    if ch is not None:                                # PvP: el retador retira su reto -> el dueño, 0x4305
        with LOCK:
            MEM.setdefault("challenges", {}).pop(ch["room"], None)
            ch["done"] = True
        _post(ch["to"], 0x4305, b"")
    c.send(0x4309, W().u32(0).bytes())
    return True


# ---------------------------------------------------------------- retos entre jugadores (PvP fase 2: V05, P0-5)
# Secuencia (research/audit/social_pvp.md P0-5; formatos del cliente):
#   B 0x4288 (rival 0) -> B: 0x428a 0 + 0x3113; el BLOQUE: 0x3113 (h_createroom)
#   A 0x4300 (roomid, pw) -> B: 0x4302 MATCHOFFER (FUN_006bae90: u32 pid, nombre16, u8, u8, u16, u16, 16 B piezas);
#                            A NO recibe 0x4301 todavía (FUN_0086d270 trata −618 = rechazado en REQMATCHRESULT: se
#                            manda cuando el dueño ha contestado)
#   B 0x4303 1  -> A: 0x4301 0 (estado 0x3f, espera 0x4306); bloque: 0x3113 con P2 = A, mstat 1; reserva {A, B};
#                  A y B: 0x4306 (ip15 + u16, FUN_006bb730) con la IP por la que entró cada uno (duel_ip_for)
#   B 0x4303 0  -> A: 0x4301 −618 (diálogo m_pNoThankYou, texto 0x1483 «La Solicitud de Duelo fue rechazada.»)
#   A 0x4304    -> A: 0x4309 0; B: 0x4305 (FUN_006bb220: borra la ficha del rival, +0x2d = 0)
#   sin respuesta en OFFER_TIMEOUT -> B: 0x4308 (FUN_006bb200: +0x2e = 2, +0x2d = 0); A: 0x4301 −616
#   B 0x4290 / se va -> bloque: 0x3113 con P1 = P2 = 0 (FUN_006ba7b0); A: 0x4301 −600 («El oponente ha salido»)
# Códigos de error del mapa del lobby FUN_00884b70 (social_pvp.md §4). La contraseña errónea no tiene código propio
# conocido: −495 «Ya no existe esa sala» (C: sale con diálogo, no cierra nada).
OFFER_TIMEOUT = float(os.environ.get("YO2_PVP_OFFER_TIMEOUT") or 30)
R_REJECTED, R_NOANSWER, R_GONE, R_INDUEL, R_NOROOM, R_FULL = -618, -616, -600, -608, -495, -492
# DUELPASS (persistencia NOTES §13): −612 = 4556 «No tienes suficientes DUELPASS points.», −614 = 4557 «Tu oponente no
# tiene suficientes DUELPASS points.» (FUN_00884b70). El cliente comprueba los suyos antes de mandar 0x4300 (C); los
# del rival solo los puede saber el servidor (P: que Konami lo mirara aquí, en el REQMATCH). Umbral = el del cliente
# (1 en sala de duelo simple, 3 en match). Apagar: YO2_DP_SALA_CHECK=0 o YO2_DP_COSTE=0 (o PP.DP_ROOM_CHECK = False).
R_NODP_ME, R_NODP_OPP = -612, -614
DEFAULT_PARTS = bytes.fromhex("0000203040608090b820000000000000")


def _dp_need(room):
    """DP que hacen falta para un duelo en room (0 = no se comprueba)."""
    pp = NS.get("PP")
    if pp is None or not getattr(pp, "DP_ROOM_CHECK", False) or not hasattr(pp, "dp_need"):
        return 0
    try:
        return pp.dp_need(room.get("dstyle", 3))
    except Exception as e:                            # noqa: BLE001
        _log(f"[lobby] DP necesarios: {e!r}")
        return 0


def _dp_balance(s):
    """DP del personaje de la sesión s (run.py dp_balance); None si no se sabe (entonces no se bloquea)."""
    f = NS.get("dp_balance")
    try:
        return f(s) if callable(f) and s is not None else None
    except Exception as e:                            # noqa: BLE001
        _log(f"[lobby] DP de {s!r}: {e!r}")
        return None


def _sess(c):
    return getattr(c, "sess", None)


# Estado del jugador (+0x130, 0x3112/LIST2) mientras tiene sala abierta. FUN_0088d170 (icono y modo de la ficha):
# 100/0x97 -> 2, 0x65 -> 7, 0x66 -> 3, 0x67..0x6a/0x98 -> 1 (en duelo), resto 0. FUN_0088add0 solo trata como «sala
# a la que se puede retar» a quien tiene estado 100..0x65 Y una sala con dstyle (+0x11) = 3. En la prueba 3 la ficha
# del otro enseñaba los datos de su sala pero solo «Mensaje de Chat»: su estado era 1. 100 frente a 0x65: P (0x65
# podría ser sala con contraseña u otro modo; sin ver).  C (tabla) / P (valor)
ROOM_STATE = 100
DUEL_STATE = 0x67


def _room_state(s, state, rival="", rival_sess=None):
    """rival_sess (PvP): con pvp_hub.RIVAL_CARD, los 25 B de «en duelo» llevan LV, victorias, derrotas y título del
    rival (FUN_006b9480 -> reg+0x173/+0x174/+0x176/+0x178, que la ficha pinta como los propios: research/lobby/NOTES.md
    «Mundo 2008», C). Sin él, a 0 (como antes)."""
    hub = NS.get("HUB")
    if hub is None or s is None or getattr(s, "pid", None) is None:
        return
    if state == 0 and getattr(s, "state", 0) not in (ROOM_STATE, 0x65, AUTO_STATE):
        return                                        # no pisar un «en duelo»: eso lo limpia pvp_hub al volver
    try:
        card = hub.rival_card(rival_sess) if rival_sess is not None and hasattr(hub, "rival_card") else None
        if card:
            hub.set_state(s, state, rival, b=card[0], u=card[1:])
        else:
            hub.set_state(s, state, rival)
    except Exception as e:
        _log(f"[lobby] estado {state:#x} de {s!r}: {e!r}")


def _block_post(key, mid, data, skip=None):
    """A los del bloque key (salvo skip), por su cola de envío (Conn.post: no duerme en este hilo)."""
    hub = NS.get("HUB")
    if hub is None or key is None:
        return
    with hub.lock:
        members = list(hub.blocks.get(key, {}).values())
    for o in members:
        if o is not skip:
            hub._post(o, mid, data)


def _block_has_others(c):
    hub, s = NS.get("HUB"), _sess(c)
    try:
        return bool(hub is not None and s is not None and hub.others(s))
    except Exception:
        return False


def _post(c, mid, data):
    """A la conexión de OTRO cliente (o a la propia desde un temporizador): por su cola si la tiene."""
    if c is None or not _alive(c):
        return False
    try:
        (c.post if hasattr(c, "post") else c.send)(mid, data)
        return True
    except Exception as e:
        _log(f"[lobby] reto: 0x{mid:04x} no se pudo mandar: {e!r}")
        return False


def _human_room(r):
    """Sala de un jugador conectado (no de NPC): su conexión tiene sesión con pid."""
    c = r.get("conn")
    return not r.get("npc") and c is not None and getattr(_sess(c), "pid", None) is not None


def _challenge_to(c):
    s = _sess(c)
    with LOCK:
        return next((ch for ch in MEM.setdefault("challenges", {}).values()
                     if ch["to"] is c or (s is not None and ch["b"] is s)), None)


def _challenge_from(c):
    s = _sess(c)
    with LOCK:
        return next((ch for ch in MEM.setdefault("challenges", {}).values()
                     if ch["from"] is c or (s is not None and ch["a"] is s)), None)


def _challenge(c, room, pw):
    """A (c) reta a B (dueño de room). Contesta 0x4301 al momento SOLO si hay error."""
    def fail(code, why):
        _log(f"[lobby] reto a la sala {room['id']} rechazado: {why} ({code})")
        c.send(0x4301, W().u32(code & 0xffffffff).bytes())
        return True
    a, owner = _sess(c), room["conn"]
    b = _sess(owner)
    if a is None or a.pid is None or b is None or b is a:
        return fail(R_NOROOM, "sin sesión o sala propia")
    if not _alive(owner) or MEM["rooms"].get(room["id"]) is not room:
        return fail(R_NOROOM, "la sala ya no existe")
    pvp = NS.get("PVP")
    if pvp is not None and (pvp.by_pid(b.pid) or pvp.by_pid(a.pid)):
        return fail(R_INDUEL, "uno de los dos ya está en un duelo")
    if _is_auto(room):
        return fail(R_AUTO, "el dueño busca autoduelo")
    if room.get("pw") and bytes(pw)[:16].ljust(16, bytes(1)) != room.get("pwd", bytes(16)):
        return fail(R_NOROOM, "contraseña")
    need = _dp_need(room)
    if need:                                          # coste del duelo (persistencia NOTES §13)
        da, db = _dp_balance(a), _dp_balance(b)
        if da is not None and da < need:
            return fail(R_NODP_ME, f"el retador tiene {da} DP (hacen falta {need})")
        if db is not None and db < need:
            return fail(R_NODP_OPP, f"el dueño de la sala tiene {db} DP (hacen falta {need})")
    with LOCK:
        chs = MEM.setdefault("challenges", {})
        off = MEM.get("offer")
        if room.get("p2") or room["id"] in chs or (off and off.get("room") == room["id"]):
            busy = True
        else:
            busy = False
            ch = chs[room["id"]] = {"room": room["id"], "from": c, "to": owner, "a": a, "b": b, "done": False}
    if busy:
        return fail(R_FULL, "sala ocupada o con otro reto pendiente")
    parts = bytes(a.parts or b"")[:16]
    parts = parts if any(parts) else DEFAULT_PARTS
    _log(f"[lobby] reto PvP: {a!r} -> {b!r} (sala {room['id']})")
    _admin_ev("pvp_challenge", a, b)                   # panel: «A reta a B»
    # Prueba 5: FUN_006bae90 copia los campos a la ficha del rival (lobby+0x9f4, mismo esquema que un registro de
    # jugador): u8 sin uso, u8 -> +0xa98 = reg+0xa4 LV, u16 -> +0xb06 = reg+0x112 título, u16 -> +0xb0a = reg+0x116.
    # Victorias/derrotas no van en 0x4302 (las pone a 0). Iban a 0: «Lv0 … [Duelista principiante]».  C
    lv = title = 0
    bp = NS.get("block_player")
    if callable(bp):
        try:
            e = bp(a.pid, a.name, a)                   # LIST1 de 36 B: LV en [20], título en [25:27]
            lv, title = e[20], struct.unpack(">H", e[25:27])[0]
        except Exception as ex:
            _log(f"[lobby] LV/título del retador: {ex!r}")
    _post(owner, 0x4302, W().u32(a.pid).bytes() + _s(a.name or "Duelista", 16)
          + W().u8(0).u8(lv).u16(title).u16(0).bytes() + parts.ljust(16, bytes(1)))
    _later(OFFER_TIMEOUT, _challenge_timeout, ch)
    return True


def _challenge_timeout(ch):
    with LOCK:
        if ch["done"]:
            return
        ch["done"] = True
        MEM.setdefault("challenges", {}).pop(ch["room"], None)
    _log(f"[lobby] reto PvP sin respuesta en {OFFER_TIMEOUT:.0f} s (sala {ch['room']})")
    _post(ch["to"], 0x4308, b"")
    _post(ch["from"], 0x4301, W().u32(R_NOANSWER & 0xffffffff).bytes())


def _challenge_reply(c, ch, ok):
    """B (c) contesta 0x4303 al reto ch."""
    with LOCK:
        if ch["done"]:
            return True
        ch["done"] = True
        MEM.setdefault("challenges", {}).pop(ch["room"], None)
        r = MEM["rooms"].get(ch["room"])
    a_conn = ch["from"]
    if not ok:
        _log(f"[lobby] reto PvP rechazado por {ch['b']!r}")
        _post(a_conn, 0x4301, W().u32(R_REJECTED & 0xffffffff).bytes())
        return True
    if r is None or not _alive(a_conn):
        _log("[lobby] reto PvP aceptado pero el retador o la sala ya no están: se retira la oferta")
        c.send(0x4305)
        if r is None:
            _post(a_conn, 0x4301, W().u32(R_GONE & 0xffffffff).bytes())
        return True
    a, b = ch["a"], ch["b"]
    pvp = NS.get("PVP")
    if pvp is None:
        _post(a_conn, 0x4301, W().u32(R_NOROOM & 0xffffffff).bytes())
        return True
    pvp.reserve(a, b)                                  # jugador 0 = el retador, 1 = el dueño de la sala
    _room_state(a, DUEL_STATE, b.name, b)              # «en duelo con X» (0x67, como el reto a NPC de pvp_hub)
    _room_state(b, DUEL_STATE, a.name, a)
    r["p2"] = a.pid; r["mstat"] = 1
    _post(a_conn, 0x4301, W().u32(0).bytes())
    c.send(0x3113, room_entry(r))
    _block_post(r.get("block"), 0x3113, room_entry(r), skip=b)
    port = NS.get("DUEL_PORT", 5760)
    ipf = NS.get("duel_ip_for") or (lambda _c: NS.get("PUBLIC_IP", "127.0.0.1"))
    _post(a_conn, 0x4306, W().s(ipf(a_conn), 15).u16(port).bytes())
    c.send(0x4306, W().s(ipf(c), 15).u16(port).bytes())
    _log(f"[lobby] reto PvP aceptado: {a!r} contra {b!r}; servidor de duelo {ipf(a_conn)}/{ipf(c)}:{port}")
    return True


# ---------------------------------------------------------------- autoduelo (AUTODUEL / AUTODUEL_MATCH, NOTES «Autoduelo»)
def _is_auto(r):
    """Sala de jugador creada desde «Autoduelo» (0x4288 con mstat = 1; el de creación, mstat0)."""
    return bool(AUTODUEL and r and not r.get("npc") and r.get("mstat0", r.get("mstat", 0)) == 1)


def _auto_card(s):
    """(LV, título) de s como en su LIST1 (36 B: [20] y [25:27]); (0, 0) si no se puede."""
    hub = NS.get("HUB")
    try:
        e = hub.list1_entry(s)
        return e[20], struct.unpack(">H", e[25:27])[0]
    except Exception:
        return 0, 0


def _auto_style(ra, rb):
    """Tipo común de dos esperas (FUN_00a430b0: 3, 4 o 6 «Ambos»). Solo se empareja en simple (3): el byte de modo
    de 0x43a0 para un match (4) es D. None = no casan."""
    x, y = ra.get("dstyle", 3), rb.get("dstyle", 3)
    st = x if x == y else (y if x == 6 else (x if y == 6 else None))
    st = 3 if st == 6 else st
    return st if st == 3 else None


def _auto_try_match(c):
    """AUTODUEL_MATCH: busca otra espera de autoduelo en el mismo bloque para la sala de c (la que más lleva
    esperando primero) y, si la hay, las empareja. Reglas del oficial: D (ver AUTODUEL_MATCH)."""
    if not (AUTODUEL and AUTODUEL_MATCH):
        return None
    pvp, s = NS.get("PVP"), _sess(c)
    if pvp is None or s is None or getattr(s, "pid", None) is None or not _alive(c) or pvp.by_pid(s.pid):
        return None
    with LOCK:
        mine = next((r for r in MEM["rooms"].values()
                     if r.get("conn") is c and _is_auto(r) and not r.get("p2")), None)
        if mine is None:
            return None
        chs = MEM.setdefault("challenges", {})
        cands = [r for r in sorted(MEM["rooms"].values(), key=lambda v: v["id"])
                 if r is not mine and _is_auto(r) and not r.get("p2") and r.get("block") == mine.get("block")
                 and _human_room(r) and _alive(r["conn"]) and _sess(r["conn"]) is not s
                 and r["id"] not in chs and _auto_style(mine, r) is not None]
    pick = None
    for r in cands:                                   # LV fuera del candado del lobby (list1_entry usa el del hub)
        o = _sess(r["conn"])
        if pvp.by_pid(o.pid) is not None:
            continue
        if (mine["x"][2] or r["x"][2]) and abs(_auto_card(s)[0] - _auto_card(o)[0]) > AUTODUEL_LV_DIFF:
            continue
        pick = (r, o)
        break
    if pick is None:
        return None
    r, o = pick
    with LOCK:                                        # otro hilo pudo emparejar a alguno entretanto
        if (mine.get("p2") or r.get("p2") or MEM["rooms"].get(mine["id"]) is not mine
                or MEM["rooms"].get(r["id"]) is not r):
            return None
        mine["p2"], r["p2"] = o.pid, s.pid
    return _auto_pair(r["conn"], o, r, c, s, mine)


def _auto_pair(ca, a, ra, cb, b, rb):
    """Reserva el PvP (a = el que más esperaba = jugador 0) y manda a cada uno 0x3113 (su sala con P2) y, sin
    petición, 0x43a0 AUTODUELSTART (FUN_006baab0: u32 pid, nombre16, u8 LV -> +0xa98, u16 título -> +0xb06, u16 ->
    +0xb0a, 16 B piezas, u8 modo 0 = simple) + 0x4306 DUELSERVERNOTICE, como _challenge_reply y los torneos."""
    pvp = NS.get("PVP")
    pvp.reserve(a, b)
    _room_state(a, AUTO_DUEL_STATE, b.name, b)
    _room_state(b, AUTO_DUEL_STATE, a.name, a)
    for r, cc, me in ((ra, ca, a), (rb, cb, b)):
        _post(cc, 0x3113, room_entry(r))
        _block_post(r.get("block"), 0x3113, room_entry(r), skip=me)
    port = NS.get("DUEL_PORT", 5760)
    ipf = NS.get("duel_ip_for") or (lambda _c: NS.get("PUBLIC_IP", "127.0.0.1"))
    for cc, ot in ((ca, b), (cb, a)):
        lv, title = _auto_card(ot)
        parts = bytes(getattr(ot, "parts", b"") or b"")[:16]
        parts = parts if any(parts) else DEFAULT_PARTS
        _post(cc, 0x43a0, W().u32(ot.pid).bytes() + _s(ot.name or "Duelista", 16) + W().u8(lv).u16(title).u16(0)
              .bytes() + parts.ljust(16, bytes(1)) + bytes([0]))
        _post(cc, 0x4306, W().s(ipf(cc), 15).u16(port).bytes())
    _log(f"[lobby] autoduelo: {a!r} contra {b!r} (salas {ra['id']}/{rb['id']}); servidor de duelo :{port}")
    return True


def h_watch(c, msg_id, param, data):
    """0x4320 REQWATCH (u32 roomid) -> 0x4321 s32, ip15, u16. Con todo a 0 el cliente da el «éxito» y conecta a una
    ip vacía; aún no hay servidor de espectador: s32 1 = error (FUN_006d1840 caso 0x16: != 0 -> error).
    WATCH (NOTES §8): el u32 es el pid de la ficha (scene+0xd8 = FUN_0086b7a0, el mismo que 0x4300; botón 8 «Ver
    duelo» solo si su estado es 0x67..0x6a/0x98). Si ese pid tiene un PvP en marcha: 0x4321 s32 0 + ip15 + u16 del
    puerto de duelo (a +0xd78/+0xd88, como 0x4306) y pvp_match espera un 0x6700 desde la IP de este cliente.  C
    (cliente) / P (servidor). Código de error del oficial: D (se deja el 1).
    07-10 (AÑADIDO NUESTRO, aprobado por el owner; no 1:1): si no es un PvP, un duelo contra NPC ya empezado
    (npc_watch.py, NS["NW"]) también se puede mirar: la misma respuesta y npc_watch espera el 0x6700 de esa IP."""
    pid = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    pvp, nw = NS.get("PVP"), NS.get("NW")
    peer = getattr(c, "peer", None)
    ipf = NS.get("duel_ip_for") or (lambda _c: NS.get("PUBLIC_IP", "127.0.0.1"))
    if WATCH and pvp is not None and hasattr(pvp, "watch_request"):
        m = pvp.watch_request(peer, pid)
        if m is not None:
            if nw is not None and hasattr(nw, "forget"):
                nw.forget(peer)                       # una petición de NPC vieja de esa IP ya no vale
            c.send(0x4321, W().u32(0).s(ipf(c), 15).u16(NS.get("DUEL_PORT", 5760)).bytes())
            _log(f"[lobby] espectador: {_sess(c)!r} mira el duelo de {pid}")
            return True
    if WATCH and nw is not None and hasattr(nw, "watch_request") and nw.watch_request(peer, pid) is not None:
        c.send(0x4321, W().u32(0).s(ipf(c), 15).u16(NS.get("DUEL_PORT", 5760)).bytes())
        _log(f"[lobby] espectador: {_sess(c)!r} mira el duelo contra NPC de {pid}")
        return True
    c.send(0x4321, W().u32(1).s("", 15).u16(0).bytes())
    return True


# ---------------------------------------------------------------- tras el duelo (ROOM_REOPEN, NOTES §9)
def _room_reopen(c):
    """0x4210 al volver al mapa: si la sala propia sigue con rival (P2) y ya no hay duelo, P2 = 0, mstat el de
    creación, 0x3113 al dueño y al bloque y estado 100 (la ficha vuelve a ofrecer «Retar»). El cliente conserva la
    marca de sala propia mientras la lista 0x4284 traiga P1 = él (FUN_006ba4f0), y tras el duelo la vuelve a pedir
    él solo (escena del mapa, FUN_00863750 estado 2 -> 0x44 -> tarea 0x41..0x48: LIST1..5 y 0x4280).  C / P"""
    s = _sess(c)
    me = getattr(s, "pid", None) or _pid()
    pvp = NS.get("PVP")
    if pvp is not None and pvp.by_pid(me) is not None:   # aún reservado o en duelo
        return
    with LOCK:
        rs = [r for r in MEM["rooms"].values() if r["p1"] == me and r.get("p2")]
        npc = [r for r in rs if r["p2"] >= FAKE_BASE]
        for r in rs:
            r["p2"], r["mstat"] = 0, r.get("mstat0", 0)
    for r in rs:
        c.send(0x3113, room_entry(r))
        _block_post(r.get("block"), 0x3113, room_entry(r), skip=s)
        _log(f"[lobby] sala {r['id']} de {me} abierta otra vez tras el duelo")
    if rs:
        auto = AUTODUEL and any(_is_auto(r) for r in rs)
        _room_state(s, AUTO_STATE if auto else ROOM_STATE)
        if auto and AUTODUEL_MATCH:
            _later(AUTODUEL_MATCH_DELAY, _auto_try_match, c)
    for r in npc:                                         # como al crearla: si sigue solo en el bloque, reta una NPC
        _later(CHALLENGE_DELAY, _npc_challenge, c, r["id"])


# ---------------------------------------------------------------- prohibidas/limitadas (LIMIT_LIST, NOTES §7)
_LIMITS = []


def limit_entries():
    """[(id YO2, id Konami, límite 0/1/2)] de research/ia/cards_yo2.csv (lista de 2008 = card_max.bin; 65535 = no
    va en mazos (dioses, fichas): no se manda, el u8 no lo admite)."""
    if not _LIMITS:
        import csv
        path = os.path.join(EMU, "research", "ia", "cards_yo2.csv")
        with open(path, encoding="utf-8") as f:
            rows = [(int(x["yo2_id"]), int(x["konami_id"]), int(x["limit"])) for x in csv.DictReader(f)
                    if x["limit"] in ("0", "1", "2") and x["konami_id"]]
        _LIMITS[:] = sorted(rows)
    return list(_LIMITS)


def limitation_msgs():
    """0x5600 LIMITATIONLIST (FUN_006c31e0): bucle hasta el final de los datos de u32 índice, u32 (-> tabla+4: id
    YO2; nadie lo lee, FUN_0073c2e0 no tiene llamadores), u32 (-> tabla+0: id KONAMI, la clave que busca
    FUN_0073c290), u8 copias. Cuenta = índice + 1 del último (FUN_0073c220): índices crecientes desde 0, varios
    mensajes de <= 1024 B (78 entradas). Nunca se vacía la tabla (solo la escribe este mensaje).  C
    0x5601 UNRELEASELIST (FUN_006c32a0): igual sin el u8; tabla+4 = id YO2 (FUN_0073c380), tabla+0 = id Konami
    (FUN_0073c330). Solo si UNRELEASED trae algo."""
    ent = [W().u32(i).u32(y).u32(k).u8(n).bytes() for i, (y, k, n) in enumerate(limit_entries())]
    out = [(0x5600, ch) for ch in _chunks(ent, 13)]
    if UNRELEASED:
        kid = {y: k for y, k, _ in _all_cards_konami()}
        ent = [W().u32(i).u32(y).u32(kid.get(y, 0)).bytes() for i, y in enumerate(UNRELEASED)]
        out += [(0x5601, ch) for ch in _chunks(ent, 12)]
    return out


def _all_cards_konami():
    import csv
    with open(os.path.join(EMU, "research", "ia", "cards_yo2.csv"), encoding="utf-8") as f:
        return [(int(x["yo2_id"]), int(x["konami_id"] or 0), 0) for x in csv.DictReader(f)]


# ---------------------------------------------------------------- chat
def h_chat(c, msg_id, param, data):
    """0x4400 CHAT (FUN_006cca30): u8 0, u8 tipo (0 general, 1 regional, 2 intercambiar, 3 susurro, 5 equipo), u32, u32 destino,
    u32, u32, texto. Sin respuesta propia: el servidor reparte 0x4402 CHATNOTICE (FUN_006bb780): u8 (+0x1c8),
    u8 canal (3 = pestaña de susurros), u32 pid, u32 idioma (1 = JP, 3 = KR, otro = latino), nombre16, cadena.
    El cliente lo ignora en My Room (+0x9ed == 12)."""
    if len(data) < 14:
        return True
    # 06-10 (FUN_006cca30 + FUN_00a77c70, C): u8 = 0 siempre (byte alto de param_1 & 0xffffff), u8 TIPO, u32, u32
    # destino, u32, texto. Antes se leía el tipo en el byte 0 (siempre 0) y el destino en 2..5: todo salía en
    # «Chat general». Tipo por pestaña: 0 general, 1 regional, 2 INTERCAMBIAR, 3 susurro (pestaña «Duelo» = al rival,
    # +0xc70+0x5dc; «responder» = al último que te susurró), 5 equipo. El canal del 0x4402 = el tipo (pestaña).
    kind, target = data[1], struct.unpack(">I", data[6:10])[0]
    text = data[14:].split(bytes(1))[0][:MAX_DATA - 27]
    # Panel (07-10): guarda la línea para «Chat en vivo» (los susurros sin texto) y, con una prohibición de chat activa
    # (sanción del panel), no se reparte ni hay eco: el cliente ya no deja escribir al volver a entrar (bit 0x20 de
    # 0x3004, texto 4581); mientras tanto lo para el servidor (nuestro).
    if _admin_ev("chat", _sess(c), kind, target, text, (lambda: _name_of(target)) if kind == 3 else None) is True:
        return True
    head = W().u8(0).u8(kind).u32(_pid()).u32(2).bytes() + _s(_me(), 16)   # idioma 2 = latino (0 = el del Windows)
    c.send(0x4402, head + text + bytes(1))                    # eco (PROBABLE: si sale duplicado, quitarlo)
    # Prueba 4 (P1-11): el 0x4402 del EMISOR (su pid y nombre en la cabecera) también a los demás. 0/1/2 = sala ->
    # todo el bloque del HUB; 3 = susurro -> la sesión de ese pid, esté en el bloque que esté. 5 (equipo): como antes
    # (sin equipos). No hay mensaje de «no conectado» en el cliente (0x4401/0x4403 no existen en el despachador
    # FUN_006ac020): si el destino no está, solo el eco.  C (formato) / P (reparto)
    s, hub = _sess(c), NS.get("HUB")
    if s is not None and hub is not None and getattr(s, "pid", None) is not None:
        msg = head + text + bytes(1)
        if kind == 0 and s.block is not None:             # general: la sala (bloque)
            for o in hub.others(s):
                hub._post(o, 0x4402, msg)
        elif kind in (1, 2):                                # regional / intercambiar: todos los conectados  P
            with hub.lock:
                todos = [o for o in hub.sessions.values() if o is not s]
            for o in todos:
                hub._post(o, 0x4402, msg)
        elif kind == 3:
            o = hub.by_pid(target)
            if o is not None and o is not s:
                hub._post(o, 0x4402, msg)
        elif kind == 5 and NS.get("EQ") is not None:        # equipo: a los miembros conectados (canal 5)
            for o in NS["EQ"].team_chat_targets(s):
                hub._post(o, 0x4402, msg)
    n = _npc_by_pid(target) if kind == 3 else None
    if n:                                                      # susurro a una NPC: contesta
        reply = "Nos vemos en la sala de duelo.".encode("latin1")
        c.send(0x4402, W().u8(0).u8(3).u32(FAKE_BASE + n[0]).u32(0).bytes() + _s(n[1], 16) + reply + bytes(1))
    return True


# ---------------------------------------------------------------- amigos, lista negra, búsqueda
def h_getpid(c, msg_id, param, data):
    """0x4500 GETPIDBYPNAME (nombre16) -> 0x4501 GETPIDBYPNAMERESULT (caso «default»: CMP EAX,0x4501 / JZ ->
    FUN_006bcf10): s32 resultado (0 = encontrado) + u32 pid (solo si 0). La escena (FUN_00a903f0) mira el pid:
    0 = diálogo «no existe». gen_auto no tenía respuesta: la tarea se quedaba esperando +0x41."""
    name = data[:16].split(bytes(1))[0].decode("latin1", "replace").strip().lower()
    pid = 0
    if name and name == _me().lower():
        pid = _pid()
    for n in _npcs():
        if n[1].lower() == name:
            pid = FAKE_BASE + n[0]
    if not pid and name and PIDNAME_PLAYERS:
        pid = _pid_of_player_name(name)
    c.send(0x4501, W().u32(0).u32(pid).bytes() if pid else W().u32(1).u32(0).bytes())
    return True


# PIDNAME_PLAYERS (agente mundo, 06-10): 0x4500 también encuentra a los JUGADORES (antes solo a uno mismo y a las
# NPC). Es la «búsqueda de amigo por nombre» del cliente 080912: la ventana de amigos / lista negra / correo manda
# 0x4500 con el nombre escrito y con el pid de 0x4501 sigue (0x4510 REQFRIEND, 0x4530, 0x4600). 0x4520
# REQFRIENDSEARCH no lo manda nadie en este exe (ni el inmediato 0x4520 existe; 0x4521 solo en el despachador) y su
# manejador FUN_006bcd40 lee la lista y la tira.  C. Primero los conectados (sesiones del hub); si no, las cuentas de
# state.json (pid = 1000 + 4·índice + hueco, el mismo que les da el hub).  Conectados: C · desconectados: P.
PIDNAME_PLAYERS = os.environ.get("YO2_PIDNAME_PLAYERS", "1") not in ("", "0")


def _pid_of_player_name(name):
    name = str(name or "").strip().lower()
    hub = NS.get("HUB")
    if hub is not None:
        try:
            with hub.lock:
                ss = list(hub.sessions.values())
            for o in ss:
                if getattr(o, "pid", None) is not None and str(o.name or "").strip().lower() == name:
                    return o.pid
        except Exception as e:                        # noqa: BLE001
            _log(f"[lobby] 0x4500 sesiones: {e!r}")
    try:
        st = NS["load_state"]() if "load_state" in NS else None
    except Exception as e:                            # noqa: BLE001
        _log(f"[lobby] 0x4500 state.json: {e!r}"); st = None
    if not st:
        return 0
    accts = [(0, st)] + [(int(a.get("idx", 0) or 0), a) for a in (st.get("accounts") or {}).values()
                         if isinstance(a, dict)]
    for idx, a in accts:
        names = {}
        for k, v in (a.get("players") or {}).items():
            names[str(k)] = v
        for k, v in (a.get("profiles") or {}).items():
            if isinstance(v, dict) and v.get("name"):
                names[str(k)] = v["name"]
        for slot, n in names.items():
            if str(n or "").strip().lower() == name and str(slot).isdigit():
                return int(getattr(sys.modules.get("pvp_hub"), "PID_BASE", 1000)) + 4 * idx + (int(slot) & 3)
    return 0


def _lv_of(s):
    """LV de una sesión tal como sale en su ficha del bloque (LIST1 de 36 B, LV en [20]); 0 si no se puede."""
    bp = NS.get("block_player")
    try:
        return bp(s.pid, s.name, s)[20] if callable(bp) else 0
    except Exception as ex:
        _log(f"[lobby] LV de {s!r}: {ex!r}")
        return 0


def h_reqfriend(c, msg_id, param, data):
    """0x4510 REQFRIEND (u32 pid) -> 0x4511 s32 0 (FUN_006d1840 caso 0x18: 0 = petición enviada). Si el destino es
    una NPC, al rato acepta: 0x4515 FRIENDNOTICE (FUN_006bc9c0): s32 1 = aceptado (otro valor = rechazado), u32 pid,
    nombre16; el cliente la apunta en SU lista local (FUN_0085d1d0 + FUN_0085cf70)."""
    pid = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    n = _npc_by_pid(pid)
    hub, me = NS.get("HUB"), _sess(c)
    o = hub.by_pid(pid) if (hub is not None and not n) else None
    if o is not None and me is not None and o is not me and getattr(me, "pid", None) is not None:
        # Social: petición a OTRO jugador conectado. 0x4511 0 = «petición enviada» (FUN_006bc860); al destino,
        # 0x4512 FRIENDOFFER (FUN_006bc8a0: u32 pid, nombre16, u8; lo apunta en +0x13a4 y pone +0x3f = 2 -> diálogo).
        # Su respuesta 0x4513 vuelve al que pidió como 0x4515 FRIENDNOTICE (FUN_006bc9c0: s32 1 = aceptado).  C
        with LOCK:
            MEM.setdefault("friend_offers", {})[(me.pid, o.pid)] = (me, c)
        c.send(0x4511, W().u32(0).bytes())
        # El u8 final es el LV: FUN_006bc8a0 lo guarda en +0x19 y el diálogo lo registra «[ID:%d][%s][Lv:%d]»
        # (DAT_00cf0174); iba a 0 -> «Lv0 Player1».  C
        hub._post(o, 0x4512, W().u32(me.pid).bytes() + _s(me.name or "Duelista", 16) + W().u8(_lv_of(me)).bytes())
        return True
    c.send(0x4511, W().u32(0 if (n or pid == _pid()) else 1).bytes())
    if n:
        def accept():
            with LOCK:
                if pid not in MEM["friends"]:
                    MEM["friends"].append(pid); _save()
            if _alive(c):                                  # B58/H8: no mandar por una conexión cerrada
                c.send(0x4515, W().u32(1).u32(pid).bytes() + _s(n[1], 16))
            _admin_ev("amistad", _sess(c), n[1])           # panel: «X y <NPC> ahora son amigos»
        _later(FRIEND_ACCEPT_DELAY, accept)
    return True


def h_friendofferreply(c, msg_id, param, data):
    """0x4513 FRIENDOFFERREPLY (u32 pid, s32 sí/no): sin respuesta propia (FUN_006d1840 caso 0x1a no espera)."""
    pid, ok = struct.unpack(">Ii", data[:8]) if len(data) >= 8 else (0, 0)
    me = _sess(c)
    with LOCK:
        off = MEM.setdefault("friend_offers", {}).pop((pid, getattr(me, "pid", None)), None)
    if off is not None:                               # Social: respuesta a un jugador (ver h_reqfriend)
        asker, asker_conn = off
        hub = NS.get("HUB")
        if hub is not None:
            hub._post(asker, 0x4515, W().u32(1 if ok == 1 else 0).u32(me.pid).bytes() + _s(me.name or "Duelista", 16))
        if ok == 1:
            _friend_add(me, asker.pid)
            _friend_add(asker, me.pid)
            _admin_ev("amistad", asker, me)            # panel: «A y B ahora son amigos»
        return True
    if ok and pid and pid not in MEM["friends"]:
        with LOCK:
            MEM["friends"].append(pid); _save()
    return True


def _friend_add(s, pid):
    """Lista de amigos persistente POR PERFIL (state.json profiles[<hueco>]["friends"], la vista de la cuenta de s).
    El cliente lleva además la suya local (FUN_0085d1d0)."""
    ctx = NS.get("CTX")
    old = getattr(ctx, "sess", None) if ctx is not None else None
    try:
        if ctx is not None:
            ctx.sess = s
        PP = NS.get("PP")
        with _state_lock():
            st = NS["load_state"]()
            slot = s.slot if getattr(s, "slot", None) is not None else _slot(st)
            p = PP.profile(st, slot) if PP is not None else st.setdefault("friends_root", {})
            fl = p.setdefault("friends", [])
            if pid not in fl:
                fl.append(pid)
                NS["save_state"](st)
    except Exception as e:
        _log(f"[lobby] amigo {pid} para {s!r}: {e!r}")
    finally:
        if ctx is not None:
            ctx.sess = old


def h_black(c, msg_id, param, data):
    """0x4530 REQADDBLACK / 0x4540 REQDELBLACK (u32 pid) -> 0x4531 / 0x4541 s32 0 (0 = éxito: FUN_006d1840 casos
    0x26/0x28 y FUN_00a903f0 k=0x2a)."""
    pid = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    with LOCK:
        if msg_id == 0x4530 and pid not in MEM["black"]:
            MEM["black"].append(pid)
        if msg_id == 0x4540 and pid in MEM["black"]:
            MEM["black"].remove(pid)
        _save()
    c.send(msg_id + 1, W().u32(0).bytes())
    return True


# 0x3604, 5 u8 por amigo (NOTES «0x3604: dónde está cada amigo»): idioma b1, tipo b2, nº de sala b3, área, nº de
# bloque. El cliente los pinta SIEMPRE que el amigo sale conectado: tipo fuera de esta lista = aserto «illegal rank»
# (FUN_00a54f90), nº de sala > 7 = «illegal area»; idioma > 7 = «unknown» y área > 9 = área 0 (sin aserto).
FRIEND_OK_KINDS = frozenset((1, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 20))
MYROOM_WHERE = (0, 12, 0, 8, 0)       # My Room: b1 0, tipo 12, sala 0, área 8 («Tu Espacio»), bloque 0


def _room_where(port, lang, s=None):
    """(b1, b2, b3, área, bloque) de una sala de las puertas por su puerto, o None si el puerto no es de sala.
    Área: la del bloque de s (AREA_BLOCKS) o la de entrada de su mundo; sala y bloque 0 (solo anunciamos la A)."""
    base, kinds = NS.get("ROOM_PORT_BASE"), tuple(NS.get("ROOM_KINDS") or ())
    if base is None or port is None or not (base <= port < base + len(kinds)):
        return None
    kind = kinds[port - base]
    acad = kind in (NS.get("ACAD_KINDS") or ())
    area = NS.get("ACAD_AREA", 5) if acad else NS.get("DOMINO_AREA", 0)
    hub = NS.get("HUB")
    if s is not None and getattr(s, "block", None) and hasattr(hub, "area_list"):
        try:
            areas = hub.area_list(s.block)
            ab = int(getattr(s, "ablock", 0) or 0)
            if areas and 0 <= ab < len(areas):
                area = areas[ab]
        except Exception:                                    # noqa: BLE001
            pass
    lang = int(lang or 0)
    return (lang if 0 <= lang <= 7 else 0), kind, 0, int(area), 0


def _conn_where(port, lang, s=None):
    """_room_where para el puerto REAL de una conexión (no la clave del bloque). Con ROOM_PER_LANG (yo2srv/red/puerta.py,
    10-10) el puerto de una sala de idioma no es ROOM_PORT_BASE + i: room_of_port da el tipo y el idioma de la SALA."""
    rop = NS.get("room_of_port")
    r = rop(port) if callable(rop) else None
    if r is not None:
        rl, i = r
        port, lang = NS.get("ROOM_PORT_BASE") + i, (lang if rl is None else rl)
    return _room_where(port, lang, s)


def _friend_where(s):
    """5 B de 0x3604 para la sesión s del hub o None = no conectado (sin conexión de lobby viva, o en un sitio que no
    sabemos codificar: menús, selección de personaje…). La sesión no se borra al desconectar: lo que manda es
    s.lobby (la conexión del último 0x4100) y que siga abierta."""
    lobby = getattr(s, "lobby", None) if s is not None else None
    if s is None or s.pid is None or lobby is None or getattr(lobby, "closed", False):
        return None
    key = getattr(s, "block", None)
    if key:                                                  # en una sala (puerto, idioma); en duelo sigue aquí
        return _room_where(key[0], key[1], s)
    port = getattr(lobby, "port", None)
    if port == NS.get("LOBBY_PORT"):
        return MYROOM_WHERE
    return _conn_where(port, getattr(lobby, "lang", getattr(s, "lang", 0)), s)   # sala sin 0x4202 todavía


def _npc_where(c):
    """NPC amiga: «en la misma sala que tú» (puerto de c, área de entrada); desde My Room u otro sitio, Domino City
    (13) con tu idioma. P: las NPC no tienen sala propia; solo importa que sea un sitio válido."""
    w = _conn_where(getattr(c, "port", None), getattr(c, "lang", 0))
    if w is None:
        lang = int(getattr(c, "lang", 0) or 0)
        w = ((lang if 0 <= lang <= 7 else 0), 13, 0, NS.get("DOMINO_AREA", 0), 0)
    return w


def h_online(c, msg_id, param, data):
    """0x3603 MATCHINGSVR_REQPLAYERSEARCH (u32 n + n×u32 pid: amigos o miembros del equipo) -> 0x3604
    (FUN_006bcdd0): u32 ?, u32 n, n×(u32 pid, u8 idioma, u8 tipo, u8 sala, u8 área, u8 bloque). Solo van los
    conectados: el cliente marca +0x13b8=1 en esos y pinta los 5 B (FUN_00a93920 -> FUN_00a943b0); los que no
    vienen salen «no está en línea». Con los 5 B a 0 saltaba «illegal rank» (tipo 0). Ver NOTES «0x3604»."""
    n = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    pids = [struct.unpack(">I", data[4 + 4 * i:8 + 4 * i])[0] for i in range(min(n, (len(data) - 4) // 4))]
    hub = NS.get("HUB")
    on = []
    for p in pids:
        s = hub.by_pid(p) if hub is not None else None
        where = _friend_where(s) if s is not None else (_npc_where(c) if _npc_by_pid(p) else None)
        if where is None or where[1] not in FRIEND_OK_KINDS or not 0 <= where[2] <= 7:
            continue                                         # nunca un tipo/sala que dispare el aserto
        on.append((p, where))
    w = W().u32(0).u32(len(on))
    for p, where in on:
        w.u32(p)
        for b in where:
            w.u8(int(b) & 0xff)
    c.send(0x3604, w.bytes())
    _log(f"[lobby] 0x3603 amigos: {len(on)}/{len(pids)} conectados " +
         " ".join(f"{p}={'/'.join(map(str, where))}" for p, where in on))
    return True


# ---------------------------------------------------------------- correo
_MAIL_T = [0]


def mail_time():
    """Fecha de envío de un correo (time_t, 07-10): el s32 de 0x4612 es la FECHA que pinta la lista «AAAA/MM/DD hh:mm»
    (con el id salía 1970/01/01 01:00). Creciente para que dos correos del mismo segundo no compartan valor."""
    with LOCK:
        _MAIL_T[0] = max(int(time.time()), _MAIL_T[0] + 1)
        return _MAIL_T[0]


def mail_entry(m):
    """0x4612 RCVMAILLISTDATA (FUN_006beac0), 653 B, UNA por mensaje: u32 pid remitente, nombre16, u32 pid destino,
    nombre16, s32 fecha de envío (time_t; los correos viejos sin "t" la reciben ahora), asunto32, cuerpo512, 64 B,
    u8 (leído)."""
    if not m.get("t"):
        m["t"] = mail_time()
    return (W().u32(m["from"]).bytes() + _s(m["from_name"], 16) + W().u32(m["to"]).bytes() + _s(m["to_name"], 16)
            + W().u32(m["t"]).bytes() + _s(m["subject"], 32) + _s(m["body"], 512) + _s(m.get("extra", ""), 64)
            + W().u8(1 if m.get("read") else 0).bytes())


def h_sendmail(c, msg_id, param, data):
    """0x4600 SENDMAIL: u32 pid destino, asunto32, cuerpo512, 64 B (FUN_006cd670) -> 0x4601 s32 0 (0 = enviado).
    El correo se guarda para el destinatario; si es una NPC, contesta y llega 0x4603 SHORTMAILNOTICE (sin datos:
    pone +0x1ba4=1, el aviso de correo nuevo, y +0x45=2)."""
    to = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0
    subject, body = _cstr(data[4:36], 0), _cstr(data[36:548], 0)
    me, my_name, to_name, n = _pid(), _me(), _name_of(to), _npc_by_pid(to)
    with LOCK:
        m = {"id": MEM["next_mail"], "from": me, "from_name": my_name, "to": to, "to_name": to_name,
             "subject": subject, "body": body, "t": mail_time()}
        MEM["next_mail"] += 1; MEM["mail"].append(m)
        if n:
            MEM["mail"].append({"id": MEM["next_mail"], "from": to, "from_name": n[1], "to": me,
                                "to_name": my_name, "subject": ("Re: " + subject)[:31],
                                "body": "Gracias por tu carta. Te espero en la sala de duelo.", "t": mail_time()})
            MEM["next_mail"] += 1
        _save()
    c.send(0x4601, W().u32(0).bytes())
    _admin_ev("correo", _sess(c), to_name, bool(n), to)  # panel: «D-mail de X a Y» (sin asunto ni texto)
    if n:
        c.send(0x4603)
    return True


def h_rcvmail(c, msg_id, param, data):
    """0x4610 RCVMAIL -> 0x4611 RCVMAILLISTSTART s32 0 (!= 0 = error: el manejador FUN_006bea70 cierra la espera
    al momento y FUN_006d1840 caso 0x24 pide 0) + 0x4612 por correo + 0x4613 END (+0x43=2)."""
    c.send(0x4611, W().u32(0).bytes())
    me = _pid()
    with LOCK:
        mine = [m for m in MEM["mail"] if m["to"] == me][-50:]
    for m in mine:
        c.send(0x4612, mail_entry(m))
    c.send(0x4613)
    return True


def h_mailcount(c, msg_id, param, data):
    """0x4620 GETMAILCOUNT -> 0x4621 s32. OJO: el manejador (FUN_006bea20) usa > 0 para encender el aviso, pero la
    tarea (FUN_006d1840 caso 0x22, k=0x28) trata cualquier valor != 0 como error. Se manda siempre 0; los correos
    nuevos se avisan con 0x4603 SHORTMAILNOTICE."""
    c.send(0x4621, W().u32(0).bytes())
    return True


# ---------------------------------------------------------------- equipos
def _team_list(c):
    """TEAMLISTRESULT_BEGIN 0x4564 (FUN_006be3e0, log «チームリスト開始 チーム名:%s, チーム員数:%d»): s32 nº de
    miembros (0 = sin equipo, +0x11e0), u32 id equipo, nombre16 equipo, 16 B; 0x4559 TEAMLISTRESULT (FUN_006be190):
    bucle (u32 pid, nombre16, u8); 0x4565 _END s32 0 (+0x88=2)."""
    t = MEM["team"]
    if not t:
        c.send(0x4564, W().u32(0).u32(0).bytes() + _s("", 16) + _s("", 16))
    else:
        c.send(0x4564, W().u32(len(t["members"])).u32(t["id"]).bytes() + _s(t["name"], 16) + _s(_me(), 16))
        ents = [W().u32(p).bytes() + _s(nm, 16) + W().u8(0).bytes() for p, nm in t["members"]]
        for chunk in _chunks(ents, 21):
            c.send(0x4559, chunk)
    c.send(0x4565, W().u32(0).bytes())


def h_teamlist(c, msg_id, param, data):
    _team_list(c)
    return True


def h_teamcreate(c, msg_id, param, data):
    """0x4550 TEAMCREATE (nombre16) -> 0x4551 TEAMCREATERESULT u32 (0 = creado; otro = «チーム作成失敗:err=%d»,
    FUN_006bcfa0) + 0x4566 TEAMCREATERESULTEND s32 0 (pone +0x84=2, lo que espera el emisor). gen_auto solo
    mandaba 0x4551: la espera de +0x84 no terminaba nunca."""
    name = _cstr(data[:16], 0) or "Equipo"
    with LOCK:
        MEM["team"] = {"id": 1, "name": name, "members": [[_pid(), _me()]]}; _save()
    c.send(0x4551, W().u32(0).bytes())
    c.send(0x4566, W().u32(0).bytes())
    return True


def h_teamdisband(c, msg_id, param, data):
    """0x4552 TEAMDISBAND -> 0x4553 s32 0 (+ aviso 0x4560 u32 id). DUDOSO: el manejador no pone +0x85=2 (el emisor
    lo espera) y el emisor no tiene llamadas en el exe."""
    with LOCK:
        t, MEM["team"] = MEM["team"], None; _save()
    c.send(0x4553, W().u32(0).bytes())
    if t:
        c.send(0x4560, W().u32(t["id"]).bytes())
    return True


def h_teamleave(c, msg_id, param, data):
    """0x4569 TEAMLEAVE (sin datos ni espera) -> 0x4570 TEAMLEAVE_NOTICE: u32 id equipo, u32 pid, nombre16."""
    with LOCK:
        t, MEM["team"] = MEM["team"], None; _save()
    c.send(0x4570, W().u32(t["id"] if t else 0).u32(_pid()).bytes() + _s(_me(), 16))
    return True


# ---------------------------------------------------------------- sobres y cartas regalo
MAX_CARD = 0xcb8              # ids válidos 1..0xcb8 (FUN_006c1f30); fuera de ahí el cliente se cierra al pintarla
_VALID = []


def _valid_cards():
    """Cartas con dibujo (research/persistencia/valid_cards.json); si no está, 1..MAX_CARD."""
    if not _VALID:
        try:
            v = json.load(open(os.path.join(HERE, "..", "persistencia", "valid_cards.json"), encoding="utf-8"))
            _VALID.extend(int(x) for x in v if 1 <= int(x) <= MAX_CARD)
        except Exception:
            pass
        if not _VALID:
            _VALID.extend(range(1, MAX_CARD + 1))
    return _VALID


def _cards(pack=1, n=5):
    if "shop_purchase" in NS:
        try:
            cards = [cid for _, cid in NS["shop_purchase"](1, pack, 1) if 1 <= cid <= MAX_CARD][:n]
        except Exception as e:
            _log(f"[lobby] shop_purchase(1, {pack}) falló: {e!r}"); cards = []
        if cards:
            return cards
    return [random.choice(_valid_cards()) for _ in range(n)]


def _trunk_store(cards):
    """B13/B40: suma las cartas al baúl del perfil activo y lo guarda ANTES de mandar 0x4803 (si no, el siguiente
    0x3100 desde el perfil las borra). Devuelve el u8 «nueva» de cada una: 1 solo si el baúl no la tenía (como hace
    el propio cliente en 0x8102, FUN_006c01b0: kaban[carta].copias == 0); la 2.ª copia de una carta nueva en el
    mismo sobre ya no es nueva. Sin persistencia (PP) no hay baúl en el servidor: todas «nuevas», como antes."""
    PP = NS.get("PP")
    if PP is None or "load_state" not in NS or "save_state" not in NS:
        return [1] * len(cards)
    with _state_lock():
        st = NS["load_state"]()
        p = PP.profile(st, _slot(st))
        trunk = p.get("trunk") or {}
        seen, flags = set(), []
        for cid in cards:
            flags.append(1 if trunk.get(str(cid), 0) == 0 and cid not in seen else 0)
            seen.add(cid)
        PP.trunk_add(p, cards)
        NS["save_state"](st)
    return flags


def _cards_result(c, cards):
    """0x4803 SELECTCARDPACKRESULT (FUN_006bc0f0): s32 + bucle (u16 carta, u8 «nueva»); suma cada carta al baúl del
    cliente (+0x18ed) y pone +0x3a=2. Cualquier s32 != -150 cuenta como «ha llegado» (FUN_006adfb0).
    B13: primero se guardan en el baúl del perfil; si eso falla, se manda igual (que el cliente no se quede
    esperando +0x3a) y queda en el log."""
    try:
        flags = _trunk_store(cards)
    except Exception as e:
        _log(f"[lobby] 0x4803: no se pudieron guardar {cards} en el baúl: {e!r}")
        flags = [1] * len(cards)
    w = W().u32(0)
    for cid, new in zip(cards, flags):
        w.u16(cid).u8(new)
    c.send(0x4803, w.bytes())


def _event_cards_result(c, championship=False):
    """Consume y guarda atómicamente un premio obtenido; nunca inventa cartas."""
    ER, PP = NS.get("ER"), NS.get("PP")
    cards, flags = [], []
    if ER is not None and PP is not None and "load_state" in NS:
        try:
            with _state_lock():
                st = NS["load_state"]()
                p = PP.profile(st, _slot(st))
                reward = ER.take_reward(p, championship=championship)
                if reward:
                    cards = [int(x) for x in reward.get("cards", ()) if PP.card_ok(int(x))]
                    trunk = p.get("trunk") or {}
                    seen = set()
                    for cid in cards:
                        flags.append(1 if trunk.get(str(cid), 0) == 0 and cid not in seen else 0)
                        seen.add(cid)
                    PP.trunk_add(p, cards, src="evento " + str(reward.get("claim", "")))
                NS["save_state"](st)
        except Exception as e:
            _log(f"[eventos] no se pudo consumir el premio: {e!r}")
            cards, flags = [], []
    w = W().u32(0)
    for cid, new in zip(cards, flags):
        w.u16(cid).u8(new)
    c.send(0x4803, w.bytes())


def h_packlist(c, msg_id, param, data):
    """0x4901 RECOVERY_PACK_LIST -> 0x4800 CARDPACKLISTNOTICE (caso «default»: CMP EAX,0x4800 / JZ -> FUN_006bbe20):
    bucle de u16 id de pack (FUN_0084d8a0 carga su ficha); pone +0x39=2, que espera la escena FUN_00850d90 caso 5.
    gen_auto no tenía respuesta (colgado)."""
    c.send(0x4800, b"".join(W().u16(p).bytes() for p in range(1, 11)))
    return True


def h_selectpack(c, msg_id, param, data):
    """0x4802 SELECTCARDPACK (u16 id de pack de la lista anterior) -> 0x4803 con 5 cartas de ese pack."""
    pack = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1
    _cards_result(c, _cards(pack))
    return True


def h_giftcards(c, msg_id, param, data):
    """0x4910 REQGUNSLINGCARD y 0x4915 REQCHAMPSCARDGET: sus indicadores (+0x68/+0x79) no los pone nadie; las escenas
    (FUN_008444c0 caso 3, FUN_00850d90 caso 0xd) esperan +0x3a = SELECTCARDPACKRESULT 0x4803."""
    _event_cards_result(c, championship=msg_id == 0x4915)
    return True


# ---------------------------------------------------------------- intercambio (sobre 0x7000)
TRADE = {   # subcomando (u16 al principio de los datos de 0x7000) -> respuestas (id, datos)
    0x7101: [(0x7133, b"")],                       # TRADESTART -> TRADESTARTRESULT (+0x4f)
    0x7102: [(0x7103, struct.pack(">i", -493))],   # REQTRADE -> REQTRADERESULT: -493 = rechazo (único valor de error)
    0x7105: [(0x7105, struct.pack(">i", 0))],      # TRADEOFFERREPLY -> eco s32 0 (k=54)
    0x7108: [(0x7109, b"")],                       # CANCELREQTRADE -> CANCELREQTRADENOTICE (+0x54)
    0x7112: [(0x7112, struct.pack(">I", 0))],      # TRADEUSEREXIT -> eco u32 (+0x5e)
    0x7113: [(0x7113, struct.pack(">i", 0))],      # ADDCARD -> s32 0 (+0x56, k=58)
    0x7114: [(0x7114, struct.pack(">i", 0))],      # DELETECARD -> s32 0 (+0x57, k=59)
    0x7117: [(0x7117, b"\x00")],                   # SETTRADESTATUS -> eco u8 (+0x5a)
    0x7119: [(0x7120, b"\x00")],                   # TRADELASTCONFIRM -> TRADELASTCONFIRMRESULT u8 (+0x5c)
    0x7122: [(0x7122, b"")],                       # ARRIVETRADECARD -> eco (+0x60)
}


def h_trade(c, msg_id, param, data):
    """0x7000 = sobre de intercambio: u16 subcomando + campos (FUN_006cde40..006ce450). Las respuestas llegan con su
    propio id (0x7103...). Sin otro jugador real, REQTRADE se rechaza (-493 -> FUN_006d1840 caso 0x2a: error).
    TRADEREADY 0x7132 (+0x4e) y TRADECLEAR 0x7128 (+0x61): ningún mensaje pone sus indicadores (DUDOSO)."""
    sub = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
    for rid, payload in TRADE.get(sub, []):
        c.send(rid, payload)
    if sub not in TRADE:
        _log(f"[lobby] intercambio 0x{sub:04x} sin respuesta (DUDOSO)")
    return True


# ---------------------------------------------------------------- perfil
def h_playerinfo(c, msg_id, param, data):
    """0x4150 REQPLAYERINFO (u32 pid) -> 0x4160 (FUN_006b9b50): s32 0, u32 pid, nombre16, y estadísticas a 0
    (mismo bloque que PSELECTRESULT tras el id; total 133 B). El emisor no tiene llamadas directas en el exe."""
    pid = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else _pid()
    head = W().u32(0).u32(pid).bytes() + _s(_name_of(pid), 16)
    c.send(0x4160, head + bytes(PLAYERINFO_SIZE - len(head)))
    return True


PLAYERINFO_FMT = ['s32', 'u32', 'bytes16', 'u8', 'u32', 'u32'] + ['u16'] * 15 + ['u32', 'u16', 'u16', 'u32', 'u32'] \
    + ['u16'] * 6 + ['u32'] * 6 + ['u16'] * 3 + ['s32'] + ['u16'] * 3 + ['u32', 'u16']
_SZ = {"u8": 1, "u16": 2, "u32": 4, "s32": 4}
PLAYERINFO_SIZE = sum(_SZ.get(f) or int(f[5:]) for f in PLAYERINFO_FMT)


# ---------------------------------------------------------------- mensajes que faltaban (NOTES.md §10)
def h_resetautowait(c, msg_id, param, data):
    """0x42a0 RESETAUTOWAIT (sin datos; FUN_006cce40, pone +0x3c=1). Lo manda la tarea del lobby en su estado 0x13
    (FUN_006d3ef0 <- escena FUN_0087e810, paso 3, cuando las dos colas de 0x12bb6b0 están vacías). Estado 0x14:
    espera +0x3c == 2 (lo pone 0x42a2, FUN_006bc230), lee k=0x20 y 0 = bien (escena -> 0xb), otro = error con
    código (escena -> 0x4a). C. Mismo resultado que auto_replies, pero ya no es «genérico»."""
    if not RESETAUTOWAIT:
        return False
    c.send(0x42a2, struct.pack(">i", 0))
    # AUTODUEL: el cliente solo lo manda al volver de un duelo con sala propia y mstat 1 (FUN_00863ec0 -> estado 3,
    # C): vuelve a «busca autoduelo» para los demás (si ROOM_REOPEN no lo hizo ya con 0x4210) y, con
    # AUTODUEL_MATCH, a la cola.  P (servidor)
    if AUTODUEL:
        try:
            s = _sess(c)
            me = getattr(s, "pid", None) or _pid()
            pvp = NS.get("PVP")
            with LOCK:
                mine = [r for r in MEM["rooms"].values() if r["p1"] == me and _is_auto(r) and not r.get("p2")]
            if mine and not (pvp is not None and pvp.by_pid(me) is not None):
                if getattr(s, "state", 0) != AUTO_STATE:
                    _room_state(s, AUTO_STATE)
                if AUTODUEL_MATCH:
                    _later(AUTODUEL_MATCH_DELAY, _auto_try_match, c)
        except Exception as e:
            _log(f"[lobby] autoduelo tras 0x42a0: {e!r}")
    return True


def h_usbdpauth(c, msg_id, param, data):
    """0x8203 REQUSBDPAUTH (16 B; FUN_006cebe0, +0x7a). Solo sale de la ventana CWindowModelDuelpassUSB
    (FUN_00a894c0, 0xa8a8a6) si antes el servidor USB (tipo 6 de la lista de servidores, conexión aparte en
    juego+0xc74, distribuidor FUN_006d6010) contestó 0x8202 USBCHECKRESULT = 0. No tenemos ese servidor.
    0x8204 (FUN_006c1710): s32; si es 0 lee además s8, 3×(u16 carta, u8) y u8, suma las cartas al baúl y da el
    DUELPASS (DPP +150, MP +50). auto_replies mandaba 9 B a cero = «éxito» con datos cortos. Aquí: s32 1 (error,
    no lee nada más). C (el cliente solo distingue 0 / no 0)."""
    if not USBDPAUTH_REFUSE:
        return False
    c.send(0x8204, struct.pack(">i", 1))
    return True


def h_tppresentinfo(c, msg_id, param, data):
    """0x8301 REQTPPRESENTINFO (sin datos; FUN_006cee50, +0x7c) desde la escena de login FUN_00850d90 caso 0x14 (solo
    con tp_rank 1-1000 o bits PBIN 0x240/0x242). 0x8302 (FUN_006c2800): u16 = carta premio («TP present victory
    card:%d») a CNetLobby+0x548, +0x7c=2; caso 0x15 sigue. Formato C, carta D (TP_PRESENT_CARD)."""
    if not TPPRESENT:
        return False
    c.send(0x8302, struct.pack(">H", TP_PRESENT_CARD & 0xffff))
    return True


# Startup Present: bits PBIN «GET権利» (derecho) que mira la escena de login (FUN_00850d90 caso 0x18, 0x852eaa: se
# queda con el ÚLTIMO puesto de 0x30e, 0x310, 0x312, 0x314, 0x316) -> presentEntry.cpp (FUN_008ef050, botón 4000)
# manda 0x8401 = u8 n + n×u16 cartas elegidas (FUN_006cef30). Caso 0x1a: si 0x8402 trae s32 0 pone el bit+1
# («GET済み», FUN_00ad6900 en 0x8535a1). Nombres de yo2pbinmap.dat: Startup Present 30/40/50/60/10 (782-791).  C
# Tope de cartas = el número del nombre: P. FUN_006c29c0 lee las cartas en una pila de 80 u16: más de 80 = desborde. C
STARTUP_PRESENT_BITS = {0x30e: 30, 0x310: 40, 0x312: 50, 0x314: 60, 0x316: 10}
STARTUP_PRESENT_MAX = 80


def startup_present_result(data, p=None, PP=None):
    """(s32, [cartas]) para 0x8402 a partir de los datos de 0x8401 y del perfil p (con persistencia PP). Sin perfil
    no se valida el derecho (pruebas). Error (s32 1, sin cartas) si la lista está mal, no hay derecho o ya se recogió."""
    if not data:
        return 1, []
    n = data[0]
    if n == 0 or n > STARTUP_PRESENT_MAX or len(data) < 1 + 2 * n:
        return 1, []
    cards = list(struct.unpack(">%dH" % n, bytes(data[1:1 + 2 * n])))
    valid = set(_valid_cards())
    if any(cid not in valid for cid in cards):
        return 1, []
    if p is not None and PP is not None:
        right = [b for b in sorted(STARTUP_PRESENT_BITS) if PP.pbin_test(p, b)]
        if not right:
            return 1, []
        bit = right[-1]                                   # como el cliente: el último puesto
        if PP.pbin_test(p, bit + 1) or n > STARTUP_PRESENT_BITS[bit]:
            return 1, []
        PP.pbin_set(p, bit + 1)                           # lo mismo que hará el cliente con s32 0 (C)
    return 0, cards


def h_startup_present(c, msg_id, param, data):
    """0x8401 -> 0x8402 STARTUP_PRESENTGET_RESULT (FUN_006c29c0): s32 (+0x968, k=0x62) y, SIEMPRE, u8 n + n×u16:
    cada carta se suma al baúl del cliente (+0x18ed) aunque el s32 sea de error; +0x7e=2. Con error mandamos n=0.
    Con flag apagado: auto_overrides.json (7 B a cero = éxito sin cartas, como hasta hoy)."""
    if not STARTUP_PRESENT:
        return False
    PP = NS.get("PP")
    if PP is not None and "load_state" in NS and "save_state" in NS:
        with _state_lock():
            st = NS["load_state"]()
            p = PP.profile(st, _slot(st))
            r, cards = startup_present_result(data, p, PP)
            if r == 0:
                PP.trunk_add(p, cards, src="0x8402 Startup Present")
                NS["save_state"](st)
    else:
        r, cards = startup_present_result(data)
    w = W().u32(r & 0xffffffff).u8(len(cards))
    for cid in cards:
        w.u16(cid)
    _log(f"[lobby] 0x8401 Startup Present: {len(cards)} cartas, resultado {r}")
    c.send(0x8402, w.bytes())
    return True


def maintenance_notice(seconds):
    """0x4700 MAINTENOTICE a todos los clientes conectados (para antes de reiniciar el servidor). Desde la consola:
    LP.maintenance_notice(600). Cliente (FUN_006bf290): s32 = SEGUNDOS que faltan -> CNetLobby+0x1bac, guarda
    time() en +0x1bb0, +0x48=2 y enciende el icono 0 de la barra del menú (FUN_00a564e0 -> FUN_00a7fb70(0)).
    Al pulsarlo (FUN_00a7a610): textos 5270 «Falta poco para ejecutar el mantenimiento.» + 5271 «Tiempo restante
    hasta que el servidor se apague:» y «%02d:%02d» = horas:minutos que quedan (cuenta hacia atrás local). C.
    Nadie espera +0x48: es un aviso, no una respuesta. Devuelve a cuántas conexiones se mandó."""
    data = struct.pack(">i", max(0, int(seconds)))
    targets = []
    hub = NS.get("HUB")
    if hub is not None and hasattr(hub, "sessions"):
        with hub.lock:
            targets = [s.lobby for s in hub.sessions.values() if getattr(s, "lobby", None) is not None]
    if not targets:                       # sin sesiones PvP: la última conexión de cada cliente al lobby o a una sala
        lp = NS.get("LOBBY_PORT", 5732)
        rb = NS.get("ROOM_PORT_BASE", lp + 8)
        ports = {lp} | {rb + i for i in range(len(NS.get("ROOM_KINDS") or ()))}
        if callable(NS.get("room_ports")):                 # + las salas de idioma (ROOM_PER_LANG, 10-10)
            ports |= set(NS["room_ports"]())
        targets = [c for (_peer, port), c in list((NS.get("PEER_CONNS") or {}).items()) if port in ports]
    seen, n = set(), 0
    for c in targets:
        if id(c) in seen:
            continue
        seen.add(id(c))
        n += bool(_post(c, 0x4700, data))
    _log(f"[lobby] 0x4700 MAINTENOTICE {int(seconds)} s -> {n} cliente(s)")
    return n


# Mensajes del protocolo que NO hace falta mandar/contestar: evidencia en NOTES.md §10 (todo C salvo lo indicado).
NO_APLICA = {
    0x4410: "REQCHATROOMLIST: emisor FUN_006ccbf0 sin llamadas ni referencias (chat por salas, código muerto)",
    0x4424: "REQCREATECHATROOM: emisor FUN_006ccc40 sin llamadas ni referencias",
    0x4428: "REQEXITCHATROOM: emisor FUN_006ccd70 sin llamadas ni referencias",
    0x430a: "AUTOMATCHREPLY: emisor FUN_006cc960 sin llamadas ni referencias",
    0x4904: "PRESENT_PACK_OPEN: emisor FUN_006ce570 sin llamadas ni referencias",
    0x4905: "PRESENT_PACK_OPEN_RESULT: respuesta de 0x4904, que nunca se manda",
    0x5201: "MILEAGELISTNOTICE: respuesta de 0x5200 (emisor FUN_006ce6b0 sin llamadas)",
    0x5203: "MILEAGERESULTEND: respuesta de 0x5202 (emisor FUN_006ce700 sin llamadas)",
    0x5215: "PURCHASE_SHOP_ITEM_RESULT_BEGIN: manejador FUN_006c1ef0 vacío",
    0x5216: "PURCHASE_SHOP_ITEM_RESULT_END: solo pone +0x83, que nadie espera (la tienda espera +0x82 = 0x5211)",
    0x4182: "SAVE_DECK: guarda 128 + 512 B en CNetLobby+0x74b/+0x54a que nadie lee; no pone indicador",
    0x4521: "REQFRIENDSEARCHRESULT: no existe la petición 0x4520; +0x40 ya lo pone 0x3604",
    0x4563: "TEAMCREATE_NOTICE: manejador FUN_006bd070 vacío",
    0x7107: "CARDLIST: manejador FUN_006bf6a0 vacío",
    0x7110: "CANCELREQTRADERESULT: manejador FUN_006bf680 vacío",
    0x7111: "TRADEOFFERTIMEOUT: manejador FUN_006bf630 vacío",
    0x7115: "MOVEOWNCARD: guarda u16+u8 en +0x1bce/+0x1bd0 y pone +0x58; la escena del intercambio no lo mira",
    0x8202: "USBCHECKRESULT: del servidor USB (tipo 6, distribuidor FUN_006d6010), no del lobby; producto «USB Duelpass»",
    0x8205: "USBGETCARDLIST: del mismo servidor USB (u8 uso, s8, 3×u16 cartas -> juego+0xc5c+0x108)",
}


# ---------------------------------------------------------------- cierre de conexión
def on_close(c):
    """B58: la conexión c se cierra (0x0003 del cliente, o run.py al salir del bucle de lectura). No manda nada
    (el socket se va) y se puede llamar varias veces. Borra las salas propias de esa conexión (si no, MEM["rooms"]
    conserva una sala con P1 = yo y el cliente, al volver, la ve como suya: +0xd8b=1 desactiva los carteles GoEvent
    y desvía la charla con NPC), anula el reto pendiente de esas salas, suelta LOBBY_ROOM["c"] si era ella (DF.finish
    no manda por un socket muerto) y marca c para que los temporizadores no manden por ella."""
    try:
        c.lp_closed = True
    except Exception:
        pass
    with LOCK:
        gone = [k for k, v in MEM["rooms"].items() if v.get("conn") is c]
        gone_rooms = [MEM["rooms"].pop(k) for k in gone]
        off = MEM["offer"]
        if off and (off.get("room") in gone or off.get("room") not in MEM["rooms"]):
            MEM["offer"] = None
        chs = MEM.setdefault("challenges", {})
        mine = [ch for k, ch in list(chs.items()) if k in gone or ch["from"] is c or ch["to"] is c]
        for ch in mine:
            chs.pop(ch["room"], None)
            ch["done"] = True
    # PvP (P1-9): la sala se borra también en el bloque, y el otro lado de un reto pendiente se entera
    if gone_rooms:
        _room_state(_sess(c), 0)
    for r in gone_rooms:
        _block_post(r.get("block"), 0x3113, room_entry(dict(r, p1=0, p2=0)), skip=_sess(c))
    for ch in mine:
        if ch["from"] is c:
            _post(ch["to"], 0x4305, b"")
        else:
            _post(ch["from"], 0x4301, W().u32(R_GONE & 0xffffffff).bytes())
    lr = NS.get("LOBBY_ROOM")
    if isinstance(lr, dict) and lr.get("c") is c:
        lr["c"] = None
    if gone:
        _log(f"[lobby] conexión {getattr(c, 'port', '?')} cerrada: salas {gone} borradas")
    return True


def h_close(c, msg_id, param, data):
    """0x0003 = el cliente cierra la sesión (FUN_0068cf60: solo manda y pone +0xe4=1; no espera respuesta).
    Antes salía en el log como «sin respuesta para 0x0003» (27 veces) y no se limpiaba nada."""
    on_close(c)
    return True


HANDLERS = {
    0x0003: h_close,
    0x4280: h_roomlist, 0x4288: h_createroom, 0x4290: h_deleteroom, 0x4300: h_reqmatch, 0x4303: h_offerreply,
    0x4304: h_cancelmatch, 0x4320: h_watch, 0x4400: h_chat, 0x4500: h_getpid, 0x4510: h_reqfriend,
    0x4513: h_friendofferreply, 0x4530: h_black, 0x4540: h_black, 0x3603: h_online, 0x4600: h_sendmail,
    0x4610: h_rcvmail, 0x4620: h_mailcount, 0x4550: h_teamcreate, 0x4552: h_teamdisband, 0x4558: h_teamlist,
    0x4569: h_teamleave, 0x4901: h_packlist, 0x4802: h_selectpack, 0x4910: h_giftcards, 0x4915: h_giftcards,
    0x7000: h_trade, 0x4150: h_playerinfo,
    0x42a0: h_resetautowait, 0x8203: h_usbdpauth, 0x8301: h_tppresentinfo, 0x8401: h_startup_present,
}


def handle(c, msg_id, param, data):
    """True = contestado aquí. B60: si el manejador falla DESPUÉS de mandar algo, devuelve True igualmente (y lo
    registra), para que run.py no conteste otra vez con su rama propia o con auto_reply (respuesta doble). Si falla
    sin haber mandado nada, False: run.py sigue como antes. Para contar se envuelve c.send solo durante la llamada
    (un envío de un temporizador en ese instante también cuenta: peor caso, no se repite la respuesta)."""
    # Tanda «espectador» (NOTES §7, §9): avisos que no contestan; lo de siempre sigue después.
    if LIMIT_LIST and msg_id == 0x4100:
        try:
            for mid, d in limitation_msgs():
                c.send(mid, d)
        except Exception as e:
            _log(f"[lobby] 0x5600: {e!r}")
    if ROOM_REOPEN and msg_id == 0x4210:
        try:
            _room_reopen(c)
        except Exception as e:
            _log(f"[lobby] sala tras el duelo: {e!r}")
    h = HANDLERS.get(msg_id)
    if h is None:
        return False
    sent = [0]
    own = "send" in getattr(c, "__dict__", {})
    orig = c.send
    def counting(*a, **k):
        sent[0] += 1
        return orig(*a, **k)
    try:
        c.send = counting
        wrapped = True
    except Exception:                       # objeto sin __dict__: sin cuenta (como antes)
        wrapped = False
    try:
        return h(c, msg_id, param, data) is not False
    except Exception as e:
        if sent[0]:
            _log(f"[lobby] error en 0x{msg_id:04x} tras mandar {sent[0]} mensaje(s): {e!r} (no se repite respuesta)")
            return True
        _log(f"[lobby] error en 0x{msg_id:04x}: {e!r}")
        return False
    finally:
        if wrapped:
            try:
                if own:
                    c.send = orig
                else:
                    del c.send
            except Exception:
                pass


# ---------------------------------------------------------------- prueba de tamaños
EXTRA_FMT = {   # mensajes que protocol.json no tiene (casos «default» del distribuidor, ver NOTES.md)
    "0x4309": ["s32"], "0x4501": ["s32", "u32"], "0x4800": ["LOOP", "u16"],
    # 0x3604: protocol.json no marca el bucle (es un do-while con contador, FUN_006bcdd0)
    "0x3604": ["u32", "u32", "LOOP", "u32", "u8", "u8", "u8", "u8", "u8"],
}


def _flat(ops):
    out = []
    for o in ops:
        out += _flat(o["ops"]) if isinstance(o, dict) else [o]
    return out


def _fmt_size(fmt):
    """(fijo, unidad del bucle, tiene cadena) a partir de una lista de lecturas."""
    fixed, unit, in_loop, cstr = 0, 0, False, False
    for f in fmt:
        if f == "LOOP":
            if in_loop:
                break
            in_loop = True; continue
        if f == "cstr":
            cstr = True; continue
        n = _SZ.get(f) or (int(f[5:]) if f.startswith("bytes") and f[5:].isdigit() else 0)
        if in_loop:
            unit += n
        else:
            fixed += n
    return fixed, unit, cstr


def selftest():
    import ast
    ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    P = json.load(open(os.path.join(EMU, "..", "..", "_original", "ghidra", "protocol.json")))
    fmts = {k: _flat(v.get("reads", [])) for k, v in P["recv"].items()}
    fmts.update(EXTRA_FMT)

    class FakeConn:
        def __init__(self, port=5745): self.port, self.out = port, []
        def send(self, mid, data=b"", pause=0): self.out.append((mid, bytes(data)))

    global CHALLENGE_DELAY, FRIEND_ACCEPT_DELAY
    CHALLENGE_DELAY = FRIEND_ACCEPT_DELAY = 0
    st = {"players": {"0": "Player1"}}
    install({"load_state": lambda: st, "save_state": lambda s: None, "PLAYER_ID": 1, "DUEL": {"rival": 0},
             "DUEL_PORT": 5760, "LOBBY_PORT": 5732, "log": lambda *a: None})
    yugi = FAKE_BASE + 51
    cases = [
        (0x4280, W().u32(0xffffffff).bytes()),
        (0x4288, bytes([3, 0, 0, 0]) + bytes(16) + bytes([1, 1, 1, 0]) + W().u32(0).bytes()),
        (0x4303, W().u32(1).bytes()),
        (0x4288, bytes([3, 0, 0, 0]) + bytes(16) + bytes([1, 1, 1, 0]) + W().u32(0).bytes()),
        (0x4303, W().u32(0).bytes()),
        (0x4290, b""), (0x4300, W().u32(1).bytes() + bytes(16)), (0x4300, W().u32(999).bytes() + bytes(16)),
        (0x4304, b""), (0x4320, W().u32(1).bytes()),
        (0x4400, bytes([0, 0]) + W().u32(0).u32(0).u32(1).bytes() + b"hola" + bytes(1)),
        (0x4400, bytes([0, 3]) + W().u32(0).u32(yugi).u32(1).bytes() + b"psst" + bytes(1)),
        (0x4500, _s("Yugi Muto", 16)), (0x4500, _s("nadie", 16)),
        (0x4510, W().u32(yugi).bytes()), (0x4510, W().u32(7).bytes()), (0x4513, W().u32(yugi).u32(1).bytes()),
        (0x4530, W().u32(yugi).bytes()), (0x4540, W().u32(yugi).bytes()),
        (0x3603, W().u32(2).u32(yugi).u32(7).bytes()),
        (0x4600, W().u32(yugi).bytes() + _s("Reto", 32) + _s("Duelo?", 512) + bytes(64)),
        (0x4610, b""), (0x4620, b""),
        (0x4558, b""), (0x4550, _s("Los Player", 16)), (0x4558, b""), (0x4569, b""), (0x4550, _s("B", 16)),
        (0x4552, b""),
        (0x4901, b""), (0x4802, W().u16(3).bytes()), (0x4910, b""), (0x4915, b""),
        (0x4150, W().u32(yugi).bytes()),
    ] + [(0x7000, W().u16(s).bytes() + bytes(8)) for s in sorted(TRADE)] + [(0x7000, W().u16(0x7128).bytes())]
    bad, n = [], 0
    for mid, data in cases:
        c = FakeConn()
        ok = handle(c, mid, 0, data)
        assert ok, f"0x{mid:04x} no manejado"
        for rid, payload in c.out:
            n += 1
            key = "0x%04x" % rid
            assert len(payload) <= MAX_DATA, f"{key} pasa de 1024 B"
            if key not in fmts:
                bad.append(f"{key}: sin formato"); continue
            fixed, unit, cstr = _fmt_size(fmts[key])
            L = len(payload)
            if cstr:
                good = L >= fixed + 1 and payload.endswith(bytes(1))
            elif unit:
                good = L >= fixed and (L - fixed) % unit == 0
            else:
                good = L == fixed
            if not good:
                bad.append(f"0x{mid:04x} -> {key}: {L} B, esperado {fixed} + n×{unit}{' + cadena' if cstr else ''}")
    for b in bad:
        print("MAL", b)
    print(f"prueba de tamaños: {n} respuestas, {len(bad)} con tamaño incorrecto")
    return not bad


def selftest_ola1():
    """Casos de los huecos del lote L6 (B11, B12, B13/B40, B58, B60, B61, B63). Sin red ni state.json real."""
    global CHALLENGE_DELAY, FRIEND_ACCEPT_DELAY, NPC_ROOMS
    errors = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    class FakeConn:
        def __init__(self, port=5745): self.port, self.out = port, []
        def send(self, mid, data=b"", pause=0): self.out.append((mid, bytes(data)))

    class FakePP:   # lo mínimo de research/persistencia que usa el lobby
        @staticmethod
        def active_slot(st): return int(st.get("active_slot", 0))
        @staticmethod
        def profile(st, slot): return st.setdefault("profiles", {}).setdefault(str(int(slot)), {"trunk": {}})
        @staticmethod
        def card_ok(cid): return 1 <= int(cid) <= MAX_CARD
        @staticmethod
        def trunk_add(p, cards, n=1, **_kw):
            t = p.setdefault("trunk", {})
            for cid in cards:
                if 1 <= int(cid) <= MAX_CARD:
                    t[str(int(cid))] = min(t.get(str(int(cid)), 0) + n, 0xff)

    old = (CHALLENGE_DELAY, FRIEND_ACCEPT_DELAY, NPC_ROOMS)
    CHALLENGE_DELAY = FRIEND_ACCEPT_DELAY = 0
    NPC_ROOMS = 0
    with LOCK:
        MEM.update(rooms={}, offer=None)
    st = {"players": {"0": "Player1", "1": "Segundo"}, "active_slot": 1, "profiles": {"1": {"trunk": {"5": 3}}}}
    saves, starts, lobby_room, duel = [], [], {"c": None}, {"rival": 0}
    ns = {"load_state": lambda: st, "save_state": lambda s: saves.append(1), "PLAYER_ID": 1, "DUEL": duel,
          "DUEL_PORT": 5760, "LOBBY_PORT": 5732, "log": lambda *a: None, "PP": FakePP(),
          "DF": {"h_autoduelstart": lambda npc, mode: starts.append((npc, mode))}, "LOBBY_ROOM": lobby_room,
          "NPCS": [(2, "Npc Dos", 1, bytes(16)), (51, "Yugi Muto", 209, bytes(16))]}
    install(ns)

    # B12: pid y nombre del hueco activo, calculados en cada llamada (dict vivo)
    check("B12 pid = hueco + 1", _pid() == 2)
    check("B12 nombre del hueco activo", _me() == "Segundo")
    check("B12 pid propio no es la NPC 2", _npc_by_pid(2) is None and _npc_by_pid(FAKE_BASE + 2) == (2, "Npc Dos"))
    ns["my_pid"], ns["my_name"] = (lambda: 7), (lambda: "Otro")
    check("B12 my_pid/my_name de run.py (definidos tras install)", _pid() == 7 and _me() == "Otro")
    del ns["my_pid"], ns["my_name"]

    # B63: sin salas de NPC; con NPC_ROOMS > 0 vuelven
    c0 = FakeConn()
    handle(c0, 0x4280, 0, W().u32(0xffffffff).bytes())
    check("B63 lista de salas vacía", c0.out[0] == (0x4282, W().u32(0).bytes()) and 0x4284 not in [m for m, _ in c0.out])
    NPC_ROOMS = 6
    c0 = FakeConn(); handle(c0, 0x4280, 0, W().u32(0xffffffff).bytes())
    check("B63 NPC_ROOMS=6 las vuelve a anunciar", c0.out[0] == (0x4282, W().u32(1).bytes()))
    NPC_ROOMS = 0

    # B11: sala propia + reto de NPC aceptado -> LOBBY_ROOM["c"] y h_autoduelstart(npc)
    c = FakeConn()
    room_req = bytes([3, 0, 0, 0]) + bytes(16) + bytes([1, 1, 1, 0]) + W().u32(0).bytes()
    check("B11 crear sala", handle(c, 0x4288, 0, room_req))
    mine = [r for r in MEM["rooms"].values() if r["conn"] is c]
    check("B12 sala con P1 = pid del hueco activo", len(mine) == 1 and mine[0]["p1"] == 2)
    check("B11 reto 0x4302", 0x4302 in [m for m, _ in c.out])
    npc = MEM["offer"]["npc"] if MEM["offer"] else None
    handle(c, 0x4303, 0, W().u32(1).bytes())
    check("B11 0x4306 mandado", c.out[-1][0] == 0x4306)
    check("B11 LOBBY_ROOM['c'] = conexión de la sala", lobby_room["c"] is c)
    check("B11 h_autoduelstart(npc, 0)", starts == [(npc, 0)] and duel["rival"] == npc and npc is not None)

    # B58: 0x0003 en silencio: borra la sala, suelta LOBBY_ROOM y los temporizadores ya no mandan por c
    n_out = len(c.out)
    check("B58 0x0003 manejado", handle(c, 0x0003, 0, b"") is True)
    check("B58 0x0003 no contesta", len(c.out) == n_out)
    check("B58 salas de c borradas", not [r for r in MEM["rooms"].values() if r["conn"] is c])
    check("B58 LOBBY_ROOM soltado", lobby_room["c"] is None)
    _npc_challenge(c, mine[0]["id"] if mine else 0)
    check("B58 temporizador no manda tras cerrar", len(c.out) == n_out)
    check("B58 on_close idempotente", on_close(c) is True)
    c2, c3 = FakeConn(), FakeConn()
    ns["CONNS"] = {5745: c3}
    check("B58 conexión sustituida no se usa", not _alive(c2) and _alive(c3))
    del ns["CONNS"]

    # B13/B40: 0x4803 guarda en el baúl antes de mandar y marca «nueva» de verdad
    ns["shop_purchase"] = lambda k, p, n: [(1, 5), (1, 9), (1, 9), (1, MAX_CARD + 1), (1, 0), (1, 11)]
    st["profiles"]["1"]["trunk"] = {"5": 3}
    saves.clear()
    c4 = FakeConn()
    handle(c4, 0x4802, 0, W().u16(1).bytes())
    exp = W().u32(0).u16(5).u8(0).u16(9).u8(1).u16(9).u8(0).u16(11).u8(1).bytes()
    check("B40 0x4802 -> 0x4803 con «nueva» real", c4.out == [(0x4803, exp)])
    check("B13 0x4802 cartas en el baúl del hueco activo",
          st["profiles"]["1"]["trunk"] == {"5": 4, "9": 2, "11": 1} and saves)
    for mid in (0x4910, 0x4915):
        ns["ER"] = type("FakeER", (), {"take_reward": staticmethod(
            lambda p, championship=False: {"claim": "test", "cards": [9, 11],
                                            "championship": championship})})
        st["profiles"]["1"]["trunk"] = {"9": 1}
        c4 = FakeConn(); handle(c4, mid, 0, b"")
        expected = W().u32(0).u16(9).u8(0).u16(11).u8(1).bytes()
        check(f"evento 0x{mid:04x} consume claim y responde 0x4803", c4.out == [(0x4803, expected)])
        check(f"evento 0x{mid:04x} guarda baúl", st["profiles"]["1"]["trunk"] == {"9": 2, "11": 1})
    del ns["ER"]
    ns["PP"] = None
    c4 = FakeConn(); handle(c4, 0x4910, 0, b"")
    check("evento sin persistencia: 0x4803 vacío", c4.out == [(0x4803, W().u32(0).bytes())])
    ns["PP"] = FakePP()
    ns["save_state"] = lambda s: (_ for _ in ()).throw(OSError("disco"))
    c4 = FakeConn(); handle(c4, 0x4910, 0, b"")
    check("B13 si el guardado falla, 0x4803 sale igual", len(c4.out) == 1 and c4.out[0][0] == 0x4803)
    ns["save_state"] = lambda s: saves.append(1)
    del ns["shop_purchase"]
    c4 = FakeConn(); handle(c4, 0x4910, 0, b"")
    check("evento sin claim: 0x4803 vacío", c4.out == [(0x4803, W().u32(0).bytes())])

    # B60: fallo tras mandar = True (sin respuesta doble); fallo sin mandar = False; c.send restaurado
    def boom_after(c, *a):
        c.send(0x4292, W().u32(0).bytes()); raise ValueError("a medias")
    def boom_before(c, *a):
        raise ValueError("antes")
    HANDLERS[0x7ffe], HANDLERS[0x7fff] = boom_after, boom_before
    try:
        c5 = FakeConn()
        check("B60 fallo tras mandar -> True", handle(c5, 0x7ffe, 0, b"") is True and len(c5.out) == 1)
        check("B60 fallo sin mandar -> False", handle(c5, 0x7fff, 0, b"") is False)
        check("B60 c.send restaurado", "send" not in vars(c5))
    finally:
        del HANDLERS[0x7ffe], HANDLERS[0x7fff]

    # temporizadores: una excepción en el hilo acaba en el log, no en stderr
    logged = []
    ns["log"] = lambda *a: logged.append(" ".join(map(str, a)))
    def bad_timer():
        raise KeyError("x")
    _later(0.01, bad_timer)
    for _ in range(50):
        if logged:
            break
        time.sleep(0.02)
    check("temporizador con excepción -> log", any("bad_timer" in m for m in logged))
    ns["log"] = lambda *a: None

    # B61 (y todos los overrides con «hex»): largo del hex = suma del fmt
    ov = json.load(open(os.path.join(HERE, "auto_overrides.json"), encoding="utf-8"))

    def fsize(fmt):
        return sum(_SZ.get(f) or (int(f[5:]) if f.startswith("bytes") and f[5:].isdigit() else 0) for f in fmt)
    reps = [r for k, v in ov.items() if k.startswith("0x") for r in v]
    reps += [r for v in ov.get("_sub", {}).get("0x7000", {}).values() for r in v]
    for r in reps:
        if r.get("hex"):
            check(f"B61 {r['id']} hex = fmt", len(bytes.fromhex(r["hex"])) == fsize(r["fmt"]))
    r8304 = (ov.get("0x8303") or [{}])[0]
    check("B61 0x8303 -> 0x8304 de 13 B", r8304.get("id") == "0x8304" and len(bytes.fromhex(r8304.get("hex", ""))) == 13)

    CHALLENGE_DELAY, FRIEND_ACCEPT_DELAY, NPC_ROOMS = old
    for e in errors:
        print("MAL", e)
    print(f"prueba ola 1 (L6): {len(errors)} fallos")
    return not errors


def selftest_espectador():
    """Tanda «espectador» (NOTES §7-§9) sin red: formato de 0x5600, h_watch con y sin WATCH, y la sala tras el duelo."""
    global LIMIT_LIST, WATCH, ROOM_REOPEN, CHALLENGE_DELAY
    errors = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    class FakeConn:
        def __init__(self, port=5745): self.port, self.out, self.peer = port, [], "127.0.0.9"
        def send(self, mid, data=b"", pause=0): self.out.append((mid, bytes(data)))

    old = (LIMIT_LIST, WATCH, ROOM_REOPEN, CHALLENGE_DELAY)
    # §7 0x5600
    msgs = limitation_msgs()
    data = b"".join(d for m, d in msgs)
    ents = [struct.unpack(">IIIB", data[i:i + 13]) for i in range(0, len(data), 13)]
    check("0x5600: solo 0x5600, <= 1024 B y múltiplo de 13", all(m == 0x5600 and len(d) <= MAX_DATA and len(d) % 13 == 0
                                                                for m, d in msgs))
    check("0x5600: índices 0..n-1 seguidos", [e[0] for e in ents] == list(range(len(ents))))
    check("0x5600: Pot of Greed (YO2 291, Konami 4844) = 0 y Mirror Force (467, 4887) = 1",
          (291, 4844, 0) in [e[1:] for e in ents] and (467, 4887, 1) in [e[1:] for e in ents])
    check("0x5600: solo 0/1/2 y claves Konami >= 3000", all(e[3] in (0, 1, 2) and e[2] >= 3000 for e in ents))
    LIMIT_LIST = True
    c = FakeConn()
    handle(c, 0x4100, 0, bytes([5, 0]))
    check("LIMIT_LIST: 0x4100 manda la lista y deja contestar a run.py", [m for m, _ in c.out] == [0x5600] * len(msgs))
    LIMIT_LIST = False
    c = FakeConn()
    handle(c, 0x4100, 0, bytes([5, 0]))
    check("sin LIMIT_LIST: nada", c.out == [])
    # §8 h_watch
    class FakePvp:
        def __init__(self): self.req = []
        def by_pid(self, pid): return None
        def watch_request(self, peer, pid):
            self.req.append((peer, pid))
            return object() if pid == 1234 else None
    pvp = FakePvp()
    NS["PVP"], NS["duel_ip_for"] = pvp, (lambda _c: "10.0.0.5")
    WATCH = False
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(1234).bytes())
    check("sin WATCH: 0x4321 = 1 y no pregunta a pvp", c.out[0][1][:4] == W().u32(1).bytes() and not pvp.req)
    WATCH = True
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(1234).bytes())
    r = c.out[0][1] if c.out else b""
    check("WATCH: 0x4321 = 0 + ip15 + puerto de duelo (21 B)", len(r) == 21 and r[:4] == bytes(4) and r[4:12] == b"10.0.0.5"
          and struct.unpack(">H", r[19:21])[0] == 5760 and pvp.req == [("127.0.0.9", 1234)])
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(77).bytes())
    check("WATCH sin duelo de ese pid: 0x4321 = 1", c.out and c.out[0][1][:4] == W().u32(1).bytes())

    class FakeNw:                                     # npc_watch.py (07-10, añadido nuestro)
        def __init__(self): self.req, self.forgot = [], []
        def watch_request(self, peer, pid):
            self.req.append((peer, pid))
            return object() if pid == 555 else None
        def forget(self, peer): self.forgot.append(peer)
    nw = NS["NW"] = FakeNw()
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(555).bytes())
    r = c.out[0][1] if c.out else b""
    check("NPC: duelo de NPC de ese pid -> 0x4321 = 0 + ip + puerto", len(r) == 21 and r[:4] == bytes(4)
          and nw.req == [("127.0.0.9", 555)])
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(1234).bytes())
    check("NPC: un PvP manda y olvida la petición de NPC de esa IP", c.out[0][1][:4] == bytes(4)
          and nw.forgot == ["127.0.0.9"] and nw.req[-1] == ("127.0.0.9", 555))
    WATCH = False
    c = FakeConn(); h_watch(c, 0x4320, 0, W().u32(555).bytes())
    check("NPC sin WATCH: 0x4321 = 1 y no pregunta", c.out[0][1][:4] == W().u32(1).bytes() and len(nw.req) == 1)
    WATCH = True
    NS.pop("NW", None)
    # §9 sala tras el duelo
    ROOM_REOPEN, CHALLENGE_DELAY = True, None
    me = _pid()
    with LOCK:
        MEM["rooms"] = {5000: {"id": 5000, "p1": me, "p2": 4321, "mstat": 1, "mstat0": 0, "dstyle": 3, "wmode": 1,
                               "pw": False, "x": (1, 0, 1), "conn": None, "block": None}}
    c = FakeConn()
    handle(c, 0x4210, 0, bytes([0xff] * 4))
    r = [d for m, d in c.out if m == 0x3113]
    check("ROOM_REOPEN: 0x3113 con P1 = yo, P2 = 0, mstat 0", r and struct.unpack(">IIIB", r[0][:13]) == (5000, me, 0, 0))
    c = FakeConn()
    handle(c, 0x4210, 0, bytes([0xff] * 4))
    check("ROOM_REOPEN: una sola vez", not c.out)
    with LOCK:
        MEM["rooms"][5000]["p2"] = 4321
    pvp.by_pid = lambda pid: object()
    c = FakeConn()
    handle(c, 0x4210, 0, bytes([0xff] * 4))
    check("ROOM_REOPEN: con el duelo aún en marcha no toca la sala", not c.out and MEM["rooms"][5000]["p2"] == 4321)
    with LOCK:
        MEM["rooms"] = {}
    NS.pop("PVP", None); NS.pop("duel_ip_for", None)
    LIMIT_LIST, WATCH, ROOM_REOPEN, CHALLENGE_DELAY = old
    for e in errors:
        print("MAL", e)
    print(f"prueba espectador (0x5600, 0x4320, sala tras el duelo): {len(errors)} fallos")
    return not errors


def selftest_mensajes():
    """Tanda «mensajes que faltaban» (NOTES §10): 0x42a0, 0x8203, 0x8301, 0x8401 y 0x4700. Sin red ni state.json."""
    global RESETAUTOWAIT, USBDPAUTH_REFUSE, TPPRESENT, STARTUP_PRESENT
    errors = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    class FakeConn:
        def __init__(self, port=5732): self.port, self.out = port, []
        def send(self, mid, data=b"", pause=0): self.out.append((mid, bytes(data)))

    class FakePP:                          # pbin_* y trunk_add con la misma firma que research/persistencia
        @staticmethod
        def profile(st, slot, create=True): return st.setdefault("p", {"pbin": set(), "trunk": {}})
        @staticmethod
        def active_slot(st): return 0
        @staticmethod
        def pbin_test(p, bit): return bit in p["pbin"]
        @staticmethod
        def pbin_set(p, bit, on=True): (p["pbin"].add if on else p["pbin"].discard)(bit)
        @staticmethod
        def trunk_add(p, cards, n=1, src=""):
            for cid in cards:
                p["trunk"][str(cid)] = p["trunk"].get(str(cid), 0) + n

    old = (RESETAUTOWAIT, USBDPAUTH_REFUSE, TPPRESENT, STARTUP_PRESENT, dict(NS))
    st, saved = {}, []
    install({"load_state": lambda: st, "save_state": lambda s: saved.append(1), "PP": FakePP, "PLAYER_ID": 1,
             "log": lambda *a: None})
    # 0x42a0
    RESETAUTOWAIT = True
    c = FakeConn(); ok = handle(c, 0x42a0, 0, b"")
    check("0x42a0 -> 0x42a2 s32 0", ok and c.out == [(0x42a2, bytes(4))])
    RESETAUTOWAIT = False
    c = FakeConn(); check("0x42a0 sin flag: a auto_replies", not handle(c, 0x42a0, 0, b"") and not c.out)
    # 0x8203
    USBDPAUTH_REFUSE = True
    c = FakeConn(); handle(c, 0x8203, 0, bytes(16))
    check("0x8203 -> 0x8204 s32 1 (4 B)", c.out == [(0x8204, struct.pack(">i", 1))])
    # 0x8301
    TPPRESENT = False
    c = FakeConn(); check("0x8301 sin flag: a auto_replies", not handle(c, 0x8301, 0, b""))
    TPPRESENT = True
    c = FakeConn(); handle(c, 0x8301, 0, b"")
    check("0x8301 -> 0x8302 u16", c.out == [(0x8302, struct.pack(">H", TP_PRESENT_CARD))])
    # 0x8401
    STARTUP_PRESENT = False
    c = FakeConn(); check("0x8401 sin flag: a auto_overrides", not handle(c, 0x8401, 0, b"\x01\x00\x01"))
    STARTUP_PRESENT = True
    good = [x for x in _valid_cards()[:3]]
    req = bytes([3]) + struct.pack(">3H", *good)
    c = FakeConn(); handle(c, 0x8401, 0, req)
    check("0x8401 sin derecho: s32 1, n 0", c.out == [(0x8402, struct.pack(">iB", 1, 0))])
    st["p"]["pbin"].update({0x30e, 0x316})               # derechos 30 y 10: el cliente usa el último (0x316)
    c = FakeConn(); handle(c, 0x8401, 0, req)
    r = c.out[0][1] if c.out else b""
    check("0x8401 con derecho: s32 0 + 3 cartas (10 B)", c.out and c.out[0][0] == 0x8402 and len(r) == 4 + 1 + 6
          and r[:5] == struct.pack(">iB", 0, 3) and list(struct.unpack(">3H", r[5:])) == good)
    check("0x8401: bit 0x317 (GET済み) puesto y cartas en el baúl",
          0x317 in st["p"]["pbin"] and all(st["p"]["trunk"].get(str(x)) for x in good) and saved)
    c = FakeConn(); handle(c, 0x8401, 0, req)
    check("0x8401 ya recogido: s32 1", c.out and c.out[0][1][:4] == struct.pack(">i", 1))
    st["p"]["pbin"] = {0x316}
    big = bytes([11]) + struct.pack(">11H", *([good[0]] * 11))
    c = FakeConn(); handle(c, 0x8401, 0, big)
    check("0x8401 más cartas que el regalo (10): s32 1", c.out and c.out[0][1][:4] == struct.pack(">i", 1))
    check("0x8401 lista corta o > 80: error", startup_present_result(b"\x02\x00\x01")[0] == 1
          and startup_present_result(bytes([81]) + bytes(162))[0] == 1)
    # 0x4700
    class FakeHub:
        def __init__(self, ss): self.lock, self.sessions = threading.RLock(), ss
    class S:
        def __init__(self, c): self.lobby = c
    a, b = FakeConn(), FakeConn(5741)
    NS["HUB"] = FakeHub({"x": S(a), "y": S(b), "z": S(None)})
    n = maintenance_notice(600)
    check("0x4700 a las 2 sesiones con s32 600", n == 2 and a.out == [(0x4700, struct.pack(">i", 600))]
          and b.out == a.out)
    NS["HUB"] = None
    p1, p2, p3 = FakeConn(5732), FakeConn(5745), FakeConn(5760)
    NS.update({"LOBBY_PORT": 5732, "ROOM_PORT_BASE": 5740, "ROOM_KINDS": tuple(range(11)),
               "PEER_CONNS": {("1.1.1.1", 5732): p1, ("1.1.1.1", 5745): p2, ("1.1.1.1", 5760): p3}})
    n = maintenance_notice(-5)
    check("0x4700 sin HUB: lobby y salas, no el duelo; negativo = 0", n == 2 and p1.out == [(0x4700, bytes(4))]
          and p2.out and not p3.out)
    # tamaños contra protocol.json (los de formato fijo)
    P = json.load(open(os.path.join(EMU, "..", "..", "_original", "ghidra", "protocol.json")))
    for mid, size in ((0x42a2, 4), (0x8302, 2), (0x4700, 4)):
        fx, _u, _c = _fmt_size(_flat(P["recv"]["0x%04x" % mid]["reads"]))
        check(f"0x{mid:04x} mide {fx} B en protocol.json", fx == size)
    check("NO_APLICA: ninguno tiene manejador", not (set(NO_APLICA) & set(HANDLERS)))
    RESETAUTOWAIT, USBDPAUTH_REFUSE, TPPRESENT, STARTUP_PRESENT, ns = old
    NS.clear(); NS.update(ns)
    for e in errors:
        print("MAL", e)
    print(f"prueba mensajes que faltaban (0x42a0, 0x8203, 0x8301, 0x8401, 0x4700): {len(errors)} fallos")
    return not errors


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok1 = selftest()
        ok2 = selftest_ola1()
        ok3 = selftest_espectador()
        ok4 = selftest_mensajes()
        sys.exit(0 if ok1 and ok2 and ok3 and ok4 else 1)
    print(__doc__)
