"""Arranque del servidor (paso E8 de la reestructura, REESTRUCTURA.md §3.4): todo lo que run.py hacía entre el log y el bucle
principal, MOVIDO en el MISMO orden y sin cambiar la lógica.

    arranque.cargar(SRV)     las cargas: red, almacén, cuentas, HUB/PVP, persistencia (PP/PH), avatar, DUELPASS, respuestas
                             por defecto, NPC (NR), mundo (MW), NPCS, lobby_reply, duelo (LS), OCG, fin de duelo (DFM/DF),
                             lobby (LP), equipos (EQ), intercambio (TP), torneos (TO), instalación de MW, sobres, reserva
                             del duelo (DT) y comprobación del mazo (DK). Sin abrir puertos ni lanzar hilos: es lo que
                             ejecutan ocg_test.py y pvp_test.py (run.py hasta «FIN DE LA CARGA»).
    arranque.escuchar(SRV)   la tabla de puertos (HANDLERS), CONNS, la consola 5799 (yo2srv/admin/consola.py), la comprobación
                             de puertos ocupados, los vetos del login y la API de gestión (yo2srv/admin/moderacion.py y
                             gestion.py), el panel (research/admin/panel.py) y los hilos `serve`/`serve_http`.

Es un guion, no una librería: cada línea deja un nombre en el namespace de run.py (`srv.ns` = `globals()` de run.py) o ejecuta
una carga. Lo importante es el ORDEN, porque unas cargas parchean a otras (REESTRUCTURA.md §1.5: LP.install, EQ.install,
TP.install, TO.install, MW.install, NR.install, DPC.install_start/install_game, el parche de comid_field de LS y OCG.TR,
el envoltorio 0x4802 de NR…); por eso cada bloque lleva su comentario de siempre y NO se reordena ni se agrupa. Los nombres que
la consola 5799, el panel, pvp_hub, research/* y los hot_*.py esperan en el namespace son los de siempre (contrato:
paridad/contrato_ns.txt). Los fallos de carga se anotan en el log y el nombre se queda en None, como antes.

Reglas (REESTRUCTURA.md §2.3): se escribe en el namespace con `ns[nombre]` / `srv.nombre = …` y se lee en el momento de usarlo;
en las lambdas que se pasan a research (PP, DFM, NR) el nombre se busca en cada llamada, como el global que eran. OJO con los
alias de compat.ALIAS: `srv.mundo` es el módulo MW, no la función; aquí se usa siempre `ns["mundo"]`.

`load_research(area)` está en el namespace (los hot_*.py y las pruebas lo usan) y carga research/<area>/proposal.py con el nombre
`research_<area>`.
"""
import importlib
import importlib.util
import os
import sys

from yo2srv import compat

srv = None      # el Server; lo fija setup(). Hasta entonces nada de este módulo se puede llamar.


def setup(servidor):
    """Enlaza el módulo con el Server (idempotente)."""
    global srv
    srv = servidor
    return servidor


def _de(modulo, *nombres):
    """`from <modulo> import n1, n2…` dejando los nombres en el namespace de run.py (los MISMOS de siempre)."""
    m = importlib.import_module(modulo)
    for n in nombres:
        srv.ns[n] = getattr(m, n)
    return m


def load_research(area):
    """Carga tools/yo2emu/research/<area>/proposal.py con nombre propio (cada agente deja un proposal.py).

    Siempre ejecuta el fichero de nuevo y deja el módulo en sys.modules["research_<area>"] (el nombre canónico, paso U1:
    compat.cargar). Lo que se cargue DESPUÉS de esto y necesite el mismo área (el traductor con lockstep, duelfin con
    lockstep y persistencia) pide ese módulo con compat.cargar(area) y recibe ESTE objeto, no una copia."""
    return compat.cargar(area, fresco=True, base=srv.HERE)


def crear_ph(PP):
    """PH = PP.make_handlers(...) con el cableado del servidor. Lo usan el arranque y la recarga en caliente de persistencia
    (yo2srv/recarga.py): un solo sitio, para que las dos formas de tener un PH no puedan diferir."""
    g = srv.ns
    # lock = el mismo cerrojo de state.json que el resto de run.py; game_conn = 5731 vivo (rearmar DUELPASS, B70)
    # pid_of: PSELECTRESULT lleva el pid de la sesión (V02), no hueco + 1. game_conn: la 5731 de ESTE cliente.
    return PP.make_handlers(
        g["load_state"], g["save_state"], g["shop_purchase"], lock=g["STATE_LOCK"],
        game_conn=lambda: srv.live_conn((srv.cur_sess().conns.get(srv.GAME_PORT) if srv.cur_sess() else None)
                                        or srv.CONNS.get(srv.GAME_PORT)),
        pid_of=lambda slot: srv.HUB.pid_for(srv.cur_sess(), slot))


