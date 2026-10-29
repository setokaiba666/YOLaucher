"""Server: el contenedor del proceso (REESTRUCTURA.md §2.3). Paso S1: existe y está enlazado, no hace nada de juego.

    from yo2srv.server import Server
    SRV = globals().get("SRV") or Server(globals())       # run.py, tras definir `log`

`srv.ns` es EL dict compartido. Durante la migración es `globals()` de run.py (lo que la consola 5799, el panel y los
`install(ns)` de research ya usan); al final (paso F1) lo creará `Server()` y run.py se quedará en ~50 líneas.

    srv.HUB / srv.hub        -> ns["HUB"]       (lo que no está en compat.ALIAS se busca con su nombre tal cual)
    srv.HUB = x              -> ns["HUB"] = x
    srv.cfg / bus / router / hot / rutas      servicios propios (atributos reales, NO se meten en el namespace)

Server NO añade nada al namespace salvo lo que se le pida con `registrar_nombres()`: lo único nuevo en `globals()` de
run.py tras S1 es `SRV` (lo asigna la propia línea de run.py). El contrato del namespace (paridad/contrato_ns.txt) lo
vigila.

Regla de oro de los módulos que se muevan (§2.3): todo lo que alguien pueda REASIGNAR en caliente (NPCS, PLAYER_ID,
wallet, duel_reply, DF, LP, save_state_raw envuelto por el panel, banderas…) se lee SIEMPRE como `srv.X` en el momento de
usarlo, nunca como global del módulo ni con `from x import nombre`.
"""
import os
import sys
import threading
import time

from . import bus as _bus
from . import compat
from . import config as _config
from . import hot as _hot
from . import router as _router
from . import rutas as _rutas

# Atributos reales del objeto (todo lo demás se lee y se escribe en el namespace).
_PROPIOS = frozenset(("ns", "cfg", "bus", "router", "hot", "rutas"))


