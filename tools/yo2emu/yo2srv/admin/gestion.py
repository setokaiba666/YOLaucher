"""API de gestión del anfitrión (11-10): una API JSON pequeña en 127.0.0.1 para que el lanzador (pestaña «Manage» de «Host a
server») modere SU servidor sin panel: jugadores, expulsar, vetar, cuentas, avisos y copias. La lógica está en moderacion.py;
esto es solo la puerta HTTP con token.

    puerto   8791 + YO2_PORT_OFFSET (YO2_GESTION_PORT lo cambia). Distinto del panel (8790): el panel es privado del owner y no
             va en el paquete público, así que el lanzador no puede depender de él. YO2_GESTION=0 la apaga.
    token    nuevo en cada arranque, en %LOCALAPPDATA%\\YO2Online\\gestion_<puerto>.txt (el mismo sitio y patrón que
             consola_<puerto>.txt de consola.py). Cabecera «Authorization: Bearer <token>», comparación en tiempo constante.
             10 tokens malos en un minuto -> 429 durante un minuto (como el panel).
    sin token  GET /publico (lo poco de la ficha pública: jugadores, duelos, perfil, cuentas; el lanzador ya lo leía del panel).
    solo desde 127.0.0.1, con Host 127.0.0.1:<puerto> o localhost:<puerto> (contra DNS rebinding) y sin cabecera Origin
    (una página web no puede llamar: ni manda el token ni pasa el CORS).

Rutas (todas JSON; errores {"error": <clave estable>, "mensaje": <español>} con su código HTTP):
    GET  /gestion/jugadores   conectados ahora: corto (6 hex de la cuenta), nombre, dueno, estado (duelo|sala|mapa|menu), mundo,
                              zona, desde_s, vetado, hueco (personaje activo 0-3 o null), nivel (su LV o null) y dp/mp
                              (sus puntos DUELPASS y MP, o null). Sin IP.
    POST /gestion/expulsar    {acct}            cierra sus conexiones (la cuenta del dueño no)
    POST /gestion/vetar       {acct, motivo}    veto (admin_vetados.json) + expulsión; se aplica al entrar (0x3004 −246)
    GET  /gestion/vetados     [{corto, nombres, motivo, fecha}]
    POST /gestion/quitar_veto {acct}
    GET  /gestion/cuentas     {estricto, max, cuentas: [{corto, usuario, dueno, personajes, fichas, en_linea, vetado}]}
                              personajes = nombres (como siempre); fichas = [{hueco, nombre, nivel, dp, mp}] (12-10)
    POST /gestion/crear       {usuario, clave}  (1-16 caracteres cp1252; la clave no se devuelve ni se registra)
    POST /gestion/cambiar_clave {acct, clave[, usuario]}
    POST /gestion/borrar      {acct, confirmar: true}   solo cuentas vacías y desconectadas
    POST /gestion/nivel       {acct, nivel[, hueco]}   12-10: LV 1-99 de un personaje (moderacion.set_level, lo mismo que el
                              panel): rec["lv"] y, si está conectado con él y fuera de un duelo, 0x4140 al momento. Sin hueco:
                              el personaje con que está conectado o, desconectado, el único que tenga la cuenta.
    POST /gestion/dp          {acct, [dp], [mp], modo[, hueco]}   12-10: puntos DUELPASS y/o MP de un personaje
                              (moderacion.set_points; el panel suma con la misma regla). modo «poner» (0-9999999) o «sumar»
                              (negativo = quitar, nunca por debajo de 0). Si está conectado con él y fuera de un duelo, 0x4140
                              al momento. hueco como en /gestion/nivel. Auditoría «g-dp».
    POST /gestion/aviso       {texto}           aviso de chat de «SISTEMA» (0x4402) a todos los conectados
    POST /gestion/copia       {}                copia de state.json (state.json.bak-admin-<fecha>)
    GET  /gestion/copias      las últimas 20
    GET  /gestion/duelpass    códigos DUELPASS (sin token completo de canje)
    POST /gestion/duelpass/crear {n,dpp,carta,mazo,nota}
    POST /gestion/duelpass/anular {codigo}      solo códigos libres
    GET/POST /gestion/duelpass/inicio            regalos de nuevos personajes
    GET  /gestion/torneo/opciones                eventos soportados por el cliente
    GET/POST /gestion/torneo                     estado y acciones de eventos/CHAMPIONSHIP
    GET  /gestion/eventos/reglas                 catálogo, recompensas y sesión competitiva
    POST /gestion/eventos/regla                  {familia,activo,recompensas:{condicion:carta}}
`acct` = el id corto (>= 6 hex) o completo. La API nunca devuelve el id completo, IP, contraseñas ni el token.

Cada acción (también las rechazadas) deja una línea en admin_auditoria.log (el del panel): fecha, «g-<acción>», destino y
detalle; nunca contraseñas ni el texto de los avisos.
"""
import hmac
import http.server
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse

