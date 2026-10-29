"""Los módulos que se recargan en caliente, y cómo se vuelve a cablear y qué conserva cada uno (paso R4, REESTRUCTURA.md §2.7).

    arranque.cargar() -> recarga.registrar_modulos(SRV)      (justo antes de aplicar el perfil de banderas)
    python admin.py "hot('lobby.lp')"                        (hot() sin argumentos lista lo registrado)

El motor está en hot.py y no sabe nada de ningún módulo; aquí está todo lo que sí se sabe. Hoy el despacho NO pasa por el
router (R3 no está hecha): cada módulo se cablea como lo cablea `arranque.py` —sus `install(g)`, las entradas de HANDLERS,
los envoltorios, `X.HANDLERS.update(...)` en otros módulos— pero SIEMPRE contra el módulo nuevo y antes de hacerlo visible
(hot.py asigna `srv.ns[...]` al final, de una vez). Reglas de cada gancho:

  keep(nuevo, viejo, srv, tx)   pasa estado vivo (el MISMO objeto: dicts, cerrojos, contadores) al módulo nuevo
  setup(nuevo, viejo, srv, tx)  cablea el nuevo; lo que toque fuera del módulo, por `tx` (se deshace si algo falla)
  post(nuevo, viejo, srv, tx)   tras cambiar sys.modules/ns

Lo de las banderas (valores cambiados en vivo) y las claves de `DEFAULT_CFG` ya lo hace hot.py solo.

Módulos registrados (nombre → ns / sys.modules → qué se conserva, cómo se recablea):

  ia.brain          brain          rehace CardDB.shared() (~0,8 s); post: vacía translator._DECL (guarda la CardDB y `declarable` del
                                   brain de la primera vez). Arrastra a duelo.ocg (hace `from brain import …`).
  duelo.traductor   translator     DEFAULT_CFG cambiado en vivo (hot.py). setup: «Rendirse» (comid_field con las máscaras de
                                   duelfin) en el traductor nuevo, como arranque. Arrastra a duelo.ocg (hace `import translator as TR`):
                                   un duelo en curso sigue con su OcgSession/YO2Duel/TR viejos; el siguiente usa los nuevos.
  duelo.ocg         OCG ocg_duel   _CORE/_CORE_LOCK (el mismo node), _CM, TRACE_START, OcgSession._next_id y PvpSession._next
                                   (si volvieran atrás, duelcore pisaría un duelo vivo con el mismo id). setup: «Rendirse» si el
                                   TR que importó no lo lleva. post: vuelve a sincronizar los contadores.
  duelo.pvp_match   pvp_match      PVP es una instancia con partidas vivas: se CONSERVA y se le reasigna la clase (`Matches` nueva);
                                   los PvpMatch en curso siguen con su clase vieja, las reservas nuevas crean la nueva.
                                   setup: envoltorio de títulos PvP de mundo.mapas sobre el pvp_record nuevo.
  lobby.hub         pvp_hub        HUB es una instancia con sesiones y bloques vivos: se CONSERVA y se le reasigna la clase (`Hub`
                                   nueva, atributos nuevos de __init__ rellenados); el envoltorio de login del panel (hub.login,
                                   instancia) se vuelve a apuntar al método nuevo. REJECTED pasa a la deque nueva. Las Session
                                   viejas siguen con su clase.
  lobby.lp          LP             MEM (rooms, correo, amigos, retos…), LOCK, _LOADED, _MAIL_T. setup: install(g), HANDLERS ← EQ,
                                   TP, TO (en ese orden), envoltorio de chat de mundo.mapas (0x4400), envoltorios de sobres
                                   (0x4802 NPC y carta del PvP): el LP.HANDLERS nuevo queda COMPLETO antes de ser visible.
  lobby.equipos     EQ             LOCK, MEM, PENDING (invitaciones). setup: install(g) (HUB.list5_body) y LP.HANDLERS ← EQ.HANDLERS.
  lobby.intercambio TP             LOCK, TRADES (intercambios en curso; los Trade siguen con su clase). setup: install + LP.HANDLERS +
                                   el envoltorio de TP._execute del panel (sec_ranking_balance._wrap_trade).
  lobby.torneos     TO             LOCK, WATCH, TOUR, CURRENT_EVENT. setup: para el reloj viejo (si no, dos relojes), install (arranca
                                   el nuevo si AUTO_CLOCK) y LP.HANDLERS ← TO.HANDLERS.
  mundo.mapas       MW             _ST (ns y copia de los premios originales de NR), _GM_LOCK. setup: quita los envoltorios del MW
                                   viejo (PP.challenge_rights_due/on_title, pvp_match.pvp_record, LP.HANDLERS[0x4400]) y vuelve
                                   a llamar a install(g). post: NPCS = load_npcs() (como cuando el panel cambia una bandera «mundo»).
  mundo.npcreglas   NR             setup: quita los envoltorios del NR viejo de duelfin (DFM.npc_duel_end, lobby_after_duel_msgs,
                                   PS.npc_rewards/check_titles/rights_due), llama a NR.install(DFM…) y vuelve a sincronizar los
                                   premios que mundo.mapas mete en NR. Si algo ajeno envolvió encima, se niega y se deshace.
  mundo.avatar      AV             sin estado ni cableado (funciones puras y la tabla PBIN).
  tienda.duelpass   DPC            setup: install_start(PP) (envuelve blank_profile/seed_trunk partiendo del original) y
                                   install_game (PH["h_dpauth"], PH_LOBBY[0x8101]); sin DPAUTH_ON, los quita.

NO se recargan (noreload): duelcore (el proceso Node lleva los duelos en curso y DuelCoreError se compara por identidad),
red.proto (todos lo importan con `from … import`), red.conexion (la numeración por (IP, puerto) y las Conn vivas).

U1 (lockstep, persistencia y duelfin son UN módulo por proceso: yo2srv/compat.py `cargar`) permitió registrar estos tres. Todos
guardan el módulo con `sys.modules["research_<área>"]`, que hot.py cambia junto con `srv.ns[...]` (un `compat.cargar(área)`
posterior devuelve el nuevo), y los tres mantienen `DFM.LS is LS` y `DFM.PS is PP`:

  duelo.lockstep    LS research_lockstep   DUEL_CFG vivo (banderas, claves de prueba, `_st`, mazos a mano: hot.py `dicts=`). Arrastra al
                                   traductor y por él a ocg_duel (el traductor hace `from research_lockstep import …`).
                                   setup: `duel_reply_base`, `log`, `DUEL_CFG["stage"]` (el del viejo; 4 de fábrica), el parche
                                   «Rendirse» de `LS.comid_field` (con `srv.DFM.surrender_masks`) y `DFM.LS = nuevo`.
  datos.perfiles    PP research_persistencia   LOCK y _SCHEMA_WARNED (la lista es la misma: el aviso del esquema sale una vez por
                                   proceso). setup: quita los envoltorios de npcreglas, `PH` nuevo (`arranque.crear_ph`) con
                                   wallet/set_wallet/player_data_update, `registrar_ph`, `PP.RAW_STATE`, `DFM.PS = nuevo`,
                                   `cablear_sobre_pp` (DPC.install_start y MW._wrap_pp), `DPC.install_game` si 0x8101 estaba puesto, y
                                   vuelve a poner npcreglas sobre el PP nuevo. NO toca `PP.log` (arranque tampoco: va a stderr).
  duelo.duelfin     DFM research_duelfin   (sin estado de módulo.) setup: `DF` nuevo (`arranque.crear_df`) que hereda el `sess` del
                                   viejo, `cablear_sobre_dfm` (NR.install y «Rendirse», que ya sigue a `srv.DFM`). El DF por sesión
                                   (`s.duel["df"]`) lo cambia `duel_state` (sesion_duelo.py), no la recarga: el duelo que empezó con
                                   el duelfin viejo acaba con él y la sesión pasa al nuevo en cuanto no tiene duelo en curso
                                   (`s.duel["dfm"]` guarda el módulo con el que se hizo). Los envoltorios de npcreglas sobre PP se
                                   rehacen para no apilarse.
"""
import os
import sys
import types

