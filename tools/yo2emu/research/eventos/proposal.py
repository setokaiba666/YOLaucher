"""Sesiones, progreso y premios de eventos de YO2 Ver.080912.

EVENTINFO (0x5000) solo muestra el evento en el cliente. Este módulo conserva la
sesión competitiva y decide los premios. Las reglas dudosas no se ejecutan.
"""
import json
import os
import tempfile
import threading
import time


FAMILIES = {
    "gunslinger": {
        "nombre": "GUNSLINGER", "eventos": (1, 3, 9, 0x0D), "salas": (7, 15),
        "modalidad": "Single Duel", "contador": "racha",
        "descripcion": "Win consecutive Single Duels. Rewards are earned at 5, 6, and 7 wins in the same streak. A loss or draw resets the streak; a new streak can earn the rewards again.",
        "milestones": (5, 6, 7), "ciclo": 0, "confirmado": True,
    },
    "duelist_trial": {
        "nombre": "DUELIST TRIAL", "eventos": (5, 0x0F), "salas": (9, 16),
        "modalidad": "Single Duel", "contador": "victorias",
        "descripcion": "Win Single Duels during the event. Rewards are earned on the 4th and 5th accumulated wins. After the 5th win, the counter starts a new five-win cycle.",
        "milestones": (4, 5), "ciclo": 5, "confirmado": True,
    },
    "anniversary": {
        "nombre": "ANNIVERSARY TRIAL", "eventos": (7,), "salas": (10,),
        "modalidad": "Single Duel", "contador": "victorias",
        "descripcion": "Win Single Duels to build an edition-wide accumulated-win total. Configure a reward card for each supported milestone; progress remains until this event edition ends.",
        "milestones": tuple(range(10, 101, 10)), "ciclo": 0, "confirmado": True,
    },
    "championship": {
        "nombre": "CHAMPIONSHIP", "eventos": (0x0B,), "salas": (8,),
        "modalidad": "Single-elimination bracket", "contador": "clasificacion",
        "descripcion": "Open registration for 2, 4, or 8 players, then advance the single-elimination bracket round by round. The runner-up and champion receive their configured rewards when the bracket is completed.",
        "milestones": ("subcampeon", "campeon"), "ciclo": 0, "confirmado": True,
    },
    "evolution": {
        "nombre": "EVOLUTION MATCH", "eventos": (0x11,), "salas": (17,),
        "modalidad": "Match", "contador": "estrellas",
        "descripcion": "Play Matches to collect stars. Winning the 1st, 2nd, or 3rd duel of a Match awards 2, 4, or 6 stars respectively. Stars from won duels are kept even if the Match is lost. Rewards are earned at 10, 20, and 30 stars.",
        "milestones": (10, 20, 30), "ciclo": 0, "confirmado": True,
    },
    "ultimate": {
        "nombre": "ULTIMATE MATCH", "eventos": (0x15,), "salas": (20,),
        "modalidad": "Match", "contador": "estrellas",
        "descripcion": "This event appears to accumulate up to 100 stars, but its original YO2 star formula and relationship with LAST BATTLE have not been confirmed. Starting it and configuring rewards remain disabled.",
        "milestones": tuple(range(10, 101, 10)), "ciclo": 0, "confirmado": False,
    },
}

BY_EVENT = {event: key for key, item in FAMILIES.items() for event in item["eventos"]}
BY_ROOM = {room: key for key, item in FAMILIES.items() for room in item["salas"]}
LOCK = threading.RLock()
NS = {}
SESSION = None
FILE_NAME = "admin_event_rules.json"


def install(ns):
    global NS
    NS = ns if isinstance(ns, dict) else dict(ns)
    _load_session()
    _log("[eventos] motor instalado")


def _log(text):
    (NS.get("log") or print)(text)


def _path():
    state = NS.get("STATE_PATH")
    base = os.path.dirname(os.path.abspath(state)) if state else os.getcwd()
    return os.path.join(base, FILE_NAME)


