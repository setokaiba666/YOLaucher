"""yo2srv/arranque, admin/consola, cliente y Server.run_forever (paso E8), más lo que salió de run.py con ellos (game_reply,
dp_balance/dp_npc_gate, room_lang, reserva del duelo, comprobación del mazo, sobres) y la forma de run.py.

Sin instancia ni puertos: un Server con un namespace falso (srv.ns), que es lo que los módulos ven en run.py. El arranque de
verdad (las cargas en su orden, la consola, el bucle) lo prueban test_vivo.py (T10.yo2srv_vivo), test_contrato.py y la batería.
"""
import ast
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

EMU = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if EMU not in sys.path:
    sys.path.insert(0, EMU)
from yo2srv import admin, arranque, cliente  # noqa: E402
from yo2srv.admin import consola  # noqa: E402
from yo2srv.cuentas import personajes  # noqa: E402
from yo2srv.duelo import mazo, reserva  # noqa: E402
from yo2srv.mundo import bloques  # noqa: E402
from yo2srv.server import Server  # noqa: E402
from yo2srv.tienda import cartera, sobres  # noqa: E402

SIN = object()


class Conn:
    def __init__(self, port=5731, sess=None):
        self.port, self.sess, self.sent, self.closed = port, sess, [], False

    def send(self, msg_id, data=b"", **kw):
        self.sent.append((msg_id, bytes(data)))


def espacio(**extra):
    log = []
    ns = {"LOG": log, "log": lambda *a: log.append(" ".join(str(x) for x in a)), "HERE": EMU, "ADMIN_PORT": 5799 + 1500,
          "GAME_PORT": 5731, "CTX": threading.local(), "SIN_SESION": SIN}
    ns.update(extra)
    return ns


def servidor(**extra):
    """Un Server enlazado a un namespace falso, con setup() de los módulos de E8 hecho."""
    s = Server(espacio(**extra))
    admin.setup(s); cliente.setup(s); arranque.setup(s)
    for m in (personajes, cartera, sobres, bloques, mazo, reserva):
        m.setup(s)
    return s


class Consola(unittest.TestCase):
    def hablar(self, s, texto, esperar=1):
        """Manda `texto` a consola.atender por un par de sockets y devuelve lo que contesta."""
        a, b = socket.socketpair()
        a.settimeout(5)
        hilo = threading.Thread(target=consola.atender, args=(b, "TOKEN"), daemon=True)
        hilo.start()
        a.sendall(texto.encode()); a.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            try:
                d = a.recv(4096)
            except OSError:
                break
            if not d:
                break
            out += d
        hilo.join(5)
        a.close()
        return out.decode()

    def test_exec_en_el_namespace_y_respuestas(self):
        s = servidor()
        r = self.hablar(s, "AUTH TOKEN\nX = 5\nraise KeyError(3)\nX += 1\n")
        self.assertEqual(r, "ok\nerror: KeyError(3)\nok\n")           # el protocolo de admin.py, sin cambios
        self.assertEqual(s.ns["X"], 6)                                  # exec(línea, srv.ns): el namespace de run.py

    def test_sin_token_no_ejecuta_nada(self):
        s = servidor()
        for texto in ("X = 1\n", "AUTH mal\nX = 1\n", "GET / HTTP/1.1\r\nX = 1\n", "\nAUTH TOKEN\nX = 1\n", "AUTH TOKENX\nX = 1\n"):
            self.assertEqual(self.hablar(s, texto), "", texto)
        self.assertNotIn("X", s.ns)
        self.assertEqual(sum("rechazada" in x for x in s.ns["LOG"]), 5)

    def test_una_linea_de_auth_larga_se_corta(self):
        s = servidor()
        self.assertEqual(self.hablar(s, "AUTH " + "A" * 600 + "\nX = 1\n"), "")
        self.assertNotIn("X", s.ns)

    def test_token_en_el_fichero_de_siempre(self):
        s = servidor(ADMIN_PORT=6999)
        with tempfile.TemporaryDirectory() as d:
            viejo = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = d
            try:
                tok = consola.guardar_token()
                ruta = os.path.join(d, "YO2Online", "consola_6999.txt")
                self.assertEqual(open(ruta, encoding="ascii").read(), tok)
                self.assertGreaterEqual(len(tok), 32)
                self.assertNotEqual(consola.guardar_token(), tok)       # nuevo en cada arranque
            finally:
                if viejo is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = viejo

    def test_send_usa_conns_del_namespace(self):
        c = Conn(5760)
        s = servidor(CONNS={5760: c})
        consola.send(5760, 0x6104, b"\1")
        self.assertEqual(c.sent, [(0x6104, b"\1")])
        with self.assertRaises(KeyError):
            consola.send(5761, 1)
        self.assertIs(s.ns["CONNS"][5760], c)

    def test_con_la_consola_se_ve_el_bucle(self):
        # «DEADLINE += 3600» desde la consola mueve el plazo que lee run_forever en cada vuelta
        s = servidor(DEADLINE=100.0)
        self.hablar(s, "AUTH TOKEN\nDEADLINE += 3600\n")
        self.assertEqual(s.ns["DEADLINE"], 3700.0)