from .hot import SIN

# ---------------------------------------------------------------------------------------------------------- utilidades


def _g(srv, nombre):
    return srv.ns.get(nombre)


def reasignar_clase(obj, clase_nueva, env, tx, anota=None):
    """Deja la instancia VIVA `obj` (HUB, PVP) con la clase del módulo nuevo: mantiene su estado, su identidad y todo lo que
    la tiene referenciada, y sus métodos pasan a ser los nuevos. Los atributos que el __init__ nuevo añade se rellenan con una
    instancia de plantilla. Los envoltorios que alguien dejó en la INSTANCIA (hub.login del panel) guardan el método viejo
    en un valor por defecto: se vuelven a apuntar al nuevo."""
    vieja = type(obj)
    if vieja is clase_nueva:
        return
    añadidos = []
    try:
        plantilla = clase_nueva(env)
        for k, v in vars(plantilla).items():
            if k not in vars(obj):
                obj.__dict__[k] = v
                añadidos.append(k)
    except Exception as e:                                   # noqa: BLE001
        if anota is not None:
            anota(f"[recarga] {clase_nueva.__name__}: no se pudo crear la plantilla de atributos: {e!r}")
    obj.__class__ = clase_nueva

    def volver():
        obj.__class__ = vieja
        for k in añadidos:
            obj.__dict__.pop(k, None)
    tx.undo(volver)
    for k, v in list(vars(obj).items()):
        d = getattr(v, "__defaults__", None)
        metodo = getattr(clase_nueva, k, None)
        if not d or metodo is None or not callable(v):
            continue
        nuevos = tuple(metodo.__get__(obj, clase_nueva)
                       if isinstance(x, types.MethodType) and x.__self__ is obj and x.__func__.__name__ == k else x
                       for x in d)
        if any(a is not b for a, b in zip(nuevos, d)):
            v.__defaults__ = nuevos
            tx.undo(lambda v=v, d=d: setattr(v, "__defaults__", d))


