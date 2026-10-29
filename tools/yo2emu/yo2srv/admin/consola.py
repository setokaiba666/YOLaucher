"""Consola de control en 127.0.0.1:5799 (+ YO2_PORT_OFFSET) con token (paso E8 de la reestructura, REESTRUCTURA.md §3.4).

Salida de run.py SIN cambiar el protocolo: cada línea es Python que se ejecuta con acceso a todo el namespace de run.py
(`exec(línea, srv.ns)`; `srv.ns` es EL dict de globals() de run.py, así que `HUB`, `DF["finish"](CONNS[5760], …)`,
`DEADLINE += 3600`, `respawn_client()` y los `exec(open('hot_x.py').read(), globals())` de admin.py siguen igual).
`send(puerto, id, datos)` manda por la última conexión de ese puerto (numerada). SIEMPRE en loopback, pase lo que pase con
YO2_BIND: es ejecución remota de código para quien llegue (P0-3).

Protocolo (el de admin.py, sin cambios):
  - El token es nuevo en cada arranque y vive en %LOCALAPPDATA%\\YO2Online\\consola_<puerto>.txt (el que lee admin.py).
  - La PRIMERA línea tiene que ser «AUTH <token>» (comparación en tiempo constante, máx. 512 B); si no, o si parece HTTP,
    se cierra sin ejecutar nada (2026-10-07: antes cualquier programa del PC, y una web abierta en el navegador con un
    POST a 127.0.0.1:5799, ejecutaba Python aquí).
  - Cada línea siguiente se ejecuta y se contesta «ok\\n» o «error: <excepción>\\n». Ningún fallo de socket sale del bucle
    (2026-10-06: admin.py lee UNA respuesta y cierra; con varias líneas el 2.º sendall daba ConnectionReset fuera del try
    y mataba el hilo: la consola dejaba de escuchar).

Reglas (REESTRUCTURA.md §2.3): `log`, `ADMIN_PORT`, `HERE` y `CONNS` se leen como `srv.X` en el momento de usarlos.
`admin` y `send` están en el namespace de run.py (el contrato de la consola: paridad/contrato_ns.txt).
"""
import hmac
import os
import secrets
import socket

srv = None      # el Server; lo fija setup(). Hasta entonces nada de este módulo se puede llamar.


def setup(servidor):
    """Enlaza el módulo con el Server (idempotente). arranque.py lo llama antes de arrancar la consola."""
    global srv
    srv = servidor
    return servidor


def guardar_token(puerto=None):
    """Genera el token de esta ejecución y lo deja en %LOCALAPPDATA%\\YO2Online\\consola_<puerto>.txt (sin LOCALAPPDATA,
    junto a run.py). Devuelve el token. Si no se puede escribir, solo avisa en el log: la consola sigue con ese token."""
    tok = secrets.token_urlsafe(32)
    tdir = os.path.join(os.environ.get("LOCALAPPDATA") or srv.HERE, "YO2Online")
    try:
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, f"consola_{srv.ADMIN_PORT if puerto is None else puerto}.txt"), "w", encoding="ascii") as f:
            f.write(tok)
    except OSError as e:
        srv.log(f"[admin] no se pudo guardar el token de la consola: {e!r}")
    return tok


def atender(c, tok):
    """Una conexión de la consola: AUTH y luego una línea de Python por mensaje. Nunca lanza ni deja la conexión abierta."""
    # 2026-10-06: admin.py lee UNA respuesta y cierra; con varias líneas el 2.º sendall daba ConnectionReset fuera
    # del try y mataba este hilo (la consola dejaba de escuchar). Ahora ningún fallo de socket sale del bucle.
    try:
        c.settimeout(10)
        lines = c.makefile(encoding="utf-8", errors="replace")
        first = lines.readline(512).strip()
        if not (first.startswith("AUTH ") and hmac.compare_digest(first[5:].encode(), tok.encode())):
            srv.log("[admin] conexión a la consola rechazada (sin token válido)")
            return
        c.settimeout(None)
        for line in lines:
            try:
                exec(line, srv.ns)
                reply = b"ok" + bytes([10])
            except Exception as e:
                reply = f"error: {e!r}".encode() + bytes([10])
            try:
                c.sendall(reply)
            except OSError:
                pass
    except Exception as e:
        srv.log(f"[admin] {e!r}")
    finally:
        c.close()


def admin():
    """Hilo de la consola: escucha en 127.0.0.1:ADMIN_PORT y atiende una conexión tras otra."""
    # 2026-10-07: con token. Antes cualquier programa del PC (y una web abierta en el navegador con un POST a
    # 127.0.0.1:5799: las líneas del cuerpo se ejecutaban) podía ejecutar Python aquí. Ahora la PRIMERA línea tiene que
    # ser «AUTH <token>» (token nuevo en cada arranque, en %LOCALAPPDATA%\YO2Online\consola_<puerto>.txt, que lee
    # admin.py); si no, o si parece HTTP, se cierra sin ejecutar nada.
    tok = guardar_token()
    ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", srv.ADMIN_PORT)); ls.listen(2)
    ls.settimeout(1.0)
    from ..red.servidor import anotar_escucha, parando
    anotar_escucha(ls)
    while not parando():
        try:
            c, _ = ls.accept()
        except socket.timeout:
            continue
        except OSError:
            if parando():
                return
            continue
        atender(c, tok)


def send(port, msg_id, data=b""):
    srv.CONNS[port].send(msg_id, data)