class RunForever(unittest.TestCase):
    def correr(self, s, argv):
        viejo = sys.argv
        sys.argv = argv
        try:
            t0 = time.time()
            s.run_forever()
            return time.time() - t0
        finally:
            sys.argv = viejo

    def test_se_apaga_a_los_n_segundos_y_deja_sus_nombres(self):
        s = servidor()
        dur = self.correr(s, ["run.py", "1"])
        self.assertTrue(0.9 <= dur < 4, dur)
        self.assertEqual(s.ns["secs"], 1)
        self.assertGreater(s.ns["DEADLINE"], 0)
        self.assertIsNone(s.ns["_warned_for"])
        self.assertTrue(any("se apaga a las" in x for x in s.ns["LOG"]))
        self.assertEqual(s.ns["LOG"][-1], "[run] fin de la captura")

    def test_cero_es_sin_limite_hasta_que_la_consola_pone_plazo(self):
        s = servidor()
        # `python run.py 0`: sin límite; otro hilo (la consola) pone DEADLINE a un instante cercano y el bucle acaba
        def consola_pone_plazo():
            while "DEADLINE" not in s.ns:
                time.sleep(0.01)
            time.sleep(0.3)
            s.ns["DEADLINE"] = time.time() + 0.2
        threading.Thread(target=consola_pone_plazo, daemon=True).start()
        dur = self.correr(s, ["run.py", "0"])
        self.assertLess(dur, 5)
        self.assertEqual(s.ns["secs"], 0)
        self.assertIn("[run] sin límite de tiempo", s.ns["LOG"])

    def test_sin_argumento_40_segundos(self):
        s = servidor()
        s.ns["DEADLINE"] = 0
        hilo = threading.Thread(target=self.correr, args=(s, ["run.py"]), daemon=True)
        hilo.start()
        t0 = time.time()
        while "secs" not in s.ns and time.time() - t0 < 3:
            time.sleep(0.01)
        self.assertEqual(s.ns["secs"], 40)
        self.assertAlmostEqual(s.ns["DEADLINE"], time.time() + 40, delta=3)
        s.ns["DEADLINE"] = time.time() - 1                        # y a acabar el bucle
        hilo.join(5)
        self.assertFalse(hilo.is_alive())

    def test_con_puertos_sale_del_proceso(self):
        # DEADLINE cumplido y escuchas abiertas: hay que salir del proceso. Si solo vuelve de run_forever,
        # el intérprete se queda en los accept() y el lanzador espera hasta matarlo.
        from yo2srv import server as srvmod
        from yo2srv.red import servidor as red
        s = servidor(_SALIDA_PROCESO=True)
        salio = []
        orig_exit, orig_sleep = srvmod.os._exit, srvmod.time.sleep
        srvmod.os._exit = lambda c: salio.append(c)

        def dormir(_t):
            s.ns["DEADLINE"] = 1          # instante ya pasado: el bucle acaba en la vuelta siguiente
        srvmod.time.sleep = dormir
        self.addCleanup(setattr, srvmod.os, "_exit", orig_exit)
        self.addCleanup(setattr, srvmod.time, "sleep", orig_sleep)
        self.addCleanup(red.rearmar_escuchas)
        self.correr(s, ["run.py", "0"])
        self.assertEqual(salio, [0])
        self.assertEqual(s.ns["LOG"][-1], "[run] fin de la captura")


