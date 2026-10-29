"""El servidor TCP: aceptar, leer, repartir al manejador del puerto (paso E1b de la reestructura, REESTRUCTURA.md §2.2
y §3.4).

Salido de run.py SIN cambiar la lógica:
  - `PEER_CONNS`, `current_conn`, `live_conn`: la última conexión de cada cliente (IP, puerto) y la «actual» de una
    conexión (la de su sesión, o la última de su IP);
  - `CONNS`: puerto -> última Conn (la consola 5799 manda por ellas: `send(puerto, id, datos)`);
  - límites (`MAX_CONNS_PER_IP`, `MAX_CONNS`, `IDLE_TIMEOUT`, `_OPEN`) y `serve` / `_handle`;
  - `tabla_puertos()`: el dict `HANDLERS` (puerto -> manejador) y `comprobar_puertos()`: los puertos ocupados.

`_handle` busca `HANDLERS[puerto]` en el namespace de run.py en CADA mensaje (`srv.HANDLERS`): el panel envuelve
`HANDLERS[GATE_PORT]` (sec_sistema) y las cargas en caliente cambian entradas del dict. Lo mismo vale para todo lo que
puede reasignarse o parchearse: `log`, `_rx_line`, `on_conn_closed`, `on_conn_accepted`, `current_conn`, `duel_reserva_6001`,
`Conn`, `CTX`, `HUB`, los límites… se leen como `srv.X` en el momento de usarlos (§2.3). Los objetos compartidos
(`CONNS`, `PEER_CONNS`, `_OPEN`) son los MISMOS en este módulo y en el namespace de run.py, que los importa con su nombre
de siempre. Dentro de `serve` el socket de escucha se llama `ls` (en run.py era `srv`, que aquí es el Server).

`on_conn_closed` y `on_conn_accepted` viven desde E6 en yo2srv/duelo/sesion_duelo.py; `duel_reserva_6001` desde E8 en
yo2srv/duelo/reserva.py; `SIN_SESION` / `SinSesion` desde E3 en yo2srv/cuentas/sesion.py (todos se leen como `srv.X`).
"""
import os
import socket
import struct
import sys
import threading
import time
import traceback

from . import proto

srv = None      # el Server; lo fija setup(). Hasta entonces nada de este módulo se puede llamar.


def setup(servidor):
    """Enlaza el módulo con el Server (idempotente). run.py lo llama (yo2srv.red.setup) antes de importar sus nombres."""
    global srv
    srv = servidor
    return servidor


PEER_CONNS = {}   # (IP del cliente, puerto) -> su última Conn

def current_conn(c):
    """La conexión actual del MISMO cliente que c en el puerto de c: la de su sesión si la tiene (0x3003 / 0x6001),
    si no la última de su IP. Antes era CONNS[puerto], la última de cualquiera (V01: con dos clientes, mal)."""
    s = getattr(c, "sess", None)
    if s is not None and c.port in s.conns:
        return s.conns[c.port]
    return srv.PEER_CONNS.get((getattr(c, "peer", None), c.port))

def live_conn(c):
    """La conexión viva del puerto de c: c si sigue siendo la actual; si ESE cliente ya abrió otra en ese puerto,
    esa (p. ej. LOBBY_ROOM["c"] tras reconectar la sala). None si no hay."""
    if c is None or not hasattr(c, "stale"):     # sin Conn (pruebas con conexiones falsas): tal cual
        return c
    if not c.stale():
        return c
    cur = srv.current_conn(c)
    return cur if cur is not None and not cur.closed else None


CONNS = {}  # puerto -> última Conn (para la consola de control)

# Entrada no confiable (el amigo llega por Tailscale): el cliente real nunca manda más de 88 B ni un MD5 malo (todos
# los logs), así que una cabecera con largo > MAX_DATA o un MD5 malo cierran la conexión; tope de conexiones por IP y
# en total; sin nada recibido en IDLE_TIMEOUT s se cierra (el cliente manda 0x0005 cada 60 s, FUN_0068d080). Ninguna
# excepción sale del bucle de escucha ni del lector.
MAX_CONNS_PER_IP = int(os.environ.get("YO2_MAX_CONEXIONES_IP", "32") or 32)   # 07-10: configurables
MAX_CONNS = int(os.environ.get("YO2_MAX_CONEXIONES", "256") or 256)
IDLE_TIMEOUT = 600
_OPEN = {}                                       # IP -> conexiones abiertas
_OPEN_LOCK = threading.Lock()