from . import moderacion as MOD

DEFAULT_PORT = 8791
MAX_BODY = 8 * 1024
AUTH_MAX_FAILS = 10            # 10 fallos en un minuto -> 429 durante un minuto
AUTH_WINDOW = 60.0
TEXTO_MAX = 200                # aviso y motivo

ST = {"srv": None, "port": None, "token": None, "token_file": None}
AUTH_FAILS = []                # horas de los intentos con token malo (último minuto)
AUTH = {"blocked_until": 0.0}
AUTH_LOCK = threading.Lock()
_srv = None


class GestionServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


# ---------------------------------------------------------------- datos de cada ruta
def _texto(a, k, maximo=TEXTO_MAX, obligatorio=True):
    v = a.get(k)
    if v is None or v == "":
        if obligatorio:
            raise MOD.ApiError(f"falta «{k}»", clave="entrada")
        return ""
    if not isinstance(v, str):
        raise MOD.ApiError(f"«{k}» tiene que ser un texto", clave="entrada")
    v = re.sub(r"[\x00-\x1f\x7f]+", " ", v).strip()
    if obligatorio and not v:
        raise MOD.ApiError(f"falta «{k}»", clave="entrada")
    if len(v) > maximo:
        raise MOD.ApiError(f"«{k}»: máximo {maximo} caracteres", clave="entrada")
    return v


def _quien(acct):
    nombres = []
    try:
        nombres = MOD.nombres_de(acct)
    except Exception:                                # noqa: BLE001
        pass
    return f"{', '.join(nombres) or '?'} (cuenta {acct[:6]}…)"


def _estado_crudo():
    """state.json crudo para leer niveles; None si no se puede (servidor sin persistencia, pruebas...)."""
    try:
        return MOD.NS["load_state_raw"]()
    except Exception:                                # noqa: BLE001
        return None


def _puntos(st, acct, hueco, owner=None):
    """{"dp", "mp"} del personaje (o null los dos si no se sabe)."""
    pt = MOD.puntos_guardados(st, acct, hueco, owner) if st is not None else None
    return {"dp": pt["dp"], "mp": pt["mp"]} if pt else {"dp": None, "mp": None}


def jugadores():
    bans = MOD.load_bans()
    ahora = time.time()
    js = MOD.jugadores_basico()
    st = _estado_crudo() if js else None
    out = [{"corto": j["acct"][:6], "nombre": MOD._clip(j["nombre"], 20), "dueno": j["dueno"], "estado": j["estado"],
            "mundo": j["mundo"], "zona": j["zona"], "desde_s": int(ahora - j["desde"]) if j["desde"] else None,
            "vetado": j["acct"] in bans, "hueco": j.get("hueco"),
            "nivel": MOD.nivel_guardado(st, j["acct"], j.get("hueco")) if st is not None else None,
            **_puntos(st, j["acct"], j.get("hueco"))} for j in js]
    out.sort(key=lambda x: (not x["dueno"], x["nombre"].lower()))
    return {"jugadores": out}