class Cliente(unittest.TestCase):
    def test_on_msg_al_log(self):
        s = servidor()
        cliente.on_msg({"type": "send", "payload": {"t": "glog", "sjis": list("hola %d".encode("cp932")), "fn": "f", "args": " 3 "}}, None)
        cliente.on_msg({"type": "send", "payload": {"t": "connect", "ip": "1.2.3.4", "data": "extra"}}, None)
        cliente.on_msg({"type": "error"}, None)
        self.assertEqual(s.ns["LOG"][0], "[game:f] hola %d  ||  3")
        self.assertEqual(s.ns["LOG"][1:3], ['[cli] connect {"ip": "1.2.3.4"}', "extra"])
        self.assertEqual(s.ns["LOG"][3], "[frida] {'type': 'error'}")

    def test_client_es_el_mismo_dict_y_registrar_deja_los_nombres(self):
        d = {"pid": None, "detached": None}
        s = servidor(CLIENT=d)
        self.assertIs(cliente.CLIENT, d)
        cliente.registrar()
        for n in ("CLIENT", "on_msg", "_on_detached", "launch_client", "respawn_client"):
            self.assertIn(n, s.ns)
        self.assertIs(s.ns["CLIENT"], d)

    def test_detached_y_respawn_sin_lanzar_nada(self):
        s = servidor()
        cliente.registrar()
        cliente._on_detached("process-terminated", types.SimpleNamespace(summary="boom"))
        self.assertEqual(s.ns["CLIENT"]["detached"], "process-terminated")
        self.assertTrue(any("Frida suelta yo2.exe" in x and "boom" in x for x in s.ns["LOG"]))
        # enganchado y con pid: respawn no hace nada salvo force
        s.ns["CLIENT"].update(pid=77, detached=None)
        lanzados = []
        s.ns["launch_client"] = lambda: lanzados.append(1) or 99
        self.assertIsNone(cliente.respawn_client())
        self.assertEqual(lanzados, [])
        self.assertEqual(cliente.respawn_client(force=True), 99)
        s.ns["CLIENT"].update(detached="x")
        self.assertEqual(cliente.respawn_client(), 99)

    def test_iniciar_sin_cliente_no_lanza_nada(self):
        s = servidor(NO_CLIENT=True, log_path="L.log")
        s.ns["launch_client"] = lambda: self.fail("YO2_NO_CLIENT: no se lanza yo2.exe")
        cliente.iniciar()
        self.assertTrue(any("YO2_NO_CLIENT" in x and "L.log" in x for x in s.ns["LOG"]))
        self.assertIn("respawn_client", s.ns)

    def test_iniciar_con_cliente_lanza_y_apunta_el_pid(self):
        s = servidor(NO_CLIENT=False, log_path="L.log")
        original = cliente.launch_client
        cliente.launch_client = lambda: s.ns.__setitem__("pid", 1234) or 1234      # (iniciar() registra la del módulo)
        try:
            cliente.iniciar()
        finally:
            cliente.launch_client = original
        self.assertTrue(any("yo2.exe pid 1234" in x for x in s.ns["LOG"]))