def parche_rendirse(srv, tr, tx=None):
    """«Rendirse» también con ocgcore: el comid_field del traductor `tr` lleva las máscaras de duelfin (lo mismo que arranque.py
    hace con OCG.TR). Idempotente (marca `_surrender_patched`). Sin duelfin cargado no hace nada."""
    g = srv.ns
    if tr is None or g.get("DF") is None or g.get("DFM") is None or getattr(tr, "_surrender_patched", False):
        return False
    orig = tr.comid_field
    tr.comid_field = lambda masks=None, extra=None, tail=None: orig(srv.DFM.surrender_masks(masks), extra, tail)
    tr._surrender_patched = True
    if tx is not None:
        tx.set_item(g, "_tcf", orig)
    else:
        g["_tcf"] = orig
    return True


def meter_handlers(srv, modulo, tx):
    """`LP.HANDLERS.update(X.HANDLERS)` de arranque.py, deshaciendo lo que pise si el grupo se deshace."""
    LP = _g(srv, "LP")
    if LP is None or modulo is None:
        return
    for k, h in modulo.HANDLERS.items():
        tx.set_item(LP.HANDLERS, k, h)


def cablear_sobre_pp(srv, pp, tx):
    """Lo que otros módulos envuelven en la persistencia (para quien recargue PP, ver el docstring): DPC.install_start y el
    envoltorio de mundo.mapas sobre challenge_rights_due/on_title."""
    DPC, MW = _g(srv, "DPC"), _g(srv, "MW")
    if DPC is not None:
        for n in ("blank_profile", "seed_trunk", "_DUELPASS_START_DIR"):
            tx.proteger_attr(pp, n)
        DPC.install_start(pp, os.path.dirname(os.path.abspath(srv.ns["STATE_PATH"])))
    if MW is not None:
        for n in ("challenge_rights_due", "on_title", "_mundo"):
            tx.proteger_attr(pp, n)
        MW._wrap_pp(pp)


def cablear_sobre_dfm(srv, dfm, tx):
    """Lo que otros módulos envuelven en duelfin (para quien recargue DFM): NR.install y «Rendirse» del traductor."""
    NR = _g(srv, "NR")
    if NR is not None:
        _poner_nr(srv, NR, dfm, tx)
    OCG = _g(srv, "OCG")
    if OCG is not None:
        parche_rendirse(srv, getattr(OCG, "TR", None), tx)


def _poner_nr(srv, NR, dfm, tx):
    PS = dfm.PS
    for n in ("npc_duel_end", "lobby_after_duel_msgs", "_npcreglas"):
        tx.proteger_attr(dfm, n)
    for n in ("npc_rewards", "check_titles", "rights_due"):
        tx.proteger_attr(PS, n)
    NR.install(dfm, lambda: srv.ns.get("NPC_RULES_2008"), srv.ns["log"])


def _quitar_nr(viejo, dfm, tx):
    """Quita los envoltorios que NR.install dejó en duelfin (si son los del NR viejo y nadie envolvió encima)."""
    st = getattr(dfm, "_npcreglas", None)
    if not st:
        return
    PS = dfm.PS
    envs = [dfm.npc_duel_end, dfm.lobby_after_duel_msgs, PS.npc_rewards, PS.check_titles, PS.rights_due]
    if viejo is None or not all(getattr(f, "__globals__", None) is viejo.__dict__ for f in envs):
        raise RuntimeError("duelfin tiene un envoltorio de npcreglas que no es del módulo cargado (o algo envuelto encima): "
                           "no se puede recargar npcreglas sin pisarlo")
    for n in ("npc_duel_end", "lobby_after_duel_msgs", "_npcreglas"):
        tx.proteger_attr(dfm, n)
    for n in ("npc_rewards", "check_titles", "rights_due"):
        tx.proteger_attr(PS, n)
    dfm.npc_duel_end, dfm.lobby_after_duel_msgs = st["orig_end"], st["orig_lob"]
    PS.npc_rewards, PS.check_titles, PS.rights_due = st["orig_rewards"], st["orig_titles"], st["orig_rights"]
    del dfm._npcreglas