def vetados():
    return {"vetados": [{"corto": k[:6], "nombres": [MOD._clip(n, 20) for n in (v.get("nombres") or [])],
                         "motivo": MOD._clip(v.get("motivo"), 200), "fecha": MOD._clip(v.get("fecha"), 20)}
                        for k, v in sorted(MOD.load_bans().items(), key=lambda kv: str(kv[1].get("fecha")), reverse=True)]}


def cuentas():
    r = MOD.cuentas()
    return {"estricto": r["estricto"], "max": r["max"],
            "cuentas": [{"corto": c["corto"], "usuario": c["usuario"], "dueno": c["dueno"],
                         "personajes": [MOD._clip(p["nombre"], 20) for p in c["personajes"]],
                         "fichas": [{"hueco": p["hueco"], "nombre": MOD._clip(p["nombre"], 20), "nivel": p.get("nivel"),
                                     "dp": p.get("dp"), "mp": p.get("mp")} for p in c["personajes"]],
                         "en_linea": c["en_linea"], "vetado": c["vetado"]} for c in r["cuentas"]]}


# Cada acción: (nombre de auditoría, función(a) -> (resultado, destino, detalle)). Se audita también si falla.
def a_expulsar(a):
    acct = MOD.resolver(a.get("acct"))
    _, es_dueno = MOD._account(acct)
    if es_dueno:
        raise MOD.ApiError("la cuenta del dueño no se expulsa", clave="dueno")
    n = MOD.kick(acct, "expulsado desde el lanzador")
    return {"ok": True, "conexiones": n}, _quien(acct), ""


def a_vetar(a):
    acct = MOD.resolver(a.get("acct"))
    motivo = _texto(a, "motivo", obligatorio=False)
    v = MOD.ban(acct, motivo)
    return {"ok": True, "veto": {"corto": acct[:6], "nombres": v["nombres"], "motivo": v["motivo"], "fecha": v["fecha"]}}, \
        _quien(acct), "motivo: " + MOD._clip(motivo, 80)


def a_quitar_veto(a):
    acct = MOD.resolver(a.get("acct"), set(MOD.load_bans()))
    MOD.unban(acct)
    return {"ok": True}, _quien(acct), ""


def a_crear(a):
    r = MOD.crear_cuenta(a.get("usuario"), a.get("clave"))
    return {"ok": True, "corto": r["corto"]}, f"cuenta {r['corto']}…", "cuenta creada"


def a_cambiar_clave(a):
    acct = MOD.resolver(a.get("acct"))
    r = MOD.cambiar_clave(acct, a.get("clave"), a.get("usuario"))
    return {"ok": True, "corto": r["corto"]}, f"cuenta {acct[:6]}…", "contraseña cambiada"


def a_borrar(a):
    acct = MOD.resolver(a.get("acct"))
    MOD.borrar_cuenta(acct, a.get("confirmar") is True)
    return {"ok": True}, f"cuenta {acct[:6]}…", "cuenta vacía borrada"


def _hueco_de(acct, hueco):
    """hueco pedido o, sin él, el personaje con que está conectado; desconectado, el único que tenga la cuenta."""
    if hueco is None:
        s = MOD._session_of(acct)
        if s is not None and MOD._online(s) and isinstance(getattr(s, "slot", None), int):
            hueco = s.slot
        else:
            st = MOD.NS["load_state_raw"]()
            players = st.get("players") if acct == MOD._owner(st) else ((st.get("accounts") or {}).get(acct) or {}).get("players")
            huecos = sorted(int(k) for k in (players or {}))
            if len(huecos) != 1:
                raise MOD.ApiError("di qué personaje (hueco): la cuenta no está conectada y tiene " + str(len(huecos))
                                   + " personajes", clave="sin_personaje" if not huecos else "hueco")
            hueco = huecos[0]
    if isinstance(hueco, bool) or not isinstance(hueco, int) or not 0 <= hueco <= 3:
        raise MOD.ApiError("hueco: de 0 a 3", clave="hueco")
    return hueco