def crear_df(DFM):
    """DF = DFM.make_handlers(...) con el cableado del servidor (arranque y recarga en caliente de duelfin)."""
    g = srv.ns
    return DFM.make_handlers(g["load_state"], g["save_state"], lambda: srv.live_conn(srv.LOBBY_ROOM["c"]),
                             confirm_surrender=True)


# --------------------------------------------------------------------------------------------------------- cargas
def cargar(servidor):
    """Todas las cargas de run.py, en su orden. No abre puertos ni lanza hilos."""
    setup(servidor)
    g = srv.ns
    log = srv.log                      # Server.log busca el `log` del namespace en cada llamada (como el global de antes)

    # Todos los módulos con estado enlazan con el Server ANTES de importar sus nombres (setup es idempotente).
    from yo2srv import admin, cliente, duelo, lobby, mundo, red, tienda
    from yo2srv.cuentas import setup as _setup_cuentas
    from yo2srv.datos import estado

    g["proto"] = importlib.import_module("yo2srv.red.proto")   # = el módulo de la antigua proto.py (alias del mismo objeto)
    admin.setup(srv); cliente.setup(srv)

    # E1b (red): los puertos, ROOM_KINDS, GATE_PAUSE y gate_reply (la puerta 5730) viven en yo2srv/red/puerta.py; Conn en
    # red/conexion.py; la tabla de puertos, serve/_handle, CONNS y PEER_CONNS en red/servidor.py; auto_reply en red/auto.py y el
    # HTTP en red/http.py. Aquí quedan los MISMOS nombres en el namespace (consola 5799, panel y hot_*.py los buscan aquí).
    red.setup(srv)
    puerta = importlib.import_module("yo2srv.red.puerta")
    # GATE 5730, GAME 5731, LOBBY 5732, ROOM_PORT_BASE 5740 (sala de tipo ROOM_KINDS[i] -> puerto 5740+i: así el servidor sabe en
    # qué sala entró el cliente), HTTP 8730 y ADMIN 5799, todos + PORT_OFFSET
    (g["GATE_PORT"], g["GAME_PORT"], g["LOBBY_PORT"], g["ROOM_PORT_BASE"], g["HTTP_PORT"],
     g["ADMIN_PORT"]) = puerta.puertos(g["PORT_OFFSET"])
    _de("yo2srv.red.puerta", "GATE_PAUSE")   # la lista de la puerta va seguida (ver gate_reply)
    _de("yo2srv.red.puerta", "ROOM_KINDS")   # b2: puerta azul 13,7,15,8; roja 14,1,9,16,17,10,20
    # 10-10: salas por idioma 1:1 (ROOM_PER_LANG, apagada por defecto): un puerto por (idioma, tipo); la mundial (b1 = 6)
    # sigue en ROOM_PORT_BASE + i y las demás en ROOM_LANG_PORT_BASE (5800 + PORT_OFFSET) en adelante (red/puerta.py).
    g["ROOM_LANG_PORT_BASE"] = puerta.puerto_idiomas(g["PORT_OFFSET"])
    _de("yo2srv.red.puerta", "ROOM_PER_LANG", "WORLD_LANG", "ROOM_LANGS", "room_ports", "room_port", "room_of_port",
        "room_block_key")
    # E2: el estado persistente (STATE_PATH, STATE_LOCK, load/save_state_raw, copia de arranque y almacén SQLite) vive
    # en yo2srv/datos/estado.py. setup() va ANTES del import: crea el almacén si YO2_STORAGE=sqlite. YO2_STATE = otro fichero
    # (prueba): ocg_test.py y pvp_test.py lo fijan en el entorno.
    estado.setup(srv)
    _de("yo2srv.datos.estado", "STATE_PATH", "STATE_LOCK", "STATE_OK", "STATE_KEEP_BOOT", "_read_json", "load_state_raw",
        "_write_atomic", "save_state_raw", "_boot_backup", "STORAGE", "STORE")
    if g.get("__name__") == "__main__":
        g["_boot_backup"]()

    # E3: la sesión del hilo (CTX/cur_sess/SIN_SESION), la vista por cuenta (load_state/save_state) y register_account.
    # ACCT_KEYS/HUB_KEYS/OWNER_ACCT/MAX_SAVED_ACCOUNTS se leen en el módulo como srv.X (hot_* amplían ACCT_KEYS y carga.py
    # sube el tope por la consola).
    _setup_cuentas(srv)
    _de("yo2srv.cuentas.sesion", "ACCT_KEYS", "HUB_KEYS", "OWNER_ACCT", "MAX_SAVED_ACCOUNTS", "CTX", "_SinSesion", "SinSesion",
        "SIN_SESION", "NET_THREADS", "_net_thread", "cur_sess", "_no_sess_ctx", "_is_owner_ctx", "load_state", "save_state",
        "register_account", "_hub_write")

    pvp_hub = importlib.import_module("pvp_hub"); g["pvp_hub"] = pvp_hub
    g["HUB"] = g.get("HUB") or pvp_hub.Hub(g)
    # PvP fase 2 (V05-V09): reservas {pidA, pidB} y partidas PvP (pvp_match.py). Sin él, todo sigue como con la NPC.
    try:
        pvp_match = importlib.import_module("pvp_match"); g["pvp_match"] = pvp_match
        g["PVP"] = g.get("PVP") or pvp_match.Matches(g)
    except Exception as _e:
        print(f"[pvp] pvp_match.py no cargado: {_e!r}")
        g["PVP"] = None
    # Duelos contra NPC vistos desde fuera (npc_watch.py; AÑADIDO NUESTRO, aprobado por el owner el 07-10, no 1:1): pose
    # «en duelo» al EMPEZAR el duelo y «Mirar» (espectador). Sin él, todo sigue como antes. En caliente: research/lobby/
    # hot_npc_mirar.py (no lo recrea: NW conserva sus duelos).
    try:
        npc_watch = importlib.import_module("npc_watch"); g["npc_watch"] = npc_watch
        g["NW"] = g.get("NW") or npc_watch.NpcWatch(g)
    except Exception as _e:
        print(f"[npcmirar] npc_watch.py no cargado: {_e!r}")
        g["NW"] = None

    g["load_research"] = load_research
    g["PH"] = None   # manejadores de persistencia (agente 5); se rellena más abajo

    # Conn (red/conexion.py) y gate_reply (red/puerta.py): ver el aviso E1b de arriba.
    _de("yo2srv.red.conexion", "Conn")
    _de("yo2srv.red.puerta", "gate_reply")

    # E3: player_entry y archive_profile viven en yo2srv/cuentas/personajes.py (junto a las ramas 0x3010/0x3020/0x3030/
    # 0x3100/0x3114 y a my_slot/my_pid/my_name/sync_player_id); E8: también game_reply, el despachador del puerto 5731.
    _de("yo2srv.cuentas.personajes", "player_entry", "PROFILES_ARCHIVE_MAX", "archive_profile", "playerlist_0x3010",
        "regplayer_0x3020", "delplayer_0x3030", "trunk_0x3100", "pbin_0x3114", "my_slot", "my_pid", "my_name",
        "sync_player_id", "game_reply")

    g["PLAYER_ID"] = 1
    # B12: el personaje activo NO es siempre el hueco 0 (ver yo2srv/cuentas/personajes.py: my_slot/my_pid/my_name). PLAYER_ID
    # se mantiene por compatibilidad y se pone al día tras cada PSELECT (sync_player_id); se REASIGNA, por eso vive en el
    # namespace y el módulo lo escribe como srv.PLAYER_ID.
    g["sync_player_id"]()

    # E7: EMPTY_LISTS (peticiones de listas del mapa que se contestan vacías) vive en yo2srv/lobby/lobby.py, con lobby_reply.
    lobby.setup(srv)
    _de("yo2srv.lobby.lobby", "EMPTY_LISTS")

    # E5: bloques, avatar, NPC y mundo de 2008 viven en yo2srv/mundo/{bloques,avatar,npc,mapas}.py. `NPCS` sigue creándose
    # aquí (más abajo, tras cargar NR/MW/AV).
    mundo.setup(srv)
    _de("yo2srv.mundo.avatar", "avatar_info")
    _de("yo2srv.mundo.bloques", "ACAD_KINDS", "ACAD_AREA", "DOMINO_AREA")
    # 08-10: el idioma de 0x4100 es el del CLIENTE (ver yo2srv/mundo/bloques.py: ROOM_LANG_MERGE y room_lang).
    _de("yo2srv.mundo.bloques", "ROOM_LANG_MERGE", "room_lang")
    _de("yo2srv.mundo.bloques", "block_key", "hub_area", "block_others", "send_block_list", "block_player", "block_type")

    # Coste del duelo en DP y país por defecto: yo2srv/tienda/cartera.py (E8; los parchea hot_dp_coste.py).
    _de("yo2srv.tienda.cartera", "dp_balance", "dp_npc_gate", "DEFAULT_COUNTRY")

    # E4: tienda y cartera viven en yo2srv/tienda/{tienda,cartera}.py (catálogo, precios, shop_purchase, START_MP/START_DPP,
    # wallet/set_wallet/player_data_update, pselect_result). OJO: más abajo PH (persistencia) REASIGNA wallet, set_wallet y
    # player_data_update.
    tienda.setup(srv)
    _de("yo2srv.tienda.tienda", "PACKINFO", "SHOP_CARDS", "shop_prices", "SHOP_SELL_PRIZE_PACKS", "SHOP_DECK_RECIPE",
        "SHOP_DECK_ITEMGET", "packinfo_packs", "shop_catalog", "PACK_SIZE", "MAX_CARD", "shop_purchase")
    _de("yo2srv.tienda.cartera", "START_MP", "START_DPP", "wallet", "set_wallet", "player_data_update", "pselect_result")

    # Persistencia por perfil (agente 5, research/persistencia: NOTES.md). state.json["profiles"][hueco] guarda baúl,
    # saldo, PBIN/NPCBIN, estadísticas y avatar; migra el «wallet» viejo. Baúl inicial «all3» = 3 copias de cada carta,
    # como hasta ahora (con «deck» empezaría solo con las cartas del deck.ydc). Si no carga, todo sigue como antes.
    os.environ.setdefault("YO2_TRUNK_START", "all3")
    try:
        PP = g["PP"] = load_research("persistencia")
        PH = g["PH"] = crear_ph(PP)        # (lock, game_conn y pid_of: ver crear_ph)
        g["wallet"], g["set_wallet"], g["player_data_update"] = PH["wallet"], PH["set_wallet"], PH["player_data_update"]
        PP.RAW_STATE = lambda: srv.load_state_raw()   # tienda2: tp_rank de 0x3012 se calcula entre TODAS las cuentas
    except Exception as _e:
        log(f"[persistencia] proposal.py no cargado: {_e!r}")
        g["PP"] = g["PH"] = None
    # Avatar (agente 4, research/avatar: NOTES.md): piezas válidas de NPC, ropa en la tienda (tipo 2, param = bit PBIN).
    try:
        g["AV"] = load_research("avatar")
    except Exception as _e:
        log(f"[avatar] proposal.py no cargado: {_e!r}")
        g["AV"] = None
    _de("yo2srv.mundo.avatar", "AV_STATE", "MYROOM_SELF_AVATAR", "myroom_self_avatar")

    # PH_GAME / PH_LOBBY (id de mensaje -> manejador PH[...]) viven en yo2srv/lobby/lobby.py (E7): son los MISMOS dicts que DPC
    # (install_game: PH_LOBBY[0x8101]) y hot_duelpass_*.py modifican por referencia. registrar_ph: 0x3110 -> PH["h_dp_saldo"].
    _de("yo2srv.lobby.lobby", "PH_GAME", "PH_LOBBY", "registrar_ph")
    g["registrar_ph"](g["PH"])
    # 07-10 Códigos DUELPASS (0x8101 REQDPAUTH -> 0x8102, 1:1: research/persistencia/duelpass.py y NOTES.md) y cifras
    # de inicio del panel (admin_regalos.json; sin él, START_MP/START_DPP/YO2_TRUNK_START como siempre).
    # YO2_DPAUTH=0 deja 0x8101 por AUTO (como antes). En caliente: research/admin/hot_duelpass_juego.py.
    try:
        _dp_spec = importlib.util.spec_from_file_location("yo2_duelpass", os.path.join(srv.HERE, "research", "persistencia", "duelpass.py"))
        DPC = g["DPC"] = importlib.util.module_from_spec(_dp_spec); sys.modules["yo2_duelpass"] = DPC; _dp_spec.loader.exec_module(DPC)
        if g["PP"] is not None:
            DPC.install_start(g["PP"], os.path.dirname(os.path.abspath(g["STATE_PATH"])))
        if DPC.DPAUTH_ON and DPC.install_game(g):
            log("[duelpass] 0x8101 REQDPAUTH atendido (códigos en admin_duelpass.json)")
    except Exception as _e:
        log(f"[duelpass] duelpass.py no cargado: {_e!r}")
        sys.modules.pop("yo2_duelpass", None); g["DPC"] = None

    # Respuestas por defecto (auto_replies.json + research/lobby/auto_overrides.json) y auto_reply: yo2srv/red/auto.py (E1b).
    # cargar() lee los dos JSON aquí, donde antes estaban las lecturas.
    g["AUTO"], g["AUTO_OV"] = importlib.import_module("yo2srv.red.auto").cargar()
    _de("yo2srv.red.auto", "_FMT_SIZE", "auto_reply")

    _de("yo2srv.mundo.npc", "SPOTS_A", "FAMOUS", "NPC_SPOTS_2008", "SPOTS_2008", "NORMAL_SPOTS_2008", "NPC_RULES_2008", "npc_rule",
        "npc_parts", "load_npcs", "NPC_SEX", "npc_list_parts", "npc_entry")
    try:
        g["NR"] = load_research("npcreglas")
    except Exception as _e:
        print(f"[npcreglas] proposal.py no cargado: {_e!r}"); g["NR"] = None

    _de("yo2srv.mundo.mapas", "MUNDO_2008", "mundo")
    try:
        g["MW"] = load_research("mundo")
    except Exception as _e:
        print(f"[mundo] proposal.py no cargado: {_e!r}"); g["MW"] = None

    g["NPCS"] = g["load_npcs"]()

    # E4: la compra de la tienda (0x5210) vive en yo2srv/tienda/tienda.py; aquí quedan los MISMOS nombres.
    _de("yo2srv.tienda.tienda", "SHOP_ENTRIES_PER_MSG", "SHOP_ANIM_MAX", "SHOP_AVATAR_ITEMGET", "shop_buy")

    # E7: lobby_reply (el manejador de 5732 y las salas 5740-5750) vive en yo2srv/lobby/lobby.py; aquí queda el MISMO nombre
    # (HANDLERS, la consola, ocg_test/pvp_test y hot_dp_coste.py lo buscan aquí).
    _de("yo2srv.lobby.lobby", "lobby_reply")

    g["DUEL_PORT"] = 5760 + g["PORT_OFFSET"]

    # E6: el puerto de duelo y la sesión de duelo viven en yo2srv/duelo/{puerto,sesion_duelo}.py. DUEL y LOBBY_ROOM son el MISMO
    # dict en el módulo y aquí (sobreviven a una re-ejecución). `duel_reply` es UNA función (con la rama «sin LS» dentro) y
    # `duel_reply_base` la antigua base, que lockstep llama para lo que no trata. Un import por nombre, a propósito (las cargas
    # en caliente completan lo que falte en un vivo viejo ejecutando la SENTENCIA que liga cada nombre).
    duelo.setup(srv)
    _de("yo2srv.duelo.sesion_duelo", "DUEL", "LOBBY_ROOM", "_npc_name", "_admin_npc", "duel_state", "df_for", "df_end_mark",
        "duel_rival", "duel_begin", "duel_ip_for", "on_conn_closed", "_abandon_duel", "on_conn_accepted")
    _de("yo2srv.duelo.puerto", "cur_world", "duel_parts", "preparation_result", "duel_reply_base", "duel_reply", "duel_task",
        "_duel_worker", "ocg_session", "npc_deck_id", "ocg_finish")

    # Duelo con servidor autoritativo (agente 2, research/lockstep: NOTES.md). El motor del cliente solo ejecuta los
    # 0x6202 que le llegan: tras 0x61fe el servidor manda DUEL_START (confirmado en vivo), las cartas reales de los dos
    # mazos (tipo 0x40), el robo de 5 y 5 (confirmado en vivo), las fases y la petición de jugada. Lo demás, duel_reply_base.
    # E6: duel_reply (una sola función, con la rama «sin LS» dentro) vive en yo2srv/duelo/puerto.py y mira `srv.LS` en cada
    # mensaje: si lockstep no carga, LS no existe en el namespace y contesta la base sola, como antes de redefinirla.
    try:
        LS = g["LS"] = load_research("lockstep")
        LS.duel_reply_base = g["duel_reply_base"]; LS.log = g["log"]
        LS.DUEL_CFG["stage"] = 4
    except Exception as _e:
        log(f"[duelo] lockstep/proposal.py no cargado: {_e!r}")

    # Duelo con reglas reales (ocg_duel.py = duelcore/ocgcore + research/traductor + mazos oficiales de NPC). DUEL_MODE
    # «ocg» lo usa; «skeleton» vuelve al esqueleto sin reglas de research/lockstep. Si no carga, esqueleto.
    g["DUEL_MODE"] = g.get("DUEL_MODE", "ocg")
    g["OCG_CLIENT_MSGS"] = (0x6350, 0x6351, 0x002e, 0x6423, 0x6426, 0x6432)
    try:
        OCG = g["OCG"] = importlib.import_module("ocg_duel")
        if not g.get("_OCG_WARM"):
            OCG.warm_up(g["log"]); g["_OCG_WARM"] = True
    except Exception as _e:
        log(f"[ocg] ocg_duel.py no cargado: {_e!r}")
        g["OCG"] = None

    # Fin de duelo (research/duelfin: NOTES.md). El cliente solo sale del duelo cuando el servidor manda 0x6202 tipo 0x4d;
    # DF["finish"](conexión de duelo, ganador 0 jugador / 1 NPC, motivo) manda la secuencia entera (y por el lobby 0x4140,
    # 0x4237 y 0x4800, sin el cual la pantalla de resultados no muestra «Siguiente»). Rendirse = comando 11 del menú del
    # mazo propio. Desatascar un duelo desde la consola: DF["finish"](CONNS[5760], 1, DFM.WHY_SURRENDER).
    try:
        DFM = g["DFM"] = load_research("duelfin")
        g["DF"] = crear_df(DFM)
        # U1: lockstep es UN solo módulo (DFM.LS is LS), pero el traductor se quedó con SU PROPIA referencia a la función
        # (`from research_lockstep import comid_field`, ligada al importarlo, antes de este parche): por eso son dos sitios
        # que envolver, LS.comid_field (esqueleto de lockstep) y OCG.TR.comid_field (ocgcore). Mismo comportamiento de siempre.
        if "LS" in g:
            _cf = g["_cf"] = g["LS"].comid_field
            g["LS"].comid_field = lambda masks=None, extra=None, tail=None: _cf(srv.DFM.surrender_masks(masks), extra, tail)
        if g["OCG"] is not None and not getattr(g["OCG"].TR, "_surrender_patched", False):   # «Rendirse» también con ocgcore
            _tcf = g["_tcf"] = g["OCG"].TR.comid_field
            g["OCG"].TR.comid_field = lambda masks=None, extra=None, tail=None: _tcf(srv.DFM.surrender_masks(masks), extra, tail)
            g["OCG"].TR._surrender_patched = True
    except Exception as _e:
        log(f"[duelfin] proposal.py no cargado: {_e!r}")
        g["DF"] = None
    if g["DF"] is not None and g.get("NR") is not None:     # NPC_RULES_2008 (research/npcreglas): se mira en cada fin
        try:
            g["NR"].install(g["DFM"], lambda: srv.ns.get("NPC_RULES_2008"), g["log"])
        except Exception as _e:
            log(f"[npcreglas] no enchufado en duelfin: {_e!r}")

    # Manejadores del lobby del agente 3 (research/lobby/proposal.py). Si falla al cargar, el lobby sigue como antes.
    try:
        g["LP"] = load_research("lobby")
        g["LP"].install(g)
    except Exception as _e:
        log(f"[lobby] proposal.py no cargado: {_e!r}")
        g["LP"] = None

    # Equipos (research/equipos/proposal.py, 06-10): 0x4550/52/54/56/58/68/69 y HUB.list5_body = EQ.list5_body.
    try:
        EQ = g["EQ"] = load_research("equipos"); EQ.install(g)
        if g["LP"] is not None: g["LP"].HANDLERS.update(EQ.HANDLERS)
    except Exception as _e:
        log(f"[equipos] proposal.py no cargado: {_e!r}"); g["EQ"] = None

    # Intercambio de cartas (research/intercambio/proposal.py, 06-10): el sobre 0x7000 pasa a TP (sustituye al del lobby).
    try:
        TP = g["TP"] = load_research("intercambio"); TP.install(g)
        if g["LP"] is not None: g["LP"].HANDLERS.update(TP.HANDLERS)
    except Exception as _e:
        log(f"[intercambio] proposal.py no cargado: {_e!r}"); g["TP"] = None

    # Torneos (CHAMPIONSHIP), eventos 0x5000 y millas (research/torneos/proposal.py, 06-10). Todo apagado por banderas
    # (TOURNAMENT_ON/EVENTS_ON/AUTO_CLOCK): con ellas en False la ráfaga 0x51xx sigue en auto_replies y 0x4230 solo se observa.
    try:
        TO = g["TO"] = load_research("torneos"); TO.install(g)
        if g["LP"] is not None: g["LP"].HANDLERS.update(TO.HANDLERS)
    except Exception as _e:
        log(f"[torneos] proposal.py no cargado: {_e!r}"); g["TO"] = None

    # Sesiones y premios de los eventos históricos de YO2. EVENTINFO sigue en TO; ER conserva progreso y claims.
    try:
        ER = g["ER"] = load_research("eventos"); ER.install(g)
    except Exception as _e:
        log(f"[eventos] proposal.py no cargado: {_e!r}"); g["ER"] = None

    # MUNDO_2008 (ver el bloque de NPC): premios de las originales en npcreglas, 挑戦権/títulos al entrar en un mapa
    # (PP), títulos tras un PvP (pvp_match.pvp_record) y 0x4174. Con todo False no cambia nada.
    if g["MW"] is not None:
        try:
            g["MW"].install(g)
        except Exception as _e:
            log(f"[mundo] install: {_e!r}")

    # Elección de carta del sobre (E8: yo2srv/tienda/sobres.py). NPC_RULES_2008 card: el 0x4802 que abre el sobre de un duelo
    # de NPC da UNA carta (si no, el manejador de siempre); 07-10 carta tras el PvP (research/persistencia/NOTES.md §14):
    # PvpMatch._finish deja la carta pendiente en el perfil y manda sus sobres en 0x4800; el cliente abre él solo el elector
    # y manda 0x4802 con el elegido (_h_selectpack_pvp, delante del anterior vía pvp_card_install).
    sobres = importlib.import_module("yo2srv.tienda.sobres")
    sobres.instalar_npc()
    g["_h_selectpack_pvp"] = sobres._h_selectpack_pvp
    g["pvp_card_install"] = sobres.pvp_card_install
    g["pvp_card_install"]()

    # B58/B59: ciclo de vida de las conexiones. serve() llama a on_conn_accepted al aceptar y a on_conn_closed al salir del
    # bucle de lectura; lobby_reply llama a on_conn_closed(c, "0x0003") cuando el cliente cierra la sesión.
    g["CLOSE_OLD_DUEL_SOCK"] = False   # cerrar desde el servidor el socket de duelo viejo al llegar uno nuevo: DUDOSO (el
                                       # cliente nunca los cierra; no se sabe qué hace si se le cierra uno). Probar aparte.

    # E1b: PEER_CONNS, current_conn y live_conn viven en yo2srv/red/servidor.py (el MISMO dict PEER_CONNS que antes).
    _de("yo2srv.red.servidor", "PEER_CONNS", "current_conn", "live_conn")
    # E6: on_conn_closed, _abandon_duel y on_conn_accepted viven en yo2srv/duelo/sesion_duelo.py (importados arriba).

    # Reserva de duelo (yo2srv/duelo/reserva.py; 07-10 research/seguridad_duelo: el 0x6001 solo vale con una reserva del pid
    # creada al mandar 0x4306, desde la misma IP). DUEL_RESERVA = modo (vigilar/on/off), DT = las reservas.
    _de("yo2srv.duelo.reserva", "DUEL_RESERVA_MODOS", "_load_tickets", "_duel_reserva_mode", "duel_reserva", "_duel_ticket_issue",
        "_duel_ticket_alive", "duel_reserva_6001")
    importlib.import_module("yo2srv.duelo.reserva").iniciar()

    # Comprobación del mazo del duelo (yo2srv/duelo/mazo.py; 08-10 research/seguridad_duelo/mazo.py): DK y deck_check().
    _de("yo2srv.duelo.mazo", "_load_mazo", "deck_check")
    importlib.import_module("yo2srv.duelo.mazo").iniciar()

    # R4: los módulos que se recargan en caliente (yo2srv/recarga.py) y `hot(...)` en el namespace (consola 5799). Va ANTES del
    # perfil: se anotan los valores «de fábrica» de cada módulo para saber luego qué banderas se cambiaron en vivo. Nunca
    # impide arrancar.
    try:
        importlib.import_module("yo2srv.recarga").registrar_modulos(srv)
    except Exception as _e:
        log(f"[recarga] no se pudieron registrar los módulos de la recarga en caliente: {_e!r}")

    # R2-corto: perfil de banderas (YO2_PERFIL=vanilla|enhanced; precedencia en yo2srv/config.py). Va al final de las cargas, cuando
    # ya existen los sitios (módulos, DEFAULT_CFG, dicts) donde se escribe. Con vanilla y sin admin_banderas.json no escribe nada.
    aplicar_perfil_banderas()


