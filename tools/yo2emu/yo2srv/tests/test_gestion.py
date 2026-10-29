"""Pruebas de la moderación del anfitrión (11-10): yo2srv/admin/moderacion.py y la API de gestión (gestion.py).

    python -m yo2srv.tests                          las unitarias (servidor falso; la API en un puerto efímero de 127.0.0.1)
    python yo2srv/tests/test_gestion.py --vivo      además, una instancia de PRUEBA de run.py SIN el panel (YO2_ADMIN_PANEL=0)
                                                    en modo de cuentas cerradas: expulsar, vetar (se cumple al entrar), quitar el
                                                    veto, crear cuenta y entrar con ella, cambiar la clave, avisos que llegan a
                                                    un cliente falso, copias, token y bloqueo por fuerza bruta
    python yo2srv/tests/test_gestion.py --vivo [--offset N | --banda 1000]

Nunca el servidor vivo: offset 0, 3000 y los que pisan 5730-5799/8730/8790/8791 los rechaza comun.Instancia.
"""
import argparse
import http.client
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.normpath(os.path.join(AQUI, "..", ".."))
PARIDAD = os.path.join(EMU, "paridad")
if EMU not in sys.path:
    sys.path.insert(0, EMU)

from yo2srv.admin import gestion, moderacion as MOD  # noqa: E402


# ============================================================================ servidor falso
class FakeSock:
    def __init__(self):
        self.cerrado = False

    def shutdown(self, how):
        self.cerrado = True

    def close(self):
        self.cerrado = True


class FakeConn:
    def __init__(self, port=5731):
        self.port, self.sock, self.closed, self.peer = port, FakeSock(), False, "127.0.0.1"
        self.enviado = []

    def send(self, mid, data=b""):
        self.enviado.append((mid, bytes(data)))


class FakeSess:
    def __init__(self, acct, name="", owner=False, state=0, block=None, ablock=0):
        self.acct, self.name, self.owner, self.state, self.block, self.ablock = acct, name, owner, state, block, ablock
        self.conns, self.lobby = {5731: FakeConn()}, FakeConn(5740)
        self.slot, self.pid = 0, 1000


class FakeHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.posted = []
        self.llamadas = []

    def _post(self, s, mid, data, pause=0.02):
        self.posted.append((s.acct, mid, bytes(data)))

    def login(self, c, data):
        self.llamadas.append(data)
        return self.sessions.get("sesion")

    def area_list(self, key):
        return [0, 1, 2]


class FakeW:
    def u32(self, v): self.v = v; return self
    def s(self, *a): return self
    def bytes(self): return b"\xff\xff\xff\x0a"


class FakeProto:
    W = FakeW


class FakeHubMod:
    """Lo que moderacion usa de pvp_hub: acct_key, _cifrado, LOGIN_ESTRICTO y REJECTED."""
    LOGIN_ESTRICTO = True
    REJECTED = []

    @staticmethod
    def acct_key(data):
        return bytes(data[:32]).hex()[:16]

    @staticmethod
    def _cifrado():
        import importlib.util
        m = sys.modules.get("yo2_cifrado")
        if m is None:
            sp = importlib.util.spec_from_file_location("yo2_cifrado", os.path.join(EMU, "research", "cuentas", "cifrado.py"))
            m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
            sys.modules["yo2_cifrado"] = m
        return m


def montar(tmp):
    """Un namespace falso con estado en memoria; devuelve (ns, estado)."""
    estado = {"owner_acct": "a" * 16, "players": {"0": "Dueno"}, "accounts": {}, "logins": {}}
    ns = {"log": lambda *a: None, "STATE_PATH": os.path.join(tmp, "state.json"), "STATE_LOCK": threading.RLock(),
          "HUB": FakeHub(), "pvp_hub": FakeHubMod, "proto": FakeProto, "OWNER_ACCT": "a" * 16, "MAX_SAVED_ACCOUNTS": 5,
          "HUB_KEYS": ("accounts", "owner_acct"), "ROOM_PORT_BASE": 5740, "ROOM_KINDS": (13, 7, 15), "ACAD_KINDS": (15,)}
    ns["load_state_raw"] = lambda: json.loads(json.dumps(estado))

    def guardar(st):
        copia = json.loads(json.dumps(st))
        estado.clear(); estado.update(copia)
        with open(ns["STATE_PATH"], "w", encoding="utf-8") as f:
            json.dump(estado, f)
    ns["save_state_raw"] = guardar
    guardar(estado)
    MOD.enlazar(ns)
    MOD.LOGIN_T.clear()
    MOD.HOOKS.update(rechazo=None, flags=None)
    return ns, estado


def sesion(ns, acct, **kw):
    s = FakeSess(acct, **kw)
    ns["HUB"].sessions[acct] = s
    return s


class FakePP:
    """Lo que moderacion usa de la persistencia para el nivel: profile() y player_data_update() (1.er byte = LV, como 0x4140)."""
    @staticmethod
    def profile(st, slot, create=True):
        profs = st.setdefault("profiles", {})
        p = profs.get(str(slot))
        if p is None and create:
            p = profs[str(slot)] = {"name": (st.get("players") or {}).get(str(slot), ""), "rec": {"lv": 1}}
        return p

    @staticmethod
    def player_data_update(p):
        # como 0x4140 (PDU_SPEC): u8 LV, u32, u32 MP, 5×u16, u32 DP, resto a 0 (125 B)
        r = p["rec"]
        return (bytes([int(r["lv"])]) + bytes(4) + struct.pack(">I", int(r.get("mp", 0))) + bytes(10)
                + struct.pack(">I", int(r.get("dpp", 0))) + bytes(102))

    @staticmethod
    def dp_of(p):
        return int(p.get("rec", {}).get("dpp", 1000))            # perfil viejo sin saldo = LEGACY_DPP


def con_perfiles(ns, estado):
    """load_state/save_state de run.py (la vista de la cuenta de CTX.sess) sobre el estado falso, más PP y CTX."""
    ctx = types.SimpleNamespace(sess=None)

    def vista():
        s = ctx.sess
        return estado if s.owner else estado["accounts"][s.acct]
    ns.update(CTX=ctx, PP=FakePP, load_state=vista, save_state=lambda st: ns["save_state_raw"](estado))