# Escuchas de este proceso (juego, HTTP, consola). Al apagar hay que cerrarlas: un hilo parado en accept()
# impide que el intérprete termine en macOS y el lanzador se queda en «parando» hasta matar el proceso.
_ESCUCHAS = []
_PARAR = threading.Event()


def anotar_escucha(ls):
    _ESCUCHAS.append(ls)


def parando():
    return _PARAR.is_set()


def cerrar_escuchas():
    """Suelta todos los accept(). Se puede llamar más de una vez."""
    _PARAR.set()
    for ls in list(_ESCUCHAS):
        try:
            ls.close()
        except OSError:
            pass


def rearmar_escuchas():
    """Solo pruebas: el proceso real no vuelve a escuchar después de cerrar."""
    _PARAR.clear()
    _ESCUCHAS.clear()


def serve(port, ip="127.0.0.1"):
    if _PARAR.is_set():
        return
    ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)    # (en run.py se llamaba srv)
    ls.bind((ip, port)); ls.listen(16)
    ls.settimeout(1.0)
    anotar_escucha(ls)
    srv.log(f"[srv] escuchando en {ip}:{port}")
    while not _PARAR.is_set():
        try:
            sock, addr = ls.accept()
        except socket.timeout:
            continue
        except OSError as e:
            if _PARAR.is_set():
                return
            srv.log(f"[srv:{port}] accept: {e!r}"); time.sleep(0.5); continue
        try:
            with srv._OPEN_LOCK:
                n_ip = srv._OPEN.get(addr[0], 0)
                if n_ip >= srv.MAX_CONNS_PER_IP or sum(srv._OPEN.values()) >= srv.MAX_CONNS:
                    srv.log(f"[srv:{port}] conexión de {addr} rechazada: demasiadas abiertas ({n_ip} de esa IP)")
                    sock.close(); continue
                srv._OPEN[addr[0]] = n_ip + 1
            sock.settimeout(srv.IDLE_TIMEOUT)
            srv.log(f"[srv:{port}] conexión de {addr}")
            c = srv.Conn(sock, port)
            old = srv.PEER_CONNS.get((c.peer, port))  # la anterior de ESTE cliente (antes: la última de cualquiera)
            srv.CONNS[port] = c
            srv.PEER_CONNS[(c.peer, port)] = c
            try:
                srv.on_conn_accepted(c, old)         # B58: sesión de duelo abandonada en el socket viejo
            except Exception as e:
                srv.log(f"[srv:{port}] on_conn_accepted: {e!r}")
            threading.Thread(target=srv._handle, args=(c, port), daemon=True, name=f"conn-{port}").start()
        except Exception as e:
            srv.log(f"[srv:{port}] error aceptando {addr}: {e!r}")

def _handle(c, port):
    rd = proto.Reader()
    why = "cerrada"
    try:
        while True:
            try: d = c.sock.recv(65536)
            except socket.timeout: why = f"sin datos en {srv.IDLE_TIMEOUT} s"; break
            except OSError: break
            if not d: break
            # Cabecera con largo > MAX_DATA: se corta ANTES de esperar los datos (si no, 64 KB de basura por mensaje)
            pend = rd.buf + d
            if len(pend) >= proto.HDR and struct.unpack(">H", proto.xor(pend[:proto.HDR])[2:4])[0] > srv.Conn.MAX_DATA:
                why = "cabecera con largo > 1024 B"; break
            bad = False
            for msg_id, param, data, ok in rd.feed(d):
                srv.log(srv._rx_line(port, msg_id, param, data, ok))   # logseguro: 0x3003 = «cuenta <6 hex>», sin bytes
                if not ok or len(data) > srv.Conn.MAX_DATA:
                    why = "MD5 malo o mensaje > 1024 B"; bad = True; break
                c.saw_client(param)
                if msg_id == 0x0003:
                    c.bye = True                 # −242: tras despedirse, la conexión ya no tiene la cuenta «en uso»
                if getattr(c, "booked_fail", None):
                    continue                     # seguridad_duelo: 0x6001 rechazado; nada más de aquí se atiende
                # V02: la sesión de ESTE cliente para load_state()/my_pid() mientras se contesta. En duelo no hay
                # 0x3003: la sesión sale del pid de 0x6001 BOOKED_CHECK (u32). 07-10 (seguridad_duelo): solo si hay
                # reserva de ESE pid desde ESTA IP (duel_reserva_6001), y la comprobación va ANTES de enganchar.
                if port == srv.DUEL_PORT and msg_id == 0x6001 and len(data) >= 4 and c.sess is None:
                    _pid = struct.unpack(">I", data[:4])[0]
                    if not srv.duel_reserva_6001(c, _pid):
                        continue                 # 0x6007 = 0 ya mandado; la sesión de la víctima no se ha tocado
                    srv.HUB.attach(c, _pid)
                srv.CTX.sess = c.sess if c.sess is not None else srv.SIN_SESION   # 07-10: nunca None (= dueño) aquí
                try:
                    srv.HANDLERS[port](c, msg_id, param, data)
                except srv.SinSesion:
                    srv.log(f"[srv:{port}] 0x{msg_id:04x} en una conexión sin sesión: descartado (no toca ningún perfil)")
                except Exception as e:
                    tb = traceback.format_exc().replace(chr(10), " | ")
                    srv.log(f"[srv:{port}] error respondiendo: {e!r} | {tb}")
            if bad:
                break
            # Lo que queda en rd.buf son trozos de mensajes de <= 1024 B (lo demás se corta arriba)
            if len(rd.buf) >= proto.HDR and struct.unpack(">H", proto.xor(rd.buf[:proto.HDR])[2:4])[0] > srv.Conn.MAX_DATA:
                why = "cabecera con largo > 1024 B"; break
    except Exception as e:
        why = f"error en el lector: {e!r}"
    finally:
        srv.CTX.sess = getattr(c, "sess", None) or srv.SIN_SESION   # on_conn_closed: en nombre de c, nunca del dueño
        srv.log(f"[srv:{port}] cerrada" + (f" ({why})" if why != "cerrada" else ""))
        try:
            c.sock.close()
        except OSError:
            pass
        with srv._OPEN_LOCK:
            ip = getattr(c, "peer", "?")
            srv._OPEN[ip] = max(0, srv._OPEN.get(ip, 1) - 1)
        try:
            srv.on_conn_closed(c)                    # B58: c.closed, lobby (salas), duelo sin acabar = h_disconnect
        except Exception as e:
            srv.log(f"[srv:{port}] on_conn_closed: {e!r}")