class SalidoDeRun(unittest.TestCase):
    def test_game_reply_latido_y_hola(self):
        s = servidor()
        c = Conn(5731)
        personajes.game_reply(c, 0x0005, 0, b"")
        personajes.game_reply(c, 0x3001, 0, b"")
        self.assertEqual([m for m, _ in c.sent], [0x3113, 0x3002])
        c2 = Conn(5732)
        personajes.game_reply(c2, 0x0005, 0, b"")                    # latido en otro puerto: nada
        self.assertEqual(c2.sent, [])

    def test_game_reply_ramas_y_auto(self):
        vistos = []
        s = servidor(PH=None, PH_GAME={}, playerlist_0x3010=lambda c: vistos.append("lista"),
                     regplayer_0x3020=lambda c, d: vistos.append(("crear", d)),
                     delplayer_0x3030=lambda c, d: vistos.append(("borrar", d)),
                     trunk_0x3100=lambda c: vistos.append("baul"), pbin_0x3114=lambda c: vistos.append("pbin"),
                     auto_reply=lambda c, m: m == 0x1234)
        c = Conn()
        for m in (0x3010, 0x3020, 0x3030, 0x3100, 0x3114, 0x3110):
            personajes.game_reply(c, m, 0, b"\2")
        self.assertEqual(vistos, ["lista", ("crear", b"\2"), ("borrar", b"\2"), "baul", "pbin"])
        self.assertEqual(c.sent, [(0x3111, b"\0\0\0\0")])
        personajes.game_reply(c, 0x1234, 0, b"")                      # por AUTO: nada en el log
        personajes.game_reply(c, 0x9999, 0, b"")
        self.assertEqual(s.ns["LOG"], ["[srv:5731] ** mensaje sin respuesta todavía: 0x9999"])

    def test_game_reply_con_persistencia_va_a_ph(self):
        vistos, sync = [], []
        s = servidor(PH={"h_x": lambda c, d: vistos.append(d)}, PH_GAME={0x3100: "h_x"}, sync_player_id=lambda: sync.append(1))
        personajes.game_reply(Conn(), 0x3100, 0, b"z")
        self.assertEqual((vistos, sync), ([b"z"], [1]))                # y el hueco activo se pone al día (B12)

    def test_game_reply_login_rechazado_cierra_y_ok_manda_0x3004(self):
        reg = []
        hub = types.SimpleNamespace(login=lambda c, d: None, reject=lambda c, code, txt: reg.append((code, txt)))
        s = servidor(HUB=hub, pvp_hub=types.SimpleNamespace(AUTH_SERVER_FULL=-200))
        c = Conn()
        personajes.game_reply(c, 0x3003, 0, b"")
        self.assertEqual(reg, [(-200, "")])
        c.closed = True
        personajes.game_reply(c, 0x3003, 0, b"")
        self.assertEqual(len(reg), 1)                                  # ya cerrada: no se rechaza dos veces
        ses = types.SimpleNamespace()
        c2 = Conn(); c2.sess = ses; c2.admin_flags = 0x60 | 0x1
        s.ns["HUB"].login = lambda c, d: ses
        personajes.game_reply(c2, 0x3003, 0, b"")
        self.assertIs(s.ns["CTX"].sess, ses)
        self.assertEqual(c2.sent[0][0], 0x3004)
        self.assertEqual(c2.sent[0][1][:8], b"\0\0\0\0\0\0\0\x61")     # u32 0 + u32 (1 | flags & 0x60)

    def test_room_lang(self):
        s = servidor(ROOM_LANG_MERGE=True)
        self.assertEqual(bloques.room_lang(5), 0)
        s.ns["ROOM_LANG_MERGE"] = False                                # el panel la cambia en caliente
        self.assertEqual(bloques.room_lang(5), 5)

    def test_dp_balance_sin_persistencia_no_bloquea(self):
        s = servidor()
        self.assertIsNone(cartera.dp_balance())
        self.assertFalse(cartera.dp_npc_gate(Conn(), bytes(40)))
        s.ns["PP"] = types.SimpleNamespace(dp_of=lambda p: 7, profile=lambda st, slot, create=False: {"p": 1},
                                           DP_NPC_BLOCK=True, dp_need=lambda style: 1, DP_SINGLE_STYLE=0, R_NO_DP_ME=-612)
        s.ns.update(load_state=lambda: {}, my_slot=lambda st: 0, npc_rule=lambda k: True, dp_balance=cartera.dp_balance)
        self.assertEqual(cartera.dp_balance(), 7)
        self.assertIsNone(getattr(s.ns["CTX"], "sess", None))          # y restaura la sesión del hilo
        self.assertFalse(cartera.dp_npc_gate(Conn(), bytes(24) + (5).to_bytes(4, "big")))        # tiene DP
        s.ns["PP"].dp_of = lambda p: 0
        c = Conn()
        self.assertTrue(cartera.dp_npc_gate(c, bytes(24) + (5).to_bytes(4, "big")))               # sin DP: 0x428a −612
        self.assertEqual(c.sent, [(0x428a, (-612 & 0xffffffff).to_bytes(4, "big"))])
        self.assertFalse(cartera.dp_npc_gate(Conn(), bytes(24) + (0xffffffff).to_bytes(4, "big")))   # sala propia: no es NPC