def _heredar_mem(nuevo, viejo, nombre="MEM"):
    """El dict de estado del módulo viejo pasa al nuevo (el MISMO objeto: lo que ya lo tiene referenciado lo sigue viendo);
    las claves que el módulo nuevo añade por defecto se completan."""
    n, v = getattr(nuevo, nombre, None), getattr(viejo, nombre, None)
    if isinstance(n, dict) and isinstance(v, dict):
        for k, x in n.items():
            v.setdefault(k, x)
        setattr(nuevo, nombre, v)


def _heredar(nuevo, viejo, *nombres):
    for n in nombres:
        if hasattr(viejo, n):
            setattr(nuevo, n, getattr(viejo, n))


# ------------------------------------------------------------------------------------------------------------ ia.brain
def _keep_brain(nuevo, viejo, srv, tx):
    try:
        nuevo.CardDB.shared()                               # cards.cdb con la clase nueva (~0,8 s), como hacía reload_ocg
    except Exception as e:                                  # noqa: BLE001
        srv.log(f"[recarga] ia.brain: CardDB.shared() del módulo nuevo falló ({e!r}); se cargará con el primer duelo")


def _post_brain(nuevo, viejo, srv, tx):
    """El traductor guarda en `_DECL` la CardDB y `declarable` del brain con el que se usó la primera vez (B42): se vacía para que
    la siguiente comprobación los coja del módulo nuevo (el traductor en sí no se recarga, no importa brain al cargarse)."""
    decl = getattr(sys.modules.get("translator"), "_DECL", None)
    if isinstance(decl, dict):
        decl.clear()


# ------------------------------------------------------------------------------------------------------ duelo.traductor
def _setup_traductor(nuevo, viejo, srv, tx):
    parche_rendirse(srv, nuevo, tx)


# ------------------------------------------------------------------------------------------------------------ duelo.ocg
def _sync_ids(nuevo, viejo):
    for clase, attr in (("OcgSession", "_next_id"), ("PvpSession", "_next")):
        c_v, c_n = getattr(viejo, clase, None), getattr(nuevo, clase, None)
        if hasattr(c_v, attr) and hasattr(c_n, attr):
            setattr(c_n, attr, max(getattr(c_v, attr), getattr(c_n, attr)))


def _keep_ocg(nuevo, viejo, srv, tx):
    lock = getattr(viejo, "_CORE_LOCK", None)
    if lock is not None:
        with lock:
            nuevo._CORE = getattr(viejo, "_CORE", None)
        nuevo._CORE_LOCK = lock
    for k in ("_CM", "TRACE_START"):
        if getattr(viejo, k, None) is not None:
            setattr(nuevo, k, getattr(viejo, k))
    _sync_ids(nuevo, viejo)


def _setup_ocg(nuevo, viejo, srv, tx):
    parche_rendirse(srv, getattr(nuevo, "TR", None), tx)


def _post_ocg(nuevo, viejo, srv, tx):
    if viejo is None:
        return
    _sync_ids(nuevo, viejo)                                 # entre keep y el cambio pudo empezar un duelo con el viejo
    if getattr(nuevo, "_CORE", None) is None and getattr(viejo, "_CORE", None) is not None:
        nuevo._CORE = viejo._CORE


# ------------------------------------------------------------------------------------------------------ duelo.lockstep
def _setup_lockstep(nuevo, viejo, srv, tx):
    """Lo que arranque.py le hace a lockstep (bloque «Duelo con servidor autoritativo» y bloque de duelfin), sobre el nuevo.
    El traductor y ocg_duel (dependientes) ya se ejecutaron con `from research_lockstep import …` del nuevo; el «Rendirse» de
    SU referencia a comid_field lo pone `_setup_traductor`."""
    g = srv.ns
    nuevo.duel_reply_base = g["duel_reply_base"]
    nuevo.log = g["log"]
    nuevo.DUEL_CFG["stage"] = viejo.DUEL_CFG.get("stage", 4) if viejo is not None else 4
    DFM = g.get("DFM")
    if DFM is not None:
        tx.set_attr(DFM, "LS", nuevo)                       # DFM.LS is LS (duelfin pidió el módulo al cargarse)
        if g.get("DF") is not None:                         # el mismo requisito que arranque.py
            _cf = nuevo.comid_field
            tx.set_item(g, "_cf", _cf)
            nuevo.comid_field = lambda masks=None, extra=None, tail=None: _cf(srv.DFM.surrender_masks(masks), extra, tail)