def _defaults():
    return {
        "version": 2,
        "reglas": {
            key: {"activo": bool(item["confirmado"]),
                  "recompensas": {str(m): 0 for m in item["milestones"]}}
            for key, item in FAMILIES.items()
        },
        "sesion": None,
    }


def load_config():
    data = _defaults()
    try:
        with open(_path(), encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key, value in (raw.get("reglas") or {}).items():
                if key in data["reglas"] and isinstance(value, dict):
                    data["reglas"][key].update(value)
            if isinstance(raw.get("sesion"), dict):
                data["sesion"] = raw["sesion"]
            if int(raw.get("version", 1) or 1) < 2:
                data["reglas"]["evolution"]["activo"] = True
    except (OSError, ValueError, TypeError):
        pass
    return data


def save_config(data):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eventos-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_session():
    global SESSION
    with LOCK:
        SESSION = load_config().get("sesion")


def catalog():
    cfg = load_config()["reglas"]
    out = []
    for key, item in FAMILIES.items():
        out.append({
            "id": key, "nombre": item["nombre"], "eventos": list(item["eventos"]),
            "salas": list(item["salas"]), "modalidad": item["modalidad"],
            "contador": item["contador"], "descripcion": item["descripcion"],
            "milestones": list(item["milestones"]), "confirmado": item["confirmado"],
            "activo": bool(cfg[key].get("activo", item["confirmado"])),
            "recompensas": dict(cfg[key].get("recompensas") or {}),
        })
    return out


def save_rule(family, rewards, active=True, valid_card=None):
    if family not in FAMILIES:
        raise ValueError("evento no soportado")
    item = FAMILIES[family]
    if active and not item["confirmado"]:
        raise ValueError("la mecánica no está confirmada para YO2 Ver.080912")
    allowed = {str(x) for x in item["milestones"]}
    clean = {}
    for milestone, card in (rewards or {}).items():
        key, cid = str(milestone), int(card or 0)
        if key not in allowed:
            raise ValueError("condición de premio no válida")
        if cid and valid_card is not None and not valid_card(cid):
            raise ValueError("carta no válida")
        clean[key] = cid
    cfg = load_config()
    cfg["reglas"][family] = {"activo": bool(active), "recompensas": clean}
    save_config(cfg)
    return next(x for x in catalog() if x["id"] == family)


def start(family, serial=1):
    global SESSION
    if family not in FAMILIES:
        raise ValueError("evento no soportado")
    rule = load_config()["reglas"][family]
    if not FAMILIES[family]["confirmado"] or not rule.get("activo"):
        raise ValueError("la mecánica de este evento no está habilitada")
    with LOCK:
        SESSION = {
            "familia": family, "serial": max(1, int(serial)), "estado": "activo",
            "inicio": int(time.time()), "fin": None, "progreso": {},
        }
        cfg = load_config()
        cfg["sesion"] = dict(SESSION)
        save_config(cfg)
        return dict(SESSION)


def pause(paused=True):
    if SESSION is None or SESSION.get("estado") == "finalizado":
        raise ValueError("no hay evento activo")
    with LOCK:
        SESSION["estado"] = "pausado" if paused else "activo"
        _persist_session()
        return dict(SESSION)


def finish():
    if SESSION is None:
        return None
    with LOCK:
        SESSION["estado"], SESSION["fin"] = "finalizado", int(time.time())
        _persist_session()
        return dict(SESSION)


def _persist_session():
    cfg = load_config()
    cfg["sesion"] = dict(SESSION) if SESSION else None
    save_config(cfg)


def state():
    return dict(SESSION) if SESSION else None


def family_for_event(event):
    return BY_EVENT.get(int(event))


def _profile_event(p, family, serial):
    events = p.setdefault("events", {})
    key = f"{family}:{serial}"
    return events.setdefault(key, {
        "familia": family, "serial": serial, "victorias": 0, "derrotas": 0,
        "empates": 0, "racha": 0, "racha_id": 0, "estrellas": 0, "claims": [], "ultimo": "",
    })


def _queue(p, progress, family, milestone, card):
    claim = f"{family}:{progress['serial']}:{milestone}"
    if claim in progress["claims"]:
        return False
    progress["claims"].append(claim)
    if int(card or 0):
        p.setdefault("event_rewards", []).append({
            "claim": claim, "familia": family, "serial": progress["serial"],
            "milestone": str(milestone), "cards": [int(card)], "championship": family == "championship",
            "created": int(time.time()),
        })
    return True


def _summary(p, progress):
    if SESSION is None:
        return
    name = str(p.get("name") or p.get("nombre") or "Jugador")
    with LOCK:
        SESSION.setdefault("progreso", {})[name] = {
            "victorias": progress["victorias"], "derrotas": progress["derrotas"],
            "empates": progress["empates"], "racha": progress["racha"],
            "estrellas": progress["estrellas"],
            "pendientes": sum(1 for x in p.get("event_rewards", ())
                              if x.get("familia") == progress["familia"]
                              and int(x.get("serial", 0)) == progress["serial"]),
        }
        _persist_session()


def on_pvp_result(p, room_kind, result, duel_key, played=True, last=True, match=False, game_number=1):
    """Actualiza el progreso dentro de la misma transacción de PvpMatch._finish."""
    session = SESSION
    if not played or session is None or session.get("estado") != "activo":
        return []
    family = BY_ROOM.get(int(room_kind))
    if family != session.get("familia") or family not in ("gunslinger", "duelist_trial", "anniversary", "evolution"):
        return []
    if family == "evolution" and not match:
        return []
    progress = _profile_event(p, family, int(session["serial"]))
    if progress.get("ultimo") == str(duel_key):
        return []
    progress["ultimo"] = str(duel_key)
    old_stars = progress["estrellas"]
    if result == "win":
        progress["victorias"] += 1
        progress["racha"] += 1
        if family == "evolution":
            progress["estrellas"] += {1: 2, 2: 4, 3: 6}.get(int(game_number), 0)
    elif result == "lose":
        progress["derrotas"] += 1
        progress["racha_id"] += 1
        progress["racha"] = 0
    else:
        progress["empates"] += 1
        if family == "gunslinger":
            progress["racha_id"] += 1
            progress["racha"] = 0
    item = FAMILIES[family]
    value = (progress["racha"] if item["contador"] == "racha" else
             progress["estrellas"] if item["contador"] == "estrellas" else progress["victorias"])
    rule = load_config()["reglas"][family]
    won = []
    for milestone in item["milestones"]:
        hit = old_stars < milestone <= value if item["contador"] == "estrellas" else value == milestone
        claim_mark = f"{milestone}@{progress['racha_id']}" if family == "gunslinger" else milestone
        if item["ciclo"]:
            hit = value % item["ciclo"] == milestone % item["ciclo"] and value >= milestone
            claim_mark = f"{milestone}@{(value - 1) // item['ciclo']}"
        card = int(rule.get("recompensas", {}).get(str(milestone), 0) or 0)
        if hit and _queue(p, progress, family, claim_mark, card) and card:
            won.append({"milestone": str(milestone), "card": card})
    _summary(p, progress)
    return won


def championship_award(p, placement):
    session = SESSION
    if session is None or session.get("familia") != "championship":
        return []
    progress = _profile_event(p, "championship", int(session["serial"]))
    card = load_config()["reglas"]["championship"].get("recompensas", {}).get(str(placement), 0)
    out = [card] if _queue(p, progress, "championship", placement, card) and card else []
    _summary(p, progress)
    return out


def take_reward(p, championship=False):
    queue = p.setdefault("event_rewards", [])
    for i, reward in enumerate(queue):
        if bool(reward.get("championship")) == bool(championship):
            return queue.pop(i)
    return None