# ============================================================================ moderacion
class TestModeracion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yo2-modtest-")
        self.ns, self.st = montar(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vetos_se_guardan_junto_al_estado_y_hacen_copia(self):
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"1": "Zeta", "0": "Alfa"}}
        MOD.ban("b" * 16, "x" * 300)
        f = os.path.join(self.tmp, "admin_vetados.json")
        d = json.load(open(f, encoding="utf-8"))
        self.assertEqual(d["b" * 16]["nombres"], ["Alfa", "Zeta"])     # por hueco, como characters() del panel
        self.assertEqual(len(d["b" * 16]["motivo"]), 200)
        self.assertTrue(MOD.is_banned("b" * 16))
        MOD.ban("b" * 16, "otra")
        self.assertTrue(os.path.exists(f + ".bak-admin"))
        MOD.unban("b" * 16)
        self.assertFalse(MOD.is_banned("b" * 16))
        with self.assertRaises(MOD.ApiError) as e:
            MOD.unban("b" * 16)
        self.assertEqual(e.exception.clave, "no_vetada")

    def test_no_se_veta_al_dueno_ni_a_quien_no_existe(self):
        with self.assertRaises(MOD.ApiError) as e:
            MOD.ban("a" * 16)
        self.assertEqual(e.exception.clave, "dueno")
        with self.assertRaises(MOD.ApiError) as e:
            MOD.ban("c" * 16)
        self.assertEqual(e.exception.clave, "no_existe")

    def test_kick_cierra_todas_las_conexiones(self):
        s = sesion(self.ns, "b" * 16, name="Zeta")
        n = MOD.kick("b" * 16)
        self.assertEqual(n, 2)
        self.assertTrue(s.lobby.sock.cerrado and s.conns[5731].sock.cerrado)
        with self.assertRaises(MOD.ApiError) as e:
            MOD.kick("c" * 16)
        self.assertEqual(e.exception.clave, "no_conectado")

    def test_ban_de_alguien_conectado_lo_expulsa(self):
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {}}
        s = sesion(self.ns, "b" * 16, name="Zeta")
        MOD.ban("b" * 16, "mal")
        self.assertTrue(s.lobby.sock.cerrado)

    def test_resolver_id_corto_completo_y_ambiguo(self):
        self.st["accounts"].update({"bc1234" + "0" * 10: {"idx": 1}, "bc1234" + "1" * 10: {"idx": 2}, "d" * 16: {"idx": 3}})
        self.assertEqual(MOD.resolver("ddddd" + "d"), "d" * 16)
        self.assertEqual(MOD.resolver("d" * 16), "d" * 16)
        self.assertEqual(MOD.resolver("AAAAAA"), "a" * 16)             # el dueño también
        for malo, clave in (("bc1234", "ambigua"), ("zzzzzz", "cuenta_invalida"), ("abc", "cuenta_invalida"),
                            ("", "cuenta_invalida"), ("eeeeee", "no_existe")):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.resolver(malo)
            self.assertEqual(e.exception.clave, clave, malo)

    def test_login_hook_cierra_al_vetado_y_deja_pasar_al_resto(self):
        hub = self.ns["HUB"]
        orig = hub.login
        self.assertTrue(MOD.install_login_hook())
        self.assertFalse(MOD.install_login_hook())                    # idempotente: no se envuelve dos veces
        self.assertTrue(hub.login._admin)
        self.assertEqual(hub.login.__defaults__[0].__func__.__name__, "login")   # el método viejo, para la recarga en caliente
        data = bytes(range(32))
        acct = FakeHubMod.acct_key(data)
        c = FakeConn()
        hub.sessions["sesion"] = FakeSess(acct)
        self.assertIsNotNone(hub.login(c, data))                       # sin veto, entra
        self.assertEqual(len(hub.llamadas), 1)
        self.assertIn(acct, MOD.LOGIN_T)
        self.st["accounts"][acct] = {"idx": 1, "players": {}}
        MOD.ban(acct, "")
        c2 = FakeConn()
        self.assertIsNone(hub.login(c2, data))
        self.assertEqual(len(hub.llamadas), 1)                         # el login de verdad ni se llamó
        self.assertEqual(c2.enviado[0][0], 0x3004)
        self.assertTrue(c2.closed)
        MOD.unban(acct)
        self.assertIsNotNone(hub.login(FakeConn(), data))
        del orig

    def test_ganchos_del_panel_en_el_login(self):
        hub = self.ns["HUB"]
        MOD.install_login_hook()
        MOD.HOOKS["rechazo"] = lambda acct: acct.startswith("00")
        MOD.HOOKS["flags"] = lambda acct: 0x40
        c = FakeConn()
        self.assertIsNone(hub.login(c, b"\x00" * 32))
        self.assertTrue(c.closed)
        c = FakeConn()
        hub.sessions["sesion"] = FakeSess("x")
        hub.login(c, bytes(range(1, 33)))
        self.assertEqual(c.admin_flags, 0x40)

    def test_sin_hub_no_instala_nada(self):
        self.ns["HUB"] = None
        self.assertFalse(MOD.install_login_hook())

    def test_jugadores_basico_y_public_status(self):
        sesion(self.ns, "b" * 16, name="Zeta", state=0x68, block=(5740, 5))
        sesion(self.ns, "a" * 16, name="Dueno", owner=True, state=100)
        sesion(self.ns, "c" * 16, name="Fuera")
        self.ns["HUB"].sessions["c" * 16].conns.clear(); self.ns["HUB"].sessions["c" * 16].lobby = None
        MOD.LOGIN_T["b" * 16] = time.time() - 30
        js = {j["nombre"]: j for j in MOD.jugadores_basico()}
        self.assertEqual(set(js), {"Zeta", "Dueno"})                  # el desconectado no sale
        self.assertEqual((js["Zeta"]["estado"], js["Zeta"]["mundo"], js["Zeta"]["zona"]), ("duelo", "Domino", "Clock Square"))
        self.assertEqual((js["Dueno"]["estado"], js["Dueno"]["dueno"]), ("sala", True))
        p = MOD.public_status()
        self.assertEqual((p["jugadores"], p["duelos"], p["cuentas"]), (2, 1, "cerradas"))
        self.assertEqual(set(p), {"jugadores", "duelos", "perfil", "version_juego", "salas", "cuentas"})

    def test_aviso_de_chat_a_todos_los_conectados(self):
        sesion(self.ns, "b" * 16, name="Zeta")
        sesion(self.ns, "c" * 16, name="Eta")
        self.assertEqual(MOD.chat_notice("hola a todos"), 2)
        msgs = [m for m in self.ns["HUB"].posted if m[1] == 0x4402]
        self.assertEqual(len(msgs), 2)
        self.assertIn(b"hola a todos", msgs[0][2])
        self.assertTrue(msgs[0][2][10:26].startswith(b"SISTEMA"))
        with self.assertRaises(MOD.ApiError) as e:
            MOD.chat_notice("   ")
        self.assertEqual(e.exception.clave, "aviso_vacio")

    def test_copias(self):
        fich = MOD.backup_state()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, fich)))
        self.assertEqual([b["fichero"] for b in MOD.backups()], [fich])

    # ---- nivel (12-10: salido del panel)
    def test_set_level_guarda_y_avisa_al_cliente_conectado(self):
        con_perfiles(self.ns, self.st)
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta", "1": "Eta"}, "profiles": {}}
        sesion(self.ns, "b" * 16, name="Zeta")
        r = MOD.set_level("b" * 16, 0, "12")
        self.assertEqual((r["antes"], r["ahora"], r["aviso_al_momento"], r["en_linea"]), (1, 12, True, True))
        self.assertEqual(self.st["accounts"]["b" * 16]["profiles"]["0"]["rec"]["lv"], 12)
        pdu = [m for m in self.ns["HUB"].posted if m[1] == 0x4140]
        self.assertEqual(pdu[-1][2][0], 12)
        self.assertEqual(MOD.nivel_guardado(self.ns["load_state_raw"](), "b" * 16, 0), 12)
        self.assertIsNone(MOD.nivel_guardado(self.ns["load_state_raw"](), "b" * 16, 1), "sin perfil todavía")
        r = MOD.set_level("b" * 16, 1, 5)                               # otro personaje: guarda, sin 0x4140
        self.assertFalse(r["aviso_al_momento"])
        self.ns["HUB"].sessions["b" * 16].state = 0x68                  # en duelo: guarda, sin 0x4140
        n = len(self.ns["HUB"].posted)
        self.assertFalse(MOD.set_level("b" * 16, 0, 2)["aviso_al_momento"])
        self.assertEqual(len(self.ns["HUB"].posted), n)
        for malo in (0, 100, -1, "x", True, 4.5, None):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.set_level("b" * 16, 0, malo)
            self.assertEqual(e.exception.clave, "nivel", malo)
        with self.assertRaises(MOD.ApiError) as e:
            MOD.set_level("b" * 16, 3, 5)
        self.assertEqual(e.exception.clave, "sin_personaje")
        self.assertEqual(self.st["accounts"]["b" * 16]["profiles"]["0"]["rec"]["lv"], 2)
        fichas = [c for c in MOD.cuentas()["cuentas"] if c["acct"] == "b" * 16][0]["personajes"]
        self.assertEqual([(p["hueco"], p["nivel"]) for p in fichas], [(0, 2), (1, 5)])

    # ---- puntos DP y MP (12-10)
    def test_set_points_pone_suma_y_avisa_al_cliente_conectado(self):
        con_perfiles(self.ns, self.st)
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta", "1": "Eta"},
                                         "profiles": {"0": {"name": "Zeta", "rec": {"lv": 2, "dpp": 10, "mp": 10000}}}}
        sesion(self.ns, "b" * 16, name="Zeta")
        self.assertEqual(MOD.puntos_guardados(self.ns["load_state_raw"](), "b" * 16, 0), {"dp": 10, "mp": 10000})
        r = MOD.set_points("b" * 16, 0, dp="500", modo="poner")
        self.assertEqual((r["antes"], r["ahora"], r["aviso_al_momento"]), ({"dp": 10}, {"dp": 500, "mp": 10000}, True))
        pdu = [m for m in self.ns["HUB"].posted if m[1] == 0x4140][-1][2]
        self.assertEqual((len(pdu), struct.unpack(">I", pdu[19:23])[0], struct.unpack(">I", pdu[5:9])[0]), (125, 500, 10000))
        r = MOD.set_points("b" * 16, 0, dp=-600, mp=250, modo="sumar")       # nunca por debajo de 0
        self.assertEqual(r["ahora"], {"dp": 0, "mp": 10250})
        r = MOD.set_points("b" * 16, 1, dp=7, modo="sumar")                  # sin perfil: lo crea; otro hueco, sin 0x4140
        self.assertFalse(r["aviso_al_momento"])
        self.assertEqual(self.st["accounts"]["b" * 16]["profiles"]["1"]["rec"]["dpp"], 1007, "parte de LEGACY_DPP (dp_of)")
        for kw in ({}, {"dp": -1}, {"dp": "x"}, {"mp": True}, {"dp": 10_000_000}, {"dp": 1.5}):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.set_points("b" * 16, 0, modo="poner", **kw)
            self.assertEqual(e.exception.clave, "puntos", kw)
        with self.assertRaises(MOD.ApiError) as e:
            MOD.set_points("b" * 16, 0, dp=1, modo="multiplicar")
        self.assertEqual(e.exception.clave, "modo")
        with self.assertRaises(MOD.ApiError) as e:
            MOD.set_points("b" * 16, 3, dp=1)
        self.assertEqual(e.exception.clave, "sin_personaje")
        fichas = [c for c in MOD.cuentas()["cuentas"] if c["acct"] == "b" * 16][0]["personajes"]
        self.assertEqual([(p["hueco"], p["dp"], p["mp"]) for p in fichas], [(0, 0, 10250), (1, 1007, 0)])

    def test_apply_money_tope_y_modos(self):
        r = {"dpp": 5}
        self.assertEqual(MOD.apply_money(r, {"dpp": -9, "mp": None}), {"dpp": (5, 0)})
        self.assertEqual(MOD.apply_money(r, {"mp": 2 ** 40}, tope=0xffffffff), {"mp": (0, 0xffffffff)})
        self.assertEqual(MOD.apply_money(r, {"dpp": 3}, "poner"), {"dpp": (0, 3)})

    @unittest.skipUnless(os.path.isfile(os.path.join(EMU, "research", "admin", "panel.py")),
                         "sin research/admin (panel privado del owner): árbol público")
    def test_el_panel_usa_la_misma_funcion_de_nivel(self):
        sys.path.insert(0, EMU)
        import importlib
        panel = importlib.import_module("research.admin.panel")
        self.assertIs(panel.set_level, MOD.set_level)
        self.assertIs(panel._with_profile, MOD._with_profile)
        self.assertIs(panel._push_pdu, MOD._push_pdu)
        self.assertEqual((panel.LV_MIN, panel.LV_MAX), (1, 99))

    # ---- cuentas (modo cerrado)
    def test_crear_cuenta_guarda_usuario_y_nunca_la_clave(self):
        r = MOD.crear_cuenta("ana", "clavesecreta")
        self.assertEqual(len(r["corto"]), 6)
        acct = r["acct"]
        self.assertIn(acct, self.st["accounts"])
        self.assertEqual(self.st["logins"]["usuarios"][acct], "ana")
        self.assertNotIn("clavesecreta", json.dumps(self.st))
        self.assertTrue(os.path.exists(self.ns["STATE_PATH"] + ".bak-admin"))
        for u, c, clave in (("ana", "clavesecreta", "ya_existe"), ("ana", "otraclave", "usuario_repetido")):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.crear_cuenta(u, c)
            self.assertEqual(e.exception.clave, clave)
        fila = [c for c in MOD.cuentas()["cuentas"] if c["acct"] == acct][0]
        self.assertEqual((fila["usuario"], fila["en_linea"], fila["vetado"]), ("ana", False, False))

    def test_validacion_de_usuario_y_clave_sin_filtrar_el_valor(self):
        for u, c in (("", "x"), ("x", ""), ("a" * 17, "x"), ("x", "c" * 17), ("x\x00y", "x"), ("x", "中"), (None, "x"), (5, "x")):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.crear_cuenta(u, c)
            self.assertEqual(e.exception.clave, "entrada")
            self.assertNotIn("c" * 17, str(e.exception))

    def test_tope_de_cuentas(self):
        for i in range(5):
            MOD.crear_cuenta(f"u{i}", "clave")
        with self.assertRaises(MOD.ApiError) as e:
            MOD.crear_cuenta("u9", "clave")
        self.assertEqual(e.exception.clave, "tope")

    def test_cambiar_clave_cambia_el_id_de_entrada_y_deja_los_datos(self):
        acct = MOD.crear_cuenta("ana", "uno")["acct"]
        self.st["accounts"][acct]["players"] = {"0": "Anita"}
        viejo = FakeHubMod._cifrado().account_id(FakeHubMod._cifrado().encrypt("ana", "uno"))
        self.assertEqual(viejo, acct)
        MOD.cambiar_clave(acct, "dos")
        nuevo = FakeHubMod._cifrado().account_id(FakeHubMod._cifrado().encrypt("ana", "dos"))
        self.assertEqual(self.st["logins"]["alias"], {nuevo: acct})
        self.assertIn("Anita", json.dumps(self.st))
        with self.assertRaises(MOD.ApiError) as e:
            MOD.cambiar_clave("f" * 16, "x")
        self.assertEqual(e.exception.clave, "no_existe")
        with self.assertRaises(MOD.ApiError) as e:
            MOD.cambiar_clave(acct, "x", usuario="otro")
        self.assertEqual(e.exception.clave, "usuario_otro")

    def test_borrar_solo_cuentas_vacias_desconectadas_y_confirmadas(self):
        acct = MOD.crear_cuenta("ana", "uno")["acct"]
        for args, clave in (((acct, False), "sin_confirmar"), (("a" * 16, True), "dueno"), (("e" * 16, True), "no_existe")):
            with self.assertRaises(MOD.ApiError) as e:
                MOD.borrar_cuenta(*args)
            self.assertEqual(e.exception.clave, clave)
        self.st["accounts"][acct]["players"] = {"0": "Anita"}
        with self.assertRaises(MOD.ApiError) as e:
            MOD.borrar_cuenta(acct, True)
        self.assertEqual(e.exception.clave, "con_personajes")
        self.st["accounts"][acct]["players"] = {}
        sesion(self.ns, acct)
        with self.assertRaises(MOD.ApiError) as e:
            MOD.borrar_cuenta(acct, True)
        self.assertEqual(e.exception.clave, "conectada")
        self.ns["HUB"].sessions.pop(acct)
        self.assertTrue(MOD.borrar_cuenta(acct, True)["borrada"])
        self.assertNotIn(acct, self.st["accounts"])
        self.assertEqual(self.st["logins"]["usuarios"], {})