def tabla_puertos():
    """HANDLERS: puerto -> manejador. Los manejadores (gate_reply, game_reply, lobby_reply, duel_reply) son los del
    namespace de run.py EN ESTE MOMENTO; la tabla es un dict normal que el panel y las cargas en caliente modifican.
    Orden de las claves (lo respeta el arranque de los hilos `serve-<puerto>`): puerta, cuentas, lobby, duelo, salas."""
    h = {srv.GATE_PORT: srv.gate_reply, srv.GAME_PORT: srv.game_reply, srv.LOBBY_PORT: srv.lobby_reply}
    h[srv.DUEL_PORT] = srv.duel_reply
    from . import puerta
    for p in puerta.room_ports():      # ROOM_PORT_BASE + i y, con ROOM_PER_LANG, los 77 de las salas de idioma (10-10)
        h[p] = srv.lobby_reply
    return h


def comprobar_puertos():
    """Puertos ocupados: en Windows SO_REUSEADDR deja enlazar un puerto que ya usa otro proceso, así que se comprueba
    antes (en cada IP de YO2_BIND; 0.0.0.0 se comprueba por loopback). Sale del proceso (sys.exit con el motivo) si
    alguno está en uso. Los de cada IP se prueban A LA VEZ (10-10): en Windows un connect_ex a un puerto libre tarda
    hasta 1 s, y con ROOM_PER_LANG son 93 puertos; el aviso sigue saliendo para el primero ocupado en el orden de siempre."""
    from concurrent.futures import ThreadPoolExecutor
    from . import puerta

    def _ocupado(dst):
        t = socket.socket(); t.settimeout(1)
        try:
            return t.connect_ex(dst) == 0
        finally:
            t.close()

    for _ip in srv.BIND_IPS:
        ps = [srv.GATE_PORT, srv.GAME_PORT, srv.LOBBY_PORT, srv.HTTP_PORT, srv.DUEL_PORT] + puerta.room_ports()
        host = "127.0.0.1" if _ip == "0.0.0.0" else _ip
        with ThreadPoolExecutor(max_workers=min(len(ps), 128)) as ex:
            usados = list(ex.map(_ocupado, [(host, p) for p in ps]))
        for p, usado in zip(ps, usados):
            if usado:
                sys.exit(f"El puerto {_ip}:{p} ya está en uso: ¿queda otro run.py vivo? (YO2_PORT_OFFSET para una 2.ª instancia)")
    t = socket.socket(); t.settimeout(1)
    if t.connect_ex(("127.0.0.1", srv.ADMIN_PORT)) == 0:
        sys.exit(f"El puerto de la consola 127.0.0.1:{srv.ADMIN_PORT} ya está en uso: ¿queda otro run.py vivo?")
    t.close()