# ---------------------------------------------------------------------------------------------------- duelo.pvp_match
def _setup_pvp_match(nuevo, viejo, srv, tx):
    MW = _g(srv, "MW")
    if MW is not None:
        MW._wrap_pvp(nuevo)                                 # títulos tras un PvP (mundo.mapas) sobre el pvp_record nuevo


def _post_pvp_match(nuevo, viejo, srv, tx):
    PVP = _g(srv, "PVP")
    if PVP is not None and hasattr(nuevo, "Matches"):
        reasignar_clase(PVP, nuevo.Matches, srv.ns, tx, srv.log)


# ---------------------------------------------------------------------------------------------------- datos.perfiles
def _keep_perfiles(nuevo, viejo, srv, tx):
    _heredar(nuevo, viejo, "LOCK", "_SCHEMA_WARNED")


def _soltar_npcreglas_de_ps(NR, viejo_dfm, tx):
    """Quita de PP (`dfm.PS`) SOLO los tres envoltorios de npcreglas, dejando los de `dfm` (npc_duel_end…): para un duelfin
    nuevo que va a volver a instalar npcreglas sobre el mismo PP sin apilar envoltorios, mientras los duelos que siguen con el
    duelfin viejo conservan sus reglas."""
    st = getattr(viejo_dfm, "_npcreglas", None)
    if not st:
        return
    PS = viejo_dfm.PS
    envs = [PS.npc_rewards, PS.check_titles, PS.rights_due]
    if NR is None or not all(getattr(f, "__globals__", None) is NR.__dict__ for f in envs):
        raise RuntimeError("persistencia tiene un envoltorio de npcreglas que no es del módulo cargado (o algo envuelto encima): "
                           "no se puede recargar sin pisarlo")
    for n in ("npc_rewards", "check_titles", "rights_due"):
        tx.proteger_attr(PS, n)
    PS.npc_rewards, PS.check_titles, PS.rights_due = st["orig_rewards"], st["orig_titles"], st["orig_rights"]


def _setup_perfiles(nuevo, viejo, srv, tx):
    """Lo que arranque.py hace con persistencia (PH, wallet…, RAW_STATE) más todo lo que otros módulos envuelven en ella."""
    from yo2srv import arranque
    g = srv.ns
    DFM, NR, DPC = g.get("DFM"), g.get("NR"), g.get("DPC")
    nr_puesto = DFM is not None and NR is not None and g.get("DF") is not None and getattr(DFM, "_npcreglas", None)
    if nr_puesto:
        _quitar_nr(NR, DFM, tx)                             # con DFM.PS todavía = el PP viejo (restaura sus tres funciones)
    PH = arranque.crear_ph(nuevo)
    tx.set_item(g, "PH", PH)
    for n in ("wallet", "set_wallet", "player_data_update"):
        tx.set_item(g, n, PH[n])
    tx.proteger_item(g["PH_GAME"], 0x3110)
    g["registrar_ph"](PH)                                   # 0x3110 -> PH["h_dp_saldo"]
    nuevo.RAW_STATE = lambda: srv.load_state_raw()
    if DFM is not None:
        tx.set_attr(DFM, "PS", nuevo)                       # DFM.PS is PP
    cablear_sobre_pp(srv, nuevo, tx)
    PHL = g.get("PH_LOBBY")
    if DPC is not None and PHL is not None and PHL.get(0x8101) == "h_dpauth":      # DUELPASS puesto: sobre el PP/PH nuevos
        tx.proteger_item(PHL, 0x8101)
        DPC.install_game({**g, "PP": nuevo, "PH": PH})
    if nr_puesto:
        _poner_nr(srv, NR, DFM, tx)                         # los tres envoltorios sobre el PP nuevo, y los de duelfin otra vez


# ----------------------------------------------------------------------------------------------------------- duelo.duelfin
def _setup_duelfin(nuevo, viejo, srv, tx):
    from yo2srv import arranque
    g = srv.ns
    if viejo is not None:
        _soltar_npcreglas_de_ps(g.get("NR"), viejo, tx)
    DF = arranque.crear_df(nuevo)
    ant = g.get("DF")
    if isinstance(ant, dict) and isinstance(ant.get("sess"), dict) and isinstance(DF.get("sess"), dict):
        DF["sess"].update(ant["sess"])                      # el duelo «global» (sin sesión: consola, ocg_test) que esté en curso
    tx.set_item(g, "DF", DF)
    cablear_sobre_dfm(srv, nuevo, tx)                       # NR.install sobre el duelfin nuevo + «Rendirse»


# --------------------------------------------------------------------------------------------------------- lobby.hub
def _keep_hub(nuevo, viejo, srv, tx):
    for x in list(getattr(viejo, "REJECTED", ())):
        nuevo.REJECTED.append(x)