# ============================================================================ personaje nuevo (12-10, research/persistencia)
def cargar_pp(entorno):
    """research/persistencia/proposal.py cargado aparte con ese entorno (lee YO2_START_LEVEL y YO2_COUNTRY_BY_LANG al cargar)."""
    import importlib.util
    antes = {k: os.environ.get(k) for k in entorno}
    try:
        for k, v in entorno.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sp = importlib.util.spec_from_file_location("pp_prueba_lv", os.path.join(EMU, "research", "persistencia", "proposal.py"))
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m
    finally:
        for k, v in antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestPerfilNuevo(unittest.TestCase):
    def test_nivel_inicial_por_defecto_1_como_el_original(self):
        pp = cargar_pp({"YO2_START_LEVEL": None})
        self.assertEqual(pp.START_LV, 1)
        self.assertEqual(pp.blank_profile("N")["rec"]["lv"], 1)

    def test_nivel_inicial_del_anfitrion_y_valores_raros(self):
        for txt, lv in (("5", 5), ("10", 10), ("0", 1), ("150", 99), ("x", 1), ("", 1)):
            with self.subTest(txt):
                self.assertEqual(cargar_pp({"YO2_START_LEVEL": txt}).blank_profile("N")["rec"]["lv"], lv)

    def test_pais_por_idioma_solo_en_perfiles_nuevos(self):
        pp = cargar_pp({"YO2_COUNTRY_BY_LANG": None})
        self.assertTrue(pp.COUNTRY_BY_LANG)
        for lang, c in ((0, "JP"), (1, "US"), (2, "DE"), (3, "IT"), (4, "FR"), (5, "ES"), (7, "KR")):
            p = pp.blank_profile("N")
            self.assertTrue(pp.country_from_lang(p, lang))
            self.assertEqual(p["country"], c)
            self.assertFalse(pp.country_from_lang(p, 0), "solo la primera vez")
            self.assertEqual(p["country"], c)
        p = pp.blank_profile("N")
        self.assertFalse(pp.country_from_lang(p, 6))                  # idioma sin país (mundial/desconocido): DEFAULT_COUNTRY
        self.assertEqual(p["country"], "")
        viejo = {"name": "V", "rec": {"lv": 4}}                        # perfil de antes: sin clave «country», no cambia
        self.assertFalse(pp.country_from_lang(viejo, 1))
        self.assertNotIn("country", viejo)
        apagado = cargar_pp({"YO2_COUNTRY_BY_LANG": "0"})
        p = apagado.blank_profile("N")
        self.assertFalse(apagado.country_from_lang(p, 1))
        self.assertEqual(p["country"], "")


