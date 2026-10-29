"""Persistencia por jugador para el emulador de YO2 (cliente Ver.080912) — propuesta del agente 5 + lote L5 (ola 1).

run.py la carga con load_research("persistencia") y usa make_handlers(); duelfin la carga como PS.
Todo sale del análisis estático de yo2.exe (Ghidra, ghidra_a5) y está documentado en NOTES.md y en
research/audit/perfil_progreso.md (lote L5 del BACKLOG).

    python proposal.py          # autoprueba: tamaños contra protocol.json + ida y vuelta + reglas del lote L5

Formatos (resumen; detalle y confianza en NOTES.md):
  - Baúl (kaban): 0xcb8 (3256) bytes, byte i = nº de copias de la carta id i+1 (u8, el cliente satura en 0xff).
    GETTRUNK 0x3100 (u8 hueco) -> 0x3101 (vacío, reinicia) + 7 x 0x3102 (6 x 512 B + 1 x 184 B) + 0x3103 (vacío).
    OJO (B05): aunque el array tiene 3256 huecos, data/mini/list_card.txt tiene 3256 filas con la 0 = «0000.png»,
    así que los ids con dibujo son 1..3255. Un id 3256 (o uno sin PNG) hace que la ventana de baúl/mazo pida
    «data/MINI/» con nombre vacío y el cliente se cierra (CTextureManager.cpp:L784). Ver card_ok().
  - PBIN: 128 B = 1024 bits (bit n = byte n>>3, máscara 1<<(n&7)); significado en data/common/yo2pbinmap.dat.
  - NPCBIN: 512 B = 128 entradas de 4 B (b0..b3) indexadas por id de NPC.
    b0 = victorias, b1 = DERROTAS, b2 = empates, b3 = nivel de progreso (B06: CWindowResultNpc 0x8c30c1-0x8c32d4 y
    texto 5079 «Victorias%d/Derrotas%d» con Σb0/Σb1 en 0x8a01c8; antes se apuntaba la derrota en b2).
    GETPBIN 0x3114 (u8 hueco) -> 0x3114 (128 B) + 0x4234 (512 B), en ese orden (0x4234 cierra la espera).
  - PSELECTRESULT 0x4101 = 133 B, PLAYERDATAUPDATE 0x4140 = 125 B, PLAYERLIST 0x3012 = s32 + 76 B por jugador.
  - Mazos: SOLO locales (profiles/<nombre>/deck.ydc, 125 x u16 LE: main[80] side[15] extra[30]); el servidor
    solo los ve en el duelo: 0x6600 (u32 main, u32 side, u32 extra), 0x6601 (u32 n + n x u16), 0x6603 (ídem extra).

Lote L5 (2026-10-06), lo que exporta para los demás lotes:
  - my_slot(st) / my_pid(st) / my_name(st), y en make_handlers() las mismas sin argumentos (para run.py y lobby).
  - npc_result(e, result): reparto correcto de NPCBIN (b0 victoria, b1 derrota, b2 empate).
  - pending_*: cola de premios del perfil (p["pending"]) que se entrega con 0x5205 en ITEMGET 1/3.
  - on_npc_duel_end(p, npc, result, turns, lp_left, stats=, surrender=) -> datos de 0x4140 (como antes) y deja en
    p["last_npc_end"]["present"] el valor de 0x4237; npc_duel_result() devuelve todo junto (pdu, present, pkg).
  - chain_fields(nid, ids_enviados) -> (umbral, previa) para npc_entry de run.py (B08), con las réplicas
    visible()/stars() del cliente.
  - card_ok(cid) / sanitize_cards(): ids de carta que el cliente puede pintar (B05).
  - title_id_from_bit(bit): 0x4174 trae el BIT, el registro guarda el ID (B10).
  - migrate_state(st): migración idempotente (esquema SCHEMA); la lanza migrar_state.py con el servidor parado.
"""
import json
import os
import re
import struct
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.normpath(os.path.join(HERE, "..", ".."))            # tools/yo2emu
ROOT = os.path.normpath(os.path.join(EMU, "..", ".."))           # DuelCards
if EMU not in sys.path:
    sys.path.insert(0, EMU)
import proto  # noqa: E402

ORIG = os.path.join(ROOT, "_original")
GAME = os.path.join(ORIG, "yo2_2008-11")
try:                                   # YO2_JUEGO_DATOS (yo2srv/rutas.py); sin ella, _original/extracted/merge2008/data
    from yo2srv import rutas as _rutas  # noqa: E402
except ImportError:                    # servidor viejo sin yo2srv/rutas.py (cargas en caliente sobre commits antiguos): la ruta de siempre
    _rutas = None
DATA2008 = _rutas.juego() if _rutas else os.path.join(ORIG, "extracted", "merge2008", "data")
DATA_OLD = os.path.join(ORIG, "extracted", "merge", "data")
PBINMAP = os.path.join(DATA2008, "common", "yo2pbinmap.dat")
LIST_CARD = os.path.join(DATA2008, "mini", "list_card.txt")

MAX_CARD = 0xcb8              # tamaño del array del baúl: ids 1..0xcb8 (FUN_006c1f30: id < 0xcb9 && id != 0)
CARD_ID_MAX = 3255            # B05: list_card.txt = 3256 filas, fila 0 = 0000.png -> ids con nombre 1..3255
TRUNK_CHUNKS = (0x200,) * 6 + (0xb8,)   # FUN_0068fa90: trozo 6 = 0xb8 B en +0x1155; NUNCA más de 7 (desborda)
PBIN_SIZE = 128
NPCBIN_SIZE = 512
NPCBIN_MAX = NPCBIN_SIZE // 4  # 128 entradas; FUN_00ad6d80 no comprueba el índice
START_MP = 10000
# Puntos DUELPASS (DP, cuenta+0x1248; NOTES §13 «Coste del duelo»). Oficial: 10 DP gratis al empezar (C, web) y 1 DP por
# duelo. START_DPP = saldo de un personaje NUEVO (blank_profile). LEGACY_DPP = lo que vale un perfil guardado SIN «dpp»
# (hasta el 07-10 todos empezaban con 1000): así los personajes que ya existen nunca cambian de saldo.
try:
    START_DPP = max(0, int(os.environ.get("YO2_DP_INICIO") or 10))
except ValueError:
    START_DPP = 10
LEGACY_DPP = 1000
# 12-10: nivel (rec.lv) de un personaje NUEVO. 1 = como el original (Konami: todos empiezan en LV 1 y el cliente solo deja
# entrar a las Salas de Principiantes con LV 0-1, research/lobby/NOTES.md). Otro valor lo elige el anfitrión en el lanzador
# («Starting level» -> YO2_START_LEVEL) para que los nuevos puedan entrar en todas las salas. 1-99 (el tope del panel).
try:
    START_LV = min(99, max(1, int(os.environ.get("YO2_START_LEVEL") or 1)))
except ValueError:
    START_LV = 1
# 12-10: país (bandera sobre el nombre, LIST1 0x4212) de un personaje NUEVO según el idioma del cliente (byte 0 de 0x4100
# PSELECT, el mismo b1 de FUN_00a54b80: 0 japonés, 1 inglés, 2 alemán, 3 italiano, 4 francés, 5 español, 7 coreano). En el
# oficial el país era el de la CUENTA de Konami (se elegía al registrarse en la web; no se puede reproducir): esto es lo más
# parecido. Los 7 códigos están en la tabla de banderas del exe (0xd0b788, 146 países: JP 141, US 146, DE 16, IT 22, FR 15,
# ES 37, KR 142; flag_2215_01.png / flag_4228_0x.png de data/avatar/flag). El perfil nuevo nace con country "" (= «aún sin
# decidir»); el primer 0x4100 lo fija. Idioma desconocido o COUNTRY_BY_LANG apagado: se queda "" y block_player usa
# DEFAULT_COUNTRY (YO2_COUNTRY, «ES»), como antes. Los perfiles de antes (sin clave «country») no cambian nunca.
COUNTRY_BY_LANG = os.environ.get("YO2_COUNTRY_BY_LANG", "1") not in ("", "0")
LANG_COUNTRY = {0: "JP", 1: "US", 2: "DE", 3: "IT", 4: "FR", 5: "ES", 7: "KR"}
# Baúl inicial de un jugador nuevo: "deck" = las cartas de su deck.ydc local (así el mazo nunca queda inválido),
# "all3" = 3 copias de todo (comportamiento actual de run.py), "empty" = vacío.
TRUNK_START = os.environ.get("YO2_TRUNK_START", "deck")

# Versión del esquema de state.json (st["schema"]). 2 = NPCBIN con b1 = derrotas y b2 = empates (B06) y título
# guardado como ID (B10). La sube migrate_state(); el código nuevo ya escribe con el convenio 2.
SCHEMA = 2

# Bits de PBIN conocidos (yo2pbinmap.dat; columnas: bit, bit%256, por_defecto, tipo, subtipo, texto, nombre, serie)
PB_BEGINNER_RIGHTS = (0x302, 0x304, 0x306)   # BeginnersDeck001-003 GET権利; +1 = GET済み (FUN_006bfcc0)

LOCK = threading.RLock()      # cerrojo por defecto; run.py debe pasar el suyo (STATE_LOCK) a make_handlers(lock=)


def log(msg):
    """run.py la sustituye por la suya (PP.log = log). Por defecto, a stderr."""
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------- campos del registro de jugador (0x1a8 B)
# Registro = cuenta(DAT_012711ac+0xc68) + 0x124c + hueco*0x1a8; getter FUN_006a5bc0 (ECX = cuenta+0x121c).
# (nombre, tipo, offset en el registro o None, significado). Orden = orden de lectura del cliente.
REC = {   # offset -> (clave en state.json, etiqueta del cliente / uso)
    0x0c: ("player_id", "id del jugador (CAvatarNetSupport); 0 = ninguno"),
    0xa4: ("lv", "LV / Nivel (texto 9000/9001)"),
    0xa8: ("score", "Puntuación (9002); s32 en PLAYERLIST"),
    0xac: ("mp", "Millas = MP (9005): saldo de la tienda"),
    0xb0: ("unk_b0", "sin lecturas en el cliente"),
    0xb2: ("wins", "Victorias (9010); win% = b2/(b2+ba)"),
    0xb4: ("unk_b4", "sin lecturas en el cliente"),
    0xb6: ("unk_b6", "sin lecturas en el cliente"),
    0xb8: ("unk_b8", "sin lecturas en el cliente (va en PLAYERLIST)"),
    0xba: ("losses", "Derrotas (9011)"),
    0xbc: ("match_wins", "Nº de partidas ganadas (9026)"),
    0xbe: ("match_losses", "Nº de partidas perdidas (9027)"),
    0xc0: ("single_wins", "Nº de «single» ganadas (9028)"),
    0xc2: ("single_losses", "Nº de «single» perdidas (9029)"),
    0xc4: ("duels", "Nº total de duelos (9017); media de LP = d8/c4"),
    0xc6: ("unk_c6", "sin lecturas en el cliente"),
    0xc8: ("duels_won", "Nº de duelos ganados (9019)"),
    0xca: ("duels_lost", "Nº de duelos perdidos (9020)"),
    0xcc: ("duels_drawn", "Nº de duelos empatados (9021)"),
    0xce: ("duels_void", "Nº de partidas sin terminar (9022)"),
    0xd0: ("turns", "Nº total de turnos (9018)"),
    0xd4: ("surrender_me", "veces que me he rendido (9023)"),
    0xd6: ("surrender_opp", "veces que se rindió el rival (9024)"),
    0xd8: ("lp_total", "suma de LP final; media = d8/c4 (9025)"),
    0xdc: ("side_swaps", "cartas cambiadas con el Side Deck (9030)"),
    0xe0: ("tribute1", "invocaciones con 1 tributo (9031)"),
    0xe2: ("tribute2", "con 2 tributos (9032)"),
    0xe4: ("tribute3", "con 3 tributos (9033)"),
    0xe6: ("fusions", "invocaciones por fusión (9034)"),
    0xe8: ("ss_grave", "especiales desde el cementerio (9035)"),
    0xea: ("ss_total", "invocaciones especiales (9036)"),
    0xec: ("trap_me", "trampas activadas por mí (9037)"),
    0xf0: ("trap_opp", "trampas del rival (9038)"),
    0xf4: ("spell_me", "mágicas activadas por mí (9039)"),
    0xf8: ("spell_opp", "mágicas del rival (9040)"),
    0xfc: ("effect_me", "efectos de monstruo míos (9041)"),
    0x100: ("effect_opp", "efectos del rival (9042)"),
    0x104: ("comeback_wins", "victorias remontando (9043)"),
    0x106: ("comeback_losses", "derrotas remontadas (9044)"),
    0x108: ("unk_108", "sin lecturas en el cliente"),
    0x10c: ("unk_10c", "s32, sin lecturas en el cliente"),
    0x110: ("unk_110", "sin lecturas en el cliente"),
    0x112: ("title", "título (称号) mostrado: ID de la tabla 0xc98d50 (FUN_0068fc50(id) -> texto); 0x4174 trae el BIT"),
    0x116: ("unk_116", "sin lecturas en el cliente"),
    0x11c: ("recovery_flags", "bits 0xc0000000 = «tienes Recovery Packs sin recoger» (MessageBox + 0x4901). Dejar 0"),
    0x120: ("champ_pending", "!=0 -> cartel de premio CHAMPIONSHIP + 0x4915; solo con claim del motor de eventos"),
    0x124: ("tp", "Puntos del Torneo / TP (9003)"),
    0x128: ("tp_rank", "puesto en el ranking TP (1-1000 = rankeado); solo en PLAYERLIST"),
}
KEY2OFF = {v[0]: k for k, v in REC.items()}

# Especificaciones de los mensajes: (clave, tipo). Claves especiales: result, dpp, npc_index, npc_b0..b3.
PSELECT_SPEC = ([("result", "s32"), ("player_id", "u32"), ("dpp", "u32"), ("unk_116", "u16"),
                 ("recovery_flags", "u32"), ("champ_pending", "u32"), ("tp", "u32"), ("lv", "u8"),
                 ("score", "u32"), ("mp", "u32")]
                + [(REC[o][0], "u16") for o in (0xb0, 0xb2, 0xb4, 0xb6, 0xb8, 0xba, 0xbc, 0xbe, 0xc0, 0xc2,
                                                0xc4, 0xc8, 0xca, 0xcc, 0xce)]
                + [("turns", "u32"), ("surrender_me", "u16"), ("surrender_opp", "u16"), ("lp_total", "u32"),
                   ("side_swaps", "u32")]
                + [(REC[o][0], "u16") for o in (0xe0, 0xe2, 0xe4, 0xe6, 0xe8, 0xea)]
                + [(REC[o][0], "u32") for o in (0xec, 0xf0, 0xf4, 0xf8, 0xfc, 0x100)]
                + [("comeback_wins", "u16"), ("comeback_losses", "u16"), ("unk_108", "u16"), ("unk_10c", "s32"),
                   ("unk_110", "u16"), ("title", "u16"), ("unk_c6", "u16")])

PDU_SPEC = ([("lv", "u8"), ("score", "u32"), ("mp", "u32")]
            + [(REC[o][0], "u16") for o in (0xb0, 0xb2, 0xb4, 0xb6, 0xb8)]
            + [("dpp", "u32")]
            + [(REC[o][0], "u16") for o in (0xba, 0xbc, 0xbe, 0xc0, 0xc2, 0xc4, 0xc8, 0xca, 0xcc, 0xce)]
            + [("turns", "u32"), ("surrender_me", "u16"), ("surrender_opp", "u16"), ("lp_total", "u32"),
               ("side_swaps", "u32")]
            + [(REC[o][0], "u16") for o in (0xe0, 0xe2, 0xe4, 0xe6, 0xe8, 0xea)]
            + [(REC[o][0], "u32") for o in (0xec, 0xf0, 0xf4, 0xf8, 0xfc, 0x100)]
            + [("comeback_wins", "u16"), ("comeback_losses", "u16"), ("unk_108", "u16"), ("unk_10c", "s32"),
               ("unk_110", "u16"), ("title", "u16"), ("unk_116", "u16"), ("tp", "u32"),
               ("npc_index", "u32"), ("npc_b0", "u8"), ("npc_b1", "u8"), ("npc_b2", "u8"), ("npc_b3", "u8"),
               ("unk_c6", "u16")])

PLAYERLIST_ENTRY_SPEC = [("slot", "u8"), ("name", "s16"), ("lv", "u8"), ("score", "s32"), ("tp", "u32"),
                         ("tp_rank", "u32"), ("title", "u16"), ("mp", "u32"), ("unk_b0", "u16"), ("wins", "u16"),
                         ("losses", "u16"), ("unk_b8", "u16"), ("avatar_a", "b16"), ("avatar_b", "b16")]