class Reserva(unittest.TestCase):
    def test_modo_por_defecto_y_cambios(self):
        s = servidor(_admin_ev=lambda *a, **k: s.ns["LOG"].append("ev:" + a[1]))
        s.ns["_duel_reserva_mode"] = reserva._duel_reserva_mode
        self.assertEqual(reserva._duel_reserva_mode(), "vigilar")       # sin DUEL_RESERVA
        s.ns["DUEL_RESERVA"] = "ON "
        self.assertEqual(reserva._duel_reserva_mode(), "on")
        s.ns["DUEL_RESERVA"] = "raro"
        self.assertEqual(reserva._duel_reserva_mode(), "vigilar")
        self.assertEqual(reserva.duel_reserva("off"), "off")
        self.assertEqual(s.ns["DUEL_RESERVA"], "off")
        self.assertTrue(any("modo vigilar -> off" in x for x in s.ns["LOG"]))
        self.assertIn("ev:seguridad", s.ns["LOG"])
        with self.assertRaises(ValueError):
            reserva.duel_reserva("nada")
        self.assertEqual(s.ns["DUEL_RESERVA"], "off")

    def test_iniciar_modo_malo_y_sin_tickets(self):
        old = os.environ.get("YO2_DUEL_RESERVA")
        os.environ["YO2_DUEL_RESERVA"] = "raro"
        try:
            s = servidor()
            s.ns["_load_tickets"] = lambda: (_ for _ in ()).throw(OSError("no está"))
            reserva.iniciar()
            self.assertEqual(s.ns["DUEL_RESERVA"], "vigilar")
            self.assertIsNone(s.ns["DT"])
            self.assertEqual(s.ns["DUEL_RESERVA_MODOS"], ("off", "vigilar", "on"))
            self.assertTrue(any("no vale" in x for x in s.ns["LOG"]) and any("tickets.py no cargado" in x for x in s.ns["LOG"]))
        finally:
            if old is None:
                os.environ.pop("YO2_DUEL_RESERVA", None)
            else:
                os.environ["YO2_DUEL_RESERVA"] = old

    def test_iniciar_con_tickets_de_verdad_y_se_conserva_el_dt_existente(self):
        s = servidor()
        s.ns["_load_tickets"] = reserva._load_tickets
        reserva.iniciar()
        dt = s.ns["DT"]
        self.assertTrue(hasattr(dt, "issue") and hasattr(dt, "check"))
        reserva.iniciar()                                                # idempotente: el mismo DT
        self.assertIs(s.ns["DT"], dt)

    def test_off_deja_pasar_y_on_sin_dt_rechaza(self):
        s = servidor(DUEL_RESERVA="off", DT=None, HUB=types.SimpleNamespace(by_pid=lambda p: None),
                     _admin_ev=lambda *a, **k: None, pvp_hub=types.SimpleNamespace(REJECT_CLOSE_S=0.01, _close_sock=lambda c: None))
        s.ns["_duel_reserva_mode"] = reserva._duel_reserva_mode
        s.ns["_duel_ticket_alive"] = reserva._duel_ticket_alive
        c = Conn(5760)
        self.assertTrue(reserva.duel_reserva_6001(c, 1000))
        s.ns["DUEL_RESERVA"] = "vigilar"
        self.assertTrue(reserva.duel_reserva_6001(c, 1000))              # vigilar: apunta y deja pasar
        self.assertTrue(any("rechazaría" in x for x in s.ns["LOG"]))
        s.ns["DUEL_RESERVA"] = "on"
        self.assertFalse(reserva.duel_reserva_6001(c, 1000))             # on con tickets.py sin cargar: se rechaza todo
        self.assertEqual(c.sent, [(0x6007, b"\0\0\0\0")])
        self.assertTrue(c.closed)
        self.assertEqual(c.booked_fail, "error interno al comprobar la reserva")
        self.assertTrue(any("tickets.py no cargado" in x for x in s.ns["LOG"]))

    def test_ticket_issue_sin_dt_o_sin_sesion(self):
        s = servidor(DT=None)
        self.assertIsNone(reserva._duel_ticket_issue(Conn()))
        s.ns["DT"] = types.SimpleNamespace(ttl=90)
        self.assertIsNone(reserva._duel_ticket_issue(Conn(sess=SIN)))
        self.assertTrue(any("sin sesión o sin personaje" in x for x in s.ns["LOG"]))