# ============================================================================ API HTTP
class Api:
    def __init__(self, port, token):
        self.port, self.token = port, token

    def req(self, method, path, body=None, token=True, host=None, ctype="application/json", origin=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["Authorization"] = "Bearer " + (self.token if token is True else token)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = ctype
            h["Content-Length"] = str(len(data))
        if origin:
            h["Origin"] = origin
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        txt = r.read()
        c.close()
        try:
            return r.status, json.loads(txt.decode("utf-8"))
        except ValueError:
            return r.status, txt

    def get(self, p, **k):
        return self.req("GET", p, **k)

    def post(self, p, b=None, **k):
        return self.req("POST", p, {} if b is None else b, **k)


class TestGestionHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yo2-gestiontest-")
        self.ns, self.st = montar(self.tmp)
        self.s = gestion.GestionServer(("127.0.0.1", 0), gestion.Handler)
        self.token = "tok-" + "x" * 40
        self._antes = dict(gestion.ST)
        gestion.ST.update(srv=self.s, port=self.s.server_address[1], token=self.token)
        gestion.AUTH_FAILS.clear(); gestion.AUTH["blocked_until"] = 0.0
        self._max = gestion.AUTH_MAX_FAILS
        gestion.AUTH_MAX_FAILS = 1000                                  # los demás casos no deben bloquearse
        threading.Thread(target=self.s.serve_forever, daemon=True).start()
        self.api = Api(gestion.ST["port"], self.token)

    def tearDown(self):
        self.s.shutdown(); self.s.server_close()
        gestion.ST.clear(); gestion.ST.update(self._antes)
        gestion.AUTH_MAX_FAILS = self._max
        gestion.AUTH_FAILS.clear(); gestion.AUTH["blocked_until"] = 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def auditoria(self):
        try:
            return open(os.path.join(self.tmp, "admin_auditoria.log"), encoding="utf-8").read()
        except OSError:
            return ""

    def test_publico_sin_token_y_el_resto_con_token(self):
        s, r = self.api.get("/publico", token=False)
        self.assertEqual(s, 200)
        self.assertEqual(set(r), {"jugadores", "duelos", "perfil", "version_juego", "salas", "cuentas"})
        for p in ("/gestion/jugadores", "/gestion/vetados", "/gestion/cuentas", "/gestion/copias"):
            self.assertEqual(self.api.get(p, token=False)[0], 401, p)
            self.assertEqual(self.api.get(p, token="mal")[0], 401, p)
            self.assertEqual(self.api.get(p)[0], 200, p)
        for p in ("/gestion/expulsar", "/gestion/vetar", "/gestion/aviso", "/gestion/crear", "/gestion/copia"):
            self.assertEqual(self.api.post(p, {}, token=False)[0], 401, p)
        self.assertEqual(self.api.get("/nada")[0], 404)
        self.assertEqual(self.api.get("/gestion/nada")[0], 404)
        self.assertEqual(self.api.req("PUT", "/gestion/vetados")[0], 405)

    def test_el_token_se_compara_exacto(self):
        for t in (self.token[:-1], self.token + "x", self.token.upper(), ""):
            self.assertEqual(self.api.get("/gestion/jugadores", token=t or "x")[0], 401)

    def test_bloqueo_por_fuerza_bruta(self):
        gestion.AUTH_MAX_FAILS = 10
        for _ in range(gestion.AUTH_MAX_FAILS):
            self.api.get("/gestion/jugadores", token="mal")
        s, r = self.api.get("/gestion/jugadores")                      # ya ni el bueno pasa durante un minuto
        self.assertEqual((s, r["error"]), (429, "bloqueado"))
        self.assertEqual(self.api.get("/publico", token=False)[0], 200)   # lo público sigue

    def test_solo_loopback_con_su_host_y_sin_origin(self):
        self.assertEqual(self.api.get("/gestion/jugadores", host="evil.example:80")[0], 403)
        self.assertEqual(self.api.get("/gestion/jugadores", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.api.get("/gestion/jugadores", origin="http://evil.example")[0], 403)
        self.assertEqual(self.api.get("/publico", token=False, host="evil.example")[0], 403)
        self.assertEqual(self.api.get("/gestion/jugadores", host=f"localhost:{self.api.port}")[0], 200)

    def test_cuerpo_y_tipo(self):
        self.assertEqual(self.api.req("POST", "/gestion/aviso", raw=b"{}", ctype="text/plain")[0], 415)
        self.assertEqual(self.api.req("POST", "/gestion/aviso", raw=b"{no json")[0], 400)
        self.assertEqual(self.api.req("POST", "/gestion/aviso", raw=b"[1,2]")[0], 400)
        s, r = self.api.req("POST", "/gestion/aviso", raw=b"{" + b'"texto":"' + b"x" * 9000 + b'"}')
        self.assertEqual(s, 413)

    def test_jugadores_vetados_y_cuentas_sin_datos_sensibles(self):
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta"}}
        MOD.ban("b" * 16, "spam")
        sesion(self.ns, "b" * 16, name="Zeta", state=0x68)
        s, r = self.api.get("/gestion/jugadores")
        self.assertEqual(r["jugadores"][0]["corto"], "bbbbbb")
        self.assertTrue(r["jugadores"][0]["vetado"])
        self.assertNotIn("b" * 16, json.dumps(r))                      # nunca el id completo
        s, r = self.api.get("/gestion/vetados")
        self.assertEqual((r["vetados"][0]["corto"], r["vetados"][0]["motivo"]), ("bbbbbb", "spam"))
        self.assertNotIn("b" * 16, json.dumps(r))
        s, r = self.api.get("/gestion/cuentas")
        self.assertTrue(r["estricto"])
        self.assertNotIn("acct", r["cuentas"][0])
        self.assertNotIn("ip", json.dumps(r).lower().replace("zip", ""))

    def test_expulsar_vetar_y_quitar_veto(self):
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta"}}
        s = sesion(self.ns, "b" * 16, name="Zeta")
        self.assertEqual(self.api.post("/gestion/expulsar", {"acct": "bbbbbb"})[0], 200)
        self.assertTrue(s.lobby.sock.cerrado)
        s, r = self.api.post("/gestion/expulsar", {"acct": "aaaaaa"})            # el dueño
        self.assertEqual((s, r["error"]), (400, "dueno"))
        s, r = self.api.post("/gestion/expulsar", {"acct": "bbbbbb"})
        self.assertEqual(self.api.post("/gestion/vetar", {"acct": "aaaaaa"})[1]["error"], "dueno")
        s, r = self.api.post("/gestion/vetar", {"acct": "bbbbbb", "motivo": "spam\nmalo"})
        self.assertEqual((s, r["veto"]["motivo"]), (200, "spam malo"))
        self.assertTrue(MOD.is_banned("b" * 16))
        self.assertEqual(self.api.post("/gestion/vetar", {"acct": "bbbbbb", "motivo": "x" * 201})[1]["error"], "entrada")
        self.assertEqual(self.api.post("/gestion/quitar_veto", {"acct": "bbbbbb"})[0], 200)
        self.assertFalse(MOD.is_banned("b" * 16))
        self.assertEqual(self.api.post("/gestion/quitar_veto", {"acct": "bbbbbb"})[1]["error"], "no_existe")
        self.assertEqual(self.api.post("/gestion/vetar", {"acct": "zz"})[1]["error"], "cuenta_invalida")
        self.assertEqual(self.api.post("/gestion/vetar", {})[1]["error"], "cuenta_invalida")
        a = self.auditoria()
        for accion in ("g-expulsar", "g-vetar", "g-quitar_veto"):
            self.assertIn(accion, a)
        self.assertIn("rechazado", a)
        self.assertNotIn(self.token, a)

    def test_cuentas_por_la_api_y_la_clave_no_aparece_en_ningun_sitio(self):
        s, r = self.api.post("/gestion/crear", {"usuario": "ana", "clave": "secretisima"})
        self.assertEqual(s, 200)
        corto = r["corto"]
        self.assertNotIn("secretisima", json.dumps(r))
        self.assertEqual(self.api.post("/gestion/crear", {"usuario": "ana", "clave": "secretisima"})[1]["error"], "ya_existe")
        self.assertEqual(self.api.post("/gestion/crear", {"usuario": "x" * 17, "clave": "c"})[1]["error"], "entrada")
        self.assertEqual(self.api.post("/gestion/cambiar_clave", {"acct": corto, "clave": "nuevaclave"})[0], 200)
        self.assertEqual(self.api.post("/gestion/borrar", {"acct": corto})[1]["error"], "sin_confirmar")
        self.assertEqual(self.api.post("/gestion/borrar", {"acct": corto, "confirmar": True})[0], 200)
        s, r = self.api.get("/gestion/cuentas")
        self.assertEqual([c["usuario"] for c in r["cuentas"]], [None])     # solo queda la del dueño
        todo = self.auditoria() + json.dumps(self.st) + open(os.path.join(self.tmp, "state.json"), encoding="utf-8").read()
        for secreto in ("secretisima", "nuevaclave"):
            self.assertNotIn(secreto, todo)
        self.assertIn("g-cuenta_crear", self.auditoria())

    def test_aviso_y_copias(self):
        sesion(self.ns, "b" * 16, name="Zeta")
        s, r = self.api.post("/gestion/aviso", {"texto": "mantenimiento a las 20:00"})
        self.assertEqual((s, r["enviado_a"]), (200, 1))
        self.assertEqual(self.api.post("/gestion/aviso", {"texto": ""})[1]["error"], "entrada")
        self.assertEqual(self.api.post("/gestion/aviso", {"texto": "x" * 201})[1]["error"], "entrada")
        self.assertEqual(self.api.post("/gestion/aviso", {"texto": 5})[1]["error"], "entrada")
        self.assertNotIn("mantenimiento a las", self.auditoria())            # el texto del aviso no se audita
        s, r = self.api.post("/gestion/copia", {})
        self.assertEqual(s, 200)
        s, r2 = self.api.get("/gestion/copias")
        self.assertEqual([c["fichero"] for c in r2["copias"]], [r["fichero"]])

    def test_reglas_de_eventos_por_la_api(self):
        path = os.path.join(EMU, "research", "eventos", "proposal.py")
        spec = importlib.util.spec_from_file_location("test_gestion_eventos", path)
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        ev.install({**self.ns, "STATE_PATH": os.path.join(self.tmp, "state.json"), "log": lambda *_: None})
        self.ns["ER"] = ev
        self.ns["PP"] = types.SimpleNamespace(card_ok=lambda cid: 1 <= cid <= 4000)
        s, r = self.api.get("/gestion/eventos/reglas")
        self.assertEqual(s, 200)
        self.assertEqual({x["id"] for x in r["familias"]},
                         {"gunslinger", "duelist_trial", "anniversary", "championship", "evolution", "ultimate"})
        s, r = self.api.post("/gestion/eventos/regla",
                             {"familia": "gunslinger", "activo": True, "recompensas": {"5": 123}})
        self.assertEqual((s, r["regla"]["familia"], r["regla"]["recompensas"]["5"]),
                         (200, "gunslinger", 123))
        s, r = self.api.post("/gestion/eventos/regla",
                             {"familia": "evolution", "activo": True, "recompensas": {"10": 123}})
        self.assertEqual((s, r["regla"]["familia"], r["regla"]["recompensas"]["10"]),
                         (200, "evolution", 123))
        s, r = self.api.post("/gestion/eventos/regla",
                             {"familia": "ultimate", "activo": True, "recompensas": {"10": 123}})
        self.assertEqual((s, r["error"]), (400, "evento_regla"))

    def test_nivel_por_la_api(self):
        con_perfiles(self.ns, self.st)
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta"}, "profiles": {"0": {"name": "Zeta", "rec": {"lv": 1}}}}
        self.st["accounts"]["c" * 16] = {"idx": 2, "players": {"0": "Uno", "2": "Dos"}, "profiles": {}}
        sesion(self.ns, "b" * 16, name="Zeta")
        s, r = self.api.get("/gestion/jugadores")
        self.assertEqual((r["jugadores"][0]["nivel"], r["jugadores"][0]["hueco"]), (1, 0))
        s, r = self.api.post("/gestion/nivel", {"acct": "bbbbbb", "nivel": 7})
        self.assertEqual((s, r["antes"], r["ahora"], r["al_momento"]), (200, 1, 7, True))
        self.assertEqual(self.api.get("/gestion/jugadores")[1]["jugadores"][0]["nivel"], 7)
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "bbbbbb", "nivel": 100})[1]["error"], "nivel")
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "bbbbbb", "nivel": "x"})[1]["error"], "nivel")
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "bbbbbb", "nivel": 3, "hueco": 9})[1]["error"], "hueco")
        # desconectada con 2 personajes: hace falta el hueco
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "cccccc", "nivel": 3})[1]["error"], "hueco")
        s, r = self.api.post("/gestion/nivel", {"acct": "cccccc", "nivel": 3, "hueco": 2})
        self.assertEqual((s, r["ahora"], r["al_momento"], r["en_linea"]), (200, 3, False, False))
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "cccccc", "nivel": 3, "hueco": 1})[1]["error"], "sin_personaje")
        s, r = self.api.get("/gestion/cuentas")
        c = [x for x in r["cuentas"] if x["corto"] == "cccccc"][0]
        self.assertEqual(c["personajes"], ["Uno", "Dos"])                 # como antes: solo nombres
        self.assertEqual(c["fichas"], [{"hueco": 0, "nombre": "Uno", "nivel": None, "dp": None, "mp": None},
                                       {"hueco": 2, "nombre": "Dos", "nivel": 3, "dp": 1000, "mp": 0}])
        self.assertEqual(self.api.post("/gestion/nivel", {"acct": "cccccc", "nivel": 3, "hueco": 2}, token=False)[0], 401)
        a = self.auditoria()
        self.assertIn("g-nivel", a)
        self.assertIn("LV 1 -> 7", a)
        self.assertIn("rechazado", a)

    def test_dp_y_mp_por_la_api(self):
        con_perfiles(self.ns, self.st)
        self.st["accounts"]["b" * 16] = {"idx": 1, "players": {"0": "Zeta"},
                                         "profiles": {"0": {"name": "Zeta", "rec": {"lv": 2, "dpp": 10, "mp": 300}}}}
        self.st["accounts"]["c" * 16] = {"idx": 2, "players": {"0": "Uno", "2": "Dos"}, "profiles": {}}
        sesion(self.ns, "b" * 16, name="Zeta")
        j = self.api.get("/gestion/jugadores")[1]["jugadores"][0]
        self.assertEqual((j["dp"], j["mp"]), (10, 300))
        s, r = self.api.post("/gestion/dp", {"acct": "bbbbbb", "dp": 500, "modo": "poner"})
        self.assertEqual((s, r["antes"], r["ahora"], r["al_momento"]), (200, {"dp": 10}, {"dp": 500, "mp": 300}, True))
        s, r = self.api.post("/gestion/dp", {"acct": "bbbbbb", "dp": -5, "mp": 700, "modo": "sumar"})
        self.assertEqual(r["ahora"], {"dp": 495, "mp": 1000})
        j = self.api.get("/gestion/jugadores")[1]["jugadores"][0]
        self.assertEqual((j["dp"], j["mp"]), (495, 1000))
        self.assertEqual(self.api.post("/gestion/dp", {"acct": "bbbbbb", "dp": -1, "modo": "poner"})[1]["error"], "puntos")
        self.assertEqual(self.api.post("/gestion/dp", {"acct": "bbbbbb", "modo": "poner"})[1]["error"], "puntos")
        self.assertEqual(self.api.post("/gestion/dp", {"acct": "bbbbbb", "dp": 1, "modo": "x"})[1]["error"], "modo")
        self.assertEqual(self.api.post("/gestion/dp", {"acct": "cccccc", "dp": 1})[1]["error"], "hueco")
        s, r = self.api.post("/gestion/dp", {"acct": "cccccc", "dp": 100, "hueco": 2})           # modo por defecto: poner
        self.assertEqual((s, r["ahora"]["dp"], r["al_momento"], r["en_linea"]), (200, 100, False, False))
        self.assertEqual(self.api.post("/gestion/dp", {"acct": "cccccc", "dp": 1, "hueco": 2}, token=False)[0], 401)
        a = self.auditoria()
        self.assertIn("g-dp", a)
        self.assertIn("DP 10 -> 500", a)

    def test_puerto_y_token(self):
        self.assertEqual(gestion.puerto(0), 8791)
        self.assertEqual(gestion.puerto(1000), 9791)
        os.environ["YO2_GESTION_PORT"] = "9999"
        try:
            self.assertEqual(gestion.puerto(1000), 9999)
        finally:
            del os.environ["YO2_GESTION_PORT"]


