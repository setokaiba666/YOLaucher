"""yo2srv/red (paso E1b): Conn, servidor TCP (_handle, tabla de puertos, puertos ocupados), puerta 5730, auto_reply y HTTP.

Sin instancia ni puertos fijos: sockets de la propia prueba (socketpair, puertos efímeros) y un Server con un namespace
falso (srv.ns), que es justo lo que los módulos ven en run.py. La instancia de verdad la prueban T2/T3/T10.yo2srv_vivo.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
import urllib.request

EMU = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if EMU not in sys.path:
    sys.path.insert(0, EMU)
from yo2srv.red import auto, conexion, http, proto, puerta, servidor  # noqa: E402
from yo2srv.server import Server  # noqa: E402


class SinSesion(RuntimeError):
    pass


def espacio(**extra):
    """El namespace mínimo de run.py que usan los módulos de red (y su log recogido en ns["LOG"])."""
    log = []
    ns = {"LOG": log, "log": lambda *a: log.append(" ".join(str(x) for x in a)), "_rx_line": lambda *a: "rx",
          "current_conn": lambda c: None, "_duel_ticket_issue": lambda c: None, "CTX": threading.local(),
          "SIN_SESION": object(), "SinSesion": SinSesion, "DUEL_PORT": 5760, "Conn": conexion.Conn,
          "IDLE_TIMEOUT": 5, "MAX_CONNS_PER_IP": 32, "MAX_CONNS": 256, "_OPEN": {}, "_OPEN_LOCK": threading.Lock(),
          "PEER_CONNS": {}, "CONNS": {}, "HANDLERS": {}, "on_conn_closed": lambda c, why="cerrada": None,
          "on_conn_accepted": lambda c, old: None, "duel_reserva_6001": lambda c, pid: True,
          "HUB": types.SimpleNamespace(attach=lambda c, pid: None)}
    ns.update(extra)
    return ns


def enlazar(ns):
    s = Server(ns)
    for m in (conexion, servidor, puerta, auto, http):
        m.setup(s)
    return s


def leer(sock, n=1, seg=3.0):
    """Los n primeros mensajes (id, param, datos) que llegan por sock."""
    rd, out, fin = proto.Reader(), [], time.time() + seg
    sock.settimeout(0.2)
    while len(out) < n and time.time() < fin:
        try:
            d = sock.recv(65536)
        except socket.timeout:
            continue
        if not d:
            break
        out += [(i, p, x) for i, p, x, ok in rd.feed(d) if ok]
    return out


def esperar(cond, seg=3.0):
    fin = time.time() + seg
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class TestConn(unittest.TestCase):
    def setUp(self):
        self.ns = espacio()
        enlazar(self.ns)
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.c = conexion.Conn(self.a, 5732)
        self.addCleanup(conexion.Conn.GATE_SEQ.pop, (self.c.peer, 5732), None)
        conexion.Conn.GATE_SEQ.pop((self.c.peer, 5732), None)

    def test_numeracion_por_conexion_y_gate_seq(self):
        self.c.send(0x2002, b"ab", pause=0)
        self.c.send(0x2004, b"", pause=0)
        self.assertEqual(leer(self.b, 2), [(0x2002, 1, b"ab"), (0x2004, 2, b"")])
        self.assertEqual(conexion.Conn.GATE_SEQ[(self.c.peer, 5732)], 2)       # el MISMO dict de clase de siempre
        self.c.saw_client(1)                                                    # el cliente vuelve a empezar desde #1
        self.assertEqual(conexion.Conn.GATE_SEQ[(self.c.peer, 5732)], 0)
        self.c.send(0x2004, b"", pause=0)
        self.assertEqual(leer(self.b), [(0x2004, 1, b"")])
        self.assertTrue(hasattr(self.c, "rx_t"))

    def test_mas_de_1024_bytes_no_se_manda(self):
        self.c.send(0x2003, bytes(conexion.Conn.MAX_DATA + 1), pause=0)
        self.assertEqual(leer(self.b, 1, seg=0.5), [])
        self.assertTrue(any("NO se manda" in x for x in self.ns["LOG"]), self.ns["LOG"])
        self.assertEqual(conexion.Conn.GATE_SEQ.get((self.c.peer, 5732), 0), 0, "el número no se gasta")

    def test_0x4306_crea_la_reserva_antes_de_mandar(self):
        vistos = []
        self.ns["_duel_ticket_issue"] = lambda c: vistos.append((c, leer(self.b, 1, seg=0.05)))
        self.c.send(0x4306, b"x", pause=0)
        self.assertEqual(len(vistos), 1)
        self.assertIs(vistos[0][0], self.c)
        self.assertEqual(vistos[0][1], [], "la reserva se crea ANTES de que el 0x4306 salga por el socket")
        self.assertEqual(leer(self.b), [(0x4306, 1, b"x")])

    def test_conexion_vieja_o_cerrada_no_manda(self):
        otra = types.SimpleNamespace(closed=False)
        self.ns["current_conn"] = lambda c: otra                 # este cliente ya usa otra conexión de este puerto
        self.assertTrue(self.c.stale())
        self.c.send(0x2002, b"", pause=0)
        self.assertEqual(leer(self.b, 1, seg=0.3), [])
        self.assertTrue(any("vieja" in x for x in self.ns["LOG"]))
        self.ns["current_conn"] = lambda c: None
        self.c.closed = True
        self.c.send(0x2002, b"", pause=0)
        self.assertTrue(any("cerrada" in x for x in self.ns["LOG"]))

    def test_sondeo_mudo_no_deja_vieja_a_la_que_habla(self):
        """13-10: el lanzador abre y cierra una conexión a la puerta cada 10 s desde 127.0.0.1 sin decir nada; si caía
        tras el 0x2005 del juego, la lista de servidores (0x2003) se descartaba y el juego se quedaba cargando."""
        sondeo = types.SimpleNamespace(closed=True)              # nunca mandó nada (sin rx_t)
        self.ns["current_conn"] = lambda c: sondeo
        self.c.saw_client(1)                                     # el juego ya habló (0x2005)
        self.assertFalse(self.c.stale())
        self.c.send(0x2003, b"lista", pause=0)
        self.assertEqual(leer(self.b), [(0x2003, 1, b"lista")])
        sondeo.rx_t = 1.0                                        # una conexión nueva que SÍ habla la deja vieja
        self.assertTrue(self.c.stale())

    def test_post_manda_en_orden_por_su_cola(self):
        for i in range(3):
            self.c.post(0x5300 + i, bytes([i]), pause=0)
        self.assertEqual(leer(self.b, 3), [(0x5300, 1, b"\0"), (0x5301, 2, b"\1"), (0x5302, 3, b"\2")])
        self.c.stop_post()


class TestCurrentConn(unittest.TestCase):
    def setUp(self):
        self.ns = espacio()
        enlazar(self.ns)

    def test_current_conn_prefiere_la_de_la_sesion(self):
        c = types.SimpleNamespace(port=5731, peer="1.2.3.4", sess=None)
        otra = types.SimpleNamespace(port=5731, peer="1.2.3.4", sess=None)
        self.assertIsNone(servidor.current_conn(c))
        self.ns["PEER_CONNS"][("1.2.3.4", 5731)] = otra          # sin sesión: la última de su IP
        self.assertIs(servidor.current_conn(c), otra)
        c.sess = types.SimpleNamespace(conns={5731: c})          # con sesión: la de la sesión
        self.assertIs(servidor.current_conn(c), c)
        c.sess = types.SimpleNamespace(conns={})                 # sesión sin conexión en ese puerto: cae a la IP
        self.assertIs(servidor.current_conn(c), otra)

    def test_live_conn(self):
        self.assertIsNone(servidor.live_conn(None))
        falsa = types.SimpleNamespace(port=1)                    # sin Conn (pruebas con conexiones falsas): tal cual
        self.assertIs(servidor.live_conn(falsa), falsa)
        vieja = types.SimpleNamespace(port=5732, stale=lambda: True, closed=False)
        nueva = types.SimpleNamespace(port=5732, closed=False)
        self.ns["current_conn"] = lambda c: nueva                # lo que reasigne el namespace manda (srv.current_conn)
        self.assertIs(servidor.live_conn(vieja), nueva)
        nueva.closed = True
        self.assertIsNone(servidor.live_conn(vieja))
        viva = types.SimpleNamespace(port=5732, stale=lambda: False)
        self.assertIs(servidor.live_conn(viva), viva)

    def test_los_objetos_compartidos_son_los_del_modulo(self):
        """run.py importa PEER_CONNS y CONNS del módulo: en el namespace real son EL MISMO dict."""
        self.assertIsInstance(servidor.PEER_CONNS, dict)
        self.assertIsInstance(servidor.CONNS, dict)
        self.assertIsNot(servidor.PEER_CONNS, servidor.CONNS)


class TestHandle(unittest.TestCase):
    def setUp(self):
        self.ns = espacio()
        self.cerradas, self.llamadas = [], []
        self.ns["on_conn_closed"] = lambda c, why="cerrada": self.cerradas.append(c)
        enlazar(self.ns)
        self.cli, self.sv = socket.socketpair()
        self.addCleanup(self.cli.close)
        self.addCleanup(self.sv.close)
        self.port = 5732
        self.c = conexion.Conn(self.sv, self.port)
        conexion.Conn.GATE_SEQ.pop((self.c.peer, self.port), None)
        self.addCleanup(conexion.Conn.GATE_SEQ.pop, (self.c.peer, self.port), None)
        self.h = threading.Thread(target=servidor._handle, args=(self.c, self.port), daemon=True)

    def manda(self, msg_id, data=b"", param=1):
        self.cli.sendall(proto.pack(msg_id, data, param))

    def test_busca_HANDLERS_en_cada_mensaje(self):
        """El panel envuelve HANDLERS[puerto]: _handle tiene que mirar el dict en CADA mensaje, no al empezar."""
        def uno(c, msg_id, param, data):
            self.llamadas.append(("uno", msg_id, param, data, self.ns["CTX"].sess))

        def dos(c, msg_id, param, data):
            self.llamadas.append(("dos", msg_id, param, data))
        self.ns["HANDLERS"][self.port] = uno
        self.h.start()
        self.manda(0x0005, b"\x01", 1)
        self.assertTrue(esperar(lambda: len(self.llamadas) == 1), self.llamadas)
        self.ns["HANDLERS"][self.port] = dos
        self.manda(0x0005, b"\x02", 2)
        self.assertTrue(esperar(lambda: len(self.llamadas) == 2), self.llamadas)
        self.assertEqual([x[0] for x in self.llamadas], ["uno", "dos"])
        self.assertEqual(self.llamadas[0][1:4], (0x0005, 1, b"\x01"))
        self.assertIs(self.llamadas[0][4], self.ns["SIN_SESION"], "sin sesión: CTX.sess es el marcador, nunca None")
        self.manda(0x0003, b"", 3)
        esperar(lambda: getattr(self.c, "bye", False))
        self.assertTrue(self.c.bye, "−242: tras 0x0003 la conexión ya no tiene la cuenta «en uso»")
        self.cli.close()
        self.h.join(5)
        self.assertFalse(self.h.is_alive())
        self.assertEqual(self.cerradas, [self.c], "al salir del bucle se llama a on_conn_closed")
        self.assertEqual(self.ns["_OPEN"].get(self.c.peer, 0), 0)

    def test_errores_del_manejador_no_cierran_la_conexion(self):
        def malo(c, msg_id, param, data):
            self.llamadas.append(msg_id)
            raise (SinSesion("x") if msg_id == 0x0010 else ValueError("boom"))
        self.ns["HANDLERS"][self.port] = malo
        self.h.start()
        self.manda(0x0010, b"", 1)
        self.manda(0x0011, b"", 2)
        self.manda(0x0012, b"", 3)
        self.assertTrue(esperar(lambda: len(self.llamadas) == 3), self.llamadas)
        self.assertTrue(any("sin sesión: descartado" in x for x in self.ns["LOG"]), self.ns["LOG"])
        self.assertTrue(any("error respondiendo" in x and "boom" in x for x in self.ns["LOG"]), self.ns["LOG"])
        self.cli.close()
        self.h.join(5)

    def test_md5_malo_o_cabecera_larga_cierran(self):
        self.ns["HANDLERS"][self.port] = lambda *a: self.llamadas.append(a)
        self.h.start()
        mal = bytearray(proto.pack(0x0005, b"abc", 1))
        mal[-1] ^= 0xff                                     # un byte de los datos cambiado: el MD5 ya no cuadra
        self.cli.sendall(bytes(mal))
        self.h.join(5)
        self.assertFalse(self.h.is_alive())
        self.assertEqual(self.llamadas, [])
        self.assertTrue(any("MD5 malo" in x for x in self.ns["LOG"]) or any("cerrada" in x for x in self.ns["LOG"]))
        self.assertEqual(self.cerradas, [self.c])

    def test_0x6001_en_el_puerto_de_duelo_pide_reserva(self):
        c = conexion.Conn(self.sv, 5760)
        self.addCleanup(conexion.Conn.GATE_SEQ.pop, (c.peer, 5760), None)
        enganchados = []
        self.ns["HUB"] = types.SimpleNamespace(attach=lambda cc, pid: enganchados.append(pid))
        self.ns["duel_reserva_6001"] = lambda cc, pid: pid == 1000
        self.ns["HANDLERS"][5760] = lambda cc, msg_id, param, data: self.llamadas.append(msg_id)
        h = threading.Thread(target=servidor._handle, args=(c, 5760), daemon=True)
        h.start()
        self.manda(0x6001, (999).to_bytes(4, "big"), 1)           # sin reserva de ese pid: no se engancha ni se atiende
        self.manda(0x6001, (1000).to_bytes(4, "big"), 2)
        self.assertTrue(esperar(lambda: self.llamadas == [0x6001]), self.llamadas)
        self.assertEqual(enganchados, [1000])
        self.cli.close()
        h.join(5)


class TestPuertos(unittest.TestCase):
    def test_puertos_con_desplazamiento(self):
        self.assertEqual(puerta.puertos(0), (5730, 5731, 5732, 5740, 8730, 5799))
        self.assertEqual(puerta.puertos(2200), (7930, 7931, 7932, 7940, 10930, 7999))
        self.assertEqual(len(puerta.ROOM_KINDS), 11)
        self.assertEqual(puerta.ROOM_KINDS[:4], (13, 7, 15, 8))            # puerta azul
        self.assertEqual(puerta.GATE_PAUSE, 0.03)

    def test_tabla_de_puertos(self):
        g, gm, lb, du = (lambda *a: "gate"), (lambda *a: "game"), (lambda *a: "lobby"), (lambda *a: "duel")
        g0, g1, g2, rb = puerta.puertos(50)[:4]
        ns = espacio(GATE_PORT=g0, GAME_PORT=g1, LOBBY_PORT=g2, ROOM_PORT_BASE=rb, ROOM_KINDS=puerta.ROOM_KINDS,
                     DUEL_PORT=5810, gate_reply=g, game_reply=gm, lobby_reply=lb, duel_reply=du)
        enlazar(ns)
        h = servidor.tabla_puertos()
        self.assertEqual(list(h)[:4], [g0, g1, g2, 5810], "orden de las claves: puerta, cuentas, lobby, duelo, salas")
        self.assertEqual([h[p] for p in list(h)[:4]], [g, gm, lb, du])
        self.assertEqual(list(h)[4:], [rb + i for i in range(11)])
        self.assertTrue(all(h[rb + i] is lb for i in range(11)))

    def test_tabla_de_puertos_con_salas_por_idioma(self):
        """ROOM_PER_LANG (10-10): las 11 de siempre (mundial) + 7 idiomas × 11 desde ROOM_LANG_PORT_BASE."""
        lb = lambda *a: "lobby"                                            # noqa: E731
        g0, g1, g2, rb = puerta.puertos(50)[:4]
        lpb = puerta.puerto_idiomas(50)
        self.assertEqual((puerta.puerto_idiomas(0), lpb), (5800, 5850))
        ns = espacio(GATE_PORT=g0, GAME_PORT=g1, LOBBY_PORT=g2, ROOM_PORT_BASE=rb, ROOM_KINDS=puerta.ROOM_KINDS,
                     ROOM_LANG_PORT_BASE=lpb, ROOM_PER_LANG=True, gate_reply=None, game_reply=None, lobby_reply=lb,
                     duel_reply=None, room_lang=lambda lang: 0)
        enlazar(ns)
        h = servidor.tabla_puertos()
        self.assertEqual(list(h)[4:], [rb + i for i in range(11)] + [lpb + i for i in range(77)])
        self.assertTrue(all(h[p] is lb for p in list(h)[4:]))
        # puerto <-> (idioma de la sala, tipo); la mundial (6) en los de siempre; coreano (7) en la última franja
        self.assertEqual(puerta.room_port(6, 13), rb)
        self.assertEqual(puerta.room_port(0, 13), lpb)
        self.assertEqual(puerta.room_port(5, 7), lpb + 5 * 11 + 1)
        self.assertEqual(puerta.room_port(7, 20), lpb + 6 * 11 + 10)
        for lang in range(8):
            for i, kind in enumerate(puerta.ROOM_KINDS):
                p = puerta.room_port(lang, kind)
                self.assertEqual(puerta.room_of_port(p), (lang, i))
                self.assertEqual(puerta.room_block_key(p, 3), (rb + i, lang), "clave = (puerto de siempre, idioma de la sala)")
        self.assertIsNone(puerta.room_of_port(lpb + 77))
        self.assertIsNone(puerta.room_of_port(g2))
        ns["HANDLERS"] = {rb: lb}                                          # encendida en caliente sin reiniciar
        self.assertEqual(puerta.room_port(5, 13), rb, "sin el puerto de idioma escuchando, se anuncia el de siempre")
        ns["ROOM_PER_LANG"] = False                                        # apagada: como antes
        self.assertEqual(puerta.room_of_port(rb + 2), (None, 2))
        self.assertIsNone(puerta.room_of_port(lpb))
        self.assertEqual(puerta.room_block_key(rb + 2, 5), (rb + 2, 0))

    def libres(self, n):
        socks = [socket.socket() for _ in range(n)]
        for s in socks:
            s.bind(("127.0.0.1", 0))
        ps = [s.getsockname()[1] for s in socks]
        for s in socks:
            s.close()
        return ps

    def espacio_puertos(self, ps, ips):
        g, gm, lb, ro, ht, ad, du = ps
        return espacio(GATE_PORT=g, GAME_PORT=gm, LOBBY_PORT=lb, ROOM_PORT_BASE=ro, HTTP_PORT=ht, ADMIN_PORT=ad,
                       DUEL_PORT=du, ROOM_KINDS=(1,), BIND_IPS=ips)

    def escucha(self, puerto):
        s = socket.socket()
        s.bind(("127.0.0.1", puerto))
        s.listen(1)
        self.addCleanup(s.close)
        return s

    # (en Windows cada connect_ex a un puerto libre tarda ~0,7 s: se prueba por separado la puerta, que se mira la primera,
    # y la consola, sin ninguna IP que comprobar antes, para que la prueba no tarde 15 s)
    def test_puerto_de_juego_ocupado(self):
        ps = self.libres(7)
        enlazar(self.espacio_puertos(ps, ["127.0.0.1"]))
        self.escucha(ps[0])
        with self.assertRaises(SystemExit) as cm:
            servidor.comprobar_puertos()
        self.assertIn(f"127.0.0.1:{ps[0]} ya está en uso", str(cm.exception))

    def test_consola_ocupada_y_libre(self):
        ps = self.libres(7)
        enlazar(self.espacio_puertos(ps, []))
        servidor.comprobar_puertos()                                 # consola libre: no sale
        self.escucha(ps[5])
        with self.assertRaises(SystemExit) as cm:
            servidor.comprobar_puertos()
        self.assertIn(f"127.0.0.1:{ps[5]} ya está en uso", str(cm.exception))
        self.assertIn("consola", str(cm.exception))


class TestAuto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auto-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def escribe(self, rel, obj):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_cargar_aplica_las_correcciones_0x(self):
        self.escribe("auto_replies.json", {"0x0001": [{"id": "0x0002", "name": "A", "fmt": ["u32"]}], "otra": 1})
        self.escribe("research/lobby/auto_overrides.json",
                     {"0x0001": [{"id": "0x0003", "name": "B", "fmt": ["u8"], "hex": "07"}], "nota": "no se aplica"})
        a, ov = auto.cargar(self.tmp)
        self.assertEqual(a["0x0001"][0]["id"], "0x0003")
        self.assertNotIn("nota", a)
        self.assertEqual(sorted(ov), ["0x0001", "nota"])

    def test_ficheros_que_faltan_dan_vacios(self):
        self.assertEqual(auto.cargar(self.tmp), ({}, {}))

    def test_los_reales_cargan(self):
        a, ov = auto.cargar()
        self.assertGreater(len(a), 50)
        for k, v in ov.items():
            if k.startswith("0x"):
                self.assertEqual(a[k], v)

    def test_auto_reply(self):
        ns = espacio(AUTO={"0x0010": [{"id": "0x0011", "name": "X", "fmt": ["u32", "u16", "cstr"]},
                                      {"id": "0x0012", "name": "Y", "fmt": ["u8"], "hex": "0a0b"}]})
        enlazar(ns)
        enviados = []
        c = types.SimpleNamespace(port=5732, send=lambda m, d=b"": enviados.append((m, d)))
        self.assertTrue(auto.auto_reply(c, 0x0010))
        self.assertEqual(enviados, [(0x0011, bytes(7)), (0x0012, b"\x0a\x0b")])
        self.assertFalse(auto.auto_reply(c, 0x0099))
        ns["AUTO"] = {"0x0099": [{"id": "0x0100", "name": "Z", "fmt": ["u8"]}]}     # lo que se reasigne en ns manda
        self.assertTrue(auto.auto_reply(c, 0x0099))
        self.assertEqual(enviados[-1], (0x0100, bytes(1)))
        self.assertTrue(any("auto 0x0010 -> 0x0011 X (7 B a cero)" in x for x in ns["LOG"]), ns["LOG"])


class TestHttp(unittest.TestCase):
    def test_sirve_www_y_registra_en_el_log(self):
        tmp = tempfile.mkdtemp(prefix="www-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "hola.txt"), "w", encoding="utf-8") as f:
            f.write("hola")
        ns = espacio(WWW=tmp)
        enlazar(ns)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), http.Http)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        self.assertEqual(urllib.request.urlopen(url + "/hola.txt", timeout=5).read(), b"hola")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(url + "/no-existe", timeout=5)
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(url + "/hola.txt", data=b"x"), timeout=5)
        self.assertEqual(cm.exception.code, 405)
        self.assertTrue(any(x.startswith("[http] ") for x in ns["LOG"]), ns["LOG"])

    def test_www_es_la_carpeta_de_siempre(self):
        self.assertEqual(os.path.normcase(http.WWW), os.path.normcase(os.path.join(EMU, "www")))


class FakeConn:
    def __init__(self):
        self.out = []

    def send(self, msg_id, data=b"", pause=0.25):
        self.out.append((msg_id, bytes(data), pause))


class TestGate(unittest.TestCase):
    def prepara(self, merge):
        hub = types.SimpleNamespace(lock=threading.Lock(), blocks={(5740, 0): {"a": 1, "b": 2}, (5741, 5): {"c": 3}})
        ns = espacio(PUBLIC_IP="127.0.0.9", GAME_PORT=5731, LOBBY_PORT=5732, ROOM_PORT_BASE=5740,
                     ROOM_KINDS=puerta.ROOM_KINDS, GATE_PAUSE=0.0, HUB=hub,
                     room_lang=(lambda lang: 0) if merge else (lambda lang: lang))
        enlazar(ns)
        return ns

    def entradas(self, c):
        datos = b"".join(d for m, d, _ in c.out if m == 0x2003)
        out = []
        for i in range(0, len(datos), 48):
            e = datos[i:i + 48]
            out.append({"kind": int.from_bytes(e[4:8], "big"), "name": e[8:24].rstrip(b"\0").decode("latin1"),
                        "ip": e[24:39].rstrip(b"\0").decode(), "port": int.from_bytes(e[39:41], "big"),
                        "extra": int.from_bytes(e[41:43], "big"), "b1": e[43], "b2": e[44]})
        return out

    def test_lista_de_la_puerta(self):
        self.prepara(True)
        c = FakeConn()
        puerta.gate_reply(c, 0x2005, 1, b"")
        ids = [m for m, _, _ in c.out]
        self.assertEqual(ids[0], 0x2002)
        self.assertEqual(ids[-1], 0x2004)
        self.assertEqual(ids[1:-1], [0x2003] * 5, "2 fijas + 8 idiomas x 11 salas = 90 entradas, 21 por mensaje")
        self.assertTrue(all(p == 0.0 for m, _, p in c.out if m != 0x2004), "la lista va seguida: pausa GATE_PAUSE")
        self.assertEqual(c.out[-1][2], 0.25, "el 0x2004 final va con la pausa por defecto de siempre")
        e = self.entradas(c)
        self.assertEqual(len(e), 90)
        self.assertEqual((e[0]["kind"], e[0]["name"], e[0]["ip"], e[0]["port"]), (1, "DuelCards", "127.0.0.9", 5731))
        self.assertEqual((e[1]["kind"], e[1]["name"], e[1]["port"], e[1]["b2"]), (3, "MyRoom", 5732, 12))
        self.assertEqual((e[2]["name"], e[2]["port"], e[2]["b1"], e[2]["b2"]), ("Sala 0-13", 5740, 0, 13))

    def test_jugadores_por_sala_con_y_sin_ROOM_LANG_MERGE(self):
        self.prepara(True)                       # master 526d16ac: el bloque es (puerto, 0) para todos los idiomas
        c = FakeConn()
        puerta.gate_reply(c, 0x2005, 1, b"")
        con_merge = [(x["name"], x["extra"]) for x in self.entradas(c) if x["extra"]]
        self.assertEqual(con_merge, [(f"Sala {lang}-13", 2) for lang in range(8)])
        self.prepara(False)                      # YO2_ROOM_LANG_MERGE=0: el bloque es (puerto, idioma del cliente)
        c = FakeConn()
        puerta.gate_reply(c, 0x2005, 1, b"")
        sin_merge = [(x["name"], x["extra"]) for x in self.entradas(c) if x["extra"]]
        self.assertEqual(sin_merge, [("Sala 0-13", 2), ("Sala 5-7", 1)])

    def test_salas_por_idioma(self):
        """ROOM_PER_LANG: cada (idioma, tipo) anuncia su puerto y cuenta los de SU bloque (idioma de la sala)."""
        ns = self.prepara(True)
        ns.update(ROOM_PER_LANG=True, ROOM_LANG_PORT_BASE=5800, HANDLERS={})
        ns["HUB"].blocks = {(5740, 5): {"a": 1, "b": 2}, (5740, 3): {"c": 3}, (5740, 6): {"d": 4}}
        c = FakeConn()
        puerta.gate_reply(c, 0x2005, 1, b"")
        e = self.entradas(c)
        self.assertEqual(len(e), 90)
        salas = {(x["b1"], x["b2"]): x for x in e[2:]}
        self.assertEqual(salas[(6, 13)]["port"], 5740, "la mundial sigue en el puerto de siempre")
        self.assertEqual(salas[(5, 13)]["port"], 5800 + 5 * 11)
        self.assertEqual(salas[(3, 13)]["port"], 5800 + 3 * 11)
        self.assertEqual(len({x["port"] for x in e[2:]}), 88, "un puerto por sala")
        self.assertEqual([(x["b1"], x["b2"], x["extra"]) for x in e if x["extra"]], [(3, 13, 1), (5, 13, 2), (6, 13, 1)])

    # Lo que pinta el cliente con esa lista (lobby_door_room_list, FUN_0082e170; research/lobby/NOTES.md «Salas de las
    # puertas»): por cada tipo de la puerta y cada nº de sala 0-7, la entrada de tipo 3 con b1 = idioma ELEGIDO en la
    # puerta, b2 = tipo, b3 = nº; solo si (LV <= 1 y b2 == 1) o (LV >= 2 y b2 != 1); texto «%s (%4d/1000)» con el u16.
    # Nombre = texto del tipo (lobby_room_name, FUN_00a54f90) + letra de la sala: 7 y 15 comparten el 3052 y 9 y 16 el 3053.
    PUERTAS = {"azul": (13, 7, 15, 8), "roja": (14, 1, 9, 16, 17, 10, 20)}
    TEXTO = {13: 3065, 7: 3052, 15: 3052, 8: 3054, 14: 3066, 1: 3050, 9: 3053, 16: 3053, 17: 7060, 10: 3055, 20: 7072}

    def pinta(self, e, puerta_, b1, lv):
        filas = []
        for kind in self.PUERTAS[puerta_]:
            for area in range(8):
                x = next((x for x in e if x["kind"] == 3 and x["b1"] == b1 and x["b2"] == kind and x["b3"] == area), None)
                if x is not None and ((lv <= 1 and kind == 1) or (lv >= 2 and kind != 1)):
                    filas.append((self.TEXTO[kind], "ABCDEFGH"[area], x["extra"]))
        return filas

    def test_lo_que_ve_el_cliente_segun_su_nivel_12_10(self):
        """12-10 (owner y un amigo LV 1): el amigo (LV 1) en la Sala de Principiantes española, el owner (LV >= 2) en la
        puerta roja española ve «0/1000» en todas: la de principiantes NO le sale (filtro del cliente, 1:1), no es un fallo
        del contador. Con LV 1 la ve con 1/1000. «Sala TRIAL A» dos veces = tipos 9 y 16 con el mismo texto (3053), 1:1."""
        ns = self.prepara(True)
        ns.update(ROOM_PER_LANG=True, ROOM_LANG_PORT_BASE=5800, HANDLERS={})
        ns["HUB"].blocks = {(5745, 5): {"amigo": 1}}             # ROOM_KINDS[5] = 1 (Principiantes), idioma 5 (español)
        c = FakeConn()
        puerta.gate_reply(c, 0x2005, 1, b"")
        datos = b"".join(d for m, d, _ in c.out if m == 0x2003)
        e = [dict(x, b3=datos[i * 48 + 45]) for i, x in enumerate(self.entradas(c))]
        lv20 = self.pinta(e, "roja", 5, 20)
        self.assertEqual([n for _t, _a, n in lv20], [0] * 6, "LV >= 2: 6 salas, ninguna la de principiantes")
        self.assertEqual([t for t, _a, _n in lv20].count(3053), 2, "TRIAL sale dos veces (tipos 9 y 16): como el original")
        self.assertEqual(self.pinta(e, "roja", 5, 1), [(3050, "A", 1)], "LV 1: solo Principiantes, con el amigo dentro")
        self.assertEqual(self.pinta(e, "azul", 5, 1), [], "LV 1 en la azul: nada («Sala no disponible»)")
        self.assertEqual(self.pinta(e, "roja", 3, 1), [(3050, "A", 0)], "otro idioma elegido en la puerta: otro bloque")

    def test_otros_mensajes_no_hacen_nada(self):
        self.prepara(True)
        c = FakeConn()
        puerta.gate_reply(c, 0x0005, 1, b"")
        self.assertEqual(c.out, [])


class TestApagado(unittest.TestCase):
    def test_cerrar_escuchas_acaba_el_accept(self):
        ns = espacio()
        enlazar(ns)
        servidor.rearmar_escuchas()
        self.addCleanup(servidor.rearmar_escuchas)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        hilo = threading.Thread(target=servidor.serve, args=(port, "127.0.0.1"), daemon=True)
        hilo.start()
        fin = time.time() + 3
        while time.time() < fin and not any(f":{port}" in x for x in ns["LOG"]):
            time.sleep(0.02)
        self.assertTrue(any("escuchando" in x for x in ns["LOG"]), ns["LOG"])
        servidor.cerrar_escuchas()
        hilo.join(3)
        self.assertFalse(hilo.is_alive())


class TestImportar(unittest.TestCase):
    def test_importar_la_red_no_abre_ficheros_ni_arranca_hilos(self):
        sonda = ("import os, sys, threading\n"
                 "antes = set(os.listdir('.')); h = threading.active_count()\n"
                 "import yo2srv.red, yo2srv.red.conexion, yo2srv.red.servidor, yo2srv.red.puerta, yo2srv.red.auto, yo2srv.red.http\n"
                 "print(threading.active_count() - h, sorted(set(os.listdir('.')) - antes - {'__pycache__'}))\n")
        p = subprocess.run([sys.executable, "-c", sonda], cwd=EMU, capture_output=True, text=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "0 []")


if __name__ == "__main__":
    unittest.main()