class Mazo(unittest.TestCase):
    def test_deck_check_sin_dk(self):
        s = servidor()
        self.assertIsNone(mazo.deck_check())
        self.assertTrue(any("mazo.py NO cargado" in x for x in s.ns["LOG"]))

    def test_deck_check_con_dk(self):
        cambios = []
        dk = types.SimpleNamespace(mode="avisar", set_mode=lambda m: cambios.append(m), snapshot=lambda: {"n": 0})
        s = servidor(DK=dk)
        self.assertEqual(mazo.deck_check(), "avisar")
        mazo.deck_check("off")
        self.assertEqual(cambios, ["off"])
        self.assertTrue(any("modo avisar -> avisar" in x for x in s.ns["LOG"]))

    def test_iniciar_crea_el_dk_con_el_namespace(self):
        s = servidor()
        s.ns["_load_mazo"] = mazo._load_mazo
        mazo.iniciar()
        self.assertIsNotNone(s.ns["DK"])
        dk = s.ns["DK"]
        mazo.iniciar()
        self.assertIs(s.ns["DK"], dk)
        s2 = servidor()
        s2.ns["_load_mazo"] = lambda: (_ for _ in ()).throw(OSError("no está"))
        mazo.iniciar()
        self.assertIsNone(s2.ns["DK"])
        self.assertTrue(any("sin comprobación del mazo" in x for x in s2.ns["LOG"]))