SIZES = {"u8": 1, "u16": 2, "u32": 4, "s32": 4, "s16": 16, "b16": 16}
REC_TYPES = {k: t for k, t in PSELECT_SPEC + PDU_SPEC}
_TYPE_MAX = {"u8": 0xff, "u16": 0xffff, "u32": 0xffffffff, "s32": 0x7fffffff}


def encode(spec, values):
    """Serializa según la especificación (big-endian, como proto.W). Faltan -> 0."""
    w = proto.W()
    for key, t in spec:
        v = values.get(key, 0)
        if t == "u8":
            w.u8(int(v) & 0xff)
        elif t == "u16":
            w.u16(int(v) & 0xffff)
        elif t in ("u32", "s32"):
            w.u32(int(v))
        elif t == "s16":
            w.s(str(v or ""), 16)
        elif t == "b16":
            raw = bytes.fromhex(v) if isinstance(v, str) else bytes(v or b"")
            w.b += raw[:16].ljust(16, bytes(1))
    return w.bytes()


def decode(spec, data):
    """Inverso de encode (para pruebas y para leer capturas)."""
    out, p = {}, 0
    for key, t in spec:
        n = SIZES[t]
        raw = data[p:p + n]; p += n
        if t == "u8":
            out[key] = raw[0]
        elif t == "u16":
            out[key] = struct.unpack(">H", raw)[0]
        elif t == "u32":
            out[key] = struct.unpack(">I", raw)[0]
        elif t == "s32":
            out[key] = struct.unpack(">i", raw)[0]
        elif t == "s16":
            out[key] = raw.split(bytes(1))[0].decode("latin1")
        else:
            out[key] = raw.hex()
    return out


def spec_size(spec):
    return sum(SIZES[t] for _, t in spec)


def rec_inc(r, key, n=1):
    """Suma n a rec[key] sin pasarse del máximo de su tipo en el mensaje (u16 = 65535…): encode() enmascara, y
    un contador que pasa de 65535 a 0 sería peor que uno que se queda en el tope."""
    top = _TYPE_MAX.get(REC_TYPES.get(key, "u32"), 0xffffffff)
    r[key] = max(0, min(int(r.get(key, 0)) + int(n), top))
    return r[key]


# ---------------------------------------------------------------- cartas válidas (B05)
_VALID = None


def valid_card_ids():
    """Ids que el cliente sabe pintar: valid_cards.json (cards_yo2.csv con data/mini/<konami>.png), recortado a
    1..CARD_ID_MAX. Si falta el fichero: 1..CARD_ID_MAX."""
    global _VALID
    if _VALID is None:
        ids = None
        try:
            with open(os.path.join(HERE, "valid_cards.json"), encoding="utf-8") as f:
                ids = frozenset(int(x) for x in json.load(f) if 1 <= int(x) <= CARD_ID_MAX)
        except (OSError, ValueError, TypeError):
            ids = None
        _VALID = ids or frozenset(range(1, CARD_ID_MAX + 1))
    return _VALID


def card_ok(cid):
    """True si el id se puede guardar en el baúl o mandar al cliente sin que la ventana de baúl/mazo pida una
    textura «data/MINI/» vacía o inexistente (B05)."""
    try:
        return int(cid) in valid_card_ids()
    except (TypeError, ValueError):
        return False


def sanitize_cards(cards, src=""):
    """Filtra una lista de ids; registra los que quita y de dónde venían (para saber qué mensaje los metió)."""
    good, bad = [], []
    for c in cards:
        (good if card_ok(c) else bad).append(c)
    if bad:
        log(f"[persistencia] B05: ids de carta descartados ({src or '?'}): {sorted(set(bad))[:20]}"
            + (" …" if len(set(bad)) > 20 else ""))
    return good


# ---------------------------------------------------------------- state.json
def blank_profile(name=""):
    rec = {v[0]: 0 for v in REC.values()}
    rec.update(lv=START_LV, mp=START_MP, dpp=START_DPP)
    return {"name": name, "rec": rec, "trunk": {}, "pbin": default_pbin_bits(), "npcbin": {},
            "avatar": {}, "decks": {}, "claims": [], "pending": [], "country": ""}


def country_from_lang(p, lang):
    """12-10: si el perfil es nuevo (country == "") y se sabe el idioma del cliente, le pone el país de ese idioma. Devuelve
    True si cambió algo (hay que guardar). Ver COUNTRY_BY_LANG."""
    if not COUNTRY_BY_LANG or p is None or p.get("country") != "":
        return False
    c = LANG_COUNTRY.get(lang)
    if not c:
        return False
    p["country"] = c
    return True


def profile(st, slot, create=True):
    """Perfil del hueco (0-3). Migra el esquema viejo: players{slot: nombre} + wallet{mp, dpp} global.
    §14: los DP de inicio solo los recibe el PRIMER personaje de la cuenta (dp_start_once)."""
    slot = str(int(slot))
    profs = st.setdefault("profiles", {})
    p = profs.get(slot)
    if p is None and create:
        name = st.get("players", {}).get(slot, "")
        p = profs[slot] = blank_profile(name)
        dp_start_once(st, slot, p, new=True)
        w = st.get("wallet")
        if w and slot == "0":                        # el saldo viejo era de Player1 (hueco 0)
            p["rec"]["mp"] = int(w.get("mp", START_MP)); p["rec"]["dpp"] = int(w.get("dpp", LEGACY_DPP))
        seed_trunk(p)
    if p is not None and not p.get("name"):
        p["name"] = st.get("players", {}).get(slot, "")
    if p is not None:
        if DP_START_PEND in p and p.get("name"):    # §14: perfil creado sin personaje que ya lo tiene
            dp_start_once(st, slot, p)
        p.setdefault("rec", {}).setdefault("dpp", LEGACY_DPP)       # perfil viejo sin saldo: el de antes
    return p


# ---------------------------------------------------------------- DP de inicio una vez por CUENTA (07-10, NOTES §14)
# Konami: «10 DP given for free to every ACCOUNT when they are first created» (C, web). El saldo sigue siendo del
# personaje (cliente: +0x1248 de cada registro, PSELECTRESULT). Regla: el primer personaje que se crea en una cuenta
# se queda con START_DPP (o lo de admin_regalos.json); los siguientes empiezan con 0. Una cuenta que ya tiene (o tuvo:
# profiles_borrados) personajes no recibe nada nuevo. La marca va en la CUENTA (DP_START_KEY; run.py la tiene en
# ACCT_KEYS, como «dp_pases»): borrar y volver a crear personaje no da otros 10. YO2_DP_INICIO_CUENTA=0 = por personaje.
DP_START_KEY = "dp_inicio"            # cuenta: 1 = ya dio los DP de inicio
DP_START_PEND = "dp_inicio_pend"      # perfil creado en un hueco SIN personaje (sin nombre): DP de inicio guardados aquí
DP_START_ONCE = os.environ.get("YO2_DP_INICIO_CUENTA", "1") != "0"


def dp_start_had(st, slot):
    """¿Ya dio la cuenta de st sus DP de inicio? Marca, personajes archivados u otro perfil con personaje."""
    if st.get(DP_START_KEY) or st.get("profiles_borrados"):
        return True
    for s, q in (st.get("profiles") or {}).items():
        if s != str(slot) and isinstance(q, dict) and DP_START_PEND not in q:
            return True
    return False


def dp_start_once(st, slot, p, new=False):
    """Al crear el perfil del hueco slot (new) o al darle nombre a uno creado sin personaje: deja sus DP de inicio
    solo si es el primero de la cuenta y marca la cuenta. Modifica st y p (guardar después)."""
    r = p.setdefault("rec", {})
    if not DP_START_ONCE:
        if DP_START_PEND in p:
            r["dpp"] = int(r.get("dpp", 0) or 0) + int(p.pop(DP_START_PEND) or 0)
        return
    if not p.get("name"):                            # hueco sin personaje (p. ej. un 0x4140 antes de crear): 0 por ahora
        if new:
            p[DP_START_PEND] = int(r.get("dpp", 0) or 0)
            r["dpp"] = 0
        return
    pend = p.pop(DP_START_PEND, None)
    start = int(pend or 0) if pend is not None else int(r.get("dpp", 0) or 0)
    had = dp_start_had(st, slot)
    if pend is not None:
        r["dpp"] = int(r.get("dpp", 0) or 0) + (0 if had else start)
    elif new and had:
        r["dpp"] = 0
    st[DP_START_KEY] = 1


def profile_created(st, slot):
    """(perfil, recién creado). Para guardar SOLO si hubo que crearlo (B24: no reescribir state.json en cada
    lectura del saldo)."""
    existed = str(int(slot)) in st.get("profiles", {})
    return profile(st, slot), not existed


def active_slot(st):
    try:
        return int(st.get("active_slot", 0)) & 3
    except (TypeError, ValueError):
        return 0


def set_active_slot(st, slot):
    st["active_slot"] = int(slot) & 3


def my_slot(st):
    """Hueco del personaje activo (0-3) = el último PSELECT/GETTRUNK (B12)."""
    return active_slot(st)


def my_pid(st):
    """Id de jugador = hueco + 1: el mismo que manda PSELECTRESULT (h_pselect)."""
    return my_slot(st) + 1


def my_name(st):
    s = my_slot(st)
    p = st.get("profiles", {}).get(str(s)) or {}
    return p.get("name") or st.get("players", {}).get(str(s), "") or "Duelista"


def seed_trunk(p, mode=None):
    """Baúl de un perfil nuevo (ver TRUNK_START). Solo cartas con dibujo (B05)."""
    mode = mode or TRUNK_START
    if mode == "all3":
        # valid_cards.json: las de cards_yo2.csv con data/mini/<konami>.png. Fichas, ids sin dibujo y el 3256
        # (sin fila en list_card.txt) hacen que el editor de mazos pete al llegar a ellos (CTextureManager).
        p["trunk"] = {str(i): 3 for i in sorted(valid_card_ids())}
    elif mode == "deck":
        deck = read_ydc(os.path.join(GAME, "profiles", p.get("name") or "-", "deck.ydc"))
        if deck:
            trunk_add(p, deck["main"] + deck["side"] + deck["extra"], src="deck.ydc")


# ---------------------------------------------------------------- baúl (kaban)
def trunk_bytes(p):
    """0xcb8 B. Nunca lleva un id que el cliente no pueda pintar (B05), aunque esté en state.json."""
    arr = bytearray(MAX_CARD)
    for k, n in p.get("trunk", {}).items():
        try:
            cid, n = int(k), int(n)
        except (TypeError, ValueError):
            continue
        if card_ok(cid) and n > 0:
            arr[cid - 1] = min(n, 0xff)
    return bytes(arr)


def trunk_add(p, cards, n=1, src=""):
    """Suma copias (lista de ids). Descarta (y registra) los ids que el cliente no sabe pintar (B05); satura en
    255 como el cliente."""
    t = p.setdefault("trunk", {})
    for cid in sanitize_cards(list(cards), src or "trunk_add"):
        k = str(int(cid))
        t[k] = min(int(t.get(k, 0)) + n, 0xff)


def trunk_remove(p, cards):
    t = p.setdefault("trunk", {})
    for cid in cards:
        k = str(int(cid))
        if t.get(k, 0) > 0:
            t[k] -= 1
            if not t[k]:
                del t[k]


def sanitize_trunk(p):
    """Quita del baúl los ids no válidos (B05). Devuelve los quitados."""
    t = p.setdefault("trunk", {})
    bad = []
    for k in list(t):
        try:
            ok = card_ok(int(k)) and int(t[k]) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            bad.append(k); del t[k]
    return bad


def trunk_messages(p):
    """[(id, datos)] para contestar a GETTRUNK 0x3100: inicio + 7 trozos + fin. Exactamente 7 trozos."""
    raw = trunk_bytes(p)
    msgs, pos = [(0x3101, b"")], 0
    for n in TRUNK_CHUNKS:
        msgs.append((0x3102, raw[pos:pos + n])); pos += n
    msgs.append((0x3103, b""))
    return msgs


# ---------------------------------------------------------------- PBIN / NPCBIN
_PBINMAP = None