def a_nivel(a):
    """12-10: LV de un personaje. hueco opcional: el activo si está conectado; si no, el único personaje de la cuenta."""
    acct = MOD.resolver(a.get("acct"))
    nivel = a.get("nivel", a.get("lv"))
    hueco = _hueco_de(acct, a.get("hueco"))
    r = MOD.set_level(acct, hueco, nivel)
    return ({"ok": True, "antes": r["antes"], "ahora": r["ahora"], "al_momento": bool(r["aviso_al_momento"]),
             "en_linea": r["en_linea"]}, _quien(acct), f"hueco {hueco}: LV {r['antes']} -> {r['ahora']}")


def a_dp(a):
    """12-10: puntos DUELPASS y/o MP de un personaje (poner o sumar). hueco como en a_nivel."""
    acct = MOD.resolver(a.get("acct"))
    modo = a.get("modo") or "poner"
    hueco = _hueco_de(acct, a.get("hueco"))
    r = MOD.set_points(acct, hueco, a.get("dp"), a.get("mp"), modo)
    det = ", ".join(f"{k.upper()} {r['antes'][k]} -> {r['ahora'][k]}" for k in r["antes"])
    return ({"ok": True, "antes": r["antes"], "ahora": r["ahora"], "al_momento": bool(r["aviso_al_momento"]),
             "en_linea": r["en_linea"]}, _quien(acct), f"hueco {hueco} ({modo}): {det}")


def _state_dir():
    return os.path.dirname(os.path.abspath(MOD.NS["STATE_PATH"]))


def _duelpass():
    m = sys.modules.get("yo2_duelpass")
    if m is None:
        raise MOD.ApiError("DUELPASS no está instalado en este servidor", clave="no_duelpass")
    return m


def duelpass_codigos():
    m = _duelpass()
    rows = []
    for code, entry in m.load(_state_dir()).items():
        rows.append({"codigo": code, "estado": str(entry.get("estado", "libre")),
                     "dpp": int(entry.get("dpp", 0) or 0), "carta": int(entry.get("carta", 0) or 0),
                     "mazo": int(entry.get("mazo", 0) or 0), "creado": entry.get("creado", ""),
                     "canje": entry.get("canje"), "nota": str(entry.get("nota", ""))[:80]})
    rows.sort(key=lambda x: (x["estado"], x["creado"], x["codigo"]), reverse=True)
    return {"codigos": rows}


def duelpass_mazos():
    m = _duelpass()
    pp = MOD.NS.get("PP")
    return {"mazos": [{"id": int(k), "nombre": str(v.get("nombre", "")), "bit": int(v.get("bit", 0))}
                      for k, v in sorted(m.packinfo_decks(pp).items())]} if pp is not None else {"mazos": []}