class Sobres(unittest.TestCase):
    def test_pvp_card_install_idempotente_y_delante_del_0x4802(self):
        h0 = lambda *a: "base"
        LP = types.SimpleNamespace(HANDLERS={0x4802: h0})
        PP = types.SimpleNamespace(pvp_card_take=lambda p, pack: 0)
        s = servidor(LP=LP, PP=PP, _h_selectpack_pvp=sobres._h_selectpack_pvp)
        self.assertTrue(sobres.pvp_card_install())
        h1 = LP.HANDLERS[0x4802]
        self.assertIsNot(h1, h0)
        self.assertTrue(sobres.pvp_card_install())
        h2 = LP.HANDLERS[0x4802]
        self.assertIs(h2._pvp_orig, h0)                                  # no se encadena dos veces
        s.ns["_h_selectpack_pvp"] = lambda c, m, p, d, _orig=None: ("pvp", _orig is h0)
        self.assertEqual(h2(Conn(), 1, 0, b"\0\1"), ("pvp", True))        # y llama al _h_selectpack_pvp del namespace

    def test_pvp_card_install_sin_persistencia_o_sin_0x4802(self):
        servidor(LP=None, PP=None)
        self.assertFalse(sobres.pvp_card_install())
        servidor(LP=types.SimpleNamespace(HANDLERS={}), PP=types.SimpleNamespace(pvp_card_take=None))
        self.assertFalse(sobres.pvp_card_install())

    def test_selectpack_pvp_sin_carta_pendiente_sigue_con_lo_de_siempre(self):
        class Lock:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        PP = types.SimpleNamespace(profile=lambda st, slot, create=False: {"x": 1}, active_slot=lambda st: 0,
                                   pvp_card_take=lambda p, pack: 0)
        servidor(LP=types.SimpleNamespace(_state_lock=lambda: Lock()), PP=PP, load_state=lambda: {}, save_state=lambda st: 1 / 0)
        self.assertEqual(sobres._h_selectpack_pvp(Conn(), 1, 0, b"\0\5", _orig=lambda *a: "orig"), "orig")
        self.assertIsNone(sobres._h_selectpack_pvp(Conn(), 1, 0, b"\0\5"))

    def test_selectpack_pvp_con_carta_pendiente_la_entrega(self):
        class Lock:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        guardado, entregado = [], []
        LP = types.SimpleNamespace(_state_lock=lambda: Lock(), _cards=lambda pack, n: [pack] * n,
                                   _cards_result=lambda c, cards: entregado.append(cards))
        PP = types.SimpleNamespace(profile=lambda st, slot, create=False: {"x": 1}, active_slot=lambda st: 0,
                                   pvp_card_take=lambda p, pack: 2)
        s = servidor(LP=LP, PP=PP, load_state=lambda: {}, save_state=lambda st: guardado.append(1))
        self.assertTrue(sobres._h_selectpack_pvp(Conn(), 1, 0, b"\0\5", _orig=lambda *a: self.fail("no tocaba lo de siempre")))
        self.assertEqual((guardado, entregado), ([1], [[5, 5]]))
        self.assertTrue(any("carta tras el PvP: sobre 5" in x for x in s.ns["LOG"]))

    def test_instalar_npc_envuelve_una_vez(self):
        h0 = lambda *a: "base"
        LP = types.SimpleNamespace(HANDLERS={0x4802: h0})
        s = servidor(LP=LP, NR=object(), npc_rule=lambda k: False)
        sobres.instalar_npc()
        h1 = LP.HANDLERS[0x4802]
        self.assertEqual(h1._npcreglas, 1)
        self.assertIs(s.ns["_h_selectpack_npc"], h1)
        self.assertEqual(h1(Conn(), 1, 0, b""), "base")                   # card apagada: el manejador de siempre
        sobres.instalar_npc()
        self.assertIs(LP.HANDLERS[0x4802], h1)                            # idempotente
        servidor(LP=None, NR=None)
        sobres.instalar_npc()                                             # sin LP ni NR: nada