# ============================================================================ en vivo (instancia de prueba, SIN panel)
def vivo(a):
    if PARIDAD not in sys.path:
        sys.path.insert(0, PARIDAD)
    import comun
    FC = comun.fake_client()
    estado = os.path.join(comun.FIX, "state-paridad.json")
    if not os.path.exists(estado):
        sys.exit(f"falta {estado} (python paridad/grabar.py --preparar)")
    if a.offset is not None:
        off = a.offset
    else:
        off = next((a.banda + 10 * k for k in range(20) if not comun.offset_valido(a.banda + 10 * k)
                    and not comun.ocupados(a.banda + 10 * k)), None)
        if off is None:
            sys.exit(f"no queda ningún offset libre en la banda {a.banda}")
    motivo = comun.offset_valido(off)
    if motivo:
        sys.exit(motivo)
    for k in [k for k in os.environ if k.startswith("YO2_")]:
        del os.environ[k]
    carpeta = os.path.join(comun.OUT, "gestion-vivo", f"inst-{off}")
    shutil.rmtree(carpeta, ignore_errors=True)
    os.makedirs(carpeta, exist_ok=True)
    local = os.path.join(carpeta, "local")
    # 12-10: nivel inicial 3 (lo que manda el lanzador con «Starting level») y país de respaldo FR: el personaje nuevo de un
    # cliente en español (0x4100 idioma 5) tiene que salir con LV 3 y bandera ES (por idioma), no FR
    # 12-10 (Duelpass): «Duelpass points for new players» = 500 -> YO2_DP_INICIO=500
    entorno = {"YO2_LOGIN_ESTRICTO": "1", "YO2_ADMIN_PANEL": "0", "LOCALAPPDATA": local, "YO2_START_LEVEL": "3",
               "YO2_COUNTRY": "FR", "YO2_DP_INICIO": "500"}
    res = []

    def check(nombre, cond, info=""):
        res.append((nombre, bool(cond)))
        print(f"  {'OK ' if cond else 'MAL'} {nombre}" + (f"  [{info}]" if info and not cond else ""), flush=True)

    print(f"== gestión en vivo: instancia de prueba SIN panel, cuentas cerradas, offset {off}", flush=True)
    cif = FC._cifrado()
    with comun.Instancia(off, carpeta, estado, entorno, espera_duelcore=3.0):
        port = 8791 + off
        tfile = os.path.join(local, "YO2Online", f"gestion_{port}.txt")
        check("el token de la API está en gestion_<puerto>.txt", os.path.exists(tfile))
        token = open(tfile, encoding="ascii").read().strip()
        api = Api(port, token)
        check("el token es largo y aleatorio (>= 40 caracteres, sin saltos)", len(token) >= 40 and "\n" not in token, len(token))
        check("SIN panel: nadie escucha en 8790 + offset", comun.puerto_ocupado(8790 + off) is False)
        s, r = api.get("/publico", token=False)
        check("GET /publico sin token", s == 200 and r.get("cuentas") == "cerradas" and r.get("jugadores") == 0, str(r))
        check("la API pide token (401 sin él, 401 con uno malo)",
              api.get("/gestion/jugadores", token=False)[0] == 401 and api.get("/gestion/jugadores", token="mal")[0] == 401)
        s, r = api.get("/gestion/cuentas")
        owner = [c for c in r["cuentas"] if c["dueno"]]
        check("cuentas: modo cerrado y existe la del dueño", s == 200 and r["estricto"] is True and len(owner) == 1, str(r)[:200])

        # ---- crear una cuenta y entrar con ella
        s, r = api.post("/gestion/crear", {"usuario": "anita", "clave": "pa$$word1"})
        check("crear cuenta", s == 200 and len(r.get("corto", "")) == 6 and "pa$$word1" not in json.dumps(r), str(r))
        corto = r.get("corto")
        check("crear la misma otra vez = ya_existe", api.post("/gestion/crear", {"usuario": "anita", "clave": "pa$$word1"})[1].get("error") == "ya_existe")
        key1 = cif.encrypt("anita", "pa$$word1")
        A = FC.FakeClient("anita", key1, offset=off)
        A.wait_s = 8.0
        check("con la cuenta creada se entra (puerta)", A.gate())
        names = A.account(slot=0, create_name="Anita")
        check("la cuenta crea su personaje", names.get(0) == "Anita", str(names))
        A.enter_room(0, lang=5, slot=0)
        time.sleep(0.5)
        s, r = api.get("/gestion/jugadores")
        mia = [j for j in r.get("jugadores", []) if j["nombre"] == "Anita"]
        check("jugadores: sale Anita con su id corto, sin IP ni id completo",
              s == 200 and len(mia) == 1 and mia[0]["corto"] == corto and mia[0]["estado"] in ("mapa", "menu", "sala")
              and "ip" not in mia[0] and len(json.dumps(r)) < 600, str(r)[:300])
        s, r = api.get("/publico", token=False)
        check("/publico (ficha pública del lanzador) cuenta a Anita: 1 jugador", s == 200 and r.get("jugadores") == 1, str(r))
        s, r = api.get("/gestion/cuentas")
        fila = [c for c in r["cuentas"] if c["corto"] == corto]
        check("cuentas: Anita en línea con su personaje", fila and fila[0]["en_linea"] and fila[0]["personajes"] == ["Anita"], str(fila))

        # ---- 12-10: nivel inicial, bandera por idioma y nivel desde la API (sin panel)
        own = [e for e in [b"".join(A.room.all(0x4212))[i:i + 36] for i in range(0, len(b"".join(A.room.all(0x4212))) - 35, 36)]
               if A.pid is not None and e[:4] == struct.pack(">I", A.pid)]
        check("nivel inicial: YO2_START_LEVEL=3 -> el personaje nuevo sale con LV 3 (LIST1)", own and own[0][20] == 3,
              own[0].hex() if own else "sin entrada propia")
        check("bandera: cliente en español (0x4100 idioma 5) -> «ES» aunque YO2_COUNTRY=FR", own and own[0][30:32] == b"ES",
              own[0][30:34] if own else "-")
        check("jugadores: Anita con su nivel (3) y hueco 0", mia and mia[0].get("nivel") == 3 and mia[0].get("hueco") == 0, str(mia))
        check("cuentas: fichas con hueco, nivel y puntos",
              fila and fila[0].get("fichas") == [{"hueco": 0, "nombre": "Anita", "nivel": 3, "dp": 500, "mp": 10000}], str(fila))
        # ---- 12-10: puntos DUELPASS de inicio (YO2_DP_INICIO=500) y DP/MP desde la API
        ps = A.room.all(0x4101)
        check("DP de inicio: YO2_DP_INICIO=500 -> PSELECTRESULT lleva 500 en +0x1248 (offset 8)",
              ps and struct.unpack(">I", ps[-1][8:12])[0] == 500, ps[-1][:12].hex() if ps else "sin 0x4101")
        check("jugadores: Anita con 500 DP y 10000 MP", mia and (mia[0].get("dp"), mia[0].get("mp")) == (500, 10000), str(mia))
        n = len(A.room.msgs)
        s, r = api.post("/gestion/dp", {"acct": corto, "dp": 1000, "modo": "poner"})
        pdu = A.room.wait(0x4140, start=n, timeout=4)
        check("DP: POST /gestion/dp poner 1000 -> 200 y 0x4140 al momento con DP 1000 (offset 19, +0x1248)",
              s == 200 and r.get("ahora", {}).get("dp") == 1000 and r.get("al_momento") is True and pdu is not None
              and struct.unpack(">I", pdu[19:23])[0] == 1000, f"{s} {r} {pdu[:24].hex() if pdu else None}")
        n = len(A.room.msgs)
        s, r = api.post("/gestion/dp", {"acct": corto, "mp": 250, "dp": -1, "modo": "sumar"})
        pdu = A.room.wait(0x4140, start=n, timeout=4)
        check("DP/MP: sumar +250 MP y -1 DP -> 0x4140 con MP 10250 (offset 5, +0xac) y DP 999",
              s == 200 and pdu is not None and struct.unpack(">I", pdu[5:9])[0] == 10250
              and struct.unpack(">I", pdu[19:23])[0] == 999, f"{s} {r} {pdu[:24].hex() if pdu else None}")
        check("DP: la lista de jugadores ya dice 999 / 10250",
              [(j.get("dp"), j.get("mp")) for j in api.get("/gestion/jugadores")[1]["jugadores"] if j["corto"] == corto] == [(999, 10250)])
        check("DP fuera de rango (-1 al poner, «x») = puntos",
              all(api.post("/gestion/dp", {"acct": corto, "dp": v, "modo": "poner"})[1].get("error") == "puntos" for v in (-1, "x")))
        n = len(A.room.msgs)
        s, r = api.post("/gestion/nivel", {"acct": corto, "nivel": 7})
        pdu = A.room.wait(0x4140, start=n, timeout=4)
        check("nivel: POST /gestion/nivel -> 200, guardado y 0x4140 al momento con LV 7",
              s == 200 and r.get("antes") == 3 and r.get("ahora") == 7 and r.get("al_momento") is True and pdu is not None
              and pdu[0] == 7, f"{s} {r} {pdu[:4].hex() if pdu else None}")
        check("nivel: la lista de jugadores ya dice 7",
              [j.get("nivel") for j in api.get("/gestion/jugadores")[1]["jugadores"] if j["corto"] == corto] == [7])
        check("nivel fuera de rango (0, 100, «x») = nivel",
              all(api.post("/gestion/nivel", {"acct": corto, "nivel": v})[1].get("error") == "nivel" for v in (0, 100, "x")))

        # ---- aviso a todos
        n = len(A.room.msgs)
        s, r = api.post("/gestion/aviso", {"texto": "Reinicio en 10 minutos"})
        got = A.room.wait(0x4402, start=n, timeout=4)
        check("aviso: llega 0x4402 de SISTEMA al cliente conectado", s == 200 and r.get("enviado_a", 0) >= 1 and got is not None
              and b"Reinicio en 10 minutos" in got and b"SISTEMA" in got, str(r))

        # ---- expulsar (no es vetar: puede volver)
        s, r = api.post("/gestion/expulsar", {"acct": corto})
        t0 = time.time()
        while not A.room.closed and time.time() - t0 < 4:
            time.sleep(0.1)
        check("expulsar: se cierra su conexión", s == 200 and A.room.closed, str(r))
        time.sleep(0.5)
        check("expulsar: ya no está en línea", not [j for j in api.get("/gestion/jugadores")[1]["jugadores"] if j["nombre"] == "Anita"])
        check("expulsar al dueño = dueno", api.post("/gestion/expulsar", {"acct": owner[0]["corto"]})[1].get("error") == "dueno")
        check("expulsar a quien no está = no_conectado", api.post("/gestion/expulsar", {"acct": corto})[1].get("error") == "no_conectado")
        A2 = FC.FakeClient("anita", key1, offset=off)
        A2.wait_s = 8.0
        A2.gate(); A2.account(slot=0)
        A2.enter_room(0, lang=5, slot=0)
        check("expulsada pero no vetada: vuelve a entrar", A2.room is not None and not A2.room.closed and A2.pid is not None)

        # ---- vetar: expulsa y se cumple al entrar (sin panel)
        check("vetar al dueño = dueno", api.post("/gestion/vetar", {"acct": owner[0]["corto"]})[1].get("error") == "dueno")
        s, r = api.post("/gestion/vetar", {"acct": corto, "motivo": "prueba"})
        t0 = time.time()
        while not A2.room.closed and time.time() - t0 < 4:
            time.sleep(0.1)
        bans_f = os.path.join(carpeta, "admin_vetados.json")
        bans = json.load(open(bans_f, encoding="utf-8")) if os.path.exists(bans_f) else {}
        check("vetar: guardado en admin_vetados.json y expulsada", s == 200 and A2.room.closed and len(bans) == 1, str(r))
        s, r = api.get("/gestion/vetados")
        check("vetados: la lista", s == 200 and [(v["corto"], v["motivo"], v["nombres"]) for v in r["vetados"]] == [(corto, "prueba", ["Anita"])], str(r))
        L = FC.Link(A2, 5731 + off)
        L.send(0x3001); L.wait(0x3002)
        L.send(0x3003, key1)
        got = L.wait(0x3004, timeout=3)
        t0 = time.time()
        while not L.closed and time.time() - t0 < 4:
            time.sleep(0.1)
        check("vetada SIN PANEL: al entrar 0x3004 con -246 y conexión cerrada",
              got is not None and struct.unpack(">i", got[:4])[0] == -246 and L.closed, got.hex() if got else "sin 0x3004")
        L.close()
        s, r = api.post("/gestion/quitar_veto", {"acct": corto})
        L = FC.Link(A2, 5731 + off)
        L.send(0x3001); L.wait(0x3002)
        L.send(0x3003, key1)
        got = L.wait(0x3004, timeout=3)
        check("quitar veto: vuelve a entrar (0x3004 no negativo)", s == 200 and got is not None and struct.unpack(">i", got[:4])[0] >= 0,
              got.hex() if got else "sin 0x3004")
        L.send(0x0003); time.sleep(0.1); L.close()

        # ---- cambiar la clave: la vieja deja de valer, la nueva entra, los datos son los mismos
        s, r = api.post("/gestion/cambiar_clave", {"acct": corto, "clave": "nueva-clave2"})
        check("cambiar clave", s == 200 and "nueva-clave2" not in json.dumps(r), str(r))

        def codigo(key):
            L = FC.Link(A2, 5731 + off)
            L.send(0x3001); L.wait(0x3002)
            L.send(0x3003, key)
            g = L.wait(0x3004, timeout=3)
            L.send(0x0003); time.sleep(0.1); L.close()
            return struct.unpack(">i", g[:4])[0] if g else None
        check("la clave vieja ya no entra (-240)", codigo(key1) == -240)
        key2 = cif.encrypt("anita", "nueva-clave2")
        check("la clave nueva entra", (codigo(key2) or 0) >= 0)
        B = FC.FakeClient("anita", key2, offset=off); B.wait_s = 8.0
        check("... y con el mismo personaje", B.account(slot=0).get(0) == "Anita")

        # ---- borrar: solo vacías
        check("borrar sin confirmar = sin_confirmar", api.post("/gestion/borrar", {"acct": corto})[1].get("error") == "sin_confirmar")
        time.sleep(1.0)                              # que la sesión de B (ya se despidió) deje de contar como conectada
        r = api.post("/gestion/borrar", {"acct": corto, "confirmar": True})[1]
        check("borrar con personajes = con_personajes", r.get("error") == "con_personajes", str(r))
        s, r = api.post("/gestion/crear", {"usuario": "vacia", "clave": "x"})
        s2, r2 = api.post("/gestion/borrar", {"acct": r.get("corto"), "confirmar": True})
        check("borrar una cuenta vacía", s == 200 and s2 == 200, str(r2))
        check("la borrada ya no entra (-240)", codigo(cif.encrypt("vacia", "x")) == -240)

        # ---- entradas raras
        check("entradas raras: 400 con su clave",
              api.post("/gestion/crear", {"usuario": "a" * 17, "clave": "x"})[1].get("error") == "entrada"
              and api.post("/gestion/aviso", {"texto": "x" * 201})[1].get("error") == "entrada"
              and api.post("/gestion/vetar", {"acct": "no-es-hex"})[1].get("error") == "cuenta_invalida"
              and api.post("/gestion/vetar", {"acct": "123456"})[1].get("error") == "no_existe"
              and api.req("POST", "/gestion/aviso", raw=b"no json")[0] == 400)
        check("Host ajeno y Origin = 403",
              api.get("/gestion/jugadores", host="evil.example")[0] == 403 and api.get("/gestion/jugadores", origin="http://x")[0] == 403)

        # ---- copias
        s, r = api.post("/gestion/copia", {})
        s2, r2 = api.get("/gestion/copias")
        check("copia ahora + lista", s == 200 and s2 == 200 and any(c["fichero"] == r.get("fichero") for c in r2["copias"])
              and os.path.exists(os.path.join(carpeta, r.get("fichero", "-"))) or
              any(f.startswith("state-prueba.json.bak-admin-") or ".bak-admin-" in f for f in os.listdir(carpeta)), str(r))

        # ---- auditoría y log
        aud = open(os.path.join(carpeta, "admin_auditoria.log"), encoding="utf-8").read() if os.path.exists(os.path.join(carpeta, "admin_auditoria.log")) else ""
        check("auditoría: una línea por acción, sin contraseñas ni token",
              all(x in aud for x in ("g-cuenta_crear", "g-aviso", "g-expulsar", "g-vetar", "g-quitar_veto", "g-cuenta_clave", "g-cuenta_borrar", "g-copia",
                                     "g-nivel", "g-dp"))
              and not any(x in aud for x in ("pa$$word1", "nueva-clave2", token)))
        log = ""
        for f in os.listdir(carpeta):
            if f.endswith(".log") or f == "stdout.txt":
                log += open(os.path.join(carpeta, f), encoding="utf-8", errors="replace").read()
        check("log: no lleva contraseñas ni token", not any(x in log for x in ("pa$$word1", "nueva-clave2", token)))
        check("log: dice que la API de gestión escucha", f"[gestion] API de gestión en http://127.0.0.1:{port}/" in log)

        # ---- bloqueo por fuerza bruta (al final: durante un minuto ni el token bueno pasa)
        for _ in range(12):
            api.get("/gestion/jugadores", token="mal")
        s, r = api.get("/gestion/jugadores")
        check("10 tokens malos en un minuto = 429 (también para el bueno)", s == 429, str(r))
        check("... y /publico sigue contestando", api.get("/publico", token=False)[0] == 200)
        for c in (A, A2, B):
            try:
                c.leave_room()
            except Exception:                        # noqa: BLE001
                pass
    bien = sum(1 for _, ok in res if ok)
    mal = [n for n, ok in res if not ok]
    print(f"== RESULTADO gestion vivo: {bien}/{len(res)} bien" + (f"; mal: {mal}" if mal else ""))
    return 0 if not mal else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vivo", action="store_true")
    ap.add_argument("--offset", type=int, default=None)
    ap.add_argument("--banda", type=int, default=1900)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.vivo:
        return vivo(a)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    r = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    return 0 if r.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