def duelpass_cartas():
    m = _duelpass()
    pp = MOD.NS.get("PP")
    if pp is None or not hasattr(pp, "valid_card_ids"):
        return {"cartas": []}
    # El nombre es opcional: el ID sigue siendo la identidad canónica del juego.
    nombres = {}
    try:
        cm = MOD.NS.get("OCG").cardmap()
        nombres = getattr(cm, "names", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    return {"cartas": [{"id": int(cid), "nombre": str(nombres.get(cid, ""))}
                       for cid in pp.valid_card_ids() if int(cid) <= m.CARD_MAX]}


def duelpass_inicio():
    m = _duelpass()
    return {"regalos": m.start_values(_state_dir())}


def _entero(a, k, minimo, maximo, por_defecto=0):
    v = a.get(k, por_defecto)
    if isinstance(v, bool) or not isinstance(v, int) or not minimo <= v <= maximo:
        raise MOD.ApiError(f"«{k}» fuera de rango", clave="duelpass_entrada")
    return v


def a_duelpass_crear(a):
    m = _duelpass()
    n = _entero(a, "n", 1, 10000)
    dpp, carta, mazo = _entero(a, "dpp", 0, 255), _entero(a, "carta", 0, 0xffff), _entero(a, "mazo", 0, 0xffff)
    pp = MOD.NS.get("PP")
    if carta and (pp is None or not pp.card_ok(carta) or carta > m.CARD_MAX):
        raise MOD.ApiError("la carta no es válida", clave="duelpass_carta")
    if mazo and (pp is None or mazo not in m.packinfo_decks(pp)):
        raise MOD.ApiError("el starter deck no es válido", clave="duelpass_mazo")
    nota = _texto(a, "nota", 80, obligatorio=False)
    codes = m.create(_state_dir(), n, dpp=dpp, carta=carta, mazo=mazo, nota=nota)
    return {"ok": True, "codigos": codes, "cantidad": len(codes)}, "DUELPASS", f"{len(codes)} códigos creados"


def a_duelpass_anular(a):
    m = _duelpass()
    code = _texto(a, "codigo", 16)
    old = m.void(_state_dir(), code)
    if old is None:
        raise MOD.ApiError("ese código no existe", clave="duelpass_no_existe")
    return {"ok": True, "estado_anterior": old}, "DUELPASS", "código anulado"


def a_duelpass_inicio(a):
    m = _duelpass()
    regalos = {"mp": _entero(a, "mp", 0, m.START_MAX), "dpp": _entero(a, "dpp", 0, m.START_MAX),
               "baul": a.get("baul", "empty")}
    if regalos["baul"] not in m.BAUL_MODES:
        raise MOD.ApiError("modo de baúl inválido", clave="duelpass_entrada")
    p = m.start_path(_state_dir())
    if regalos == {"mp": 0, "dpp": 0, "baul": "empty"}:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        return {"ok": True, "regalos": {}}, "DUELPASS", "regalos iniciales restaurados"
    m._write(p, json.dumps(regalos, ensure_ascii=False, indent=1))
    return {"ok": True, "regalos": regalos}, "DUELPASS", "regalos iniciales actualizados"


def _torneos():
    t = MOD.NS.get("TO")
    if t is None:
        raise MOD.ApiError("torneos no están instalados", clave="no_torneos")
    return t


def _eventos():
    e = MOD.NS.get("ER")
    if e is None:
        raise MOD.ApiError("motor de eventos no está instalado", clave="no_eventos")
    return e


def eventos_reglas():
    e = _eventos()
    return {"familias": e.catalog(), "sesion": e.state()}


def a_eventos_regla(a):
    e, pp = _eventos(), MOD.NS.get("PP")
    family = _texto(a, "familia", 40)
    rewards = a.get("recompensas") or {}
    if not isinstance(rewards, dict):
        raise MOD.ApiError("recompensas inválidas", clave="evento_regla")
    try:
        e.save_rule(family, rewards, bool(a.get("activo", True)),
                    valid_card=(pp.card_ok if pp is not None else None))
    except (ValueError, TypeError) as ex:
        raise MOD.ApiError(str(ex), clave="evento_regla") from ex
    out = next(x for x in torneo_opciones()["eventos"] if x["familia"] == family)
    return {"ok": True, "regla": out}, family, "regla actualizada"


def torneo_opciones():
    starts = []
    for item in _eventos().catalog():
        starts.append({
            "evento": int(item["eventos"][0]), "familia": item["id"], "nombre": item["nombre"],
            "cuadro": item["id"] == "championship", "modalidad": item["modalidad"],
            "contador": item["contador"], "descripcion": item["descripcion"],
            "confirmado": item["confirmado"], "activo": item["activo"],
            "milestones": item["milestones"], "recompensas": item["recompensas"],
        })
    return {"eventos": starts}


def torneo_estado():
    tmod = _torneos()
    sesion = _eventos().state()
    t = tmod.TOUR
    evento = None
    if tmod.CURRENT_EVENT is not None:
        serial, world, state = tmod.CURRENT_EVENT
        if int(state) % 2:
            evento = {"serial": int(serial), "world": int(world), "estado": int(state),
                      "nombre": str(tmod.EVENT_NAMES.get(state, "EVENT"))}
    if t is None:
        return {"activo": False, "estado": -1, "evento": evento, "sesion": sesion,
                "pausado": bool(sesion and sesion.get("estado") == "pausado"), "config": {
            "torneos": bool(tmod.TOURNAMENT_ON), "eventos": bool(tmod.EVENTS_ON),
            "reloj": bool(tmod.AUTO_CLOCK)}}
    return {"activo": int(t.state) != tmod.ST_END, "serial": t.serial, "dia": t.day, "nombre": t.name, "max_jugadores": t.max_players,
            "estado": int(t.state), "evento": evento, "sesion": sesion,
            "pausado": bool(getattr(tmod, "PAUSED", False)) or bool(sesion and sesion.get("estado") == "pausado"),
            "fase": int(t.phase), "ronda": int(t.round),
            "inscritos": len(t.seats), "vivos": len(t.alive()), "espectadores": t.audience(),
            "segundos": t.seconds_left(), "campeon": t.champion, "subcampeon": t.runner_up}


def a_torneo(a):
    tmod, emod = _torneos(), _eventos()
    accion = a.get("accion", "estado")
    if accion == "iniciar":
        evento = _entero(a, "evento", 1, 0x100, tmod.EV_CHAMPIONSHIP_ON)
        serial = _entero(a, "serial", 1, 0xffffffff, 1)
        family = emod.family_for_event(evento)
        if family is None:
            raise MOD.ApiError("evento no soportado", clave="torneo_entrada")
        try:
            emod.start(family, serial)
        except ValueError as ex:
            raise MOD.ApiError(str(ex), clave="evento_regla") from ex
        if evento != tmod.EV_CHAMPIONSHIP_ON:
            if evento not in tmod.EVENT_NAMES or evento % 2 == 0:
                raise MOD.ApiError("evento no soportado", clave="torneo_entrada")
            tmod.push_event(evento, serial=serial)
        else:
            nombre = _texto(a, "nombre", 100)
            dia, maxp = _entero(a, "dia", 1, 0x11, 1), _entero(a, "max_jugadores", 2, 8, 8)
            tmod.push_event(tmod.EV_CHAMPIONSHIP_ON, serial=serial)
            tmod.start_tournament(serial=serial, day=dia, name=nombre, max_players=maxp)
    elif accion == "evento_on":
        if tmod.CURRENT_EVENT is None or int(tmod.CURRENT_EVENT[2]) % 2 == 0:
            raise MOD.ApiError("no hay evento activo para anunciar", clave="torneo_entrada")
        tmod.push_event()
    elif accion == "evento_off":
        if tmod.CURRENT_EVENT is None or int(tmod.CURRENT_EVENT[2]) % 2 == 0:
            raise MOD.ApiError("no hay evento activo para finalizar", clave="torneo_entrada")
        tmod.push_event(int(tmod.CURRENT_EVENT[2]) + 1)
        emod.finish()
    elif accion == "tick":
        tmod.tick()
    elif accion == "pausar":
        if tmod.TOUR is not None:
            tmod.pause_tournament()
        try:
            emod.pause(True)
        except ValueError as ex:
            raise MOD.ApiError(str(ex), clave="torneo_entrada") from ex
    elif accion == "reanudar":
        if tmod.TOUR is not None:
            tmod.resume_tournament()
        try:
            emod.pause(False)
        except ValueError as ex:
            raise MOD.ApiError(str(ex), clave="torneo_entrada") from ex
    elif accion == "finalizar":
        if tmod.TOUR is not None:
            tmod.finish_tournament()
        emod.finish()
        if tmod.CURRENT_EVENT is not None:
            tmod.push_event(int(tmod.CURRENT_EVENT[2]) + 1)
    elif accion != "estado":
        raise MOD.ApiError("acción de torneo desconocida", clave="torneo_entrada")
    return {"ok": True, "torneo": torneo_estado()}, "torneo", accion


def a_aviso(a):
    texto = _texto(a, "texto")
    n = MOD.chat_notice(texto, 0)
    return {"ok": True, "enviado_a": n}, "todos", f"{len(texto)} caracteres, a {n} jugadores"


def a_copia(a):
    f = MOD.backup_state()
    return {"ok": True, "fichero": f}, f, ""


GET = {"/gestion/jugadores": jugadores, "/gestion/vetados": vetados, "/gestion/cuentas": cuentas,
       "/gestion/copias": lambda: {"copias": MOD.backups()}, "/gestion/duelpass": duelpass_codigos,
       "/gestion/duelpass/mazos": duelpass_mazos, "/gestion/duelpass/cartas": duelpass_cartas,
       "/gestion/duelpass/inicio": duelpass_inicio, "/gestion/eventos/reglas": eventos_reglas,
       "/gestion/torneo": torneo_estado,
       "/gestion/torneo/opciones": torneo_opciones}
POST = {"/gestion/expulsar": ("expulsar", a_expulsar), "/gestion/vetar": ("vetar", a_vetar),
        "/gestion/quitar_veto": ("quitar_veto", a_quitar_veto), "/gestion/crear": ("cuenta_crear", a_crear),
        "/gestion/cambiar_clave": ("cuenta_clave", a_cambiar_clave), "/gestion/borrar": ("cuenta_borrar", a_borrar),
        "/gestion/aviso": ("aviso", a_aviso), "/gestion/copia": ("copia", a_copia), "/gestion/nivel": ("nivel", a_nivel),
        "/gestion/dp": ("dp", a_dp), "/gestion/duelpass/crear": ("duelpass_crear", a_duelpass_crear),
        "/gestion/duelpass/anular": ("duelpass_anular", a_duelpass_anular),
        "/gestion/duelpass/inicio": ("duelpass_inicio", a_duelpass_inicio),
        "/gestion/eventos/regla": ("evento_regla", a_eventos_regla),
        "/gestion/torneo": ("torneo", a_torneo)}


# ---------------------------------------------------------------- HTTP
def _auth_blocked():
    with AUTH_LOCK:
        return time.time() < AUTH["blocked_until"]


def _auth_fail():
    now = time.time()
    with AUTH_LOCK:
        AUTH_FAILS[:] = [t for t in AUTH_FAILS if now - t < AUTH_WINDOW] + [now]
        if len(AUTH_FAILS) >= AUTH_MAX_FAILS:
            AUTH["blocked_until"] = now + AUTH_WINDOW
            AUTH_FAILS.clear()
            MOD.log(f"[gestion] {AUTH_MAX_FAILS} intentos con token malo en un minuto; API bloqueada 1 min (429)")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "YO2Gestion"
    sys_version = ""
    timeout = 10                                    # una conexión que no habla se suelta

    def log_message(self, fmt, *a):                 # nada al log: ni rutas ni cabeceras
        pass

    def _hosts_ok(self):
        ok = {f"127.0.0.1:{ST['port']}", f"localhost:{ST['port']}"}
        return (self.headers.get("Host") or "") in ok and self.headers.get("Origin") is None

    def _send(self, code, body, extra=()):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, e):
        self._send(e.code, {"error": e.clave, "mensaje": str(e)})

    def _guard(self):
        if self.client_address[0] != "127.0.0.1" or not self._hosts_ok():
            self._send(403, {"error": "prohibido", "mensaje": "solo desde la propia máquina del servidor (127.0.0.1)"})
            return False
        return True

    def _authed(self):
        h = self.headers.get("Authorization") or ""
        tok = h[7:] if h[:7].lower() == "bearer " else ""
        return bool(ST["token"]) and hmac.compare_digest(tok.encode("utf-8", "replace"), ST["token"].encode())

    def _api_auth(self):
        if _auth_blocked():
            self._send(429, {"error": "bloqueado", "mensaje": "demasiados intentos con token malo: espera un minuto"},
                       extra=(("Retry-After", "60"),))
            return False
        if not self._authed():
            _auth_fail()
            self._send(401, {"error": "sin_token", "mensaje": "token incorrecto o ausente"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/publico":                  # sin token: solo lo de la ficha pública (como la del panel)
                return self._send(200, MOD.public_status())
            f = GET.get(path)
            if f is None:
                return self._send(404, {"error": "no_existe", "mensaje": "no existe"})
            if not self._api_auth():
                return
            self._send(200, f())
        except MOD.ApiError as e:
            self._error(e)
        except Exception as e:                       # noqa: BLE001
            MOD.log(f"[gestion] GET {path}: {e!r} | " + traceback.format_exc().replace("\n", " | "))
            self._send(500, {"error": "interno", "mensaje": f"fallo interno ({type(e).__name__})"})

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        acc = POST.get(path)
        if acc is None:
            return self._send(404, {"error": "no_existe", "mensaje": "no existe"})
        if not self._api_auth():
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "solo_json", "mensaje": "solo JSON"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            return self._send(413, {"error": "grande", "mensaje": "petición demasiado grande"})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "json", "mensaje": "JSON no válido"})
        nombre, fn = acc
        res, err, target, detail = None, None, "-", ""
        try:
            res, target, detail = fn(body)
            self._send(200, res)
        except MOD.ApiError as e:
            err = str(e)
            self._error(e)
        except Exception as e:                       # noqa: BLE001
            err = f"fallo interno {type(e).__name__}"
            MOD.log(f"[gestion] POST {path}: {e!r} | " + traceback.format_exc().replace("\n", " | "))
            self._send(500, {"error": "interno", "mensaje": f"fallo interno ({type(e).__name__})"})
        finally:
            body.pop("clave", None)                 # que nada más abajo la vea
            try:
                MOD.auditar("g-" + nombre, target, (detail + "; " if detail else "") +
                            ("ok" if err is None else "rechazado: " + MOD._clip(err, 100)), ocultar=(ST["token"],))
            except Exception as e:                   # noqa: BLE001
                MOD.log(f"[gestion] auditoría: {e!r}")

    def _no(self):
        self._send(405, {"error": "metodo", "mensaje": "no"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


# ---------------------------------------------------------------- arranque
def puerto(offset=0):
    """8791 + desplazamiento (YO2_GESTION_PORT lo cambia)."""
    try:
        p = int(os.environ.get("YO2_GESTION_PORT") or 0)
    except ValueError:
        p = 0
    return p if p > 0 else DEFAULT_PORT + int(offset or 0)


def token_dir(srv):
    return os.path.join(os.environ.get("LOCALAPPDATA") or srv.HERE, "YO2Online")


def iniciar(servidor):
    """Arranca la API (idempotente). Devuelve el puerto o None si no se pudo; el servidor sigue igual en ese caso."""
    global _srv
    MOD.setup(servidor)
    if ST["srv"] is not None:
        return ST["port"]
    if os.environ.get("YO2_GESTION", "1") in ("", "0"):
        MOD.log("[gestion] API de gestión apagada (YO2_GESTION=0)")
        return None
    port = puerto(servidor.ns.get("PORT_OFFSET", 0))
    t = socket.socket(); t.settimeout(1)
    busy = t.connect_ex(("127.0.0.1", port)) == 0
    t.close()
    if busy:
        MOD.log(f"[gestion] el puerto 127.0.0.1:{port} ya está en uso; la API de gestión no arranca")
        return None
    token = secrets.token_urlsafe(32)
    tfile = os.path.join(token_dir(servidor), f"gestion_{port}.txt")
    try:
        MOD._write_text(tfile, token)
    except OSError as e:
        MOD.log(f"[gestion] no se pudo guardar el token ({e!r}); la API de gestión no arranca")
        return None
    try:
        s = GestionServer(("127.0.0.1", port), Handler)
    except OSError as e:
        MOD.log(f"[gestion] no se pudo abrir 127.0.0.1:{port} ({e!r})")
        return None
    ST.update(srv=s, port=port, token=token, token_file=tfile)
    _srv = servidor
    threading.Thread(target=s.serve_forever, daemon=True, name="gestion").start()
    MOD.log(f"[gestion] API de gestión en http://127.0.0.1:{port}/ (token en {os.path.dirname(tfile)})")
    return port


def detener():
    s = ST.get("srv")
    if s is not None:
        s.shutdown(); s.server_close()
        ST["srv"] = None