def _post_hub(nuevo, viejo, srv, tx):
    HUB = _g(srv, "HUB")
    if HUB is not None and hasattr(nuevo, "Hub"):
        reasignar_clase(HUB, nuevo.Hub, srv.ns, tx, srv.log)


# ---------------------------------------------------------------------------------------------------------- lobby.lp
def _keep_lp(nuevo, viejo, srv, tx):
    _heredar(nuevo, viejo, "LOCK", "_LOADED", "_MAIL_T")
    _heredar_mem(nuevo, viejo)


def _setup_lp(nuevo, viejo, srv, tx):
    g = srv.ns
    nuevo.install(g)                                        # NS (el dict vivo) y state.json["lobby"], como arranque
    for n in ("EQ", "TP", "TO"):                            # LP.HANDLERS.update(...) en el orden de arranque
        m = g.get(n)
        if m is not None:
            nuevo.HANDLERS.update(m.HANDLERS)
    MW = g.get("MW")
    if MW is not None:
        MW._wrap_chat(nuevo)                                # 0x4400: sin «@n» de color para quien no es GM
    from yo2srv.tienda import sobres
    sobres.instalar_npc(nuevo)                              # 0x4802: carta de NPC, y encima la carta del PvP
    sobres.pvp_card_install(nuevo)


# ------------------------------------------------------------------------------------------- equipos / intercambio / torneos
def _keep_equipos(nuevo, viejo, srv, tx):
    _heredar(nuevo, viejo, "LOCK", "PENDING")
    _heredar_mem(nuevo, viejo)


def _setup_equipos(nuevo, viejo, srv, tx):
    hub = _g(srv, "HUB")
    if hub is not None:
        tx.proteger_attr(hub, "list5_body")                 # install() lo sustituye por el del módulo nuevo
    nuevo.install(srv.ns)
    meter_handlers(srv, nuevo, tx)


def _keep_intercambio(nuevo, viejo, srv, tx):
    _heredar(nuevo, viejo, "LOCK", "TRADES")


def _setup_intercambio(nuevo, viejo, srv, tx):
    nuevo.install(srv.ns)
    meter_handlers(srv, nuevo, tx)
    rb = sys.modules.get("yo2_sec_ranking_balance")           # panel (Ranking y balance): envuelve TP._execute
    if rb is not None and hasattr(rb, "_wrap_trade"):
        rb._wrap_trade(nuevo)


def _keep_torneos(nuevo, viejo, srv, tx):
    _heredar(nuevo, viejo, "LOCK", "WATCH", "TOUR", "CURRENT_EVENT", "PAUSED")


def _parar_reloj(viejo, tx):
    """El reloj del torneo es un hilo con el módulo viejo: se apaga antes de arrancar el nuevo (dos relojes harían dos ticks)."""
    th = getattr(viejo, "_CLOCK", None)
    if th is None or not th.is_alive():
        return
    ant = viejo.AUTO_CLOCK
    viejo.AUTO_CLOCK = False                                # su bucle es «while AUTO_CLOCK»
    th.join(2.5)

    def volver():
        viejo.AUTO_CLOCK = ant
        viejo.start_clock()
    tx.undo(volver)


def _setup_torneos(nuevo, viejo, srv, tx):
    if viejo is not None:
        _parar_reloj(viejo, tx)
    nuevo.install(srv.ns)                                   # arranca el reloj si AUTO_CLOCK
    meter_handlers(srv, nuevo, tx)


# ------------------------------------------------------------------------------------------------------- mundo.mapas
def _quitar_envoltorios_mundo(srv, viejo, tx):
    g = srv.ns

    def nuestro(f):
        return getattr(f, "__globals__", None) is viejo.__dict__

    PP = g.get("PP")
    if PP is not None and getattr(PP, "_mundo", None):
        if not (nuestro(PP.challenge_rights_due) and nuestro(PP.on_title)):
            raise RuntimeError("PP.challenge_rights_due/on_title llevan algo envuelto encima del envoltorio de mundo")
        o = PP._mundo
        PP.challenge_rights_due, PP.on_title = o["due"], o["title"]
        del PP._mundo
    pm = g.get("pvp_match")
    if pm is not None and getattr(pm, "_mundo", None):
        if not nuestro(pm.pvp_record):
            raise RuntimeError("pvp_match.pvp_record lleva algo envuelto encima del envoltorio de mundo")
        pm.pvp_record = pm._mundo["pvp_record"]
        del pm._mundo
    LP = g.get("LP")
    h = LP.HANDLERS.get(0x4400) if LP is not None else None
    if h is not None and getattr(h, "_gm", False):
        if not nuestro(h):
            raise RuntimeError("LP.HANDLERS[0x4400] lleva algo envuelto encima del envoltorio de mundo")
        LP.HANDLERS[0x4400] = h._orig