def pbinmap():
    """{bit: (por_defecto, tipo, subtipo, id_texto, nombre, serie)} de yo2pbinmap.dat (Shift-JIS)."""
    global _PBINMAP
    if _PBINMAP is None:
        _PBINMAP = {}
        try:
            text = open(PBINMAP, "rb").read().decode("cp932", "replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            f = line.split(",")
            if len(f) >= 8 and f[0].strip().isdigit():
                try:
                    _PBINMAP[int(f[0])] = (int(f[2]), int(f[3]), int(f[4]), int(f[5]), f[6], int(f[7]))
                except ValueError:
                    pass
    return _PBINMAP


def pbin_type(bit):
    v = pbinmap().get(int(bit))
    return v[1] if v else 0


def default_pbin_bits():
    """Piezas de avatar que todo jugador tiene (columna «por defecto» = 1): 55 bits, chico 0-184, chica 256-440.
    Coinciden con las que el editor de avatar marca en su PBIN temporal (FUN_008b07d0)."""
    return sorted(b for b, v in pbinmap().items() if v[0] == 1)


def npc_prize_bits():
    """{npc_id: {"mid_right", "mid_got", "comp_right", "comp_got"}: bit} (filas NPCID_xxx de yo2pbinmap.dat)."""
    out = {}
    rx = re.compile("NPCID_(\\d+)\\s+(途中景品|コンプ景品)\\s*GET(権利|済み)")
    for b, v in pbinmap().items():
        m = rx.search(v[4])
        if m:
            k = ("mid_" if m.group(2) == "途中景品" else "comp_") + ("right" if m.group(3) == "権利" else "got")
            out.setdefault(int(m.group(1)), {})[k] = b
    return out


def pbin_bytes(p):
    arr = bytearray(PBIN_SIZE)
    for b in p.get("pbin", []):
        if 0 <= b < PBIN_SIZE * 8:
            arr[b >> 3] |= 1 << (b & 7)
    return bytes(arr)


def pbin_test(p, bit):
    return int(bit) in set(p.get("pbin", []))


def pbin_set(p, bit, on=True):
    s = set(p.get("pbin", []))
    if on:
        s.add(int(bit))
    else:
        s.discard(int(bit))
    p["pbin"] = sorted(s)


def npcbin_bytes(p):
    """128 x 4 B; entrada = NPCBIN[id] (b0..b3 en orden de bytes)."""
    arr = bytearray(NPCBIN_SIZE)
    for k, v in p.get("npcbin", {}).items():
        i = int(k)
        if 0 <= i < NPCBIN_MAX:
            arr[i * 4:i * 4 + 4] = bytes(int(x) & 0xff for x in (list(v) + [0, 0, 0, 0])[:4])
    return bytes(arr)


def npcbin_get(p, npc):
    return (list(p.get("npcbin", {}).get(str(int(npc)), [])) + [0, 0, 0, 0])[:4]


def npcbin_set(p, npc, entry):
    if 0 < int(npc) < NPCBIN_MAX:
        p.setdefault("npcbin", {})[str(int(npc))] = [int(x) & 0xff for x in (list(entry) + [0, 0, 0, 0])[:4]]


# NPCBIN[npc] = b0..b3. CWindowResultNpc (0x8c30c1-0x8c32d4, setters 0x8ccad0/0x8ccb20/0x8ccb70) pinta
# WIN = b0, LOSE = b1, DRAW = b2; la ficha de la NPC (FUN_0089e430, texto 5079) usa Σb0/Σb1.  C (B06)
NPCBIN_WIN, NPCBIN_LOSE, NPCBIN_DRAW, NPCBIN_PROGRESS = 0, 1, 2, 3


def npc_result(e, result):
    """Reparto de un resultado en la entrada NPCBIN e = [b0, b1, b2, b3]: "win" -> b0, "lose" -> b1 (derrotas),
    otro -> b2 (empates). No toca b3 (progreso). Devuelve una lista nueva."""
    e = (list(e) + [0, 0, 0, 0])[:4]
    k = {"win": NPCBIN_WIN, "lose": NPCBIN_LOSE}.get(result, NPCBIN_DRAW)
    e[k] = min(int(e[k]) + 1, 255)
    return e


# ---------------------------------------------------------------- títulos (B10, B74)
_TITLES = None


def titles():
    """[(id, bit, nombre)] de la tabla 0xc98d50 del exe (titles.json; FUN_0068fe10 busca por el bit y devuelve
    el id). Ids 0-3 y 0xf102-0xf16b; bits 152-255 y 408-511 (no se solapan)."""
    global _TITLES
    if _TITLES is None:
        try:
            with open(os.path.join(HERE, "titles.json"), encoding="utf-8") as f:
                _TITLES = [tuple(r) for r in json.load(f)["titles"]]
        except (OSError, ValueError, KeyError):
            _TITLES = [(0, 224, "Duelista principiante")]
    return _TITLES


def title_id_from_bit(bit):
    """Lo que hace el cliente al recibir 0x4175 = 0 (FUN_00ac7bf0 caso 2): +0x112 = FUN_0068fe10(bit).
    None si el bit no es de un título."""
    for tid, b, _ in titles():
        if b == int(bit):
            return tid
    return None


def title_bits():
    return {b for _, b, _ in titles()}


def title_ids():
    return {t for t, _, _ in titles()}


def fix_title_value(v):
    """Valor guardado en rec.title -> ID. Los perfiles viejos guardaban el BIT (224, 230…): se convierte; un id
    válido se deja; cualquier otra cosa -> 0 (Duelista principiante)."""
    v = int(v or 0)
    if v in title_ids():
        return v
    tid = title_id_from_bit(v)
    return tid if tid is not None else 0


# ---------------------------------------------------------------- mensajes del jugador
def rec_values(p, player_id=None):
    v = dict(p.get("rec", {}))
    v.setdefault("dpp", LEGACY_DPP)
    v["player_id"] = player_id if player_id is not None else v.get("player_id", 0)
    v["recovery_flags"] = 0          # ver REC[0x11c]: con bits altos sale el aviso de Recovery Packs
    # El motor de eventos instala un 0x4915 -> 0x4803 válido; solo se muestra el cartel si existe un claim real.
    v["champ_pending"] = 1 if any(bool(x.get("championship")) for x in p.get("event_rewards", ())) else 0
    v["title"] = fix_title_value(v.get("title", 0))   # B10: aunque state.json no esté migrado
    return v


def pselect_result(p, player_id, result=0):
    """PSELECTRESULT 0x4101 (FUN_006b7ef0), 133 B. Con result != 0 el cliente no lee nada más."""
    v = rec_values(p, player_id); v["result"] = result
    return encode(PSELECT_SPEC, v)


def player_data_update(p, npc=None):
    """PLAYERDATAUPDATE 0x4140 (FUN_006bc270), 125 B. npc = id para copiar NPCBIN[id] del perfil al cliente
    (FUN_00ad6d80); None/0 = no tocar NPCBIN. Ojo: reescribe LV, MP, DPP, estadísticas, título y TP."""
    v = rec_values(p)
    if npc and 0 < int(npc) < NPCBIN_MAX:
        b = npcbin_get(p, npc)
        v.update(npc_index=int(npc), npc_b0=b[0], npc_b1=b[1], npc_b2=b[2], npc_b3=b[3])
    return encode(PDU_SPEC, v)


def player_entry(slot, p, tp_rank=None):
    """Entrada de PLAYERLIST 0x3012 (FUN_0068f520), 76 B. tp_rank = puesto TP calculado (None = el de rec, 0)."""
    v = dict(p.get("rec", {}))
    v["title"] = fix_title_value(v.get("title", 0))
    v.update(slot=int(slot), name=p.get("name", ""),
             avatar_a=p.get("avatar", {}).get("a", ""), avatar_b=p.get("avatar", {}).get("b", ""))
    if tp_rank is not None:
        v["tp_rank"] = int(tp_rank)
    return encode(PLAYERLIST_ENTRY_SPEC, v)


# tp_rank (tienda2, 07-10): puesto en el ranking TP, reg+0x128, SOLO por PLAYERLIST. El cliente lo usa en dos sitios:  C
#  - yo2TpWindow.cpp (FUN_009884e0): 1..1000 -> texto 5289 «¡Ahora eres el Número %d en el Ranking TP!»; 0 o > 1000 ->
#    nada de puesto (si PBIN 0x240 está puesto, 5288 «¡Has pasado la marca %d de TP Points!» con 100).
#  - escena de login (FUN_00850d90 casos 0x13-0x16): 1..1000 abre el premio TP en CADA entrada: 0x8301
#    REQTPPRESENTINFO -> 0x8302 (carta) -> 0x8303 -> 0x8304, y al acabar pone su copia local a 0. Con TPPRESENT apagado
#    (lobby) el 0x8302 automático lleva la carta 0. Cuándo lo mandaba el oficial (siempre o solo con premio
#    pendiente) = D -> apagado. Encender con YO2_TP_RANK=1 o PP.TP_RANK = True, a la vez que el premio TP del lobby.
# Puesto = orden por rec.tp (desc.) y, a igual TP, por victorias (rec.wins, desc.) entre TODOS los personajes
# guardados (dueño en la raíz + state["accounts"]); empate total = mismo puesto. TP 0 o puesto > 1000 -> 0.  P
TP_RANK = os.environ.get("YO2_TP_RANK", "") == "1"
TP_RANK_MAX = 1000
RAW_STATE = None   # run.py pone aquí load_state_raw (todas las cuentas); sin él, el st que llegue


def _int0(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def tp_ranks(raw):
    """{(cuenta, hueco): puesto 1..TP_RANK_MAX} de los personajes con TP > 0 (clave de cuenta del dueño =
    raw["owner_acct"] o "")."""
    rows = []

    def add(acct, profs, players):
        if not isinstance(profs, dict):
            return
        for slot, p in profs.items():
            if not isinstance(p, dict) or (isinstance(players, dict) and players and str(slot) not in players):
                continue
            r = p.get("rec") or {}
            tp = _int0(r.get("tp"))
            if tp > 0:
                rows.append((tp, _int0(r.get("wins")), acct, str(slot)))
    add(raw.get("_acct") or raw.get("owner_acct") or "", raw.get("profiles"), raw.get("players"))   # _acct: vista
    for acct, a in (raw.get("accounts") or {}).items():
        if isinstance(a, dict):
            add(acct, a.get("profiles"), a.get("players"))
    rows.sort(key=lambda x: (-x[0], -x[1], x[2], x[3]))
    out, prev, rank = {}, None, 0
    for i, (tp, wins, acct, slot) in enumerate(rows):
        if (tp, wins) != prev:
            rank, prev = i + 1, (tp, wins)
        if rank > TP_RANK_MAX:
            break
        out[(acct, slot)] = rank
    return out


def playerlist(st, raw=None):
    """0x3012 completo: s32 0 + una entrada por hueco ocupado (máx. 4). Con TP_RANK, el puesto real (raw = estado con
    todas las cuentas; si no, RAW_STATE() o el propio st)."""
    ranks, acct = None, ""
    if TP_RANK:
        try:
            if raw is None:
                raw = RAW_STATE() if callable(RAW_STATE) else st
            ranks = tp_ranks(raw)
            acct = st.get("_acct") or raw.get("owner_acct") or ""
        except Exception:
            ranks = {}
    w = proto.W().u32(0).bytes()
    for slot in sorted(st.get("players", {}), key=int)[:4]:
        w += player_entry(slot, profile(st, slot), None if ranks is None else ranks.get((acct, str(slot)), 0))
    return w


# ---------------------------------------------------------------- mazos
def read_ydc(path):
    """deck.ydc / <id>.ydc: 125 x u16 LE = main[0:80] side[80:95] extra[95:125]; 0 = hueco."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    v = struct.unpack("<%dH" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    v = list(v) + [0] * max(0, 125 - len(v))
    return {"main": [x for x in v[0:80] if x], "side": [x for x in v[80:95] if x], "extra": [x for x in v[95:125] if x]}


def pack_file(pid, ext):
    for base in (DATA2008, DATA_OLD):
        f = os.path.join(base, "cardpack", "%d%s" % (pid, ext))
        if os.path.exists(f):
            return f
    return None


def structure_deck_cards(deck_id):
    """Cartas de <id>.ydc (canje SD_GET / decks de la tienda): todas, main+side+extra."""
    f = pack_file(deck_id, ".ydc")
    d = read_ydc(f) if f else None
    return (d["main"] + d["side"] + d["extra"]) if d else []


class DuelDeck:
    """Junta el mazo que manda el cliente al empezar el duelo (FUN_00692840). Solo envía 0x6600, 0x6601 y 0x6603:
    el side (0x6602) nunca se manda. Formatos: 0x6600 = u32 main, u32 side, u32 extra; 0x6601/0x6603 = u32 n +
    n x u16 (n repetido)."""

    def __init__(self):
        self.counts = None; self.main = []; self.extra = []

    def feed(self, msg_id, data):
        """Devuelve True cuando el mazo está completo (tras 0x6603)."""
        if msg_id == 0x6600 and len(data) >= 12:
            self.counts = struct.unpack(">III", data[:12]); self.main = []; self.extra = []
        elif msg_id in (0x6601, 0x6603) and len(data) >= 4:
            n = struct.unpack(">I", data[:4])[0]
            cards = list(struct.unpack(">%dH" % n, data[4:4 + 2 * n])) if len(data) >= 4 + 2 * n else []
            if msg_id == 0x6601:
                self.main = cards
            else:
                self.extra = cards
                return True
        return False

    def as_dict(self):
        return {"main": self.main, "extra": self.extra, "side_count": self.counts[1] if self.counts else 0}

    def bad_ids(self):
        """B05: ids del mazo que el cliente no sabría pintar (el mazo es local; solo se puede avisar)."""
        return sorted({c for c in self.main + self.extra if not card_ok(c)})

    def check_against(self, p):
        """Cartas del mazo que el baúl no cubre (informativo: el cliente monta el mazo él solo)."""
        need = {}
        for c in self.main + self.extra:
            need[c] = need.get(c, 0) + 1
        have = p.get("trunk", {})
        return {c: n - have.get(str(c), 0) for c, n in need.items() if n > have.get(str(c), 0)}


def save_duel_deck(c, msg_id, data, load_state, save_state, lk):
    """0x6600/0x6601/0x6603 de la conexión de duelo c -> decks.last_duel del personaje de c (informativo: «missing» y
    «bad_ids»). 08-10: el mazo se junta POR CONEXIÓN (antes un DuelDeck único para todo el servidor: con dos duelos a
    la vez se mezclaban el principal de uno y el extra de otro) y va al perfil de la cuenta + hueco de la SESIÓN de c
    (antes active_slot = el último PSELECT de la cuenta)."""
    dd = getattr(c, "duel_deck", None)
    if dd is None:
        dd = DuelDeck()
        try:
            c.duel_deck = dd
        except AttributeError:
            pass
    if not dd.feed(msg_id, data):
        return
    bad = dd.bad_ids()
    if bad:
        log(f"[persistencia] B05: el mazo del duelo lleva ids sin dibujo/fuera de list_card.txt: {bad}")
    s = getattr(c, "sess", None)
    with lk:
        st = load_state()
        slot = getattr(s, "slot", None)
        p = profile(st, active_slot(st) if slot is None else int(slot) & 3)
        p.setdefault("decks", {})["last_duel"] = dd.as_dict()
        missing = dd.check_against(p)
        if missing:
            p["decks"]["last_duel"]["missing"] = {str(k): v for k, v in missing.items()}
        if bad:
            p["decks"]["last_duel"]["bad_ids"] = bad
        save_state(st)


# ---------------------------------------------------------------- cola de premios (B09): p["pending"]
# Cada paquete = {"bits": [...], "cards": [...], "via": "npc" | "any", "src": texto}. Los BITS se aplican al perfil
# al encolar (así nada se pierde si el cliente nunca lo pide: el siguiente GETPBIN ya los lleva); la cola solo
# decide qué 0x5205 se manda y cuándo (la ventana «¡Has ganado un nuevo objeto!», CCardPackCheckScene 0x1f-0x24).
# Las CARTAS también se suman al baúl del servidor al encolar: el cliente las suma en local al recibir el 0x5205,
# así que los dos quedan iguales. «npc» = regalo tras el duelo (lo pide ITEMGET(3,0) en la charla, diálogos 8/10/12
# tras 0x4237); «any» = lo demás (títulos, 挑戦権), que sale en el siguiente ITEMGET(1,0) al entrar en un mapa.
PENDING = "pending"
VIA_NPC, VIA_ANY = "npc", "any"
ITEMGET_MAX = 480             # bits + cartas por 0x5205: 4 + 2·n <= 1024 B (FUN_00689ea0 corta por encima)


def pending_list(p):
    return p.setdefault(PENDING, [])


def pending_add(p, bits=(), cards=(), via=VIA_ANY, src=""):
    """Aplica y encola un paquete. Devuelve el paquete o None si no hay nada nuevo que enseñar."""
    bits = [int(b) for b in bits if 0 <= int(b) < PBIN_SIZE * 8]
    cards = sanitize_cards([int(c) for c in cards], "premio " + (src or via))
    for b in bits:
        pbin_set(p, b)
    trunk_add(p, cards, src="premio " + (src or via))
    if not bits and not cards:
        return None
    pkg = {"bits": bits, "cards": cards, "via": via, "src": src}
    pending_list(p).append(pkg)
    return pkg


def pending_pop(p, via=VIA_ANY):
    """Saca el primer paquete de esa vía (None si no hay)."""
    q = pending_list(p)
    for i, pkg in enumerate(q):
        if pkg.get("via", VIA_ANY) == via:
            return q.pop(i)
    return None


def pending_take_all(p, via=VIA_ANY):
    """Saca y junta TODOS los paquetes de esa vía en uno (o None)."""
    q = pending_list(p)
    take = [x for x in q if x.get("via", VIA_ANY) == via]
    if not take:
        return None
    p[PENDING] = [x for x in q if x.get("via", VIA_ANY) != via]
    bits, cards = [], []
    for x in take:
        bits += [b for b in x.get("bits", []) if b not in bits]
        cards += list(x.get("cards", []))
    return {"bits": bits, "cards": cards, "via": via, "src": "+".join(x.get("src", "") for x in take)}


def pending_clear(p, via=None):
    """Vacía la cola (o solo una vía). Los bits/cartas ya están en el perfil: no se pierde nada."""
    if via is None:
        p[PENDING] = []
    else:
        p[PENDING] = [x for x in pending_list(p) if x.get("via", VIA_ANY) != via]


def itemget_result(pbin_bits, cards):
    """0x5205 (FUN_006c2b30): u16 n + n×u16 bits de PBIN que se ponen, u16 m + m×u16 cartas (+1 en el baúl)."""
    pbin_bits = list(pbin_bits)[:ITEMGET_MAX]
    cards = list(cards)[:max(0, ITEMGET_MAX - len(pbin_bits))]
    w = proto.W().u16(len(pbin_bits))
    for b in pbin_bits:
        w.u16(b)
    w.u16(len(cards))
    for c in cards:
        w.u16(c)
    return w.bytes()


def apply_itemget_result(p, pbin_bits, cards):
    for b in pbin_bits:
        pbin_set(p, b)
    trunk_add(p, cards, src="0x5205")


# ---------------------------------------------------------------- cadenas de NPC (B08)
# NPCINFOLIST (FUN_006c30f0): +0x34 umbral y +0x40 previa. FUN_009ff2d0 (quién aparece): una NPC con previa solo
# sale si NPCBIN[previa].b3 >= umbral(previa); una NPC se va si umbral <= b3 y alguien la tiene como previa.
# FUN_009d7bf0 (estrellas): profundidad en la cadena (+1 si b3 >= umbral); medidor b3*5/umbral + 1.  C
# b3 empieza en 1 (primera charla, ITEMGET tipo 4) y sube 1 por victoria -> con umbral 4, 3 victorias por nivel (D).
CHAIN_THRESHOLD = max(2, _env_int("YO2_NPC_THRESHOLD", 4))
CHAIN_BASES = tuple(range(51, 94, 3))        # 51 Yami Yugi … 93 Jaden B: niveles base, base+1, base+2
FAMOUS_BASE = {51: "Yami Yugi", 54: "Seto Kaiba", 57: "Tea", 60: "Jaden", 63: "Chazz", 66: "Alexis",
               69: "Joey", 72: "Jesse", 75: "Yami Marik", 78: "Hell Kaiser", 81: "Yami Bakura", 84: "Aster",
               87: "Mai", 90: "Jim", 93: "Jaden B"}


def chain_base(nid):
    """Id base de la cadena de una NPC famosa (51-95), o None (NPC normal 1-50 o Ghoul 96-98)."""
    nid = int(nid)
    if 51 <= nid <= 95:
        b = 51 + (nid - 51) // 3 * 3
        return b if b in CHAIN_BASES else None
    return None


def chain_fields(nid, sent_ids=None):
    """(umbral, previa) para el registro de NPCINFOLIST de la NPC nid. sent_ids = ids que van en la MISMA lista:
    la previa solo se pone si está en ella (si no, FUN_009ff2d0/FUN_009d7bf0 leen un puntero nulo y el cliente se
    cae). Sin ciclos (previa = nid-1 < nid). Umbral siempre > 0 (con 0 y sucesora, el nivel 1 se iría sin jugar)."""
    nid = int(nid)
    base = chain_base(nid)
    prev = nid - 1 if base is not None and nid > base else 0
    if prev and sent_ids is not None and prev not in set(int(x) for x in sent_ids):
        prev = 0
    return CHAIN_THRESHOLD, prev


def chain_records(famous_spots, levels=3):
    """famous_spots = {id_base: punto_del_mapa}. Devuelve [(id, punto, umbral, previa)] con los niveles 1..levels
    de cada famosa en el MISMO punto (run.py, npc_entry, lote L11)."""
    out = []
    for base, spot in famous_spots.items():
        for lvl in range(levels):
            out.append((base + lvl, spot, CHAIN_THRESHOLD, base + lvl - 1 if lvl else 0))
    return out


def visible(records, npcbin, area_of_spot=lambda s: 0, area=0):
    """Réplica de FUN_009ff2d0: ids que el mapa crea en `area`. npcbin = {id(int): [b0..b3]}."""
    by_id = {r[0]: r for r in records}
    b3 = lambda i: (npcbin.get(i) or [0, 0, 0, 0])[3]
    shown = []
    for nid, spot, thr, prev in records:
        if area_of_spot(spot) != area:
            continue
        if prev:
            pr = by_id[prev]                       # KeyError aquí = acceso nulo en el cliente
            if b3(prev) < pr[2]:
                continue
        if thr <= b3(nid) and any(r[3] == nid for r in records):
            continue                               # completada y con sucesora: se va
        shown.append(nid)
    return shown


def stars(records, npcbin, nid):
    """Réplica de FUN_009d7bf0: (estrellas, fotograma del medidor)."""
    by_id = {r[0]: r for r in records}
    depth, prev, seen = 0, by_id[nid][3], {nid}
    while prev:
        if prev in seen:                           # en el cliente: bucle infinito
            raise ValueError("ciclo en la cadena de NPC")
        seen.add(prev)
        depth += 1
        prev = by_id[prev][3]
    thr = by_id[nid][2]
    b3 = (npcbin.get(nid) or [0, 0, 0, 0])[3]
    if b3 < thr:
        return depth, int(b3 * 5.0 / thr + 1.0)
    return depth + 1, 0


# ---------------------------------------------------------------- premios por NPC (B09) y 挑戦権 (B07)
# Títulos por personaje (descripciones del juego, textos 76xx): amistad / derrota / amistad eterna
TITLE_BITS = {
    "Yami Yugi": (160, 175, 422), "Seto Kaiba": (161, 408, 423), "Tea": (162, 409, 424), "Joey": (163, 410, 425),
    "Yami Bakura": (164, 411, 426), "Yami Marik": (165, 412, 427), "Jaden": (166, 413, 428),
    "Chazz": (167, 414, 429), "Alexis": (168, 415, 430), "Jesse": (169, 416, 431), "Hell Kaiser": (170, 417, 480),
    "Aster": (171, 418, 481), "Mai": (172, 419, 482), "Jim": (173, 420, 483), "Jaden B": (174, 421, 484)}
PRESENT_CLOTHES = {"Yami Yugi": (121, None), "Seto Kaiba": (122, None), "Joey": (123, None),
                   "Yami Marik": (124, 375), "Jaden": (135, None), "Chazz": (136, None), "Jesse": (137, None),
                   "Hell Kaiser": (138, None), "Aster": (139, None), "Tea": (None, 374), "Alexis": (None, 390),
                   "Yami Bakura": (84, 342)}   # (bit chico, bit chica)
TITLE_FRIEND_ALL = 159        # «AMIGO DE TODOS»: con todos los títulos de amistad de las famosas del mapa
# 挑戦権 (FUN_00a56060 vía FUN_00a55cd0): sin el bit, Kaiba/Alexis/Yami Marik dicen «¿Quién rayos eres?» y no hay
# duelo (FUN_0089f790 -> estado 6).  C
CHALLENGE_RIGHT = {54: 592, 66: 593, 75: 594}
# Modo de concesión (B07): "free" = se dan en el primer ITEMGET(1,0) (entrar en un mapa), con su ventana de
# «¡Ahora puedes retar a … a un Duelo!» (textos 3463/3464/3473); "earn" = al completar el nivel 1 de otra cadena
# (regla PROPUESTA, DUDOSO: la del original no está en el cliente); "off" = nunca.
CHALLENGE_MODE = os.environ.get("YO2_CHALLENGE", "free")
CHALLENGE_EARN = {592: 51, 593: 60, 594: 81}   # bit -> base cuya cadena de nivel 1 hay que completar (D)
# Cartas en los premios de NPC: el original no se sabe (D). Desactivado: solo bits (receta, título, ropa).
PRIZE_CARDS = os.environ.get("YO2_PRIZE_CARDS", "0") == "1"


def player_sex(p):
    """"F" si la pieza 0 del avatar (sexo) es 1, si no "M"."""
    a = (p.get("avatar") or {}).get("a") or (p.get("avatar") or {}).get("b") or ""
    return "F" if a[:2] == "01" else "M"


def npc_rewards(p, nid, old_b3, new_b3):
    """Bits de premio al pasar NPCBIN[nid].b3 de old a new (regla PROPUESTA, DUDOSO: la decidía el servidor de
    Konami): mitad del umbral -> «途中景品 GET済み»; umbral -> «コンプ景品 GET済み» (receta 100NN) y, en el 3.er
    nivel de una famosa, su título de amistad y su ropa. Devuelve (bits, cartas)."""
    pb = npc_prize_bits().get(int(nid), {})
    mid = 1 + (CHAIN_THRESHOLD - 1) // 2
    bits, cards = [], []
    if old_b3 < mid <= new_b3 and "mid_got" in pb:
        bits.append(pb["mid_got"])
    if old_b3 < CHAIN_THRESHOLD <= new_b3:
        if "comp_got" in pb:
            bits.append(pb["comp_got"])
            if PRIZE_CARDS:
                cards += structure_deck_cards(10000 + int(nid))   # receta cardpack/100NN.ydc (D)
        base = chain_base(nid)
        name = FAMOUS_BASE.get(base) if base is not None else None
        if name and int(nid) == base + 2:
            if name in TITLE_BITS:
                bits.append(TITLE_BITS[name][0])
            boy, girl = PRESENT_CLOTHES.get(name, (None, None))
            b = girl if player_sex(p) == "F" else boy
            if b is not None:
                bits.append(b)
    return [b for b in bits if not pbin_test(p, b)], cards


def challenge_rights_due(p):
    """Bits 挑戦権 que tocan y aún no tiene, según CHALLENGE_MODE."""
    if CHALLENGE_MODE == "free":
        return [b for b in sorted(CHALLENGE_RIGHT.values()) if not pbin_test(p, b)]
    if CHALLENGE_MODE == "earn":
        return [b for b, base in sorted(CHALLENGE_EARN.items())
                if not pbin_test(p, b) and npcbin_get(p, base)[3] >= CHAIN_THRESHOLD]
    return []


# ---------------------------------------------------------------- títulos por logros (B74)
# Umbrales PROPUESTOS (DUDOSO: el cliente solo tiene la descripción, titles_es.txt). (bit, métrica, umbral).
TITLE_RULES = [
    (250, "duels_won", 10), (251, "duels_won", 30), (252, "duels_won", 100), (253, "duels_won", 300),
    (254, "duels_won", 1000),                                         # DUELISTA DE BRONCE … DEFINITIVO
    (235, "_tributes", 10), (236, "_tributes", 50), (237, "_tributes", 200),       # INVOCADOR
    (238, "fusions", 10), (239, "fusions", 50), (240, "fusions", 200),             # FUSIONADOR
    (241, "trap_me", 20), (242, "trap_me", 100), (243, "trap_me", 500),            # ARTISTA TRAMPA
    (244, "spell_me", 20), (245, "spell_me", 100), (246, "spell_me", 500),         # LANZADOR DE CONJUROS
    (247, "comeback_wins", 1), (248, "comeback_wins", 5), (249, "comeback_wins", 20),   # NUNCA SE RINDE
    (154, "_collection_pct", 25), (155, "_collection_pct", 50), (156, "_collection_pct", 90),   # COLECCIONISTA
]
TITLE_SURRENDER_ONE_TURN = 255   # «SE RINDE EN UN TURNO»: rendirse antes del turno 2


def _metric(p, key):
    r = p.get("rec", {})
    if key == "_tributes":
        return int(r.get("tribute1", 0)) + int(r.get("tribute2", 0)) + int(r.get("tribute3", 0))
    if key == "_collection_pct":
        have = sum(1 for k, n in p.get("trunk", {}).items() if str(k).isdigit() and card_ok(int(k)) and n > 0)
        return have * 100 // max(1, len(valid_card_ids()))
    return int(r.get(key, 0))


def check_titles(p, surrender=None, turns=0):
    """Títulos de logros que el perfil ya merece y no tiene. Devuelve la lista de bits (sin aplicar)."""
    tb = title_bits()
    new = [b for b, key, thr in TITLE_RULES if _metric(p, key) >= thr]
    if surrender == "me" and 0 < int(turns or 0) <= 1:
        new.append(TITLE_SURRENDER_ONE_TURN)
    friends = [TITLE_BITS[n][0] for b, n in FAMOUS_BASE.items() if b <= 84 and n in TITLE_BITS]
    if friends and all(pbin_test(p, b) for b in friends):
        new.append(TITLE_FRIEND_ALL)
    out = []
    for b in new:
        if b in tb and not pbin_test(p, b) and b not in out:
            out.append(b)
    return out


# ---------------------------------------------------------------- derechos de decks / premios (B76)
# Mecanismo listo, REGLA DESACTIVADA (decisión del owner, D): YO2_RIGHTS=1 la enciende. Un «GET権利» de deck
# (tipo 5) se canjea en la escena de login con 0x5400 (on_sd_get pone el «済み» y da las cartas).
RIGHTS_ON = os.environ.get("YO2_RIGHTS", "0") == "1"
RIGHT_RULES = [(776, "duels_won", 5), (778, "duels_won", 15), (780, "duels_won", 30)]   # MiddleDeck001-003 (D)


def rights_due(p):
    if not RIGHTS_ON:
        return []
    return [b for b, key, thr in RIGHT_RULES
            if _metric(p, key) >= thr and not pbin_test(p, b) and not pbin_test(p, b + 1)]


# ---------------------------------------------------------------- reglas que el cliente aplica en local
def on_shop_purchase(p, cards):
    """Tras PURCHASE_SHOP_ITEM_RESULT 0x5212: el cliente suma 1 copia por entrada de tipo 0/1 (FUN_006c1f30).
    cards = [(tipo, id)] como devuelve shop_purchase() de run.py. El MP lo descuenta el servidor (rec.mp)."""
    trunk_add(p, [cid for kind, cid in cards if kind in (0, 1)], src="0x5212 tienda")


def on_pack_open(p, cards, st=None):
    """Tras PURCHASE_PACK_OPEN_RESULT 0x4903 con resultado 0 el cliente (FUN_006bfcc0), en local:
    suma las cartas, DPP (de la cuenta, +0x1248) += 150, MP del HUECO 0 (+0x12f8 = registro 0 + 0xac) += 50 sea
    cual sea el hueco activo (B70), y, si no tiene ni el derecho ni el «GET済み», marca los derechos de
    BeginnersDeck001-003 (PBIN 0x302/0x304/0x306). El servidor hace lo mismo para no desincronizarse.
    st = estado completo (para llegar al perfil «0»); sin él, el MP va a p, como antes."""
    trunk_add(p, cards, src="0x4903 DUELPASS")
    rec_inc(p["rec"], "dpp", 150)
    p0 = (st.get("profiles", {}).get("0") if st is not None else None) or p
    rec_inc(p0.setdefault("rec", {}), "mp", 50)
    for b in PB_BEGINNER_RIGHTS:
        if not pbin_test(p, b) and not pbin_test(p, b + 1):
            pbin_set(p, b)


# B70 revisado (07-10): pases DUELPASS por CUENTA (en el oficial, un pase comprado = un sobre; finito).
# 0x3110 «GetDuelPassPurchase» (FUN_0068f3c0, puerto de cuentas, escena de login) -> 0x3111 s32 = pases sin abrir.
# El cliente lo guarda en cuenta+0x1210 (objeto de la cuenta, común a los 4 huecos: por eso el saldo es de la cuenta)
# y al pulsar «Puntos de DUELPASS» (FUN_00a67270) abre la ventana del sobre si es >= 1 (y lo pone a 0 en local) o la
# del código (0x8101, duelpass.py) si es < 1. Con 0x4903 s32 != 0, FUN_006bfcc0 no suma nada (ni DPP, ni MP, ni
# derechos) y la ventana (0xa88391: lee el resultado 0x48 = +0x900) suena el aviso 0x10000003 y saca un cuadro con
# «OK» (texto 4002) en vez de abrir el sobre. C (camino); el valor exacto del error no lo distingue: 1 (D).
# Dónde: state["dp_pases"] de la cuenta (run.py lo tiene en ACCT_KEYS: raíz para el dueño, accounts[clave] el resto).
DP_PASS_KEY = "dp_pases"
DP_PASS_MAX = 9999
PACK_OPEN_NO_PASS = 1


def dp_passes(st):
    """Pases DUELPASS sin abrir de la cuenta de st (0 si no hay)."""
    try:
        n = int(st.get(DP_PASS_KEY, 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(n, DP_PASS_MAX))


def dp_passes_set(st, n):
    n = max(0, min(int(n), DP_PASS_MAX))
    st[DP_PASS_KEY] = n
    return n


def dp_passes_msg(n):
    """Cuerpo de 0x3111 DUELPASSPURCHASERESULT: s32 pases (cuenta+0x1210)."""
    return proto.W().u32(max(0, int(n))).bytes()


def on_sd_get(p, deck_id, right_bit):
    """SD_GET_REQUEST 0x5400 = u16 id del deck + u16 bit de PBIN del derecho (p. ej. 8001, 0x302). El cliente borra
    ese bit antes de pedir y pone bit+1 («GET済み») si el resultado es 0 (escena CCardPackCheckScene). Devuelve las
    cartas para 0x5401 (u32 0 + u16 por carta) y las suma al baúl."""
    cards = sanitize_cards(structure_deck_cards(deck_id), "0x5401 SD_GET %d" % deck_id)
    if right_bit:
        pbin_set(p, right_bit, False)
        pbin_set(p, right_bit + 1, True)
    p.setdefault("claims", []).append(["sd", deck_id, right_bit])
    trunk_add(p, cards, src="0x5401 SD_GET")
    return cards


# DPAUTH 0x8101 (16 B de código) -> 0x8102 (FUN_006c01b0) = s32 resultado, u16 carta, u8 (sin uso visto), u8 DPP,
# u16 deck. Con resultado 0 el cliente: carta +1 si id <= 0xcb7; deck 8999 = cartas del pack ×3, DPP = 1000 y bit
# 0x246; otro deck = sus cartas ×1 y bit «済み» del pack; DPP += u8; MP del hueco 0 += 10 si DPP es 15/20/30;
# derechos BeginnersDeck. Con resultado != 0: texto 4056 (código no válido).  C (formato y efectos)
# Los códigos válidos y lo que dan los decide el owner: dpauth_codes.json en esta carpeta (opcional),
# {"CODIGO": {"card": id, "dpp": n, "deck": id, "bit": bit_del_derecho}}. Sin fichero, todo código es inválido.
DPAUTH_CODES = os.path.join(HERE, "dpauth_codes.json")


def dpauth_codes():
    try:
        with open(DPAUTH_CODES, encoding="utf-8") as f:
            return {str(k).strip().upper(): v for k, v in json.load(f).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def on_dpauth(p, code, st=None):
    """Devuelve los datos de 0x8102 y replica en el perfil lo que hará el cliente. code = str."""
    ent = dpauth_codes().get(str(code).strip().upper())
    if not ent:
        return proto.W().u32(1).u16(0).u8(0).u8(0).u16(0).bytes()
    card = int(ent.get("card", 0)); dpp = int(ent.get("dpp", 0)) & 0xff; deck = int(ent.get("deck", 0)) & 0xffff
    if not card_ok(card) or card > 0xcb7:
        card = 0
    if deck == 8999:                      # especial: el cliente usa su lista local del pack 8999 (no replicable)
        deck = 0
    if card:
        trunk_add(p, [card], src="0x8102 DPAUTH")
    if deck:
        trunk_add(p, structure_deck_cards(deck), src="0x8102 DPAUTH deck %d" % deck)
        if ent.get("bit"):
            pbin_set(p, int(ent["bit"]) + 1)
    rec_inc(p["rec"], "dpp", dpp)
    if dpp in (15, 20, 30):
        p0 = (st.get("profiles", {}).get("0") if st is not None else None) or p
        rec_inc(p0.setdefault("rec", {}), "mp", 10)
    for b in PB_BEGINNER_RIGHTS:
        if not pbin_test(p, b) and not pbin_test(p, b + 1):
            pbin_set(p, b)
    return proto.W().u32(0).u16(card).u8(0).u8(dpp).u16(deck).bytes()


def first_right(p):
    """Derecho de premio que el cliente consume en CCardPackCheckScene caso 0x1c (0x8535d6-0x8536a6), mismo orden:
    0x246 > 0x244 > el bit de tipo 9 más bajo puesto. (0x1f3 lo pone el propio cliente: no se replica.)"""
    s = set(p.get("pbin", []))
    if 0x246 in s:
        return 0x246
    if 0x244 in s:
        return 0x244
    return next((b for b in sorted(s) if b < 0x400 and pbin_type(b) == 9), 0)


def on_itemget(p, kind, param, st=None):
    """ITEMGET 0x5204 = u16 tipo + u32 parámetro -> datos de 0x5205 (FUN_006c2b30): u16 n + n x u16 bits de PBIN
    que se activan, u16 m + m x u16 cartas que entran al baúl.
      - tipo 4 (1.ª charla con la NPC param): el cliente pone NPCBIN[param].b3 = 1 si era 0 (FUN_006cf000).
      - tipo 1 (cada entrada a un mapa, FUN_009fcca0): entrega lo pendiente «any» (títulos, 挑戦権 en modo free).
      - tipo 3 (regalo de la NPC tras 0x4237, o derecho de premio en la escena de login): el último regalo
        «npc»; si no hay, el derecho que el cliente acaba de consumir (se quita y se pone su «済み»); si tampoco,
        lo pendiente «any».
    Nunca lleva cartas que el cliente no haya pedido (0x5205 siempre es respuesta a un 0x5204)."""
    kind = int(kind); param = int(param)
    if kind == 4:
        if 0 < param < NPCBIN_MAX:
            e = npcbin_get(p, param)
            if e[3] == 0:
                e[3] = 1; npcbin_set(p, param, e)
        return itemget_result([], [])
    if kind == 1:
        due = challenge_rights_due(p)
        if due:
            pending_add(p, due, [], VIA_ANY, "挑戦権")
        pkg = pending_take_all(p, VIA_ANY)
        return itemget_result(pkg["bits"], pkg["cards"]) if pkg else itemget_result([], [])
    if kind == 3:
        pkg = pending_pop(p, VIA_NPC)
        if pkg is None:
            r = first_right(p)
            if r:
                pbin_set(p, r, False)                      # el cliente ya lo ha quitado en local
                if pbin_type(r + 1) in (4, 6, 8) or r in (0x244, 0x246):
                    pkg = pending_add(p, [r + 1], [], VIA_NPC, "derecho %d" % r)
                    if pkg is not None:
                        pending_list(p).remove(pkg)
        if pkg is None:
            pkg = pending_take_all(p, VIA_ANY)
        return itemget_result(pkg["bits"], pkg["cards"]) if pkg else itemget_result([], [])
    return itemget_result([], [])


def on_title(p, title):
    """UPDATE_PLAYERTITLE 0x4174 (u32 = BIT de PBIN del título, FUN_00ac7bf0 caso 1) -> 0x4175 s32.
    Con 0 el cliente guarda en +0x112 el ID (FUN_0068fe10(bit)); el servidor guarda lo mismo (B10). Bit
    desconocido -> 0x4175 = 1 y no se toca nada."""
    tid = title_id_from_bit(int(title) & 0xffff)
    if tid is None:
        return proto.W().u32(1).bytes()
    p["rec"]["title"] = tid & 0xffff
    return proto.W().u32(0).bytes()


# ---------------------------------------------------------------- fin de duelo contra NPC
# Knobs de decisiones del owner (B55/B56). Por defecto, el comportamiento de hoy (nada cambia hasta que decida).
NPC_IN_RECORD = os.environ.get("YO2_NPC_IN_RECORD", "1") == "1"   # B56: ¿cuentan en Victorias/Derrotas?
NPC_MP_REWARD = {"win": _env_int("YO2_NPC_MP_WIN", 0), "lose": _env_int("YO2_NPC_MP_LOSE", 0),
                 "draw": _env_int("YO2_NPC_MP_DRAW", 0)}             # B55: Millas por resultado
NPC_DP_COST = _env_int("YO2_NPC_DP_COST", 0)                         # B55 (viejo, déjalo a 0): el coste de verdad es DUEL_DP_COST

# ---------------------------------------------------------------- coste del duelo en DP (07-10, NOTES §13)
# Oficial: «1 DP por 1 duelo» (C, web: Yugipedia/Konami Wiki vía buscador) para NPC y PvP, también al rendirse o
# desconectarse (C para NPC, OF-CPU; P para PvP). Los dos jugadores pagan (P: el servidor comprueba también al rival,
# texto 4557 «Tu oponente no tiene suficientes DUELPASS points», −614). Un match (mejor de 3) paga por DUELO jugado:
# 2 o 3 DP (P: el cliente pide 3 para entrar y el texto 7180 dice «máximo»). El cliente NUNCA resta: lo manda el
# servidor en 0x4140 (campo dpp) al acabar. Cuándo cobraba Konami (al empezar o al acabar) no se sabe (D): aquí al
# acabar, en el mismo 0x4140 del fin. Un duelo que no llega a empezar no cobra; un fallo del servidor (WHY_ABORT) en
# PvP tampoco (nuestro, D).
#   YO2_DP_COSTE=0           apaga el cobro PvP y las comprobaciones (la NPC: NPC_RULES_2008["dp"] = False).
#   YO2_DP_NPC_BLOQUEO=0     no rechaza el reto a una NPC sin DP (el cliente no lo comprueba en ese camino).
#   YO2_DP_SALA_CHECK=0      no comprueba en el servidor los DP de retador y dueño de sala (−612/−614).
DUEL_DP_COST = max(0, _env_int("YO2_DP_COSTE", 1))
DP_NPC_BLOCK = os.environ.get("YO2_DP_NPC_BLOQUEO", "1") != "0"
DP_ROOM_CHECK = os.environ.get("YO2_DP_SALA_CHECK", "1") != "0"
R_NO_DP_ME, R_NO_DP_OPP = -612, -614        # FUN_00884b70: 4556 «No tienes…» / 4557 «Tu oponente no tiene…»  C
DP_SINGLE_STYLE = 3                         # dstyle de sala «duelo simple» (4 = match, 6 = otro)  C (cliente) / P


def dp_of(p):
    """DP guardados del perfil p (un perfil viejo sin «dpp» = LEGACY_DPP)."""
    try:
        return int((p or {}).get("rec", {}).get("dpp", LEGACY_DPP))
    except (TypeError, ValueError):
        return 0


def dp_need(dstyle=DP_SINGLE_STYLE, cost=None):
    """DP mínimos para empezar un duelo en una sala de tipo dstyle, igual que el cliente (FUN_00863ec0, 4 sitios:
    `+0x1248 < ((tipo != 3) ? 3 : 1)`): 1 duelo simple, 3 el match (puede durar 3 duelos). 0 si no se cobra."""
    cost = DUEL_DP_COST if cost is None else max(0, int(cost))
    if not cost:
        return 0
    return cost if int(dstyle or 0) == DP_SINGLE_STYLE else 3 * cost


def dp_charge(p, cost=None):
    """Cobra un duelo al perfil p (se modifica: guardar después). Nunca baja de 0. Devuelve (antes, después)."""
    cost = DUEL_DP_COST if cost is None else max(0, int(cost))
    r = p.setdefault("rec", {})
    before = dp_of(p)
    r["dpp"] = max(0, before - cost)
    return before, r["dpp"]


# ---------------------------------------------------------------- carta tras el PvP (07-10, NOTES §14)
# Web (Konami Wiki/Yugipedia vía buscador, P): en PvP un duelo simple da 1 carta a CADA jugador, gane o pierda, de un
# sobre que elige él; un match (2-3 duelos) da 3. Cliente (C): tras «Siguiente» de CResultScene (FUN_008c4230 ->
# escena 0x19) CCardPackCheckScene abre CWindowPackSelect (FUN_008444c0, carrusel con TODOS los sobres de la lista de
# 0x4800) si esa lista no está vacía; el elegido va en 0x4802 SELECTCARDPACK (u16) y la escena espera 0x4803 (lobby
# +0x3a = 2) y enseña las cartas que traiga (CCardGetResultScene). Es el mismo camino que la carta de NPC (en vivo, log
# 20261007-014127: 0x4800 00 04 -> 0x4802 00 04 -> 0x4803 con 1 carta). Qué sobres ofrecía Konami: D (aquí los de
# 0x4901, 1-10, como la NPC). PRESENT_PACK_OPEN 0x4904/0x4905: emisor sin llamadas (código muerto, lobby §10).
#   YO2_PVP_CARTA=0      no se ofrece carta tras el PvP (0x4800 vacío, como antes).
#   YO2_PVP_SOBRES=a,b-c sobres que se ofrecen (por defecto 1-10).
PVP_CARD_ON = os.environ.get("YO2_PVP_CARTA", "1") != "0"
PVP_CARDS_DUEL = 1                    # duelo simple (P, web)
PVP_CARDS_MATCH = 3                   # match de 2-3 duelos (P, web). El match PvP no existe aún: preparado, sin usar
PVP_CARD_KEY = "pvp_card_due"         # perfil: cartas de PvP pendientes de elegir sobre [{packs, n, t}]
PVP_CARD_DUE_MAX = 5


def _env_packs(name, default):
    """Lista de sobres de una variable «1,2,5-8» (ids 1..0xffff). Vacía o mal escrita = default."""
    out = []
    for part in (os.environ.get(name) or "").replace(" ", "").split(","):
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                out += list(range(a, b + 1))
            elif part:
                out.append(int(part))
        except ValueError:
            return tuple(default)
    out = [x for x in out if 0 < x <= 0xffff]
    return tuple(out[:64]) if out else tuple(default)


PVP_CARD_PACKS = _env_packs("YO2_PVP_SOBRES", range(1, 11))


def pvp_card_entry(match=False, packs=None):
    """Carta(s) de un PvP: {"packs": sobres a elegir, "n": cartas, "t": hora}; None si está apagado o sin sobres."""
    if not PVP_CARD_ON:
        return None
    packs = [int(x) for x in (packs if packs is not None else PVP_CARD_PACKS) if 0 < int(x) <= 0xffff]
    if not packs:
        return None
    import time as _t
    return {"packs": packs, "n": PVP_CARDS_MATCH if match else PVP_CARDS_DUEL, "t": int(_t.time())}


def pvp_card_offer(p, match=False, packs=None):
    """Apunta en el perfil p la carta pendiente de un PvP (guardar después) y la devuelve; sus sobres van en 0x4800."""
    e = pvp_card_entry(match, packs)
    if e is not None:
        due = p.setdefault(PVP_CARD_KEY, [])
        due.append(e)
        del due[:-PVP_CARD_DUE_MAX]
    return e


def pvp_card_take(p, pack):
    """0x4802 con el sobre elegido: quita la carta pendiente MÁS RECIENTE que ofrecía ese sobre y devuelve cuántas
    cartas tocan (1..5); 0 si no hay ninguna (el 0x4802 no es de un PvP)."""
    due = (p or {}).get(PVP_CARD_KEY) or []
    for i in range(len(due) - 1, -1, -1):
        e = due[i] if isinstance(due[i], dict) else {}
        if int(pack) in [int(x) for x in (e.get("packs") or [])]:
            due.pop(i)
            if not due:
                p.pop(PVP_CARD_KEY, None)
            try:
                return max(1, min(5, int(e.get("n", 1))))
            except (TypeError, ValueError):
                return 1
    return 0


def pvp_card_grant(p, e, draw):
    """Sin elector (el jugador ya no está conectado al acabar): n cartas de un sobre al azar de la oferta, directas al
    baúl del perfil. draw(sobre, n) -> ids. Devuelve (sobre, cartas guardadas)."""
    import random as _r
    pack = _r.choice(e["packs"])
    cards = [int(x) for x in list(draw(pack, e["n"]))[:e["n"]]]
    trunk_add(p, cards, src="pvp")
    return pack, cards

# B51: contadores que el lado del duelo (ocg_duel/duelfin) puede pasar en stats (se SUMAN a rec).
STAT_KEYS = ("tribute1", "tribute2", "tribute3", "fusions", "ss_grave", "ss_total", "trap_me", "trap_opp",
             "spell_me", "spell_opp", "effect_me", "effect_opp", "comeback_wins", "comeback_losses", "side_swaps")


# 24-10 (agente MIS, rx/MIS): los contadores de STAT_KEYS que se recogen DURANTE el duelo (research/persistencia/NOTES.md
# «Contadores del perfil»). Etiquetas del cliente (textos 9031-9044): 9031-9033 «veces que se ha ofrecido N monstruos como
# Tributo», 9034 «veces que ha salido un Monstruo de Fusión», 9035 Especiales desde el Cementerio, 9036 Especiales,
# 9037-9042 «veces que he / el oponente ha activado» Trampa / Mágica / efecto de Monstruo de Efecto, 9043-9044
# «victorias / derrotas anunciadas» (come-from-behind), 9030 cartas cambiadas con el Side Deck. Qué cuenta el oficial
# exactamente: D (el cliente solo pinta el número). Apagar todo: YO2_STATS_PVP=0.
STATS_PVP_ON = os.environ.get("YO2_STATS_PVP", "1") not in ("", "0")
# Remontada (9043/9044): el oficial no dice cuánto. Apagado (0) salvo que se ponga un umbral en LP: YO2_STAT_COMEBACK=4000
# cuenta victoria remontada si el ganador llegó a ir perdiendo por >= esos LP (y derrota remontada al perdedor que
# llegó a ir ganando por >= esos LP), medido en cada decisión. D.
STAT_COMEBACK_LP = int(os.environ.get("YO2_STAT_COMEBACK") or 0)
_T_MONSTER, _T_SPELL, _T_TRAP, _T_FUSION = 0x1, 0x2, 0x4, 0x40       # tipos de cards.cdb
_LOC_MZONE, _LOC_GRAVE = 0x4, 0x10                                    # ubicaciones de ocgcore
_R_TRIBUTE = 0x1a        # motivo del MOVE de un tributo de Invocación Normal: REASON_RELEASE | MATERIAL | SUMMON (medido)
_R_SPSUMMON = 0x800


class DuelTally:
    """Cuenta, sobre los eventos de duelcore de UN duelo (jugadores y controladores ABSOLUTOS, 0/1), lo que enseñan las
    estadísticas del perfil. typeof(código) -> tipo de cards.cdb (o 0). Se alimenta con feed_many(eventos) de cada tanda;
    for_player(k) da el dict para apply_duel_stats(stats=) desde el punto de vista del jugador k.
      tributo N:      N MOVE de monstruos del campo al cementerio con motivo 0x1a justo antes de un SUMMONING/SET de monstruo
                      (se cuentan 1, 2 y 3 o más; el que invoca es el controlador del SUMMONING/SET)
      Especiales:     cada SPSUMMONING; del Cementerio si el MOVE que lo trae viene de ahí; de Fusión si es un monstruo
                      del tipo Fusión (aparezca como aparezca)
      activaciones:   cada CHAINING según el tipo de la carta que activa (Trampa / Mágica / Monstruo), para el jugador que
                      activa (triggering_controller): mías en trap_me/spell_me/effect_me y las del rival en *_opp
    """

    def __init__(self, typeof=None, comeback_lp=None):
        self.typeof = typeof or (lambda code: 0)
        self.comeback_lp = STAT_COMEBACK_LP if comeback_lp is None else int(comeback_lp)
        self.n = {k: [0, 0] for k in ("tribute1", "tribute2", "tribute3", "fusions", "ss_grave", "ss_total",
                                      "trap", "spell", "effect", "comeback_wins", "comeback_losses")}
        self.side_swaps = [0, 0]
        self.trib = 0                      # tributos pendientes del próximo SUMMONING/SET
        self.mv = None                     # último MOVE hacia una zona de monstruos (origen del SPSUMMONING siguiente)
        self.lp = [8000, 8000]
        self.behind = [0, 0]               # lo máximo que ha ido perdiendo cada jugador (LP del rival - los suyos)

    def feed_many(self, evs):
        for ev in evs:
            self.feed(ev)

    def _lp(self, lp):
        try:
            self.lp = [int(lp[0]), int(lp[1])]
        except (TypeError, ValueError, IndexError):
            return
        for p in (0, 1):
            self.behind[p] = max(self.behind[p], self.lp[1 - p] - self.lp[p])

    def feed(self, ev):
        kind = ev.get("ev")
        if isinstance(ev.get("lp"), (list, tuple)) and len(ev["lp"]) == 2:
            self._lp(ev["lp"])
        if kind == "end":
            w = ev.get("winner")
            if self.comeback_lp > 0 and w in (0, 1) and self.behind[w] >= self.comeback_lp:
                self.n["comeback_wins"][w] += 1
                self.n["comeback_losses"][1 - w] += 1
            return
        if kind != "msg":
            return
        t = ev.get("type")
        if t == "MOVE":
            fr, to = ev.get("from") or {}, ev.get("to") or {}
            reason = int(ev.get("reason") or 0)
            if fr.get("location") == _LOC_MZONE and reason & _R_TRIBUTE == _R_TRIBUTE:
                self.trib += 1                            # (el tributo va al cementerio o, p. ej. Spirit, a otro sitio)
            if to.get("location") == _LOC_MZONE and reason & _R_SPSUMMON:
                self.mv = ev
        elif t in ("SUMMONING", "SET"):
            if t == "SUMMONING" or ev.get("location") == _LOC_MZONE:
                p = ev.get("controller")
                if self.trib and p in (0, 1):
                    self.n["tribute%d" % min(self.trib, 3)][p] += 1
                self.trib = 0
        elif t == "SPSUMMONING":
            p = ev.get("controller")
            if p in (0, 1):
                self.n["ss_total"][p] += 1
                m, self.mv = self.mv, None
                if m is not None and (m.get("to") or {}).get("sequence") == ev.get("sequence")                         and (m.get("to") or {}).get("controller") == p:
                    if (m.get("from") or {}).get("location") == _LOC_GRAVE:
                        self.n["ss_grave"][p] += 1
                    code = ev.get("code") or m.get("card")
                else:
                    code = ev.get("code")
                if code and int(self.typeof(code) or 0) & _T_FUSION:
                    self.n["fusions"][p] += 1
            self.trib = 0
        elif t == "CHAINING":
            p = ev.get("triggering_controller", ev.get("controller"))
            ty = int(self.typeof(ev.get("code")) or 0) if ev.get("code") else 0
            key = "spell" if ty & _T_SPELL else "trap" if ty & _T_TRAP else "effect" if ty & _T_MONSTER else None
            if key and p in (0, 1):
                self.n[key][p] += 1
            self.trib = 0
        elif t in ("NEW_PHASE", "NEW_TURN"):
            self.trib = 0

    def for_player(self, k):
        """Contadores del jugador k (0/1 absoluto) para apply_duel_stats: solo los distintos de 0."""
        o, n = 1 - k, self.n
        out = {"tribute1": n["tribute1"][k], "tribute2": n["tribute2"][k], "tribute3": n["tribute3"][k],
               "fusions": n["fusions"][k], "ss_grave": n["ss_grave"][k], "ss_total": n["ss_total"][k],
               "trap_me": n["trap"][k], "trap_opp": n["trap"][o], "spell_me": n["spell"][k],
               "spell_opp": n["spell"][o], "effect_me": n["effect"][k], "effect_opp": n["effect"][o],
               "comeback_wins": n["comeback_wins"][k], "comeback_losses": n["comeback_losses"][k],
               "side_swaps": self.side_swaps[k]}
        return {key: v for key, v in out.items() if v}


def apply_duel_stats(p, stats=None, lp_left=0, surrender=None):
    """B51 (lado perfil): suma los contadores del duelo al registro. surrender: "me" | "opp" | None."""
    r = p.setdefault("rec", {})
    for k, v in (stats or {}).items():
        if k in STAT_KEYS:
            try:
                rec_inc(r, k, max(0, int(v)))
            except (TypeError, ValueError):
                pass
    rec_inc(r, "lp_total", max(0, int(lp_left or 0)))
    if surrender == "me":
        rec_inc(r, "surrender_me")
    elif surrender == "opp":
        rec_inc(r, "surrender_opp")


# ---------------------------------------------------------------- Puntuación y LV tras un PvP (rx/XP, 13-10)
# Fuente: guía de un jugador (https://duelevolution.wordpress.com/level-list/, 2008, «trabajo en curso») sobre
# Yu-Gi-Oh! Online: Duel Evolution = la edición occidental de YO2. Confianza P: la tabla puntuación -> LV y las cifras
# de ganar/perder vienen de ahí; lo CIERTO (C) es que el LV lo decide la puntuación y que solo cuenta el PvP (los
# duelos de NPC no tocan LV ni puntuación: regla oficial `npc.no_record`). Los que ganan suben; los que pierden
# ante alguien de menor nivel bajan; hasta 36 duelos se está en el LV 1 (sala de principiantes, wiki YO1/YO2).
# Apagar: YO2_XP=0 (o PP.XP_ON = False y DFM.PS.XP_ON = False). Las cifras son constantes del módulo.
XP_ON = os.environ.get("YO2_XP", "1") != "0"
XP_BEGINNER_DUELS = 36        # duelos jugados antes de poder salir del LV 1
XP_WIN = 40                   # por duelo ganado
XP_WIN_HIGHER = 7             # +7 por duelo ganado a un rival de LV mayor
XP_MATCH_WIN_HIGHER = 1       # +1 por match ganado a un rival de LV mayor
XP_WIN_FAR = 1                # +1 (una vez por duelo suelto o por match) si el rival era 4 LV o más por encima
XP_FAR_GAP = 4
XP_LOSE_LOWER = -16           # perder contra un rival de LV menor (por duelo)
XP_LOSE_SAME = -1             # perder contra el mismo LV (por duelo)
XP_LOSE_HIGHER = -1           # perder (match, o duelo suelto) contra un rival 1-3 LV por encima; con 4 o más: 0
# Puntuación mínima de cada LV a partir del 2 (LV 1 = principiante, no depende de la puntuación). Del 2 al 28 son
# los umbrales de la guía; el 29 y el 30 NO están en ninguna fuente (extrapolados, inalcanzables en la práctica).
XP_LEVEL_FLOORS = (0, 50, 150, 300, 500, 800, 1200, 1700, 2400, 3300, 4500, 6000, 8000, 10500, 13500, 17500, 22500,
                   29500, 40000, 55000, 75000, 105000, 150000, 200000, 260000, 330000, 470000, 650000, 900000)


def xp_level_for_score(score):
    """LV (2..30) que corresponde a una puntuación."""
    lv = 2
    for i, floor in enumerate(XP_LEVEL_FLOORS):
        if int(score) >= floor:
            lv = i + 2
    return lv


def xp_game_delta(my_lv, opp_lv, result, in_match=False, match_result=None, last=True):
    """Puntos de UNA partida (duelo) para el jugador de LV my_lv contra opp_lv. result y match_result: "win" | "lose" |
    "draw" (su punto de vista). En un match (in_match), las bonificaciones/penalizaciones de match solo se cobran en la
    última partida (last) según match_result. Las cifras son las de la guía; ver XP_* arriba."""
    gap = int(opp_lv) - int(my_lv)
    d = 0
    if result == "win":
        d += XP_WIN + (XP_WIN_HIGHER if gap >= 1 else 0)
        if not in_match and gap >= XP_FAR_GAP:
            d += XP_WIN_FAR
    elif result == "lose":
        d += XP_LOSE_LOWER if gap < 0 else XP_LOSE_SAME if gap == 0 else 0
        if not in_match and 1 <= gap < XP_FAR_GAP:
            d += XP_LOSE_HIGHER
    if in_match and last:
        if match_result == "win" and gap >= 1:
            d += XP_MATCH_WIN_HIGHER + (XP_WIN_FAR if gap >= XP_FAR_GAP else 0)
        elif match_result == "lose" and 1 <= gap < XP_FAR_GAP:
            d += XP_LOSE_HIGHER
    return d


def xp_new_level(old_lv, old_score, new_score, duels):
    """LV tras cambiar la puntuación. Hasta XP_BEGINNER_DUELS duelos, el LV 1 se queda. Un LV que el administrador puso
    por encima de lo que dice la puntuación se respeta como suelo hasta que la puntuación lo alcance."""
    old_lv = int(old_lv or 1)
    if old_lv <= 1 and int(duels) < XP_BEGINNER_DUELS:
        return 1
    by_new = xp_level_for_score(new_score)
    if old_lv > xp_level_for_score(old_score):
        return max(by_new, old_lv)
    return by_new


def pvp_xp(p, opp_lv, result, in_match=False, match_result=None, last=True, my_lv=None):
    """Aplica al perfil p (se modifica; guardar después) la puntuación y el LV de una partida PvP. opp_lv = LV del rival
    ANTES del duelo; my_lv = el LV propio antes del duelo (si no, el del perfil). Devuelve {"delta", "score", "lv_old",
    "lv"} o None si XP_ON está apagado. Llamarlo antes de contar el duelo en rec (usa el contador propio
    p["pvp"]["n"], que arranca de rec["duels"])."""
    if not XP_ON:
        return None
    r = p.setdefault("rec", {})
    pv = p.setdefault("pvp", {})
    n_before = int(pv["n"]) if "n" in pv else int(r.get("duels", 0))
    pv["n"] = n_before + 1
    lv_old = int(r.get("lv", 1) or 1)
    s_old = int(r.get("score", 0))
    delta = xp_game_delta(lv_old if my_lv is None else my_lv, opp_lv, result, in_match, match_result, last)
    s_new = max(0, min(s_old + delta, 0xffffffff))
    r["score"] = s_new
    r["lv"] = max(1, min(255, xp_new_level(lv_old, s_old, s_new, pv["n"])))
    return {"delta": delta, "score": s_new, "lv_old": lv_old, "lv": r["lv"]}


def npc_duel_result(p, npc, result, turns=0, lp_left=0, stats=None, surrender=None):
    """Aplica el fin de un duelo contra la NPC npc al perfil p (se modifica: guardar después). result: "win" |
    "lose" | "draw" (punto de vista del jugador). Devuelve {"pdu": datos de 0x4140 con la NPC, "present": valor
    para 0x4237 (0 = no mandar), "pkg": paquete de regalo encolado o None, "titles": bits de títulos nuevos}.
      - NPCBIN: b0 victoria, b1 derrota, b2 empate (B06); b3 +1 por victoria (B08).
      - Premios de la NPC (B09): paquete «npc» -> present = 1 (diálogo 8 «toma» si ganaste; lo pide ITEMGET 3).
      - 挑戦権 en modo earn (B07), títulos por logros (B74) y derechos (B76): paquete «any» (ITEMGET 1)."""
    npc = int(npc or 0)
    r = p.setdefault("rec", {})
    rec_inc(r, "duels")
    rec_inc(r, "turns", max(0, int(turns or 0)))
    apply_duel_stats(p, stats, lp_left, surrender)
    if result == "win":
        rec_inc(r, "duels_won")
        if NPC_IN_RECORD:
            rec_inc(r, "wins")
    elif result == "lose":
        rec_inc(r, "duels_lost")
        if NPC_IN_RECORD:
            rec_inc(r, "losses")
    else:
        result = "draw"
        rec_inc(r, "duels_drawn")
    if NPC_MP_REWARD.get(result):
        rec_inc(r, "mp", NPC_MP_REWARD[result])
    if NPC_DP_COST:
        r["dpp"] = max(0, int(r.get("dpp", 0)) - NPC_DP_COST)
    present, pkg = 0, None
    if 0 < npc < NPCBIN_MAX:
        e = npcbin_get(p, npc)
        old_b3 = e[3]
        e = npc_result(e, result)
        if result == "win":
            e[3] = min(max(e[3], 1) + 1, 255)
        npcbin_set(p, npc, e)
        pending_clear(p, VIA_NPC)                # un regalo viejo que nadie pidió no debe salir con otra NPC
        bits, cards = npc_rewards(p, npc, old_b3, e[3]) if result == "win" else ([], [])
        if bits or cards:
            pkg = pending_add(p, bits, cards, VIA_NPC, "NPC %d b3 %d->%d" % (npc, old_b3, e[3]))
            present = 1 if pkg else 0
    extra = challenge_rights_due(p) if CHALLENGE_MODE == "earn" else []
    tit = check_titles(p, surrender=surrender, turns=turns)
    extra += [b for b in tit + rights_due(p) if b not in extra]
    if extra:
        pending_add(p, extra, [], VIA_ANY, "fin de duelo")
    p["last_npc_end"] = {"npc": npc, "result": result, "present": present}
    return {"pdu": player_data_update(p, npc=npc if 0 < npc < NPCBIN_MAX else None), "present": present,
            "pkg": pkg, "titles": tit}


def on_npc_duel_end(p, npc, result, turns=0, lp_left=0, stats=None, surrender=None):
    """Compatibilidad (duelfin): igual que npc_duel_result pero devuelve solo los datos de 0x4140. El valor de
    0x4237 queda en p["last_npc_end"]["present"] (o take_present(p))."""
    return npc_duel_result(p, npc, result, turns=turns, lp_left=lp_left, stats=stats, surrender=surrender)["pdu"]


def take_present(p):
    """Valor para 0x4237 NPCPRESENTTYPE del último fin de duelo (y lo consume). 0 = no mandar 0x4237."""
    d = p.get("last_npc_end") or {}
    v = int(d.get("present", 0) or 0)
    d["present"] = 0
    return v


# ---------------------------------------------------------------- migración de state.json (la lanza migrar_state.py)
def _npcbin_swap_needed(p):
    """¿Están b1/b2 con el convenio viejo (derrota en b2)? Se decide comparando con las estadísticas del registro:
    Σb1/Σb2 contra duels_lost/duels_drawn. Devuelve (necesario, explicación). Con datos ya corregidos a mano
    (como el state.json del 2026-10-06) dice que NO."""
    nb = p.get("npcbin", {})
    s1 = sum(int((list(v) + [0] * 4)[1]) for v in nb.values())
    s2 = sum(int((list(v) + [0] * 4)[2]) for v in nb.values())
    r = p.get("rec", {})
    lost, drawn = int(r.get("duels_lost", 0)), int(r.get("duels_drawn", 0))
    ok_dist = abs(s1 - lost) + abs(s2 - drawn)
    sw_dist = abs(s2 - lost) + abs(s1 - drawn)
    why = "Σb1=%d Σb2=%d, derrotas=%d empates=%d" % (s1, s2, lost, drawn)
    return sw_dist < ok_dist, why


def migrate_state(st, swap="auto"):
    """Migración idempotente al esquema SCHEMA. Modifica st y devuelve las líneas del informe.
    swap: "auto" (decide por perfil con _npcbin_swap_needed), "si" (intercambia b1<->b2) o "no".
    El intercambio solo se hace si st["schema"] < 2 (así no se repite nunca); lo demás (título, baúl, cola) es
    idempotente por construcción y se comprueba siempre."""
    rep = []
    ver = int(st.get("schema", 1) or 1)
    for slot, p in sorted(st.get("profiles", {}).items()):
        tag = "perfil %s (%s)" % (slot, p.get("name", ""))
        if ver < 2 and p.get("npcbin"):
            need, why = _npcbin_swap_needed(p)
            do = need if swap == "auto" else swap == "si"
            if do:
                for k, v in list(p["npcbin"].items()):
                    v = (list(v) + [0, 0, 0, 0])[:4]
                    v[1], v[2] = v[2], v[1]
                    p["npcbin"][k] = v
                rep.append("%s: NPCBIN b1<->b2 intercambiados (%s)" % (tag, why))
            else:
                rep.append("%s: NPCBIN ya con b1 = derrotas, no se toca (%s)" % (tag, why))
        r = p.setdefault("rec", {})
        old = r.get("title", 0)
        new = fix_title_value(old)
        if new != old:
            r["title"] = new
            rep.append("%s: título %r (bit) -> id 0x%x" % (tag, old, new))
        bad = sanitize_trunk(p)
        if bad:
            rep.append("%s: baúl sin %d ids no válidos (B05): %s" % (tag, len(bad), bad[:20]))
        if PENDING not in p:
            p[PENDING] = []
            rep.append("%s: cola de premios creada" % tag)
    if ver < SCHEMA:
        st["schema"] = SCHEMA
        rep.append("esquema %d -> %d" % (ver, SCHEMA))
    return rep


_SCHEMA_WARNED = []


def warn_schema(st):
    """Una sola vez por proceso: avisa si state.json no está migrado."""
    if not _SCHEMA_WARNED and int(st.get("schema", 1) or 1) < SCHEMA and st.get("profiles"):
        _SCHEMA_WARNED.append(1)
        log("[persistencia] state.json en esquema %s < %d: con el servidor parado, "
            "python research/persistencia/migrar_state.py --aplicar" % (st.get("schema", 1), SCHEMA))


# ---------------------------------------------------------------- manejadores listos para run.py
# Firmas como las de run.py: (c, data) con c.send(id, datos). load/save = load_state/save_state.
def make_handlers(load_state, save_state, shop_purchase=None, lock=None, game_conn=None, pid_of=None):
    """lock = cerrojo común de state.json (STATE_LOCK de run.py, RLock): cada leer-modificar-guardar va dentro.
    game_conn() = conexión viva del servidor de cuentas (5731) o None: tras cada 0x4902 se le manda 0x3111 con los
    pases que le quedan a la cuenta (>= 1: el siguiente clic abre otro sobre; 0: la ventana de código) (B70).
    pid_of(hueco) = pid del jugador (PvP V02: run.py da 1000 + 4·cuenta + hueco, de la sesión del hilo); sin él,
    hueco + 1 como antes. Es el id que el cliente guarda de PSELECTRESULT (FUN_006b7ef0) y busca en la lista del
    bloque (FUN_009be6b0): tiene que ser el mismo en todas partes."""
    lk = lock if lock is not None and hasattr(lock, "__enter__") else LOCK
    rearm = os.environ.get("YO2_DUELPASS_REARM", "1") == "1"

    def h_playerlist(c, data):                      # 0x3010 (5731)
        with lk:
            st = load_state(); created = [s for s in st.get("players", {}) if s not in st.get("profiles", {})]
            body = playerlist(st)
            if created:
                save_state(st)
        c.send(0x3012, body)

    def h_gettrunk(c, data):                        # 0x3100 (5731): u8 hueco
        with lk:
            st = load_state(); slot = data[0] if data else 0
            set_active_slot(st, slot); p = profile(st, slot)
            bad = sanitize_trunk(p)
            if bad:
                log(f"[persistencia] B05: quitados del baúl ids no válidos: {bad[:20]}")
            save_state(st)
            msgs = trunk_messages(p)
        for mid, payload in msgs:
            c.send(mid, payload)

    def h_getpbin(c, data):                         # 0x3114 (5731): u8 hueco -> PBIN + NPCBIN
        with lk:
            st = load_state(); slot = data[0] if data else 0
            p = profile(st, slot)
            pending_clear(p)                         # todo lo encolado ya va dentro del PBIN que se manda ahora
            save_state(st)
            pb, nb = pbin_bytes(p), npcbin_bytes(p)
        c.send(0x3114, pb)
        c.send(0x4234, nb)

    def h_pselect(c, data):                         # 0x4100 (lobby): u8 idioma, u8 hueco, [u32...]
        with lk:
            st = load_state(); slot = data[1] if len(data) > 1 else active_slot(st)
            set_active_slot(st, slot); p = profile(st, slot)
            if data and p.get("name"):
                country_from_lang(p, data[0])         # 12-10: bandera del personaje nuevo por el idioma del cliente
            save_state(st)
            warn_schema(st)
            body = pselect_result(p, player_id=_pid(slot))
        c.lang = data[0] if data else 0
        c.send(0x4101, body)

    def h_shop_bought(c, cards):                    # llamar dentro del 0x5210 de run.py, tras descontar el MP
        with lk:
            st = load_state(); p = profile(st, active_slot(st)); on_shop_purchase(p, cards); save_state(st)

    def h_dp_saldo(c, data):                        # 0x3110 (5731) -> 0x3111 s32 pases de la cuenta (B70)
        with lk:
            n = dp_passes(load_state())
        c.send(0x3111, dp_passes_msg(n))

    def h_pack_open(c, data):                       # 0x4902 -> 0x4903 (u32 0, u32, n x (u16 carta, u8))
        cards = [cid for _, cid in shop_purchase(1, 1, 1)] if shop_purchase else []
        cards = sanitize_cards(cards, "0x4903 DUELPASS")
        with lk:
            st = load_state()
            left = dp_passes(st)
            if left < 1:                             # sin pase: nada (FUN_006bfcc0 con s32 != 0 no suma nada)
                body = None
            else:
                p = profile(st, active_slot(st))
                have = p.get("trunk", {})
                w = proto.W().u32(0).u32(0)
                seen = set()
                for cid in cards:                    # u8 «nueva» = no la tenía (como 0x8102, FUN_006c01b0) (B40)
                    w.u16(cid).u8(1 if have.get(str(cid), 0) == 0 and cid not in seen else 0); seen.add(cid)
                on_pack_open(p, cards, st)
                left = dp_passes_set(st, left - 1)
                save_state(st)
                body = w.bytes()
        if body is None:
            log(f"[persistencia] 0x4902 DUELPASS sin pases en la cuenta: 0x4903 s32 {PACK_OPEN_NO_PASS}, sin sobre")
            c.send(0x4903, proto.W().u32(PACK_OPEN_NO_PASS).u32(0).bytes())
        else:
            log(f"[persistencia] 0x4902 DUELPASS: sobre abierto, quedan {left} pase(s)")
            c.send(0x4903, body)
        if rearm and game_conn is not None:          # B70: el saldo nuevo (>= 1 -> el siguiente clic abre otro sobre)
            try:
                gc = game_conn()
                if gc is not None:
                    gc.send(0x3111, dp_passes_msg(left))
            except Exception as e:
                log(f"[persistencia] 0x3111 tras DUELPASS falló: {e!r}")

    def h_dpauth(c, data):                          # 0x8101: 16 B de código -> 0x8102 (B70)
        code = bytes(data[:16]).split(bytes(1))[0].decode("latin1", "replace")
        with lk:
            st = load_state(); p = profile(st, active_slot(st))
            reply = on_dpauth(p, code, st); save_state(st)
        log(f"[persistencia] DPAUTH {code[:4]!r}… -> {'OK' if reply[:4] == bytes(4) else 'código no válido'}")   # nunca el código entero
        c.send(0x8102, reply)

    def h_sd_get(c, data):                          # 0x5400: u16 deck, u16 bit -> 0x5401
        deck, bit = struct.unpack(">HH", data[:4]) if len(data) >= 4 else (0, 0)
        with lk:
            st = load_state(); p = profile(st, active_slot(st))
            cards = on_sd_get(p, deck, bit); save_state(st)
        w = proto.W().u32(0 if cards else 1)
        for cid in cards:
            w.u16(cid)
        c.send(0x5401, w.bytes())

    def h_itemget(c, data):                         # 0x5204: u16 tipo, u32 parámetro -> 0x5205
        kind, prm = struct.unpack(">HI", data[:6]) if len(data) >= 6 else (0, 0)
        with lk:
            st = load_state(); p = profile(st, active_slot(st))
            reply = on_itemget(p, kind, prm, st); save_state(st)
        if len(reply) > 4:
            log(f"[persistencia] ITEMGET({kind},{prm}) -> 0x5205 {reply.hex()}")
        c.send(0x5205, reply)

    def h_title(c, data):                           # 0x4174: u32 bit del título -> 0x4175
        with lk:
            st = load_state(); p = profile(st, active_slot(st))
            reply = on_title(p, struct.unpack(">I", data[:4])[0] if len(data) >= 4 else 0); save_state(st)
        c.send(0x4175, reply)

    def h_avatar(c, data):                          # 0x4170: u32 estilo, 16 B piezas, u32 -> 0x4171 s32 0
        if len(data) >= 20:
            with lk:
                st = load_state(); p = profile(st, active_slot(st))
                style = struct.unpack(">I", data[:4])[0]
                p.setdefault("avatar", {})["a" if style == 0 else "b"] = data[4:20].hex()
                save_state(st)
        c.send(0x4171, proto.W().u32(0).bytes())

    def h_duel_deck(c, msg_id, data):               # 0x6600/0x6601/0x6603 (puerto de duelo)
        save_duel_deck(c, msg_id, data, load_state, save_state, lk)

    def wallet():                                   # sustitutos de wallet()/set_wallet() de run.py
        with lk:
            st = load_state(); p, created = profile_created(st, active_slot(st))
            if created:                             # B24: solo se guarda si hubo que crear el perfil
                save_state(st)
            r = p["rec"]
            return {"mp": int(r.get("mp", 0)), "dpp": int(r.get("dpp", LEGACY_DPP))}

    def set_wallet(**kv):
        with lk:
            st = load_state(); r = profile(st, active_slot(st))["rec"]
            r.update({k: max(0, int(v)) for k, v in kv.items() if k in ("mp", "dpp")}); save_state(st)
            return {"mp": r["mp"], "dpp": r["dpp"]}

    def pdu():                                      # sustituto de player_data_update() de run.py
        with lk:
            st = load_state(); p, created = profile_created(st, active_slot(st))
            if created:
                save_state(st)
            return player_data_update(p)

    def h_my_slot():
        with lk:
            return my_slot(load_state())

    def _pid(slot):
        return int(pid_of(int(slot) & 3)) if pid_of is not None else (int(slot) & 3) + 1

    def h_my_pid():
        with lk:
            return _pid(my_slot(load_state()))

    def h_my_name():
        with lk:
            return my_name(load_state())

    return dict(h_playerlist=h_playerlist, h_gettrunk=h_gettrunk, h_getpbin=h_getpbin, h_pselect=h_pselect,
                h_shop_bought=h_shop_bought, h_pack_open=h_pack_open, h_dp_saldo=h_dp_saldo, h_dpauth=h_dpauth, h_sd_get=h_sd_get,
                h_itemget=h_itemget, h_title=h_title, h_avatar=h_avatar, h_duel_deck=h_duel_deck, wallet=wallet,
                set_wallet=set_wallet, player_data_update=pdu, my_slot=h_my_slot, my_pid=h_my_pid,
                my_name=h_my_name, lock=lk)


# ---------------------------------------------------------------- autoprueba
def _proto_size(fmt):
    n = 0
    for f in fmt:
        if isinstance(f, dict):
            n += _proto_size(f["ops"])
        elif f in SIZES and f not in ("s16", "b16"):
            n += SIZES[f]
        elif f.startswith("bytes") and f[5:].isdigit():
            n += int(f[5:])
        elif f == "LOOP":
            pass
        else:
            raise ValueError(f)
    return n


def selftest():
    import ast
    ast.parse(open(__file__, encoding="utf-8").read())
    pj = json.load(open(os.path.join(ORIG, "ghidra", "protocol.json"), encoding="utf-8"))["recv"]
    ok = True

    def check(name, got, want):
        nonlocal ok
        flag = "OK " if got == want else "MAL"
        ok &= got == want
        print("  %s %-34s %4d B (protocol.json %d B)" % (flag, name, got, want))

    def expect(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print("  %s %s%s" % ("OK " if good else "MAL", name, "" if good else " -> %r (esperado %r)" % (got, want)))

    print("Tamaños contra protocol.json:")
    check("PSELECTRESULT 0x4101", spec_size(PSELECT_SPEC), _proto_size(pj["0x4101"]["reads"]))
    check("PLAYERDATAUPDATE 0x4140", spec_size(PDU_SPEC), _proto_size(pj["0x4140"]["reads"]))
    r3012 = pj["0x3012"]["reads"]; loop = r3012[r3012.index("LOOP") + 1:]
    check("PLAYERLIST 0x3012 (entrada)", spec_size(PLAYERLIST_ENTRY_SPEC), _proto_size(loop))
    check("GETPBIN 0x3114", PBIN_SIZE, _proto_size(pj["0x3114"]["reads"]))
    check("trunk total (bytes)", sum(TRUNK_CHUNKS), MAX_CARD)
    check("DPAUTHRESULT 0x8102", len(on_dpauth(blank_profile("x"), "NOEXISTE")), _proto_size(pj["0x8102"]["reads"]))
    # tipos campo a campo contra protocol.json (s16/b16 = bytes16)
    for name, spec, key in (("PSELECT", PSELECT_SPEC, "0x4101"), ("PDU", PDU_SPEC, "0x4140")):
        mine = [{"s16": "bytes16", "b16": "bytes16"}.get(t, t) for _, t in spec]
        if mine != pj[key]["reads"]:
            ok = False; print("  MAL tipos de", name)
        else:
            print("  OK  tipos campo a campo de", name, "(%d campos)" % len(mine))
    mine = [{"s16": "bytes16", "b16": "bytes16"}.get(t, t) for _, t in PLAYERLIST_ENTRY_SPEC]
    print("  %s tipos de la entrada de PLAYERLIST" % ("OK " if mine == loop else "MAL")); ok &= mine == loop

    # ida y vuelta (formato)
    p = blank_profile("Prueba"); p["trunk"] = {}
    trunk_add(p, [1, 1, 2, CARD_ID_MAX, 0, MAX_CARD, MAX_CARD + 1], src="prueba")
    msgs = trunk_messages(p)
    raw = b"".join(d for i, d in msgs if i == 0x3102)
    assert [i for i, _ in msgs] == [0x3101] + [0x3102] * 7 + [0x3103]
    assert len(raw) == MAX_CARD and raw[0] == 2 and raw[1] == 1 and raw[CARD_ID_MAX - 1] == 1 and sum(raw) == 4
    pbin_set(p, 0x302); assert pbin_bytes(p)[0x302 >> 3] & (1 << (0x302 & 7))
    npcbin_set(p, 51, [1, 2, 3, 4]); nb = npcbin_bytes(p); assert nb[51 * 4:51 * 4 + 4] == bytes([1, 2, 3, 4])
    p["rec"].update(mp=12345, dpp=678, wins=7, losses=3, lv=5, title=0xf102, tp=99, score=-5)
    ps = pselect_result(p, 1); d = decode(PSELECT_SPEC, ps)
    assert len(ps) == 133 and d["mp"] == 12345 and d["dpp"] == 678 and d["player_id"] == 1 and d["champ_pending"] == 0
    assert ps[31:35] == struct.pack(">I", 12345)       # MP en el offset 31 (igual que run.py)
    assert ps[8:12] == struct.pack(">I", 678) and ps[22:26] == struct.pack(">I", 99)   # DPP y TP
    pdu = player_data_update(p, npc=51); d = decode(PDU_SPEC, pdu)
    assert len(pdu) == 125 and d["npc_index"] == 51 and (d["npc_b0"], d["npc_b3"]) == (1, 4) and d["dpp"] == 678
    e = player_entry(0, p); d = decode(PLAYERLIST_ENTRY_SPEC, e)
    assert len(e) == 76 and d["name"] == "Prueba" and d["score"] == -5
    dd = DuelDeck()
    dd.feed(0x6600, struct.pack(">III", 2, 0, 1)); dd.feed(0x6601, struct.pack(">IHH", 2, 5, 6))
    assert dd.feed(0x6603, struct.pack(">IH", 1, 10)) and dd.main == [5, 6] and dd.extra == [10]
    on_pack_open(p, [3]); assert pbin_test(p, 0x302) and p["rec"]["dpp"] == 828
    on_sd_get(p, 8001, 0x302); assert not pbin_test(p, 0x302) and pbin_test(p, 0x303)
    print("  OK  ida y vuelta: baúl, PBIN, NPCBIN, PSELECT, PDU, PLAYERLIST, mazo de duelo, DUELPASS, SD_GET")

    print("Lote L5:")
    # B05
    expect("B05: 3256 y 0 fuera; 3255 dentro", (card_ok(MAX_CARD), card_ok(0), card_ok(CARD_ID_MAX)), (False, False, True))
    expect("B05: carta sin dibujo (171) fuera", card_ok(171), False)
    q = {"trunk": {"1": 3, "3256": 3, "171": 2, "x": 1}}
    expect("B05: sanitize_trunk quita 3256, 171 y basura", (sorted(sanitize_trunk(q)), q["trunk"]),
           (["171", "3256", "x"], {"1": 3}))
    expect("B05: trunk_bytes no manda un 3256 guardado", trunk_bytes({"trunk": {"3256": 3}})[MAX_CARD - 1], 0)
    dd.feed(0x6601, struct.pack(">IHH", 2, 5, 3256)); expect("B05: DuelDeck.bad_ids", dd.bad_ids(), [3256])
    # B06
    expect("B06: npc_result derrota -> b1", npc_result([0, 0, 0, 1], "lose"), [0, 1, 0, 1])
    expect("B06: npc_result empate -> b2", npc_result([0, 0, 0, 1], "draw"), [0, 0, 1, 1])
    st = {"players": {"0": "Player1"}, "wallet": {"mp": 9800, "dpp": 1000}}
    p = profile(st, 0)
    on_npc_duel_end(p, 18, "lose")
    expect("B06: on_npc_duel_end derrota en b1, b3 sin tocar", npcbin_get(p, 18), [0, 1, 0, 0])
    # B07
    expect("B07: ITEMGET(1) da los 3 挑戦権 (modo free)",
           decode([("n", "u16"), ("a", "u16"), ("b", "u16"), ("c", "u16"), ("m", "u16")], on_itemget(p, 1, 0)),
           {"n": 3, "a": 592, "b": 593, "c": 594, "m": 0})
    expect("B07: y quedan en el PBIN; el 2.º ITEMGET(1) va vacío",
           (all(pbin_test(p, b) for b in (592, 593, 594)), on_itemget(p, 1, 0)), (True, bytes(4)))
    # B08 (cadenas)
    recs = chain_records({51: 204, 54: 201})
    nbm = {}
    expect("B08: sin progreso solo el nivel 1", sorted(visible(recs, nbm)), [51, 54])
    nbm[51] = [3, 0, 0, CHAIN_THRESHOLD]
    expect("B08: 51 completada -> sale 52 y 51 se va", sorted(visible(recs, nbm)), [52, 54])
    expect("B08: chain_fields(53) = (T, 52); sin 52 en la lista, previa 0",
           (chain_fields(53, [51, 52, 53]), chain_fields(53, [51, 53]), chain_fields(18), chain_fields(96)),
           ((CHAIN_THRESHOLD, 52), (CHAIN_THRESHOLD, 0), (CHAIN_THRESHOLD, 0), (CHAIN_THRESHOLD, 0)))
    nbm[52] = [0, 0, 0, CHAIN_THRESHOLD]; nbm[53] = [0, 0, 0, CHAIN_THRESHOLD]
    expect("B08: 53 completada: visible con 3 estrellas", (53 in visible(recs, nbm), stars(recs, nbm, 53)),
           (True, (3, 0)))
    # B08 + B09: b3 sube por victoria; regalos
    p = profile({"players": {"0": "X"}}, 0)
    on_itemget(p, 4, 53)
    r1 = npc_duel_result(p, 53, "win")
    prize = npc_prize_bits()
    expect("B09: 1.ª victoria (b3 1->2): 途中景品 GET済み y present = 1",
           (npcbin_get(p, 53), r1["present"], r1["pkg"]["bits"]), ([1, 0, 0, 2], 1, [prize[53]["mid_got"]]))
    expect("B09: take_present lo consume", (take_present(p), take_present(p)), (1, 0))
    npc_duel_result(p, 53, "win")
    r3 = npc_duel_result(p, 53, "win")
    expect("B09: 3.ª victoria (b3 = 4): コンプ (619) + título amistad Yugi (160) + ropa (121)",
           r3["pkg"]["bits"], [619, 160, 121])
    rep = on_itemget(p, 3, 0)
    expect("B09: ITEMGET(3) entrega ese regalo (3 bits, 0 cartas = 10 B)", (len(rep), rep[:4]), (10, bytes([0, 3, 2, 0x6b])))
    expect("B09: los bits ya están en el PBIN y la cola «npc» vacía",
           (all(pbin_test(p, b) for b in (619, 160, 121)), [x for x in p["pending"] if x["via"] == "npc"]), (True, []))
    expect("B09: ITEMGET(3) sin nada pendiente = 4 B a cero", on_itemget(p, 3, 0), bytes(4))
    q = blank_profile("y"); pbin_set(q, prize[18]["mid_right"])
    rep = on_itemget(q, 3, 0)
    expect("B09: derecho de la escena de login: se quita y se da el «済み»",
           (pbin_test(q, prize[18]["mid_right"]), pbin_test(q, prize[18]["mid_got"]), rep),
           (False, True, struct.pack(">HHH", 1, prize[18]["mid_got"], 0)))
    q = blank_profile("z"); npc_duel_result(q, 18, "win"); npc_duel_result(q, 20, "win")
    expect("B09: un regalo viejo sin pedir no sale con otra NPC", [x["src"][:6] for x in q["pending"]], ["NPC 20"])
    # B10
    expect("B10: bit 224 -> id 0, bit 230 -> 0xf102, 160 -> 0xf124",
           (title_id_from_bit(224), title_id_from_bit(230), title_id_from_bit(160)), (0, 0xf102, 0xf124))
    q = blank_profile("t"); r = on_title(q, 230)
    expect("B10: on_title guarda el id", (r, q["rec"]["title"]), (bytes(4), 0xf102))
    expect("B10: bit desconocido -> 0x4175 = 1 sin tocar", (on_title(q, 5), q["rec"]["title"]),
           (struct.pack(">I", 1), 0xf102))
    q["rec"]["title"] = 230
    expect("B10: un bit viejo guardado sale como id en PSELECT", decode(PSELECT_SPEC, pselect_result(q, 1))["title"], 0xf102)
    # B51
    q = blank_profile("s")
    npc_duel_result(q, 18, "lose", turns=3, lp_left=0, stats={"tribute1": 2, "spell_me": 5, "nope": 9},
                    surrender="me")
    expect("B51: stats, rendición y LP", {k: q["rec"][k] for k in ("tribute1", "spell_me", "surrender_me", "turns")},
           {"tribute1": 2, "spell_me": 5, "surrender_me": 1, "turns": 3})
    rec_inc(q["rec"], "tribute1", 70000)
    expect("B51: contador u16 topado en 65535", q["rec"]["tribute1"], 0xffff)
    # B74
    q = blank_profile("t74"); q["trunk"] = {}
    q["rec"]["duels_won"] = 9
    npc_duel_result(q, 18, "win")
    expect("B74: 10 victorias -> DUELISTA DE BRONCE (250) por ITEMGET(1)", 250 in on_itemget(q, 1, 0)[2:12:2] or
           pbin_test(q, 250), True)
    q = blank_profile("t255"); q["trunk"] = {}
    npc_duel_result(q, 18, "lose", turns=1, surrender="me")
    expect("B74: rendirse en el turno 1 -> 255", pbin_test(q, 255), True)
    expect("B74: todos los bits de TITLE_RULES son títulos", all(b in title_bits() for b, _, _ in TITLE_RULES), True)
    # B70
    st = {"players": {"0": "A", "1": "B"}, "active_slot": 1}
    p0, p1 = profile(st, 0), profile(st, 1)
    mp0, mp1, dp1 = p0["rec"]["mp"], p1["rec"]["mp"], p1["rec"]["dpp"]
    on_pack_open(p1, [1], st)
    expect("B70: DUELPASS: MP +50 al hueco 0, DPP +150 al activo",
           (p0["rec"]["mp"] - mp0, p1["rec"]["mp"] - mp1, p1["rec"]["dpp"] - dp1), (50, 0, 150))
    expect("B70: DPAUTH con código desconocido -> s32 1", on_dpauth(p1, "XXXX")[:4], struct.pack(">I", 1))
    # §13 coste del duelo: personaje nuevo con START_DPP, perfil viejo sin «dpp» = LEGACY_DPP, cobro y umbrales
    expect("DP: personaje nuevo empieza con START_DPP", blank_profile("N")["rec"]["dpp"], START_DPP)
    old = {"rec": {"lv": 3}}
    expect("DP: perfil viejo sin dpp vale LEGACY_DPP (no cambia de saldo)", dp_of(old), LEGACY_DPP)
    pc = blank_profile("C"); pc["rec"]["dpp"] = 2
    expect("DP: cobro 2 -> 1 -> 0 -> 0", (dp_charge(pc, 1), dp_charge(pc, 1), dp_charge(pc, 1)),
           ((2, 1), (1, 0), (0, 0)))
    expect("DP: umbrales como el cliente (simple 1, match 3, apagado 0)",
           (dp_need(3, 1), dp_need(4, 1), dp_need(6, 1), dp_need(0, 1), dp_need(3, 0)), (1, 3, 3, 3, 0))
    expect("DP: el 0x4140 lleva el saldo cobrado", decode(PDU_SPEC, player_data_update(pc))["dpp"], 0)
    # §14 DP de inicio una vez por CUENTA
    if DP_START_ONCE:
        sa = {"players": {"0": "Uno", "1": "Dos"}}
        qa, qb = profile(sa, 0), profile(sa, 1)
        expect("DP §14: 1.er personaje de la cuenta START_DPP, el 2.º 0, cuenta marcada",
               (qa["rec"]["dpp"], qb["rec"]["dpp"], sa.get(DP_START_KEY)), (START_DPP, 0, 1))
        sb = {"players": {"0": "Viejo"}, "profiles": {"0": {"name": "Viejo", "rec": {"dpp": 7}}}}
        sb["players"]["2"] = "Nuevo"
        expect("DP §14: cuenta que ya tenía personaje: el nuevo empieza con 0 y el viejo conserva el suyo",
               (profile(sb, 2)["rec"]["dpp"], profile(sb, 0)["rec"]["dpp"]), (0, 7))
        sc = {"players": {}, "profiles_borrados": [{"slot": "0"}]}
        sc["players"]["0"] = "Otra"
        expect("DP §14: borrar y volver a crear no da otros DP", profile(sc, 0)["rec"]["dpp"], 0)
        sd = {"players": {}}
        ph = profile(sd, 0)                          # hueco sin personaje (p. ej. un 0x4140 antes de crearlo)
        z = (ph["rec"]["dpp"], DP_START_PEND in ph)
        sd["players"]["0"] = "Tarde"
        expect("DP §14: perfil creado sin personaje: 0 hasta que tiene nombre, luego START_DPP",
               (z, profile(sd, 0)["rec"]["dpp"], DP_START_PEND in sd["profiles"]["0"], sd.get(DP_START_KEY)),
               ((0, True), START_DPP, False, 1))
        se = {"players": {}}
        ph = profile(se, 1); se["players"]["0"] = "Real"; se["players"]["1"] = "Luego"
        expect("DP §14: el perfil sin personaje no cuenta como «ya tuvo»; el primero con nombre se queda los DP",
               (profile(se, 0)["rec"]["dpp"], profile(se, 1)["rec"]["dpp"]), (START_DPP, 0))
    # §14 carta tras el PvP
    if PVP_CARD_ON:
        pv = blank_profile("P")
        e1 = pvp_card_offer(pv)
        expect("PvP §14: 1 carta por duelo, sobres de PVP_CARD_PACKS", (e1["n"], e1["packs"]),
               (PVP_CARDS_DUEL, list(PVP_CARD_PACKS)))
        expect("PvP §14: match preparado (3 cartas)", pvp_card_entry(match=True)["n"], PVP_CARDS_MATCH)
        expect("PvP §14: 0x4802 con un sobre ofrecido da n y lo quita; repetir da 0; sobre no ofrecido da 0",
               (pvp_card_take(pv, 99999), pvp_card_take(pv, PVP_CARD_PACKS[0]), pvp_card_take(pv, PVP_CARD_PACKS[0]),
                PVP_CARD_KEY in pv), (0, 1, 0, False))
        for _ in range(PVP_CARD_DUE_MAX + 3):
            pvp_card_offer(pv)
        expect("PvP §14: como mucho PVP_CARD_DUE_MAX pendientes", len(pv[PVP_CARD_KEY]), PVP_CARD_DUE_MAX)
        pg = blank_profile("G"); pg["trunk"] = {}
        pk, cs = pvp_card_grant(pg, pvp_card_entry(), lambda k, n: [1] * n)
        expect("PvP §14: sin elector (desconectado) la carta va directa al baúl",
               (pk in PVP_CARD_PACKS, cs, pg["trunk"]), (True, [1], {"1": 1}))
    # my_slot / my_pid / my_name
    expect("my_slot/my_pid/my_name", (my_slot(st), my_pid(st), my_name(st)), (1, 2, "B"))
    # make_handlers (sin red)
    box = {"st": {"players": {"0": "Player1"}, "profiles": {}}}
    saves = []

    class _C:
        def __init__(self):
            self.out = []

        def send(self, mid, d=b""):
            self.out.append((mid, d))
    H = make_handlers(lambda: json.loads(json.dumps(box["st"])),
                      lambda s: (saves.append(1), box.__setitem__("st", json.loads(json.dumps(s)))),
                      game_conn=lambda: gconn)
    gconn = _C()
    H["wallet"](); n1 = len(saves); H["wallet"](); H["wallet"]()
    expect("B24: wallet() solo guarda al crear el perfil", (n1, len(saves)), (1, 1))
    c = _C(); H["h_title"](c, struct.pack(">I", 230))
    expect("handlers: h_title guarda el id", (c.out[0], box["st"]["profiles"]["0"]["rec"]["title"]),
           ((0x4175, bytes(4)), 0xf102))
    u32 = lambda n: struct.pack(">I", n)            # noqa: E731
    c = _C(); H["h_dp_saldo"](c, b"")
    expect("B70: 0x3110 sin pases -> 0x3111 = 0", c.out, [(0x3111, u32(0))])
    mp_before = box["st"]["profiles"]["0"]["rec"]["mp"]; pb_before = list(box["st"]["profiles"]["0"].get("pbin", []))
    c = _C(); H["h_pack_open"](c, b"")
    expect("B70: 0x4902 sin pases -> 0x4903 s32 1, sin cartas; 0x3111 = 0; nada cambia",
           (c.out, gconn.out, box["st"]["profiles"]["0"]["rec"]["mp"], box["st"]["profiles"]["0"].get("pbin", [])),
           ([(0x4903, u32(1) + u32(0))], [(0x3111, u32(0))], mp_before, pb_before))
    box["st"]["dp_pases"] = 2; gconn.out.clear()
    c = _C(); H["h_dp_saldo"](c, b"")
    expect("B70: 0x3110 con 2 pases -> 0x3111 = 2", c.out, [(0x3111, u32(2))])
    c = _C(); H["h_pack_open"](c, b"")
    p0 = box["st"]["profiles"]["0"]
    expect("B70: 1.er canje -> 0x4903 s32 0 + 0x3111 = 1; derechos 770/772/774; MP +50",
           (c.out[0][0], c.out[0][1][:4], gconn.out, box["st"]["dp_pases"], sorted(set(p0["pbin"]) & {770, 772, 774}),
            p0["rec"]["mp"] - mp_before), (0x4903, u32(0), [(0x3111, u32(1))], 1, [770, 772, 774], 50))
    for b in PB_BEGINNER_RIGHTS:                    # el cliente los canjea (SD_GET): derecho fuera, «GET済み» puesto
        pbin_set(p0, b, False); pbin_set(p0, b + 1, True)
    gconn.out.clear()
    c = _C(); H["h_pack_open"](c, b"")
    p0 = box["st"]["profiles"]["0"]
    expect("B70: 2.º canje -> 0x3111 = 0 y los derechos NO vuelven (ya canjeados)",
           (c.out[0][1][:4], gconn.out, box["st"]["dp_pases"], sorted(set(p0["pbin"]) & {770, 772, 774})),
           (u32(0), [(0x3111, u32(0))], 0, []))
    gconn.out.clear()
    c = _C(); H["h_pack_open"](c, b"")
    expect("B70: 3.er canje rechazado (s32 1, sin cartas)", (c.out, gconn.out), ([(0x4903, u32(1) + u32(0))], [(0x3111, u32(0))]))
    expect("B70: saldo fuera de rango -> 0..DP_PASS_MAX", (dp_passes({"dp_pases": -3}), dp_passes({"dp_pases": "x"}),
                                                          dp_passes({"dp_pases": 10 ** 9})), (0, 0, DP_PASS_MAX))
    expect("handlers: my_pid()", H["my_pid"](), 1)
    # migración
    st = {"players": {"0": "S"}, "profiles": {"0": {"name": "S", "rec": {"duels_lost": 6, "duels_drawn": 0, "title": 224},
                                                    "npcbin": {"78": [0, 2, 0, 1], "18": [0, 1, 0, 1], "2": [0, 0, 0, 1]},
                                                    "trunk": {"1": 3, "3256": 3}, "pbin": []}}}
    rep = migrate_state(st)
    expect("migración: datos ya corregidos a mano -> NO intercambia; título 224 -> 0; quita 3256",
           (st["profiles"]["0"]["npcbin"]["78"], st["profiles"]["0"]["rec"]["title"], st["profiles"]["0"]["trunk"],
            st["schema"]), ([0, 2, 0, 1], 0, {"1": 3}, SCHEMA))
    old = {"players": {"0": "S"}, "profiles": {"0": {"name": "S", "rec": {"duels_lost": 3, "duels_drawn": 0},
                                                     "npcbin": {"18": [0, 0, 1, 1], "72": [0, 0, 2, 1]}}}}
    migrate_state(old)
    expect("migración: convenio viejo (derrotas en b2) -> intercambia", old["profiles"]["0"]["npcbin"],
           {"18": [0, 1, 0, 1], "72": [0, 2, 0, 1]})
    snap = json.dumps(old, sort_keys=True); rep2 = migrate_state(old)
    expect("migración: idempotente (2.ª pasada no cambia nada)", (json.dumps(old, sort_keys=True) == snap, rep2), (True, []))
    print("  default PBIN: %d bits; premios de NPC en el mapa: %d NPC; títulos: %d; cartas válidas: %d"
          % (len(default_pbin_bits()), len(npc_prize_bits()), len(titles()), len(valid_card_ids())))
    raw = open(__file__, "rb").read()
    expect("sin bytes nulos en el fichero", bytes(1) in raw, False)
    print("RESULTADO:", "todo bien" if ok else "HAY FALLOS")
    return ok


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if selftest() else 1)
