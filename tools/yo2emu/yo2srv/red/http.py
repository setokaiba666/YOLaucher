"""Servidor HTTP del cliente (paso E1b de la reestructura, REESTRUCTURA.md §2.2 y §3.4): sirve tools/yo2emu/www/<ruta>
en HTTP_PORT (8730 + desplazamiento) y registra cada petición en el log (lo que no existe, 404). Solo GET/HEAD.

Es el `Http` + `serve_http` de run.py de siempre. `WWW` es la carpeta `www` de tools/yo2emu (la misma fórmula que
`rutas.desde().www`); `HTTP_PORT` y `log` se leen del namespace de run.py en el momento de usarlos (`srv.X`).
"""
import http.server
import os

from .. import rutas

srv = None      # el Server; lo fija setup(). Hasta entonces nada de este módulo se puede llamar.


def setup(servidor):
    """Enlaza el módulo con el Server (idempotente). run.py lo llama (yo2srv.red.setup) antes de importar `serve_http`."""
    global srv
    srv = servidor
    return servidor


WWW = os.path.join(rutas.EMU, "www")


class Http(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=srv.WWW, **k)
    def log_message(self, fmt, *a): srv.log("[http] " + (fmt % a))
    def do_PUT(self): self.send_error(405)
    def do_POST(self): self.send_error(405)


def serve_http(ip="127.0.0.1"):
    s = http.server.ThreadingHTTPServer((ip, srv.HTTP_PORT), Http)
    from .servidor import anotar_escucha
    anotar_escucha(s.socket)
    s.serve_forever()