def _setup_mundo(nuevo, viejo, srv, tx):
    g = srv.ns
    PP, pm, LP = g.get("PP"), g.get("pvp_match"), g.get("LP")
    for obj, nombres in ((PP, ("challenge_rights_due", "on_title", "_mundo")), (pm, ("pvp_record", "_mundo"))):
        if obj is not None:
            for n in nombres:
                tx.proteger_attr(obj, n)
    if LP is not None:
        tx.proteger_item(LP.HANDLERS, 0x4400)
    if viejo is not None:
        _quitar_envoltorios_mundo(srv, viejo, tx)
    nuevo.install(g)


def _keep_mundo(nuevo, viejo, srv, tx):
    nuevo._ST.update(viejo._ST)                             # ns y la copia de los premios originales de NR (nr_orig)
    _heredar(nuevo, viejo, "_GM_LOCK")


def _post_mundo(nuevo, viejo, srv, tx):
    f = _g(srv, "load_npcs")
    if callable(f):                                         # igual que el panel al cambiar una bandera «mundo»
        tx.set_item(srv.ns, "NPCS", f())


# ----------------------------------------------------------------------------------------------------- mundo.npcreglas
def _setup_npcreglas(nuevo, viejo, srv, tx):
    g = srv.ns
    DFM = g.get("DFM")
    if g.get("DF") is not None and DFM is not None:         # el mismo requisito que arranque.py
        _quitar_nr(viejo, DFM, tx)
        _poner_nr(srv, nuevo, DFM, tx)
    MW = g.get("MW")
    if MW is not None:                                      # los premios que mundo.mapas mete en NR: sobre el NR nuevo
        ant = MW._ST.get("nr_orig", SIN)

        def volver():
            if ant is SIN:
                MW._ST.pop("nr_orig", None)
            else:
                MW._ST["nr_orig"] = ant
            if viejo is not None:
                MW._sync_prizes(MW._cfg()["prizes"], NR=viejo)
        tx.undo(volver)
        MW._ST.pop("nr_orig", None)                         # que tome como «original» el NR nuevo, recién cargado
        MW._sync_prizes(MW._cfg()["prizes"], NR=nuevo)


# --------------------------------------------------------------------------------------------------- tienda.duelpass
def _setup_duelpass(nuevo, viejo, srv, tx):
    g = srv.ns
    PP = g.get("PP")
    if PP is not None:
        for n in ("blank_profile", "seed_trunk", "_DUELPASS_START_DIR"):
            tx.proteger_attr(PP, n)
        nuevo.install_start(PP, os.path.dirname(os.path.abspath(g["STATE_PATH"])))
    PH, PHL = g.get("PH"), g.get("PH_LOBBY")
    if PH is None or PHL is None:
        return
    tx.proteger_item(PH, "h_dpauth")
    tx.proteger_item(PHL, 0x8101)
    if nuevo.DPAUTH_ON:
        nuevo.install_game(g)
    elif PHL.get(0x8101) == "h_dpauth":                     # apagado en vivo: 0x8101 vuelve a ser de auto_replies
        PHL.pop(0x8101, None)
        PH.pop("h_dpauth", None)