class Arranque(unittest.TestCase):
    def test_de_deja_los_nombres_en_el_namespace(self):
        s = servidor()
        m = arranque._de("yo2srv.cuentas.personajes", "game_reply", "my_pid")
        self.assertIs(s.ns["game_reply"], personajes.game_reply)
        self.assertIs(s.ns["my_pid"], personajes.my_pid)
        self.assertIs(m, personajes)
        with self.assertRaises(AttributeError):
            arranque._de("yo2srv.cuentas.personajes", "no_existe")

    def test_load_research_con_nombre_propio(self):
        s = servidor()
        mod = arranque.load_research("npcreglas")
        self.assertEqual(mod.__name__, "research_npcreglas")
        self.assertIs(sys.modules["research_npcreglas"], mod)

    def test_arranque_no_pisa_los_alias_de_compat(self):
        # srv.mundo es el módulo MW (compat.ALIAS), pero ns["mundo"] es la FUNCIÓN de yo2srv/mundo/mapas.py:
        # arranque escribe siempre con ns["nombre"]
        src = open(os.path.join(EMU, "yo2srv", "arranque.py"), encoding="utf-8").read()
        usos = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "srv" and n.attr in ("mundo", "lobby", "avatar", "equipos", "intercambio", "torneos")]
        self.assertEqual(usos, [])
        self.assertIn('"mundo"', src)


class FormaDeRunPy(unittest.TestCase):
    """run.py es pequeño y conserva lo que ocg_test/pvp_test/hot_* esperan encontrar en su texto."""

    @classmethod
    def setUpClass(cls):
        cls.texto = open(os.path.join(EMU, "run.py"), encoding="utf-8").read()
        cls.arbol = ast.parse(cls.texto)

    def test_es_pequeno(self):
        lineas = [l for l in self.texto.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertLess(len(self.texto.splitlines()), 90)
        self.assertLess(len(lineas), 45, "run.py solo prepara el entorno y arranca")

    def test_marcador_de_ocg_test_y_pvp_test(self):
        marca = "# ---- FIN DE LA CARGA"
        self.assertEqual(self.texto.count(marca), 1)
        linea = self.texto[:self.texto.index(marca)].count(chr(10)) + 1
        llamadas = lambda antes: {ast.unparse(n.value.func) for n in self.arbol.body    # noqa: E731
                                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                                  and ((n.lineno < linea) == antes)}
        self.assertEqual(llamadas(True), {"arranque.cargar"})        # sin puertos, hilos ni bucle antes del marcador
        # P1: _paquete.comprobar (token de la consola al salir; modo paquete: para sin datos o sin reglas) va antes de abrir puertos
        self.assertEqual(llamadas(False), {"_paquete.comprobar", "arranque.escuchar", "cliente.iniciar", "SRV.run_forever"})
        for fich in ("ocg_test.py", "pvp_test.py"):
            if os.path.isfile(os.path.join(EMU, fich)):                  # (no van en el árbol público)
                self.assertIn(marca, open(os.path.join(EMU, fich), encoding="utf-8").read())

    def test_el_log_sigue_en_run_py_un_import_por_nombre(self):
        # hot_reserva.py recupera `_rx_line` ejecutando la SENTENCIA de run.py que liga ese nombre
        ligados = {}
        for n in self.arbol.body:
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    ligados[a.asname or a.name] = len(n.names)
        for nombre in ("log", "_logseg", "_LOGSEG", "_log_mask", "_rx_line", "_thread_excepthook", "_admin_ev"):
            self.assertEqual(ligados.get(nombre), 1, nombre)

    def test_entorno_y_modulos_del_namespace(self):
        asignados = {t.id for n in self.arbol.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        for nombre in ("HERE", "GAME", "PORT_OFFSET", "BIND_IPS", "PUBLIC_IP", "NO_CLIENT", "LOGDIR", "SRV"):
            self.assertIn(nombre, asignados)
        importados = {a.name for n in self.arbol.body if isinstance(n, ast.Import) for a in n.names}
        for modulo in ("os", "sys", "time", "json", "socket", "struct", "threading"):
            self.assertIn(modulo, importados)
        # P1 (empaquetado): el servidor no necesita frida; solo yo2srv/cliente.py lo importa al lanzar yo2.exe
        self.assertNotIn("frida", importados)


if __name__ == "__main__":
    unittest.main()