def aplicar_perfil_banderas():
    """Aplica el perfil de banderas (Config.aplicar_perfil) y deja UNA línea en el log. Nunca impide arrancar."""
    try:
        r = srv.cfg.aplicar_perfil()
    except Exception as e:
        srv.log(f"[config] perfil: no se pudo aplicar ({e!r}); el servidor sigue con los valores del código")
        return None
    srv.log(f"[config] perfil {r['perfil']}: {len(r['escritas'])} escritas, {len(r['entorno'])} mandadas por el entorno, "
            f"{len(r['fichero'])} de admin_banderas.json")
    for a in r["avisos"]:
        srv.log(f"[config] aviso: {a}")
    return r


# ------------------------------------------------------------------------------------------------------- escuchar
def escuchar(servidor):
    """Tabla de puertos, consola 5799, panel y los hilos que escuchan. Lo que run.py hacía tras las cargas."""
    setup(servidor)
    g = srv.ns
    import threading

    # Tabla de puertos (E1b): yo2srv/red/servidor.py tabla_puertos() la arma como antes la armaba la sentencia «HANDLERS =
    # {5730 + PORT_OFFSET: gate_reply, GAME_PORT: game_reply, LOBBY_PORT: lobby_reply}» + duelo + salas.
    _de("yo2srv.red.servidor", "tabla_puertos")
    g["HANDLERS"] = g["tabla_puertos"]()
    _de("yo2srv.red.servidor", "CONNS")   # puerto -> última Conn (para la consola de control)

    # Consola de control en 127.0.0.1:5799 (+ YO2_PORT_OFFSET), con token: yo2srv/admin/consola.py (E8).
    _de("yo2srv.admin.consola", "admin", "send")

    # Entrada no confiable (límites por IP y en total, corte por inactividad), serve y _handle: yo2srv/red/servidor.py; el servidor
    # HTTP (WWW, Http, serve_http): yo2srv/red/http.py (E1b). Un import por nombre, a propósito.
    _de("yo2srv.red.servidor", "MAX_CONNS_PER_IP", "MAX_CONNS", "IDLE_TIMEOUT", "_OPEN", "_OPEN_LOCK", "serve", "_handle")
    _de("yo2srv.red.http", "WWW", "Http", "serve_http")
    # Puertos ocupados (E1b: yo2srv/red/servidor.py comprobar_puertos(); sale con el motivo si alguno está en uso).
    _de("yo2srv.red.servidor", "comprobar_puertos")
    g["comprobar_puertos"]()
    threading.Thread(target=g["admin"], daemon=True).start()
    # Moderación del anfitrión (11-10, yo2srv/admin/moderacion.py y gestion.py): los vetos se aplican al entrar SIEMPRE (con o sin
    # el panel de abajo, que ya no es quien los hace cumplir) y la API de gestión del lanzador (pestaña «Manage») escucha en
    # 127.0.0.1:8791 + offset con token en %LOCALAPPDATA%\YO2Online\gestion_<puerto>.txt. Si algo de esto falla, el servidor sigue.
    try:
        from yo2srv.admin import moderacion as _mod
        _mod.setup(srv); _mod.instalar_veto()
    except Exception as _e:
        srv.log(f"[admin] el envoltorio de vetos del login no se instaló: {_e!r}")
    try:
        from yo2srv.admin import gestion as _gestion
        _gestion.iniciar(srv)
    except Exception as _e:
        srv.log(f"[gestion] API de gestión no arrancada: {_e!r}")
    # Panel de administración web (research/admin/panel.py y NOTES.md, 06-10): SOLO 127.0.0.1, puerto 8790 + desplazamiento,
    # token nuevo en cada arranque en %LOCALAPPDATA%\YO2Online\admin_token.txt (abrir con «Panel admin YO2.url»). No cambia
    # nada del juego hasta que el owner pulsa un botón (con la lista de vetos vacía, el enganche de HUB.login no hace nada).
    g["ADMIN_PANEL"] = os.environ.get("YO2_ADMIN_PANEL", "0" if os.environ.get("YO2_PAQUETE", "") not in ("", "0") else "1") not in ("", "0")   # YO2_ADMIN_PANEL=0 lo apaga; P5: en modo paquete (público) apagado: el panel es privado del owner y no va
    if g["ADMIN_PANEL"] and g.get("ADMIN_WEB") is None:
        try:
            _sp = importlib.util.spec_from_file_location("research_admin", os.path.join(srv.HERE, "research", "admin", "panel.py"))
            ADMIN_WEB = g["ADMIN_WEB"] = importlib.util.module_from_spec(_sp); sys.modules[_sp.name] = ADMIN_WEB
            _sp.loader.exec_module(ADMIN_WEB)
            ADMIN_WEB.start(g)
        except Exception as _e:
            srv.log(f"[admin] panel web no arrancado: {_e!r}"); g["ADMIN_WEB"] = None
    for _ip in g["BIND_IPS"]:
        threading.Thread(target=g["serve_http"], args=(_ip,), daemon=True).start()
        for _p in g["HANDLERS"]:
            threading.Thread(target=g["serve"], args=(_p, _ip), daemon=True, name=f"serve-{_p}").start()   # NET_THREADS
    srv.log(f"[run] escuchando en {', '.join(g['BIND_IPS'])} (puertos {g['GATE_PORT']}-{g['LOBBY_PORT']}, {g['ROOM_PORT_BASE']}-"
            f"{g['ROOM_PORT_BASE'] + len(g['ROOM_KINDS']) - 1}"
            + (f", salas de idioma {g['ROOM_LANG_PORT_BASE']}-{max(g['HANDLERS'])}" if g.get("ROOM_PER_LANG") else "")
            + f", {g['DUEL_PORT']}, HTTP {g['HTTP_PORT']}); consola 127.0.0.1:{g['ADMIN_PORT']}; "
            f"state {g['STATE_PATH']}")
    # run_forever, al cumplir DEADLINE, sale del proceso: si no, los accept() de arriba cuelgan el apagado.
    g["_SALIDA_PROCESO"] = True