# ---------------------------------------------------------------------------------------------------------- registro
def registrar_modulos(srv):
    """Registra en srv.hot lo que se recarga en caliente y deja `hot` en el namespace (la consola). Llamar cuando ya están
    cargados todos los módulos y ANTES de aplicar el perfil de banderas (se anotan los valores «de fábrica» de cada uno)."""
    hot, g = srv.hot, srv.ns
    emu = g.get("HERE") or srv.rutas.emu

    def P(*a):
        return os.path.join(emu, *a)

    # los que NO se recargan, con el motivo
    hot.registrar("duelcore", P("duelcore", "duelcore.py"), sys_nombre="duelcore", noreload=True,
                  motivo="el proceso Node lleva los duelos en curso y ocg_duel compara DuelCoreError por identidad")
    hot.registrar("red.proto", P("yo2srv", "red", "proto.py"), sys_nombre="yo2srv.red.proto", noreload=True,
                  motivo="cabecera XOR/MD5 y escritor: todos lo importan con from … import")
    hot.registrar("red.conexion", P("yo2srv", "red", "conexion.py"), sys_nombre="yo2srv.red.conexion", noreload=True,
                  motivo="la numeración por (IP, puerto) vive ahí y las Conn en uso son instancias de su clase")
    # duelo (el orden es el de dependencia: lockstep, brain y traductor antes que ocg)
    hot.registrar("duelo.lockstep", P("research", "lockstep", "proposal.py"), ns_nombre="LS", sys_nombre="research_lockstep",
                  dependientes=("duelo.traductor",), dicts=("DUEL_CFG",), setup=_setup_lockstep,
                  parches=("comid_field", "duel_reply_base", "log"), cargado=True)
    hot.registrar("ia.brain", P("research", "ia", "brain.py"), sys_nombre="brain", dependientes=("duelo.ocg",),
                  keep=_keep_brain, post=_post_brain, cargado=True)
    hot.registrar("duelo.traductor", P("research", "traductor", "translator.py"), sys_nombre="translator",
                  dependientes=("duelo.ocg",), dicts=("DEFAULT_CFG",), trcfg=True, setup=_setup_traductor,
                  parches=("comid_field",), cargado=True)
    hot.registrar("duelo.ocg", P("ocg_duel.py"), ns_nombre="OCG", sys_nombre="ocg_duel", keep=_keep_ocg, setup=_setup_ocg,
                  post=_post_ocg, cargado=True)
    hot.registrar("duelo.pvp_match", P("pvp_match.py"), ns_nombre="pvp_match", sys_nombre="pvp_match",
                  setup=_setup_pvp_match, post=_post_pvp_match, parches=("pvp_record",), cargado=True)
    # persistencia y fin de duelo (perfiles antes que duelfin: duelfin pide persistencia con compat.cargar)
    hot.registrar("datos.perfiles", P("research", "persistencia", "proposal.py"), ns_nombre="PP",
                  sys_nombre="research_persistencia", keep=_keep_perfiles, setup=_setup_perfiles,
                  parches=("challenge_rights_due", "on_title", "blank_profile", "seed_trunk", "npc_rewards", "check_titles",
                           "rights_due"), cargado=True)
    hot.registrar("duelo.duelfin", P("research", "duelfin", "proposal.py"), ns_nombre="DFM", sys_nombre="research_duelfin",
                  setup=_setup_duelfin, parches=("npc_duel_end", "lobby_after_duel_msgs"), cargado=True)
    # lobby
    hot.registrar("lobby.hub", P("pvp_hub.py"), ns_nombre="pvp_hub", sys_nombre="pvp_hub", keep=_keep_hub, post=_post_hub,
                  cargado=True)
    hot.registrar("lobby.lp", P("research", "lobby", "proposal.py"), ns_nombre="LP", sys_nombre="research_lobby",
                  keep=_keep_lp, setup=_setup_lp, cargado=True)
    hot.registrar("lobby.equipos", P("research", "equipos", "proposal.py"), ns_nombre="EQ", sys_nombre="research_equipos",
                  keep=_keep_equipos, setup=_setup_equipos, cargado=True)
    hot.registrar("lobby.intercambio", P("research", "intercambio", "proposal.py"), ns_nombre="TP",
                  sys_nombre="research_intercambio", keep=_keep_intercambio, setup=_setup_intercambio,
                  parches=("_execute",), cargado=True)
    hot.registrar("lobby.torneos", P("research", "torneos", "proposal.py"), ns_nombre="TO", sys_nombre="research_torneos",
                  keep=_keep_torneos, setup=_setup_torneos, cargado=True)
    # mundo y tienda
    hot.registrar("mundo.mapas", P("research", "mundo", "proposal.py"), ns_nombre="MW", sys_nombre="research_mundo",
                  keep=_keep_mundo, setup=_setup_mundo, post=_post_mundo, cargado=True)
    hot.registrar("mundo.npcreglas", P("research", "npcreglas", "proposal.py"), ns_nombre="NR",
                  sys_nombre="research_npcreglas", setup=_setup_npcreglas, cargado=True)
    hot.registrar("mundo.avatar", P("research", "avatar", "proposal.py"), ns_nombre="AV", sys_nombre="research_avatar",
                  cargado=True)
    hot.registrar("tienda.duelpass", P("research", "persistencia", "duelpass.py"), ns_nombre="DPC", sys_nombre="yo2_duelpass",
                  setup=_setup_duelpass, cargado=True)
    srv.registrar_nombres(hot=_consola_hot(srv))
    return hot


def _consola_hot(srv):
    """`hot(...)` de la consola 5799: python admin.py "hot('lobby.lp')" / "hot('duelo.traductor', 'duelo.ocg')" / "hot()"."""
    def hot(*nombres):
        if not nombres:
            r = srv.hot.listar()
            for n, d in r.items():
                srv.log(f"[hot] {n:18s} " + ("NO se recarga: " + d["motivo"] if d["noreload"] else
                                              f"sha={d['sha']}" + (f" (el disco ya es otro: {d['disco']})" if d["cambiado"] else "")))
            return r
        return srv.hot.recargar(*nombres)
    return hot