class Server:
    """Contenedor del proceso. Ver el docstring del módulo."""

    def __init__(self, ns, *, rutas=None, env=None):
        if not isinstance(ns, dict):
            # varios install(ns) de research hacen `NS = ns if isinstance(ns, dict) else dict(ns)`: una vista que no
            # fuera dict se COPIARÍA y dejaría de estar viva (plan §2.3)
            raise TypeError(f"srv.ns tiene que ser un dict de verdad, no {type(ns).__name__}")
        object.__setattr__(self, "ns", ns)
        object.__setattr__(self, "rutas", rutas if rutas is not None else _rutas.desde())
        object.__setattr__(self, "bus", _bus.Bus(log=self._log_dif))
        object.__setattr__(self, "router", _router.Router(log=self._log_dif))
        object.__setattr__(self, "cfg", _config.Config(ns, entorno=env))
        object.__setattr__(self, "hot", _hot.Hot(self))

    # ------------------------------------------------------------------ namespace
    def __getattr__(self, k):
        # solo se llama cuando el atributo no existe como tal: `ns` aún sin fijar (copy/pickle) no debe recursar
        if k.startswith("__") or k in _PROPIOS:
            raise AttributeError(k)
        ns = object.__getattribute__(self, "ns")
        nombre = compat.nombre_ns(k)
        try:
            return ns[nombre]
        except KeyError:
            raise AttributeError(f"srv.{k}: no hay {nombre!r} en el namespace") from None

    def __setattr__(self, k, v):
        if k in _PROPIOS:
            object.__setattr__(self, k, v)
        else:
            self.ns[compat.nombre_ns(k)] = v

    def __dir__(self):
        return sorted(set(object.__dir__(self)) | set(self.ns))

    def registrar_nombres(self, **nombres):
        """Deja en el namespace estos nombres (los que la consola, el panel y los hot_*.py esperan encontrar)."""
        self.ns.update(nombres)

    def existe(self, k):
        return compat.nombre_ns(k) in self.ns

    # ------------------------------------------------------------------ log
    def log(self, *a):
        """El `log` de run.py (la función viva del namespace); sin él, a stderr. Nunca lanza."""
        f = self.ns.get("log")
        try:
            if callable(f):
                f(*a)
            else:
                print(*a, file=sys.stderr)
        except Exception:
            pass

    def _log_dif(self, texto):
        # el bus y el router anotan por aquí; el `log` se busca en cada llamada (run.py puede redefinirlo)
        self.log(texto)

    # ------------------------------------------------------------------ bucle principal
    def run_forever(self):
        """El bucle principal de run.py (paso E8). B28: antes run.py se apagaba solo a los `secs` segundos
        (time.sleep(secs)), en mitad del juego. Ahora:
            python run.py 0      -> sin límite
            python run.py N      -> se apaga a los N s (sin argumento, 40 s, como antes); aviso en el log 10 min antes
        y la hora de apagado se puede mover desde la consola: DEADLINE += 3600 (una hora más) o DEADLINE = 0 (sin límite).
        `secs`, `DEADLINE` y `_warned_for` viven en el namespace de run.py (la consola los cambia con exec en él), y el bucle
        los lee de ahí en cada vuelta. Con --login (y cliente) lanza login.py cuando el log dice «merge.txt»."""
        ns = self.ns
        secs = ns["secs"] = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 40
        ns["DEADLINE"] = time.time() + secs if secs > 0 else 0
        if "--login" in sys.argv and not ns.get("NO_CLIENT"):
            from . import cliente
            cliente.auto_login()
        self.log("[run] sin límite de tiempo" if not ns["DEADLINE"] else
                 f"[run] se apaga a las {time.strftime('%H:%M:%S', time.localtime(ns['DEADLINE']))} "
                 f"(DEADLINE += 3600 desde la consola para alargar)")
        ns["_warned_for"] = None
        while not ns["DEADLINE"] or time.time() < ns["DEADLINE"]:
            dl = ns["DEADLINE"]
            if dl and dl - time.time() <= 600 and ns["_warned_for"] != dl and ns["secs"] > 600:
                ns["_warned_for"] = dl
                self.log(f"[AVISO] run.py se apaga a las {time.strftime('%H:%M:%S', time.localtime(dl))} "
                         f"(quedan {int(dl - time.time())} s): DEADLINE += 3600 desde la consola para alargar")
            time.sleep(1)
        self.log("[run] fin de la captura")
        # escuchar() dejó los hilos de accept() en marcha. En macOS el intérprete no acaba con esos hilos
        # bloqueados: el lanzador se queda en «parando» hasta los 30 s y luego mata el proceso.
        if ns.get("_SALIDA_PROCESO"):
            self.salir_proceso()

    def salir_proceso(self):
        """Cierra las escuchas, borra el token de la consola y sale. Un temporizador mata el proceso si
        alguna de esas piezas se queda colgada (p. ej. shutdown() de la API de gestión)."""
        hecho = threading.Event()

        def fuerza():
            if not hecho.wait(3):
                os._exit(0)

        threading.Thread(target=fuerza, name="salida-forzada", daemon=True).start()
        try:
            from .red.servidor import cerrar_escuchas
            cerrar_escuchas()
        except Exception as e:
            self.log(f"[run] al cerrar escuchas: {e!r}")
        try:
            from .admin import gestion
            gestion.detener()
        except Exception as e:
            self.log(f"[run] al parar la gestión: {e!r}")
        try:
            from . import paquete
            paquete.borrar_token(self.ns.get("ADMIN_PORT"))
        except Exception:
            pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        hecho.set()
        os._exit(0)

    # ------------------------------------------------------------------ diagnóstico
    def estado(self):
        """Resumen para la consola: qué servicios hay y qué tienen registrado (S1: nada)."""
        return {
            "version": sys.modules[__package__].__version__,
            "perfil": self.cfg.perfil,
            "banderas": len(_config.REGISTRO),
            "bus": self.bus.eventos(),
            "router": {f"{t}:{i:#06x}": d for (t, i), d in self.router.tabla().items()},
            "modulos_hot": sorted(self.hot.registro),
            "ns": len(self.ns),
        }

    def __repr__(self):
        return f"<Server S1 ns={len(self.ns)} nombres, perfil={self.cfg.perfil}>"
