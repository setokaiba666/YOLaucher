"""PvP fase 2 (BACKLOG V05-V09; research/audit/social_pvp.md P0-6, P0-7, P0-8, P1-9 y §3 «Plan mínimo» paso 6).

- Matches: reservas {pidA, pidB} que deja el reto aceptado (research/lobby/proposal.py _challenge_reply) y enrutado de
  las conexiones de duelo: la que manda 0x6001 con un pid reservado pasa entera a su PvpMatch (run.py duel_reply).
- PvpMatch: el arranque sincronizado de los dos clientes y el duelo (ocg_duel.PvpSession, dos vistas del traductor).
  UN hilo trabajador por partida: los dos sockets encolan aquí y todo (duelcore incluido) va en orden y sin candados.
  Cada cliente tiene UNA cola de envío (ocg_duel.Sender) para todo lo del puerto de duelo, también el arranque: dos
  colas a la misma conexión desordenarían los mensajes.

Arranque, lado servidor (lo que hace el cliente: research/lockstep NOTES.md y social_pvp.md P0-6):
  0x6001 u32 pid      -> 0x6007 s32 1 (con 0: «LB_DUEL_BOOKEDCHECK_WAIT ERROR» y se cierra)
  0x6601 / 0x6603     -> mazo del jugador (u32 n + n × u16 id YO2)
  0x600b              -> cuando han llegado LOS DOS: a cada uno 0x600c (A = él, B = el otro; FUN_006918d0, 168 B) y
                         0x6005 (u16 principal, u16 extra) DEL MAZO DEL OTRO (FUN_0079cbf0 monta el mazo tapado del rival
                         con ese tamaño; sin él se sale de memoria)
  0x6100 / 0x6102     -> eco a cada uno (+0x1e / +0x20 = 2)
  0x6101 u8 mano      -> con las dos: a cada uno 0x6101 (0, mano del otro). El cliente calcula (rival − mía) mod 3:
                         1 = gana él y elige, 2 = espera 0x6103, 0 = empate: vuelve solo al estado 2 y manda otra 0x6101
                         (FUN_007c76b0 estado 5, +0x8f8 == 0)
  0x6103 u8 v (solo el ganador; 0 = empiezo yo) -> eco al ganador (como con la NPC: lo lee +0x67b y ya ha salido del
                         estado 7, inofensivo) y al perdedor 0x6103 (1 − v): índice ABSOLUTO desde cada cliente
                         (research/lockstep §0.4; FUN_007c76b0 estado 7, rama +0x8f8 == 2 lee +0x67b). Al perdedor se
                         le manda después de su propio eco de 0x6102 (orden de siempre)
  0x61fe              -> cuando están LOS DOS: PvpSession.start(first)
Fin (V08, P0-8): a cada uno su fin con SU lobby (sesión): 0x4140 PLAYERDATAUPDATE por el lobby, la cola de fin de
research/duelfin (DUEL_END, 0x6613 DUELSCORE: las ventanas PvP CWindowResult lo pintan, y 0x4d) por el duelo y,
detrás, 0x4800 por el lobby (CResultScene espera lobby+0x39 == 2 para «Siguiente»). Sin 0x4237 ni NPCBIN.
Rendición y desconexión (P1-9): al que se queda, 0x649b DUELSVRINFOMSG (u8 código, u8 bit0 = el rival en su vista;
FUN_0078c140: 2 = «%s se ha rendido» 6513, 1 = «%s se ha desconectado» 4519) y su victoria.
Límite de tiempo (07-10, research/traductor/TIEMPO.md «Implementado»): el reloj lo lleva ocg_duel.PvpSession; este
hilo recibe («clock», gen, t0) cuando la petición sale hacia el cliente y («tick», gen) de su temporizador. Al
agotarse: 0x649b código 3 («Se acabó el tiempo de %s», 6514) A LOS DOS y derrota del que se quedó sin tiempo, por el
mismo camino que la rendición (_timeout).
Match al mejor de 3 (10-10, PVP_MATCH; research/duelfin/NOTES.md §5b): sala con dstyle 4. Entre partidas los dos
clientes reenvían el mazo (0x6600/01/03, sin 0x600b) y 0x6102; elige quién empieza el que PERDIÓ la partida anterior
(0x6103), y con los dos 0x61fe arranca la siguiente. El cliente cuenta el match él solo (0x4d P1 = 0).
"""
import contextlib
import os
import queue
import struct
import threading
import time
import traceback

TEST_AUTO = os.environ.get("YO2_PVP_TEST", "") not in ("", "0")   # solo la instancia de prueba (fake_client pvp)
M_TEST_AUTO = 0x7ffe          # prueba: «que decida bot.ts por mí». Fuera de YO2_PVP_TEST se ignora (no existe en yo2.exe)
ATTACH_TIMEOUT = float(os.environ.get("YO2_PVP_ATTACH_TIMEOUT") or 90)   # s para que los dos conecten al duelo
# 0x649b al que se queda. Formato C (FUN_0078c140; protocol.json: 2 B, research/duelfin selftest) y textos C. Lo que
# hace el cliente al pulsar «OK» (+0xc5, +0xc0..c2) está SIN seguir (D) y el diálogo solo sale con la cola de 0x6202
# vacía: por eso va antes de la cola de fin, con una pausa. False = sin aviso (solo la victoria).
PVP_SVRINFO = True
# Prueba 3: tras el primer PvP real los dos se quedaron en WINNER/LOSER sin «Siguiente». CResultScene::Update
# (0x8c413d-0x8c4178) solo pone +0xcd (y luego enseña «Siguiente» y manda 0x0003) si el modo DAT_012a9084 es 0 o 1
# (duelo de NPC) o si ND+0x678 & 1. En PvP el modo no es 0/1; ese bit lo pone 0x6012 YG_MSG_DUELLOG_FINISH
# (FUN_00694840, despachado en FUN_00691310; sin datos: solo begin/end) al cerrar el registro del duelo
# (duellogs/list.txt). Sin 0x6010/0x6011 antes, el registro que escribe el cliente sale vacío: P (sin probar si eso
# molesta). False = como antes (atascado).
PVP_DUELLOG_FINISH = True    # 06-10: 0x6010 + (0x6013) 0x6012. En el PC OK; el portatil salia por un perfil copiado de otro PC
# Prueba 5 (en vivo con el flag a mano): PC: 0x6010 -> <- 0x6013 01 -> 0x6012 -> «Siguiente» y duellogs/pvptest.log;
# portátil: igual y el proceso SALE limpio (sin WER). Lo que se sabe (C): CResultScene (FUN_008c1a70) abre en PvP
# un CWindowModelProgress «Descargando un Registro de Duelo...» (texto 5040) con «Cancelar» (botón 0xfa3, texto 4003);
# Update (0x8c3f30) pinta +0xbce8/+0xbcec y, si se pulsa Cancelar, pone ND+0x678 |= 3 (0x8c3fae-0x8c3ffd) ->
# «Siguiente» SIN registro: es la salida 1:1 sin tocar el servidor. Salidas limpias del exe: FUN_00b0ae12 = exit(1)
# tras los asertos de yo2TaskPtr («NULL access»/«Wild Task», FUN_0060cff0); list.txt inexistente NO sale
# (FUN_00a2e900: si no abre, devuelve 0). Causa exacta en el portátil: sin ver (P: un aserto de TaskPtr).
# Prueba 4: por qué petó el 0x6012 suelto (P, el log del portátil no está): FUN_00694840 recorre +0xbcec bytes del
# búfer +0xbcf4 (0x694fd9-0x694ff7) y luego lo libera (0x69500c, FUN_00b097fa). Ese búfer y ese número SOLO los pone
# 0x6010 DUELLOG_HEADER (FUN_006944b0: +0xbcec = u32 n, +0xbcf4 = calloc(1, n + 0x40)); sin cabecera eran basura o
# restos: lectura/liberación de un puntero malo. Ahora, con el flag, se manda 0x6010 con n = 0 (búfer de 0x40 B,
# vacío) y DESPUÉS 0x6012, ambos al final (el oficial no sabía n hasta acabar: el registro completo, P). Formato de
# 0x6010 según FUN_006944b0 (el protocol.json se salta un u32: FUN_0068a930 ESCRIBE 4 B en el cursor = 4 B que se
# ignoran): u16 +0xba94, u16 +0xba96, cstr +0xbaa2, u32 n +0xbcec, u32 +0xbcf8, u32 +0xba98, u32 +0xbbbc,
# u32 +0xbbc0, 16 B +0xbbc4, 16 B +0xbbd5, u32 +0xba9c, 4 B (ignorados), u32 +0xbbac, u8, u8, u8.  C (formato) /
# D (sentido de los campos: nombres y pids en +0xbbc4/+0xbbd5 y +0xbbbc/+0xbbc0 son suposición)
# 06-10 (repeticiones, research/repeticion/NOTES.md): con PVP_DUELLOG_RECORD = True el registro va COMPLETO: se
# apuntan todos los 0x6202 que salen hacia cada jugador (lado 0/1), y al acabar cada cliente recibe 0x6010 (cabecera
# con los tamaños reales) -> 0x6011 × k (blob RLE en trozos de 1024 B) -> 0x6016 (u32 0) -> 0x6012, uno tras cada
# 0x6013. El cliente lo guarda en duellogs/<título>.log + list.txt y se ve desde el menú sin servidor (modo 10/11).
# Formato C (descompilado); varios campos del blob D (ver NOTES). False = flujo de PVP_DUELLOG_FINISH sin cambios.
PVP_DUELLOG_RECORD = True   # 06-10: activado con el OK del owner
# 07-10 (OK del owner; NUESTRO, el oficial no guardaba nada: research/repeticion/NOTES.md §8 y rep_archivo.py): el
# SERVIDOR guarda su propia copia de cada duelo (PvP aquí, NPC en npc_watch.py) para el panel alojado:
# <datos>/repeticiones/AAAA-MM/<pvp|npc>_<AAAAMMDD-HHMMSS>_<n>.log (el .log exacto del cliente) + indice.jsonl. Lo que
# reciben los clientes no cambia. YO2_REPLAY_ARCHIVE=0 lo apaga (el panel lee esta misma bandera).
REPLAY_ARCHIVE = os.environ.get("YO2_REPLAY_ARCHIVE", "1") not in ("", "0")
SVRINFO_WAIT = 1.5
SVR_DISCONNECT, SVR_SURRENDER = 1, 2
# 24-10 (agente MIS, research/duelfin/NOTES.md «Reglas de rendición del oficial»): el oficial limitaba la rendición de dos
# maneras. (1) Texto dlg 82 «Puedes rendirte a partir del 10º turno»: el cliente NO lo comprueba (FUN_007390a0 / menú: solo
# manda 0x6350 (…, 10)); lo decía el servidor, como un cartel de aceptar (CUOkDialog, formato de los duellogs: 0x60 P0 = 1,
# 0x61 (texto, 2), 0x1b B = 0x32 P = (0, texto, 0)). Duellogs oficiales: las 6 rendiciones de oct-nov 2008 son del turno 11
# en adelante (10 cambios de turno o más); las de 2009 llegan a ser del turno 1 (35, 26 antes del 10º; la primera, el
# 2009-03-21): la regla se quitó entre 2008-11-18 y 2009-03-21 (P). SURRENDER_MIN_TURN = turno global de ocgcore a partir del cual se puede (10; 0 = siempre);
# YO2_SURRENDER_MIN_TURN lo cambia. En la instancia de PRUEBA (YO2_PVP_TEST) el valor por defecto es 0: las pruebas se rinden
# en su primera decisión. (2) Opción de sala «Rendirse ON/OFF» (textos 5081/5082; byte 21 de 0x4288, DAT_012bb6b0+0xc1, que el
# cliente solo manda y NUNCA lee en el duelo): con OFF no hay rendición; se quita «Rendirse» del menú del mazo (0x6611) y,
# si llegara la petición, se descarta. YO2_SURRENDER_ROOM=0 la ignora (como antes).
DLG_SURRENDER_TURN = 82       # dlg_text: «Puedes rendirte a partir del 10º turno.»  C
_SMT = (os.environ.get("YO2_SURRENDER_MIN_TURN") or "").strip()
SURRENDER_MIN_TURN = int(_SMT) if _SMT else (0 if TEST_AUTO else 10)
SURRENDER_ROOM_OPTION = os.environ.get("YO2_SURRENDER_ROOM", "1") not in ("", "0")
# 24-10 (agente SUR, research/duelfin/NOTES.md «Rendición en un match»): los jugadores avisaron de que rendirse en un match
# lo perdía entero aunque fuera la 1.ª partida. El oficial NO hacía eso (C, duellogs oficiales de match de 2008-2009: 29
# registros, 17 con rendiciones, 26 en total): al rendirse en la partida 1 (12 de 12) o en la 2 de un match 1-1 (5 de 5) el MISMO
# registro sigue con la partida siguiente; rendirse es perder esa partida y el match lo decide el marcador (2 victorias o
# 3 partidas), como cualquier otro fin. Por eso SURRENDER_ENDS_MATCH vale False (0x4d con P1 de partida, el cliente
# cuenta); YO2_SURRENDER_ENDS_MATCH=1 devuelve lo de antes (0x4d P1 = 5: el match acaba ya con derrota del que se rinde).
# La desconexión a mitad de partida NO cambia (sigue cerrando el match, P1 = 5: sin evidencia oficial, el rival no puede jugar).
SURRENDER_ENDS_MATCH = os.environ.get("YO2_SURRENDER_ENDS_MATCH", "0") not in ("", "0")
# Cómo manda el oficial el fin por rendición (51/51 duellogs, duelo suelto y match, C): SIN el 0x23 (LP a 0) y con el 0x05 de
# motivo 12 (perdedor (2, 0, 12), ganador (1, 12, 0)) y el 0x4d con P1 = 2. Antes: 0x23 + 0x05 motivo 0 + 0x4d P1 = 0 (P).
# El 12 no tiene animación propia (solo 3..10 abren la vista 0x3b, C); si el resultado enseñara el motivo, sería D.
# YO2_SURRENDER_FIN_OFICIAL=0 devuelve la secuencia de antes.
SURRENDER_FIN_OFICIAL = os.environ.get("YO2_SURRENDER_FIN_OFICIAL", "1") not in ("", "0")
SVR_TIMEUP = 3                # 0x649b código 3: «Se acabó el tiempo de %s» (texto 6514, FUN_0078c140). C
GUNSLINGER_KINDS = (7, 15)    # b2 de las salas «Sala GUNSLINGER» (research/lobby/NOTES.md, texto 3052). C
LP_START = 8000
COUNTRY = b"ESP" + bytes(1)
# Espectador (research/lobby/NOTES.md §8). Apagado: YO2_WATCH=1 (lo mismo que research/lobby/proposal.WATCH, que es
# quien da paso con 0x4321). Cliente (C): tras 0x4321 conecta al puerto de duelo y manda 0x6700 (FUN_006926d0, sin
# datos; ND+0x676 |= 0x20 = modo espectador), espera 0x6006 (+0x22), juega el piedra-papel-tijera con 0x6701
# (manos y quién empieza) y en el duelo pide más con 0x6702 cada vez que se le vacía la cola de 0x6202 (como mucho
# cada 10 s si no llega nada; un 0x6701 o un 0x6202 le dejan volver a pedir). Servidor (P): se le manda lo que ve el
# jugador 0 (como la repetición), en tandas de WATCH_BATCH (la cola del cliente tiene 512 huecos y no comprueba).
PVP_WATCH = os.environ.get("YO2_WATCH", "1") not in ("", "0")   # 06-10: activado por defecto con el OK del owner
WATCH_BATCH = 64
WATCH_REQ_TTL = 60.0          # s entre 0x4320 y el 0x6700 del mismo cliente
# 25-10 (rx/ESP): el espectador NO recibe lo que le pide una decisión a un jugador. Su cola es la del jugador 0 (como la
# repetición), y ahí van las ventanas SÍ/NO, de opciones, de anuncio, de posición, de descarte, el aviso de aceptar
# (CUOkDialog) y las listas de cartas (CWindowDeckList) con sus peticiones de jugada (tipo 0x1a). Todo eso sale del
# mismo DUEL_VIEW 0x32 (CUYesNoDialog / CUOkDialog / CUSelect*Dialog en duel_view_dispatch 0x73dd80) o 0x33 (lista),
# sin ninguna comprobación de modo espectador (C): el cliente abre la ventana y manda su 0x6423 / 0x6432 a un servidor
# que no lo espera, y la que pide lista (0x33) sale en blanco porque el 0x6610 con las cartas no es un 0x6202 y no se
# le manda (el propio traductor ya vio esa «ventana en blanco que no se cerraba»). En los 327 duellogs oficiales las
# ventanas SÍ/NO (tipo 2) van solo al lado que decide (230 al 0, 267 al 1) y las de aviso de cadena (tipo 3) a los dos
# jugadores, cada uno con la suya (C); los registros solo traen esos dos lados, así que qué mandaba Konami a un
# espectador: D. 0x32 con P0 = 0xff (el rótulo «Selecciona…», CULockOnDialog) no es modal ni pide nada: se queda.
# YO2_WATCH_SIN_DECISIONES=0 devuelve el espectador de antes (todo lo del jugador 0).
WATCH_SIN_DECISIONES = os.environ.get("YO2_WATCH_SIN_DECISIONES", "1") not in ("", "0")
T_REQUEST, T_VIEW, VIEW_DIALOG, VIEW_LIST, DLG_LOCKON = 0x1a, 0x1b, 0x32, 0x33, 0xff


def watch_oculta(d):
    """True si este 0x6202 (40 B: u32 tipo, u32 B, 8 × u32 P) NO debe llegar a un espectador: la petición de jugada
    (tipo 0x1a; el ejecutor del cliente en modo 7/8 la salta, C) y cualquier ventana de decisión (DUEL_VIEW 0x32 con
    P0 distinto de 0xff, y la lista 0x33). El resto, la jugada, pasa tal cual."""
    if len(d) < 12:
        return False
    tipo, b, p0 = struct.unpack_from(">III", d, 0)
    tipo &= 0xff                                          # solo cuenta el byte bajo (lockstep/proposal.setcommand)
    if tipo == T_REQUEST:
        return True
    if tipo == T_VIEW:
        return (b == VIEW_DIALOG and p0 != DLG_LOCKON) or b == VIEW_LIST
    return False
# NUESTRO, no 1:1: el 0x6006 no trae vidas y la escena de duelo las lee de ND+0x570/+0x604, que solo pone 0x600c
# (FUN_006918d0 / FUN_0071fe10). Con esto se le manda también un 0x600c (vista del jugador 0) antes del 0x6006.
WATCH_SEND_PREP = os.environ.get("YO2_WATCH_PREP", "") not in ("", "0")
# 10-10: MATCH AL MEJOR DE 3 (research/duelfin/NOTES.md §5b). Qué hace el cliente (C):
# - la sala: byte 0 de 0x4288 = dstyle (3 duelo, 4 match; log vivo 20261009-120748: 04 en la sala de pvp-3); el cliente
#   lo pone en DAT_012bb6b0+0x9c (decomp c/00a30000.c:8353, c/00a40000.c:846) y FUN_00794d90(+0x9c) da el modo 4 = match
#   (FUN_007904f0: modos 1/4/8/0xb). Para empezar un match exige 3 DP (1 en duelo simple).
# - fin de cada partida: 0x4d P1 = 0 -> FUN_00794a90 apunta el resultado y decide él (2 victorias, o 3 partidas);
#   sin decidir -> CDuelEndScene -> CCardSwapScene (0x25) -> FUN_00794d90(5) (partida + 1) -> piedra-papel-tijera.
# - partida >= 2 (init de CJankenScene, 0x7c45ae-0x7c4611 y 0x7c554e-0x7c55a6, sin decompilar: desensamblado): si la
#   anterior fue victoria o derrota, reenvía el mazo (FUN_00692840: 0x6600/01/03, SIN 0x600b) y manda 0x6102; salta al
#   estado 7 con +0x8f8 = 1 (elige) si PERDIÓ la anterior y 2 (espera el 0x6103 del servidor) si la ganó. Si fue
#   empate: reenvía el mazo, sin 0x6102, y piedra-papel-tijera normal (0x6101).
# - 0x649b código 1 en la partida >= 2 (estado 9 de CJankenScene): apunta victorias suyas hasta 3 y va a resultados.
# Servidor (P salvo donde se dice): eco de 0x6102, 0x6103 del que elige -> eco y (1 - v) al otro (_maybe_6103), los
# dos 0x61fe -> _start. 1 DP y estadísticas por partida; carta (3, persistencia §14) y 0x4800 solo al acabar el match;
# un registro de duelo (modo match) para todo el match. Rendición o desconexión: el match acaba ya (0x4d P1 = 5, D);
# tiempo agotado: pierde la partida (D).
# "1" (por defecto) = al mejor de 3; "0" = como antes (una partida y se cierra; el cliente se queda entre partidas);
# "cerrar" = una partida y 0x4d P1 = 5 (el cliente da el match por acabado con esa partida, C).
PVP_MATCH = (os.environ.get("YO2_PVP_MATCH", "1") or "0").strip().lower()
MATCH_STYLE = 4
MATCH_WAIT = float(os.environ.get("YO2_PVP_MATCH_WAIT") or 300)   # s entre partidas hasta los dos 0x61fe (D)
NEXT_MSGS = (0x6600, 0x6601, 0x6603, 0x6100, 0x6101, 0x6102, 0x6103, 0x61fe)   # la partida siguiente ya viene
# 24-10 (bug "Side decking sometimes not possible", research/duelfin/NOTES.md §5c). CCardSwapScene (el cambio con el side
# deck) se cierra SOLA cuando FUN_00692e30(1) llega a 0 (FUN_008d4b80, 0x8d4b9f-0x8d4c60: duel_mode_set + escena 0x1e), y
# FUN_00692e30 devuelve el reloj GLOBAL del duelo, ND+0x534. Ese reloj solo lo escribe 0x6461 con quién = 3 (FUN_00692bf0);
# si el servidor nunca lo manda vale 0 (en el piedra-papel-tijera sale «00:00», visto en vivo) y la escena del side deck se
# va en el primer fotograma. El cliente solo crea ese reloj (CJankenTimer, 0x8d3b15) si la sala tiene «Límite de Tiempo»
# ON (DAT_00cd82d4): por eso «a veces» se podía (salas con el límite OFF) y a veces no (ON, que es lo normal). C.
# SWAP_TIME = segundos del side deck que se mandan; el valor del oficial es D (el cliente no lo dice; 180 = el límite por
# turno de un duelo normal, texto 7006). 0 = no mandar nada (como antes: el side deck se corta solo en las salas con límite).
# SWAP_LEAD = segundos de colchón: el reloj cuenta desde que llega 0x6461 y el cliente tarda en llegar a la escena (fin del
# duelo + ventana del marcador del match: 6-12 s en los logs 20261010-230407 y 20261012-012417). P.
SWAP_TIME = float(os.environ.get("YO2_PVP_SWAP_TIME") or 180)
SWAP_LEAD = float(os.environ.get("YO2_PVP_SWAP_LEAD") or 10)
GLOBAL_CLOCK = 3              # 0x6461 «quién» = 3: el reloj global ND+0x534 (también el del piedra-papel-tijera)
M_TEST_LOSE = 0x7ffd          # prueba: «pierdo esta partida». Fuera de YO2_PVP_TEST se ignora (no existe en yo2.exe)


def _admin_ev(name, *a, **k):
    """Panel (research/admin/eventos.py, 07-10): evento para «Actividad». Sin panel no hace nada; nunca lanza."""
    try:
        import sys as _sys
        m = _sys.modules.get("yo2_eventos")
        f = getattr(m, name, None) if m is not None else None
        return f(*a, **k) if callable(f) else None
    except Exception:                                # noqa: BLE001
        return None


class Matches:
    def __init__(self, env):
        self.env = env                      # globals() VIVO de run.py
        self.lock = threading.RLock()
        self.pids = {}                      # pid -> PvpMatch (reservada o en juego)

    def log(self, *a):
        (self.env.get("log") or print)(*a)

    def reserve(self, sa, sb):
        """Reto aceptado: A (retador) = jugador 0, B (dueño de la sala) = 1. Una reserva vieja de cualquiera de los
        dos que no llegó a empezar se descarta."""
        with self.lock:
            for s in (sa, sb):
                old = self.pids.get(s.pid)
                if old is not None:
                    old.cancel("nueva reserva")
            m = PvpMatch(self, sa, sb)
            self.pids[sa.pid] = self.pids[sb.pid] = m
        m.room_time = self._room_time(sa, sb)
        m.match_style = self._room_style(sa, sb)
        m.room_surrender = self._room_surrender(sa, sb)
        self.log(f"[pvp] reserva {m.name}: {sa!r} contra {sb!r}"
                 + (" (sala con «Límite de Tiempo» OFF)" if m.room_time == 0 else "")
                 + (" (sala con «Rendirse» OFF)" if m.room_surrender == 0 and SURRENDER_ROOM_OPTION else "")
                 + ((" (MATCH al mejor de 3)" if m._bo3() else f" (sala de match; YO2_PVP_MATCH={PVP_MATCH})")
                    if m.match_style == MATCH_STYLE else ""))
        return m

    def _room_style(self, sa, sb):
        """dstyle de la sala del duelo (byte 0 de 0x4288: 3 duelo, 4 match, C), la del dueño como _room_time; None
        si no se encuentra (duelo simple: autoduelo, torneo)."""
        LP = self.env.get("LP")
        try:
            rooms = list(LP.MEM["rooms"].values())
        except Exception:                                # noqa: BLE001
            return None
        for s in (sb, sa):
            for r in rooms:
                if r.get("p1") == getattr(s, "pid", None) and not r.get("npc"):
                    return r.get("dstyle")
        return None

    def _room_time(self, sa, sb):
        """Opción «Límite de Tiempo» de la sala donde se pacta el duelo: byte 22 de 0x4288 (+0xa6 de la escena,
        DAT_012bb6b0+0xc2) = r["x"][1] del lobby (C: TIEMPO.md §2.2). La del dueño de la sala (jugador 1 en un reto;
        en el autoduelo las dos salas lo traen siempre a 1, C). 1/0, o None si no se encuentra (se aplica el límite)."""
        LP = self.env.get("LP")
        try:
            rooms = list(LP.MEM["rooms"].values())
        except Exception:                                # noqa: BLE001
            return None
        for s in (sb, sa):
            for r in rooms:
                x = r.get("x") or ()
                if r.get("p1") == getattr(s, "pid", None) and not r.get("npc") and len(x) >= 2:
                    return 1 if x[1] else 0
        return None

    def _room_surrender(self, sa, sb):
        """Opción «Rendirse ON/OFF» de la sala donde se pacta el duelo: byte 21 de 0x4288 (DAT_012bb6b0+0xc1; textos
        5081/5082) = r["x"][0] del lobby, la del dueño de la sala como _room_time. 1/0, o None si no se encuentra (se
        puede rendir). Por defecto el cliente la manda a 1 (log vivo de 0x4288: 1, 1, tiempo)."""
        LP = self.env.get("LP")
        try:
            rooms = list(LP.MEM["rooms"].values())
        except Exception:                                # noqa: BLE001
            return None
        for s in (sb, sa):
            for r in rooms:
                x = r.get("x") or ()
                if r.get("p1") == getattr(s, "pid", None) and not r.get("npc") and len(x) >= 1:
                    return 1 if x[0] else 0
        return None

    def by_pid(self, pid):
        with self.lock:
            return self.pids.get(pid)

    def drop(self, m):
        with self.lock:
            for p in m.pids:
                if self.pids.get(p) is m:
                    self.pids.pop(p, None)

    def watch_request(self, peer, pid):
        """0x4320 REQWATCH aceptado (research/lobby h_watch): el próximo 0x6700 desde la IP peer mira la partida de
        pid. None si pid no tiene un PvP sin acabar."""
        if not PVP_WATCH:
            return None
        m = self.by_pid(pid)
        if m is None or m.done:
            return None
        with self.lock:
            self.watch_req = getattr(self, "watch_req", {})
            self.watch_req[peer] = (m, time.time())
        return m

    def route(self, c, msg_id):
        """PvpMatch de esta conexión de duelo, o None (flujo de la NPC). Se engancha con el 0x6001 (run.py ya ha
        puesto c.sess desde su pid: HUB.attach)."""
        m = getattr(c, "pvp", None)
        if m is not None:
            return m
        if msg_id == 0x6700 and PVP_WATCH:                 # espectador (NOTES §8): no manda 0x6001
            with self.lock:
                req = getattr(self, "watch_req", {}).pop(getattr(c, "peer", None), None)
            if req is None or req[0].done or time.time() - req[1] > WATCH_REQ_TTL:
                self.log(f"[pvp] 0x6700 de {getattr(c, 'peer', '?')} sin 0x4320 válido: no hay duelo que mirar")
                return None
            req[0].watch_attach(c)
            return req[0]
        if msg_id != 0x6001:
            return None
        s = getattr(c, "sess", None)
        m = self.by_pid(getattr(s, "pid", None)) if s is not None else None
        if m is None or not m.attach(c):
            return None
        return m


class PvpMatch:
    _n = 0

    def __init__(self, hub, sa, sb):
        PvpMatch._n += 1
        self.hub, self.env = hub, hub.env
        self.name = f"pvp-{PvpMatch._n}"
        self.sess = [sa, sb]
        self.pids = [sa.pid, sb.pid]
        self.c = [None, None]
        self.tx = [None, None]
        self.deck = [{"main": [], "extra": []}, {"main": [], "extra": []}]
        self.prep = [False, False]
        self.prep_sent = False
        self.hand = [None, None]
        self.echo6102 = [False, False]
        self.jwinner = None             # quién ganó el piedra-papel-tijera (absoluto)
        self.first_v = None             # lo que eligió (0 = empieza él)
        self.first = None               # quién empieza (absoluto)
        self.sent6103 = False
        self.ready = [False, False]
        self.ask = [False, False]       # diálogo «¿Te rindes?» abierto
        self.ok_ask = [False, False]    # cartel de aceptar (dlg 82) mandado: un 0x6423 que llegue se descarta
        self.room_surrender = None      # opción de sala «Rendirse» (1 ON, 0 OFF, None no se sabe)
        self.closed = [False, False]
        self.ps = None                  # ocg_duel.PvpSession
        self.done = False
        self.bad_auto = 0               # prueba: 0x7ffe de quien no decidía
        self.rec = None                 # PVP_DUELLOG_RECORD: duellog.Recorder con los 0x6202 de cada lado
        self.watchers = []              # PVP_WATCH: espectadores {c, tx, i (cursor en rec.entries), want, jseen}
        self.jhands = None              # manos del piedra-papel-tijera que decidió (jugador 0, jugador 1)
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name=f"{self.name}").start()
        t = threading.Timer(ATTACH_TIMEOUT, lambda: self.q.put(("timeout",)))
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------ utilidades
    def log(self, *a):
        self.hub.log(*a)

    @contextlib.contextmanager
    def as_sess(self, s):
        """CTX.sess = s mientras dura (load_state/save_state/duel_parts de run.py son de la sesión del hilo)."""
        ctx = self.env.get("CTX")
        old = getattr(ctx, "sess", None) if ctx is not None else None
        if ctx is not None:
            ctx.sess = s
        try:
            yield
        finally:
            if ctx is not None:
                ctx.sess = old

    def put(self, i, msgs):
        if self.tx[i] is not None and not self.closed[i]:
            self.tx[i].put(msgs)

    def send(self, i, mid, data=b""):
        self.put(i, [(mid, bytes(data))])

    def lobby(self, i):
        lc = self.env.get("live_conn")
        c = self.sess[i].lobby
        return lc(c) if lc is not None else c

    # ------------------------------------------------------------------ entrada (hilos de los sockets)
    def attach(self, c):
        s = getattr(c, "sess", None)
        if self.done or s is None or s.pid not in self.pids:
            return False
        i = self.pids.index(s.pid)
        OCG = self.env.get("OCG")
        if OCG is None:
            return False
        old = self.c[i]
        self.c[i] = c
        self.closed[i] = False
        self.tx[i] = OCG.Sender(c)
        self.tx[i] = _SurrTx(self.tx[i], self)           # sala con «Rendirse» OFF: sin la entrada en el menú del mazo
        if PVP_DUELLOG_RECORD or PVP_WATCH or REPLAY_ARCHIVE:   # apunta los 0x6202 de este lado (repetición,
            # espectador, archivo del servidor)
            self.tx[i] = _RecTx(self.tx[i], self._recorder(), i, self._watch_pump if PVP_WATCH else None)
        c.pvp, c.pvp_i = self, i
        self.log(f"[pvp] {self.name}: conexión de duelo del jugador {i} ({s!r})"
                 + (" (sustituye a otra)" if old is not None else ""))
        return True

    def on(self, c, msg_id, param, data):
        self.q.put(("msg", c, msg_id, bytes(data)))

    def on_close(self, c):
        self.q.put(("close", c))

    def watch_attach(self, c):
        """Hilo del socket: c (espectador, 0x6700) pasa a esta partida; lo demás, en el trabajador."""
        c.pvp, c.pvp_i, c.pvp_w = self, None, True
        self.q.put(("watch", c))

    def cancel(self, why):
        self.q.put(("cancel", why))

    # ------------------------------------------------------------------ hilo trabajador
    def _worker(self):
        self._clock_ok = True                           # este hilo atiende «clock»/«tick» (límite de tiempo)
        while True:
            item = self.q.get()
            try:
                if item[0] == "watch":
                    self._watch_new(item[1])
                elif item[0] == "msg" and getattr(item[1], "pvp_w", False):
                    self._watch_msg(item[1], item[2], item[3])
                elif item[0] == "close" and getattr(item[1], "pvp_w", False):
                    self.watchers = [w for w in self.watchers if w["c"] is not item[1]]
                elif item[0] == "msg":
                    _, c, mid, data = item
                    i = getattr(c, "pvp_i", None)
                    if i is None or self.c[i] is not c:
                        continue                     # una conexión vieja de este jugador
                    with self.as_sess(self.sess[i]):
                        self._msg(i, c, mid, data)
                elif item[0] == "close":
                    i = getattr(item[1], "pvp_i", None)
                    if i is not None and self.c[i] is item[1]:
                        self._closed(i)
                elif item[0] == "timeout":
                    if not self.done and (None in self.c or not self.ps):
                        self.log(f"[pvp] {self.name}: no han conectado los dos al duelo en {ATTACH_TIMEOUT:.0f} s")
                        self._abort(SVR_DISCONNECT)
                elif item[0] == "cancel":
                    if not self.done and self.ps is None:
                        self.log(f"[pvp] {self.name} anulada: {item[1]}")
                        self._abort(None)
                elif item[0] in ("clock", "tick"):          # límite de tiempo (PvpSession._clk_begin / temporizador)
                    self._clock_item(item)
                elif item[0] == "between":                  # match: tiempo de espera entre partidas
                    self._between_timeout(item[1])
            except Exception as e:
                tb = traceback.format_exc().replace(chr(10), " | ")
                self.log(f"[EXC] {self.name}: {e!r} | {tb}")

    def _msg(self, i, c, mid, data):
        o = 1 - i
        if getattr(self, "need_start", False) and mid in NEXT_MSGS:   # match: la partida siguiente (PVP_MATCH)
            if getattr(self, "between", False):
                self._next_game()
            seen = self.__dict__.setdefault("seen_next", [False, False])
            if not seen[i]:
                self._swap_reset(i)                      # i ya está en el piedra-papel-tijera: sale de la escena del side deck
            seen[i] = True
        if mid == 0x0005:                            # latido -> 0x0004 (como duel_reply)
            return self.send(i, 0x0004, struct.pack(">I", 0))
        if mid == 0x0003:                            # el cliente cierra la sesión de duelo tras los resultados
            return
        if mid == 0x6001:
            return self.send(i, 0x6007, struct.pack(">I", 1))
        if mid == 0x6600:                            # cabecera del mazo: sin uso
            return
        if mid in (0x6601, 0x6603) and len(data) >= 4:
            n = min(struct.unpack(">I", data[:4])[0], (len(data) - 4) // 2)
            ids = list(struct.unpack(">%dH" % n, data[4:4 + 2 * n]))
            self.deck[i]["main" if mid == 0x6601 else "extra"] = ids
            if mid == 0x6603:                        # mazo completo: comprobación del servidor (YO2_DECK_CHECK)
                self._deck_check(i)
            return
        if mid == 0x600b:
            self.prep[i] = True
            if all(self.prep) and not self.prep_sent:
                self.prep_sent = True
                for k in (0, 1):
                    self.send(k, 0x600c, self.preparation(k))
                    od = self.deck[1 - k]
                    self.send(k, 0x6005, struct.pack(">HH", len(od["main"]), len(od["extra"])))
                self.log(f"[pvp] {self.name}: mazos {len(self.deck[0]['main'])}/{len(self.deck[1]['main'])}, "
                         f"0x600c + 0x6005 a los dos")
            return
        if mid == 0x6013:
            # DUELLOG: el cliente lo manda al acabar de leer 0x6010 y cada 0x6011 (FUN_00694460(1), u8 1): «listo para
            # lo siguiente». Con 0 comandos, lo siguiente es 0x6012 FINISH (control de flujo 1:1; antes se mandaba
            # sin esperar).  C (emisor) / P (que el oficial esperase)
            # 06-10 (cierre del portátil, C): FUN_00694840 (0x6012) recorre ND+0xd78c entradas de 4 B y escribe 8 B por
            # cada una en un búfer de ~500 B de la pila; ese contador SOLO lo pone a 0 el 0x6016 DUELLOG_SERCHINFO
            # (FUN_00694340: u32 total -> +0xd790, +0xd78c = 0, y contesta 0x6013). Sin él quedaba basura de memoria:
            # en el PC salía 0 por suerte, en el portátil desbordaba la pila (access-violation en 0x6949a3).
            # Orden: 0x6010 -> 0x6013 -> 0x6016 (u32 0) -> 0x6013 -> 0x6012.
            if PVP_DUELLOG_RECORD:                       # registro completo: la siguiente de la cola de i
                q = getattr(self, "log_q", None)
                if q and q[i]:
                    self.put(i, [q[i].pop(0)])
                return
            if PVP_DUELLOG_FINISH and self.done:
                fase = getattr(self, "log_fase", None) or [0, 0]
                self.log_fase = fase
                if fase[i] == 0:
                    fase[i] = 1
                    self.send(i, 0x6016, struct.pack(">I", 0))
                elif fase[i] == 1:
                    fase[i] = 2
                    self.send(i, 0x6012)
            return
        if getattr(self, "deck_bad", None) and mid in (0x6100, 0x6101, 0x6102, 0x6103, 0x6104, 0x61fe):
            if getattr(self, "need_start", False) and self._bo3():   # match, partida >= 2 (mazo cambiado): no hay
                return self._match_gone(min(self.deck_bad), "mazo bloqueado")   # 0x6100 que esperar: pierde (D)
            return self._deck_cut(i, mid)            # mazo bloqueado: el duelo no empieza (NOTES «Comprobación del mazo»)
        if mid in (0x6100, 0x6104):
            return self.send(i, mid)
        if mid == 0x6101:
            if self.jwinner is not None:
                return
            self.hand[i] = (data[0] if data else 0) % 3
            if None in self.hand:
                return
            for k in (0, 1):
                self.send(k, 0x6101, bytes([0, self.hand[1 - k]]))
            r = (self.hand[1] - self.hand[0]) % 3        # visto desde A: 1 = gana A, 2 = gana B, 0 = empate
            self.log(f"[pvp] {self.name}: manos {self.hand} -> " + ("empate" if r == 0 else f"gana {0 if r == 1 else 1}"))
            if r and getattr(self, "game_no", 1) == 1:   # match: las de la partida 1 (las que lee la repetición)
                self.jhands = tuple(self.hand)           # espectador: 0x6701 lleva las manos que decidieron
            self.hand = [None, None]
            if r:
                self.jwinner = 0 if r == 1 else 1
            return
        if mid == 0x6102:
            self.send(i, 0x6102)
            self.echo6102[i] = True
            return self._maybe_6103()
        if mid == 0x6103:
            if i != self.jwinner or self.first is not None:
                return self.log(f"[pvp] {self.name}: 0x6103 de {i} sin ser el ganador: ignorado")
            v = (data[0] if data else 0) & 1
            self.first_v, self.first = v, (i if v == 0 else o)
            self.send(i, 0x6103, bytes([v]))
            if self.watchers:
                self._watch_pump()
            return self._maybe_6103()
        if mid == 0x61fe:
            # u8 = opción «Auto Card Placing» del cliente (0 = automática, 1 = manual: elige él la zona); se manda al
            # empezar y cada vez que cambia las opciones en pleno duelo (translator.py, place_manual)
            self.__dict__.setdefault("place_manual", [False, False])[i] = bool(data[:1] and data[0])
            if self.ps is not None and not getattr(self, "need_start", False) and self.ps.views[i] is not None:
                self.ps.views[i].place_manual = self.place_manual[i]
            if self.ready[i]:
                return self.log(f"[pvp] {self.name}: 0x61fe repetido de {i} ({data.hex()}): ignorado")
            self.ready[i] = True
            if all(self.ready) and (self.ps is None or getattr(self, "need_start", False)):
                self._start()
            return
        if mid == M_TEST_AUTO:
            if not TEST_AUTO:
                return self.log(f"[pvp] 0x{mid:04x} fuera de la instancia de prueba: ignorado")
            if self.ps is not None and not self.ps.auto(i):
                self.bad_auto += 1
            return
        if mid == M_TEST_LOSE:                       # prueba: i pierde esta partida (como si sus LP llegaran a 0)
            if not TEST_AUTO:
                return self.log(f"[pvp] 0x{mid:04x} fuera de la instancia de prueba: ignorado")
            if self.ps is not None and self.ps.started and not self.ps.over and not getattr(self, "need_start", False):
                DFM = self.env["DFM"]
                turns = getattr(self.ps, "turn", 0) or 0
                self.ps.end()
                _admin_ev("pvp_end", self.sess[0], self.sess[1], o, DFM.WHY_LP, turns)
                self._game_end(o, DFM.WHY_LP, turns)
            return
        if self.ps is None or self.done or getattr(self, "need_start", False):
            auto = self.env.get("auto_reply")
            if not (auto and auto(c, mid)):
                self.log(f"[pvp] {self.name}: 0x{mid:04x} de {i} sin respuesta")
            return
        DFM = self.env.get("DFM")
        if mid != 0x6423:
            self.ok_ask[i] = False                       # el jugador ya ha tocado otra cosa: el cartel está cerrado
        if mid == 0x6423 and self.ok_ask[i] and not self.ask[i]:
            self.ok_ask[i] = False
            return self.log(f"[pvp] {self.name}: 0x6423 del jugador {i} tras el cartel de rendición ({data.hex()}): descartado")
        if mid == 0x6350 and DFM is not None and DFM.is_surrender(data):
            why = self._surrender_denied()
            if why:
                return self._surrender_refuse(i, why)
            self.ask[i] = True                           # «Rendirse» (bit 10 del mazo propio): diálogo SÍ/NO
            self.log(f"[pvp] {self.name}: el jugador {i} pide rendirse (decide {getattr(self.ps, 'decider', None)}): diálogo SÍ/NO")
            return self.put(i, DFM.surrender_confirm_msgs())
        if mid == 0x6423 and self.ask[i]:
            self.ask[i] = False
            yes = len(data) >= 4 and struct.unpack(">I", data[:4])[0] == DFM.YES
            if yes:
                return self._surrender(i)
            self.log(f"[pvp] {self.name}: el jugador {i} contesta NO al diálogo de rendición ({data.hex()}): sigue el duelo")
            return self.ps.resend(i)
        if mid in (self.env.get("OCG_CLIENT_MSGS") or ()):
            return self.ps.on_client(i, mid, data)
        auto = self.env.get("auto_reply")
        if not (auto and auto(c, mid)):
            self.log(f"[pvp] {self.name}: 0x{mid:04x} de {i} sin respuesta")

    # ------------------------------------------------------------------ mazo (research/seguridad_duelo/mazo.py)
    def _deck_check(self, i):
        """Mazo completo de i (tras 0x6603): DK.review apunta el historial en SU perfil y avisa; con «bloquear» y un
        mazo que no cuadra, i entra en deck_bad y el duelo no empezará."""
        dk = self.env.get("DK")
        if dk is None:
            return
        d = self.deck[i]
        r = dk.review(self.sess[i], d["main"], d["extra"], partida=f"pvp:{self.name}")
        if r and r["bloquear"]:
            self.deck_bad = set(getattr(self, "deck_bad", None) or ()) | {i}

    def _deck_svr(self, k):
        """0x649b código 1 para k: bit0 = 0 si el mazo malo es el suyo («<él> se ha desconectado»), 1 si es el del
        rival («<rival> se ha desconectado»). En el Janken el código 1 lleva de vuelta al mapa (C, duelfin §5)."""
        who = 0 if k in (getattr(self, "deck_bad", None) or ()) else 1
        LS = self.env.get("LS")
        return LS.duelsvrinfomsg(SVR_DISCONNECT, who) if LS is not None else bytes([SVR_DISCONNECT, who])

    def _deck_cut(self, i, mid):
        """Duelo bloqueado por el mazo: al 0x6100 de cada uno (ya en el piedra-papel-tijera, tras 0x600c), su eco y
        0x649b código 1; lo demás del Janken se ignora. Cuando los dos lo tienen, fuera la partida (sin _finish: ni
        resultado, ni DP, ni carta, ni registro)."""
        told = self.__dict__.setdefault("deck_told", [False, False])
        if mid != 0x6100 or told[i] or self.done:
            return
        told[i] = True
        self.send(i, 0x6100)
        self.send(i, 0x649b, self._deck_svr(i))
        self.log(f"[pvp] {self.name}: duelo NO empieza (mazo de {sorted(self.deck_bad)} bloqueado): 0x649b a {i}")
        if all(told[k] or self.closed[k] or self.c[k] is None for k in (0, 1)):
            self._close_match()

    def _maybe_6103(self):
        if self.first is None or self.sent6103:
            return
        lo = 1 - self.jwinner
        if not self.echo6102[lo]:
            return
        self.sent6103 = True
        self.send(lo, 0x6103, bytes([1 - self.first_v]))
        self.log(f"[pvp] {self.name}: empieza el jugador {self.first}")

    def preparation(self, k):
        """0x600c PREPARATION_RESULT (FUN_006918d0), 168 B, desde el cliente k: A = k, B = el otro. Mismo formato que
        run.py preparation_result(): nombre A16, nombre B16, u32 id A, u32 id B, s32 LP A, s32 LP B, 3×16 B de A,
        3×16 B de B (uno por mundo: 0 Domino, 1 Academia, el 3.º sin uso), 4 B país A, 4 B país B, 16 B."""
        W = self.env["proto"].W
        me, ot = self.sess[k], self.sess[1 - k]
        w = W().s(me.name or "Duelista", 16).s(ot.name or "Duelista", 16).u32(me.pid).u32(ot.pid)
        w = w.u32(LP_START).u32(LP_START).bytes()
        for s in (me, ot):
            with self.as_sess(s):                        # duel_parts(None) lee el perfil de la sesión del hilo
                w += self.env["duel_parts"](None, 0) + self.env["duel_parts"](None, 1) + bytes(16)
        return w + COUNTRY + COUNTRY + bytes(16)

    # ------------------------------------------------------------------ duelo
    def _start(self):
        OCG = self.env["OCG"]
        first = self.first if self.first is not None else 0
        clock, why = self._clock_cfg()
        self.need_start = False                          # match: la partida nueva ya arranca
        self.__dict__.setdefault("firsts", []).append(first)   # quién empieza cada partida (+0xbbe6.. del registro)
        self.ps = OCG.PvpSession(self.c, self.deck, self.log, self._on_end, out=self.tx,   # una cola por cliente
                                 clock=clock)
        self.ps.place_manual = tuple(getattr(self, "place_manual", None) or (False, False))   # opción de cada cliente
        self._stats_attach()
        self.log(f"[pvp] {self.name}: arranca" + (f" la partida {len(self.firsts)} del match" if self._bo3() else "")
                 + f", empieza {first}; {why}")
        self.ps.start(first)
        _admin_ev("pvp_start", self.sess[0], self.sess[1])          # panel: «A y B empiezan un duelo»

    def _stats_attach(self):
        """B51: contadores del perfil de ESTA partida (tributos, Especiales, activaciones…) sobre los eventos de duelcore:
        persistencia.DuelTally en la sesión (ocg_duel.PvpSession._pump los alimenta) y las cartas cambiadas con el Side
        Deck respecto a la partida anterior del match. YO2_STATS_PVP=0 lo apaga. Un fallo solo va al log."""
        try:
            PS = self.env["DFM"].PS
            OCG = self.env.get("OCG")
            if not getattr(PS, "STATS_PVP_ON", False) or not hasattr(PS, "DuelTally") or OCG is None:
                return
            cm = OCG.cardmap()

            def typeof(code):
                return cm.cdb_type.get(code) or cm.cdb_type.get(cm.cdb_alias.get(code, 0), 0)
            t = PS.DuelTally(typeof=typeof)
            used = [{"main": list(d.get("main") or []), "extra": list(d.get("extra") or [])} for d in self.deck]
            prev, self.decks_used = getattr(self, "decks_used", None), used
            if prev:                                     # match: cartas que han entrado respecto a la partida anterior
                import collections
                for k in (0, 1):
                    a = collections.Counter(prev[k]["main"] + prev[k]["extra"])
                    b = collections.Counter(used[k]["main"] + used[k]["extra"])
                    t.side_swaps[k] = sum((b - a).values())
            self.ps.stats = t
        except Exception as e:                           # noqa: BLE001
            self.log(f"[EXC] {self.name}: estadísticas del perfil: {e!r}")

    def _on_end(self, winner, reason, turns):
        """Fin normal (duelcore «end»): winner absoluto 0/1 o None (empate)."""
        DFM = self.env["DFM"]
        # como ocg_finish (B50): motivo numérico de ocgcore -> lp/deckout/exodia…; B25: «error» (duelcore caído o
        # roto, PvpSession._fatal) -> abort = empate sin LP a 0 para los dos
        if hasattr(DFM, "why_from_ocg"):
            why = DFM.why_from_ocg(reason)
        else:
            why = DFM.WHY_DECKOUT if "deck" in str(reason).lower() else DFM.WHY_LP
        _admin_ev("pvp_end", self.sess[0], self.sess[1], winner, why, turns)   # panel: Actividad
        self._game_end(winner, why, turns)

    def _surrender_denied(self):
        """Motivo por el que NO se puede rendir ahora (reglas del oficial, ver SURRENDER_MIN_TURN), o None: "room" = la
        sala tiene «Rendirse» OFF, "turn" = antes del turno SURRENDER_MIN_TURN (turno global de ocgcore, el de la última
        decisión)."""
        if SURRENDER_ROOM_OPTION and self.room_surrender == 0:
            return "room"
        turn = getattr(self.ps, "turn", 0) or 0
        if SURRENDER_MIN_TURN and turn < SURRENDER_MIN_TURN:
            return "turn"
        return None

    def _surrender_refuse(self, i, why):
        """Petición de rendirse que no se admite. "turn": cartel de aceptar con el dlg 82 (lo que enseñaba el oficial,
        formato C de los duellogs) y se vuelve a pedir la jugada, que el cliente gasta al pulsar «Rendirse» (como con el
        NO del diálogo SÍ/NO). "room": el menú ya no lo trae; si llega igual, solo se vuelve a pedir la jugada."""
        DFM = self.env.get("DFM")
        self.log(f"[pvp] {self.name}: el jugador {i} pide rendirse y NO se admite ({why}; turno {getattr(self.ps, 'turn', '?')}, "
                 f"mínimo {SURRENDER_MIN_TURN}, sala {self.room_surrender})")
        if why == "turn" and DFM is not None and hasattr(DFM, "info_dialog_msgs"):
            self.ok_ask[i] = True
            self.put(i, DFM.info_dialog_msgs(DLG_SURRENDER_TURN))
        return self.ps.resend(i)

    def _surrender(self, i):
        DFM = self.env["DFM"]
        o = 1 - i
        self.log(f"[pvp] {self.name}: se rinde el jugador {i}")
        self.ps.end()
        self.surr = i                                    # quién se rinde: surrender_me/surrender_opp del perfil (apply_duel_stats)
        _admin_ev("pvp_end", self.sess[0], self.sess[1], o, "surrender")      # panel: Actividad
        # en un match, rendirse es perder ESTA partida (el oficial, C: ver SURRENDER_ENDS_MATCH); fuera de un match close no cuenta
        self._game_end(o, DFM.WHY_SURRENDER, 0, svr={o: SVR_SURRENDER}, close=SURRENDER_ENDS_MATCH)

    # ------------------------------------------------------------------ fin de partida y match (PVP_MATCH)
    def _bo3(self):
        """¿Match al mejor de 3? Sala con dstyle 4 y PVP_MATCH = "1" (sin match_style: reservada antes de la carga)."""
        return PVP_MATCH == "1" and getattr(self, "match_style", None) == MATCH_STYLE

    def _match_over(self, games):
        """Como FUN_00794a90 (C), con los resultados del jugador 0 (1 gana, 2 pierde, 3 empate): diferencia ±2 ->
        acabado; con 3 partidas, gana quien vaya por delante o empate. (acabado, ganador absoluto o None)."""
        DFM = self.env["DFM"]
        d = games.count(DFM.RESULT_WIN) - games.count(DFM.RESULT_LOSE)
        if d >= 2 or d <= -2:
            return True, (0 if d > 0 else 1)
        if len(games) >= 3:
            return True, (0 if d > 0 else 1 if d < 0 else None)
        return False, None

    def _score(self, k):
        """DUELSCORE del match desde k: ((victorias, derrotas, empates, 0) suyas, las del rival). P (como el oficial
        pintase el marcador del match en CWindowResult no se sabe)."""
        DFM = self.env["DFM"]
        g = getattr(self, "games", None) or []
        w0, l0, e = g.count(DFM.RESULT_WIN), g.count(DFM.RESULT_LOSE), g.count(DFM.RESULT_DRAW)
        me = (w0, l0, e, 0) if k == 0 else (l0, w0, e, 0)
        return me, (me[1], me[0], me[2], 0)

    def _game_end(self, winner, why, turns=0, svr=None, close=False):
        """Fin de una partida: winner absoluto 0/1 o None (empate). svr = {k: código 0x649b} antes de su fin. close =
        el match acaba ya aunque no esté decidido (desconexión; la rendición solo con SURRENDER_ENDS_MATCH): 0x4d P1 = 5 (C: DAT_00cd82d0 = P0).
        Fuera de un match: como siempre (_finish a los dos, registro y fuera). En un match sin decidir: 0x4d P1 = 0
        (el cliente cuenta), sin carta ni 0x4800 ni registro, y a esperar la partida siguiente (_between_begin)."""
        DFM = self.env["DFM"]
        svr = svr or {}

        def wv(k):
            return None if winner is None else (0 if winner == k else 1)
        if hasattr(self, "_xp_levels"):                  # LV de los dos ANTES de tocar ningún perfil (rx/XP; las pruebas con un M falso no lo tienen)
            self._xp_levels()
        if not self._bo3():
            fin = (DFM.FINISH_CLOSE if PVP_MATCH == "cerrar" and getattr(self, "match_style", None) == MATCH_STYLE
                   else None)
            for k in (0, 1):
                self._finish(k, wv(k), why, turns, svr=svr.get(k), fin=fin)
            self._duellog_record(winner, why, turns)
            return self._close_match()
        games = self.__dict__.setdefault("games", [])
        games.append(DFM.result_for_client(wv(0)))
        over, mwin = self._match_over(games)
        if close:
            over, mwin = True, winner
        self.log(f"[pvp] {self.name}: partida {len(games)} del match: "
                 + ("empate" if winner is None else f"gana {winner}") + f" ({why}); marcador {self._score(0)[0][:3]}"
                 + ((" -> match acabado: " + ("empate" if mwin is None else f"gana {mwin}")
                     + (" (se cierra ya)" if close else "")) if over else " -> sigue"))
        fin = DFM.FINISH_CLOSE if close else None       # None = FINISH_NORMAL (o FINISH_SURRENDER si es una rendición, ver _finish)
        for k in (0, 1):
            mres = None if not over else "draw" if mwin is None else "win" if mwin == k else "lose"   # puntuación (rx/XP)
            self._finish(k, wv(k), why, turns, svr=svr.get(k), fin=fin, score=self._score(k), last=over, mres=mres)
        if over:
            self._duellog_record(mwin, why, turns)
            return self._close_match()
        self._between_begin(winner)

    def _between_begin(self, winner):
        """Partida del match acabada sin decidir: elige el que perdió (C, init de CJankenScene); empate -> piedra-
        papel-tijera otra vez. need_start hasta el _start de la siguiente; el ps viejo (ya acabado) se queda hasta
        entonces (el «timeout» de conexión del hilo mira «not self.ps»)."""
        self.between = True
        self.need_start = True
        self.chooser = None if winner is None else 1 - winner
        self.btw_gen = getattr(self, "btw_gen", 0) + 1
        t = threading.Timer(MATCH_WAIT, lambda g=self.btw_gen: self.q.put(("between", g)))
        t.daemon = True
        t.start()
        self.log(f"[pvp] {self.name}: entre partidas; elige quién empieza "
                 + ("(piedra-papel-tijera)" if self.chooser is None else f"el jugador {self.chooser}"))
        self._swap_clock()

    def _swap_clock(self):
        """Reloj del side deck (SWAP_TIME): 0x6461 quién = 3 a los dos tras el fin de la partida. Sin él ND+0x534 vale 0 y
        la escena del side deck se cierra sola al instante en las salas con «Límite de Tiempo» ON (ver SWAP_TIME). Con la
        sala en OFF el cliente no crea ese reloj: no se manda nada. La cuenta atrás la hace el cliente (FUN_00692d50) y
        al llegar a 0 pasa solo al piedra-papel-tijera con el mazo que tenga."""
        self.swap_clock = [False, False]
        if SWAP_TIME <= 0 or getattr(self, "room_time", None) == 0:
            return
        secs = int(round(SWAP_TIME + SWAP_LEAD))
        for k in (0, 1):
            if self.tx[k] is not None and not self.closed[k]:
                self.send(k, 0x6461, self._clock3(secs, 0))
                self.swap_clock[k] = True
        self.log(f"[pvp] {self.name}: reloj del side deck: {secs} s a los dos (global, corriendo)")

    def _swap_reset(self, i):
        """i ya ha vuelto al piedra-papel-tijera (primer mensaje de la partida siguiente): su reloj global a 0 y parado, como
        en la partida 1 (si no, el piedra-papel-tijera seguiría contando lo que sobró del side deck). Solo a i: el otro
        puede seguir cambiando cartas y con un 0 se le cerraría la escena."""
        sc = getattr(self, "swap_clock", None)
        if not sc or not sc[i]:
            return
        sc[i] = False
        self.send(i, 0x6461, self._clock3(0, 1))

    def _clock3(self, secs, paused):
        LS = self.env.get("LS")
        return LS.timeup_warning(GLOBAL_CLOCK, secs, paused) if LS is not None else struct.pack(">BiBi", GLOBAL_CLOCK, secs, paused, -1)

    def _next_game(self):
        """Primer mensaje de la partida siguiente (0x6600.. o 0x6102): estado del arranque a cero. Los dos reenvían
        el mazo (C), sin 0x600b: no se repiten 0x600c ni 0x6005 (D: el cambio con el side deck no cambia el tamaño)."""
        self.between = False
        self.game_no = getattr(self, "game_no", 1) + 1
        self.deck = [{"main": [], "extra": []}, {"main": [], "extra": []}]
        self.hand = [None, None]
        self.echo6102 = [False, False]
        self.jwinner = getattr(self, "chooser", None)
        self.first_v = self.first = None
        self.sent6103 = False
        self.ready = [False, False]
        self.ask = [False, False]
        self.surr = None                                 # quién se rindió en la partida anterior (apply_duel_stats solo de ESA partida)
        self.seen_next = [False, False]
        self.log_fase = self.log_q = None
        self.log(f"[pvp] {self.name}: partida {self.game_no} del match: llegan los mazos")

    def _between_timeout(self, gen):
        """MATCH_WAIT sin arrancar la partida siguiente: pierde el match quien no llegó al 0x61fe (si son los dos, el
        que ni ha vuelto; si no se distingue, se cierra sin resultado). D: el oficial no se sabe."""
        if self.done or gen != getattr(self, "btw_gen", 0) or not getattr(self, "need_start", False):
            return
        late = [0, 1]                                    # «between»: no ha llegado nada de ninguno
        if not getattr(self, "between", False):
            late = [k for k in (0, 1) if not self.ready[k]]
            if len(late) == 2:
                seen = getattr(self, "seen_next", None) or [False, False]
                late = [k for k in (0, 1) if not seen[k]] or late
        if len(late) == 1:
            return self._match_gone(late[0], f"no vuelve en {MATCH_WAIT:.0f} s")
        self.log(f"[pvp] {self.name}: ninguno vuelve a la partida siguiente en {MATCH_WAIT:.0f} s: match sin resultado")
        self._close_match()

    def _match_gone(self, i, motivo):
        """i se va ENTRE partidas (o no vuelve, o su mazo nuevo no vale): el otro gana el match. Al que se queda,
        0x649b código 1 (bit0 = 1, el rival): en la partida >= 2 CJankenScene (estado 9, C) le apunta victorias hasta
        3 y va sola a resultados (escena 0x1d); por eso NO va 0x4d. Sí 0x6613 con el marcador (P), 0x4140, carta,
        0x4800 y el registro. A i no se le manda nada (si sigue conectado, su 0x649b le daría la victoria a él)."""
        DFM = self.env["DFM"]
        o = 1 - i
        self.between = self.need_start = False
        self.log(f"[pvp] {self.name}: el jugador {i} {motivo} entre partidas del match: gana el match {o}")
        self._finish(i, 1, DFM.WHY_DISCONNECT, played=False, ends=False, send=False)
        self._finish(o, 0, DFM.WHY_DISCONNECT, svr=SVR_DISCONNECT, score=self._score(o), played=False, ends=False)
        _admin_ev("pvp_end", self.sess[0], self.sess[1], o, "disconnect")   # panel: Actividad
        self._duellog_record(o, DFM.WHY_DISCONNECT)
        self._close_match()

    # ------------------------------------------------------------------ límite de tiempo (TIEMPO.md «Implementado»)
    def _clock_cfg(self):
        """(reloj para PvpSession o None, texto para el log). Se lee al empezar cada PvP (OCG.TR.DEFAULT_CFG: cambiarlo
        en caliente afecta solo a los duelos nuevos)."""
        cfg = getattr(getattr(self.env.get("OCG"), "TR", None), "DEFAULT_CFG", None) or {}
        if not cfg.get("turn_limit"):
            return None, "sin límite de tiempo (turn_limit apagado)"
        if not getattr(self, "_clock_ok", False):        # partida reservada antes de cargar el límite en caliente
            return None, "sin límite de tiempo (su hilo es anterior a la carga del límite)"
        if getattr(self, "room_time", None) == 0:
            return None, "sin límite de tiempo (sala con «Límite de Tiempo» OFF)"
        turn = float(cfg.get("turn_timer") or 0)
        gs = float(cfg.get("tiempo_gunslinger") or 0)
        guns = self._room_kind(self.sess[0]) in GUNSLINGER_KINDS and gs > 0
        if guns:
            turn = gs
        resp = float(cfg.get("tiempo_respuesta") or 0)
        if turn <= 0 and resp <= 0:
            return None, "sin límite de tiempo (turn_timer y tiempo_respuesta a 0)"
        margin = float(cfg.get("tiempo_margen") or 0)
        txt = (f"límite de tiempo: {f'{turn:g} s por turno' if turn > 0 else 'turno sin límite'}"
               f"{' (GUNSLINGER)' if guns else ''}, {f'{resp:g} s' if resp > 0 else 'sin límite'} para contestar en "
               f"el turno del rival, margen {margin:g} s")
        return {"turn": turn, "resp": resp, "margin": margin, "notify": self.q.put}, txt

    def _clock_item(self, item):
        """(«clock», gen, t0): la petición de la decisión gen ya salió (empieza a contar); («tick», gen): mirar si se
        ha acabado. Arma el temporizador siguiente o da la derrota por tiempo."""
        if self.done or self.ps is None or not hasattr(self.ps, "clock_check"):
            return
        r = self.ps.clock_check(item[1], item[2] if item[0] == "clock" and len(item) > 2 else None)
        if r is None:
            return
        if r[0] == "expired":
            return self._timeout(r[1])
        t = threading.Timer(r[1] + 0.05, lambda g=item[1]: self.q.put(("tick", g)))
        t.daemon = True
        t.start()

    def _timeout(self, i):
        """A i se le acabó el tiempo: 0x649b código 3 a los dos (bit0 = i en la vista de cada uno: 0 para él, 1 para
        el rival) y fin como la rendición: derrota de i (DP, carta, registro, repetición, resultados)."""
        DFM = self.env["DFM"]
        o = 1 - i
        why = getattr(DFM, "WHY_TIMEOUT", "timeout")
        clk = getattr(self.ps, "clk", None) or {}
        pool = "contestar en el turno del rival" if clk.get("tp") not in (None, i) else "su turno"
        turns = getattr(self.ps, "turn", 0) or 0
        self.log(f"[pvp] {self.name}: se acaba el tiempo del jugador {i} ({pool}): gana {o}")
        self.ps.end()
        LS = self.env.get("LS")
        for k, who in ((i, 0), (o, 1)):                  # el aviso va delante de la cola de fin (como en _finish)
            if PVP_SVRINFO and self.c[k] is not None and not self.closed[k]:
                svr = LS.duelsvrinfomsg(SVR_TIMEUP, who) if LS is not None else bytes([SVR_TIMEUP, who])
                self.put(k, [(0x649b, svr), (None, lambda: time.sleep(SVRINFO_WAIT))])
        _admin_ev("pvp_end", self.sess[0], self.sess[1], o, "timeout", turns)   # panel: Actividad «por tiempo»
        self._game_end(o, why, turns)                    # en un match: pierde la partida, no el match (D)

    def _closed(self, i):
        """El socket de duelo de i se cae (cliente cerrado o caído)."""
        self.closed[i] = True
        if self.done:
            return
        DFM = self.env.get("DFM")
        o = 1 - i
        if getattr(self, "need_start", False) and self._bo3() and DFM is not None:   # entre partidas de un match
            return self._match_gone(i, "se desconecta")
        if getattr(self, "deck_bad", None) and self.ps is None:   # duelo bloqueado por el mazo: nada cuenta
            told = self.__dict__.setdefault("deck_told", [False, False])
            told[i] = True
            if not told[o] and self.c[o] is not None and not self.closed[o]:
                told[o] = True
                self.send(o, 0x649b, self._deck_svr(o))
            self.log(f"[pvp] {self.name}: el jugador {i} se va con el duelo bloqueado por el mazo")
            return self._close_match()
        if self.ps is not None and self.ps.started and not self.ps.over and DFM is not None:
            self.log(f"[pvp] {self.name}: el jugador {i} se desconecta en mitad del duelo: gana {o}")
            self.ps.end()
            _admin_ev("pvp_end", self.sess[0], self.sess[1], o, "disconnect")   # panel: Actividad
            # i: solo su perfil (su socket ya no está); en un match se cierra ya (0x4d P1 = 5)
            self._game_end(o, DFM.WHY_DISCONNECT, 0, svr={o: SVR_DISCONNECT}, close=True)
        else:
            self.log(f"[pvp] {self.name}: el jugador {i} se desconecta antes del duelo")
            self._abort(SVR_DISCONNECT)

    def _abort(self, svr):
        """Sin duelo empezado: aviso 0x649b (si se pide) a quien siga conectado y fuera la reserva. En el piedra-
        papel-tijera el código 1 hace volver al mapa y mandar 0x0003 (research/duelfin §«Desconexión durante el
        piedra-papel-tijera», C); antes del 0x600c no está comprobado (P)."""
        if self.ps is not None:
            self.ps.end()
        if svr and PVP_SVRINFO:
            for k in (0, 1):
                if self.c[k] is not None and not self.closed[k]:
                    self.send(k, 0x649b, self._svrinfo(svr))
        self._close_match()

    # ------------------------------------------------------------------ espectador (PVP_WATCH, NOTES §8)
    def watch_info(self):
        """0x6006 DUEL_OTHERDECKINFO_WATCH (FUN_00692470), 147 B: por jugador 0 y 1 (ND+0x54c + k*0x94): u16 cartas
        del mazo (+0x18 -> mazo tapado, FUN_0079cbf0), u16 extra (+0x1c), u8 (+0x11, D: 0), 16 B nombre (+0),
        3 × 16 B piezas del avatar por mundo (+0x28/+0x39/+0x4a, como 0x600c), 4 B país (+0x6c); y u8 -> ND+0x67c:
        4 = modo 8 (match), cualquier otro = modo 7 (duelo suelto) (00870000.c:6170, FUN_00794d90). Va 3 (P).  C"""
        out = b""
        for k in (0, 1):
            s, d = self.sess[k], self.deck[k]
            out += struct.pack(">HHB", len(d["main"]), len(d["extra"]), 0)
            out += (s.name or "Duelista").encode("latin1", "replace")[:15].ljust(16, b"\0")
            dp = self.env.get("duel_parts")
            with self.as_sess(s):
                parts = (dp(None, 0) + dp(None, 1)) if dp is not None else bytes(32)
            out += parts[:32].ljust(32, b"\0") + bytes(16) + COUNTRY
        return out + bytes([3])

    def _watch_new(self, c):
        OCG = self.env.get("OCG")
        if OCG is None:
            return
        w = {"c": c, "tx": OCG.Sender(c), "i": 0, "want": False, "jseen": False}
        self.watchers.append(w)
        self.log(f"[pvp] {self.name}: espectador {getattr(c, 'peer', '?')} ({len(self.watchers)})")
        if WATCH_SEND_PREP:
            w["tx"].put([(0x600c, self.preparation(0))])
        w["tx"].put([(0x6006, self.watch_info())])
        self._watch_pump()

    def _watch_msg(self, c, mid, data):
        w = next((x for x in self.watchers if x["c"] is c), None)
        if w is None:
            return
        if mid == 0x0005:
            return w["tx"].put([(0x0004, struct.pack(">I", 0))])
        if mid == 0x6702:                                # «se me ha vaciado la cola»: la siguiente tanda
            w["want"] = True
            return self._watch_pump()
        if mid in (0x6700, 0x6703, 0x0003):              # 0x6703: sale del piedra-papel-tijera (FUN_006927b0, 0x7c556a)
            return
        self.log(f"[pvp] {self.name}: espectador 0x{mid:04x} sin respuesta")

    def _watch_pump(self):
        """0x6701 (FUN_00692660: u8 mano del jugador 0 -> +0x66a0, u8 mano del 1 -> +0x66a1, u8 quién empieza ->
        +0x66a2; ND+0x677 = 1) cuando ya se sabe, y 0x6202 del jugador 0 cuando el espectador pide (0x6702). El
        piedra-papel-tijera del espectador (FUN_007c76b0) espera +0x677 == 1 en el estado 2 (manos) y en el 7 (quién
        empieza, y ahí manda su primer 0x6702); como cada 0x6202 pone +0x677 = 0 (FUN_00692580), ningún 0x6202 va
        antes de ese 0x6702. Con un empate el cliente volvería al estado 2 con las mismas manos: van las que
        decidieron. Quién empieza: índice absoluto (P)."""
        if not self.watchers:
            return
        ents = self.rec.entries if self.rec is not None else []
        for w in list(self.watchers):
            if w["c"] is None or getattr(w["c"], "closed", False):
                self.watchers.remove(w)
                continue
            if not w["jseen"]:
                if self.jhands is None or self.first is None:
                    continue
                w["jseen"] = True
                w["tx"].put([(0x6701, bytes([self.jhands[0] & 0xff, self.jhands[1] & 0xff, self.first & 0xff]))])
                continue
            if not w["want"]:
                continue
            batch, i = [], w["i"]
            while i < len(ents) and len(batch) < WATCH_BATCH:
                side, d = ents[i]
                i += 1
                if side == 0 and not (WATCH_SIN_DECISIONES and watch_oculta(d)):
                    batch.append((0x6202, d))
            w["i"] = i
            if batch:
                w["want"] = False
                w["tx"].put(batch)

    # ------------------------------------------------------------------ repetición (PVP_DUELLOG_RECORD)
    def _recorder(self):
        if getattr(self, "rec", None) is None:
            self.rec = _DL().Recorder()
        return self.rec

    def _duellog_record(self, winner, why=None, turns=None):
        """Tras los dos _finish: arma el registro (un blob para los dos, como el oficial: P) y manda el 0x6010 a cada
        cliente conectado; el resto sale de uno en uno con sus 0x6013 (rama 0x6013 de _msg).
        07-10: la repetición juega su piedra-papel-tijera con la mano de cada bloque (+0xbc04 / +0xbc08) y empieza
        quien diga +0xbbe6 (research/repeticion/NOTES.md §7, C). Antes iban 0 y 0 (empate: bucle sin fin al verla) y
        el GANADOR en +0xbbe6: ahora las manos que decidieron (jhands) y quién empezó (first). winner ya no va al
        registro (el resultado sale del 0x4d de la grabación).
        07-10 (REPLAY_ARCHIVE, NOTES §8): los MISMOS mensajes van también al archivo del servidor (_archive, en un hilo
        aparte), con winner (absoluto, None = empate), why (WHY_* de duelfin) y turns para el índice."""
        if not (PVP_DUELLOG_RECORD or REPLAY_ARCHIVE):
            return
        msgs = None
        when = int(time.time())
        try:
            DL = _DL()
            # match (PVP_MATCH, 10-10): UN registro para todo el match, modo 4 (+0xba98, C), +0xbbe6..8 = quién empezó
            # cada partida (C: la repetición lo lee en el estado 7 de CJankenScene) y los 0x6202 de todas seguidos.
            # Que la repetición de un match se vea bien: P (sin probar en el cliente)
            firsts = list(getattr(self, "firsts", None) or [getattr(self, "first", None)])
            hands, first, avisos = DL.replay_start(getattr(self, "jhands", None), firsts[0])
            players = []
            for k in (0, 1):
                s = self.sess[k]
                parts = []
                dp = self.env.get("duel_parts")
                if dp is not None:
                    with self.as_sess(s):                # duel_parts(None, mundo) lee el perfil de la sesión del hilo
                        parts = [dp(None, 0), dp(None, 1), bytes(16)]
                players.append({"pid": s.pid, "listval": s.pid, "name": s.name or "Duelista", "parts": parts,
                                "main": len(self.deck[k]["main"]), "extra": len(self.deck[k]["extra"]),
                                "country": COUNTRY, "hand": hands[k]})
            games = (first, 0xFF, 0xFF)                   # +0xbbe6: quién empieza la partida 1 (duelo suelto)
            extra = {}
            if self._bo3():
                games = tuple(([first] + [0xFF if f is None else f for f in firsts[1:3]] + [0xFF, 0xFF])[:3])
                extra["mode"] = DL.MODE_MATCH
            msgs = DL.messages(players, list(self.rec.entries) if getattr(self, "rec", None) else [], games=games,
                               room_kind=self._room_kind(self.sess[0]), when=when, **extra)
            if PVP_DUELLOG_RECORD:
                self.log_q = [list(msgs), list(msgs)]
                n = sum(len(d) for m, d in msgs if m == DL.M_COMMAND)
                self.log(f"[pvp] {self.name}: registro de duelo: {len(self.rec.entries) if self.rec else 0} comandos, "
                         f"{n} B en {len(msgs) - 3} trozos 0x6011; piedra-papel-tijera {list(hands)}, empieza {first}"
                         + (f" ({'; '.join(avisos)})" if avisos else ""))
                for k in (0, 1):
                    if self.c[k] is not None and not self.closed[k]:
                        self.put(k, [self.log_q[k].pop(0)])
        except Exception as e:                            # noqa: BLE001  (sin registro: el cliente puede Cancelar)
            tb = traceback.format_exc().replace(chr(10), " | ")
            self.log(f"[EXC] {self.name}: registro de duelo: {e!r} | {tb}")
        if REPLAY_ARCHIVE and msgs is not None:
            self._archive(msgs, when, winner, why, turns)

    def _archive(self, msgs, when, winner, why=None, turns=None):
        """REPLAY_ARCHIVE: copia del servidor de este PvP (research/repeticion/rep_archivo.py). El .log y la línea del
        índice se escriben en un hilo aparte; un fallo solo va al log (nunca rompe el fin del duelo). Sin nada grabado
        (ningún 0x6202) no se guarda."""
        try:
            ncmd = len(self.rec.entries) if getattr(self, "rec", None) is not None else 0
            RA = _RA()
            root = RA.raiz(self.env.get("STATE_PATH"))
            if root is None:
                return
            if turns is None:
                turns = getattr(getattr(self, "ps", None), "turn", 0) or 0
            jug = [{"nombre": s.name or "Duelista", "pid": s.pid, "acct": getattr(s, "acct", None)} for s in self.sess]
            msgs = list(msgs)
            RA.archivar(root, "pvp", when, lambda: (msgs, ncmd, ""), jug, None, winner, why, turns, self.log,
                        duelo=self.name)
        except Exception as e:                            # noqa: BLE001
            tb = traceback.format_exc().replace(chr(10), " | ")
            self.log(f"[EXC] {self.name}: archivo de repeticiones: {e!r} | {tb}")

    def _room_kind(self, s):
        """b2 de la sala donde se pactó el duelo (ROOM_KINDS de run.py); 12 (My Room = mundo actual) si no se sabe.
        Va a +0xbba3 del registro: un valor fuera de la tabla de FUN_00a587a0 tumba al cliente al abrirlo."""
        try:
            return self.env["ROOM_KINDS"][s.block[0] - self.env["ROOM_PORT_BASE"]]
        except Exception:                                 # noqa: BLE001
            return 12

    def _close_match(self):
        self.done = True
        self.hub.drop(self)

    def _svrinfo(self, code):
        LS = self.env.get("LS")
        return LS.duelsvrinfomsg(code, 1) if LS is not None else bytes([code, 1])

    def _xp_levels(self):
        """[LV del jugador 0, LV del 1] antes del duelo (o del match: se lee una vez). Con ellos se reparte la puntuación
        (PS.pvp_xp): si se leyera en cada _finish, el segundo jugador vería ya el LV nuevo del primero."""
        lv = self.__dict__.get("_xp_lv")
        if lv is None:
            lv = []
            PS = self.env["DFM"].PS
            for s in self.sess:
                try:
                    with self.as_sess(s):
                        with (self.env.get("STATE_LOCK") or threading.RLock()):
                            st = self.env["load_state"]()
                            lv.append(int(PS.profile(st, PS.active_slot(st)).get("rec", {}).get("lv", 1) or 1))
                except Exception as e:                    # noqa: BLE001
                    self.log(f"[EXC] {self.name}: LV previo: {e!r}")
                    lv.append(1)
            self._xp_lv = lv
        return lv

    def _finish(self, k, wv, why, turns=0, svr=None, fin=None, score=None, last=True, played=True, ends=True,
                send=True, mres=None):
        """Fin para el cliente k: wv = 0 gana él, 1 pierde, None empate (su vista). Perfil + lobby + cola de fin.
        Match (PVP_MATCH): fin = P1 de 0x4d (None = FINISH_NORMAL), score = DUELSCORE del match, last = el match se
        acaba (carta, 0x4800 y cabecera de registro solo entonces), played = se jugó una partida (estadísticas y 1 DP),
        ends = mandar 0x4d (si no, solo el 0x6613 con score), send = mandarle algo (False: solo su perfil)."""
        DFM = self.env["DFM"]
        PS = DFM.PS
        s = self.sess[k]
        res = DFM.result_for_client(wv)
        on = send and self.c[k] is not None and not self.closed[k]
        lob = self.lobby(k) if on else None              # ¿puede elegir sobre? (§14)
        dp, packs, card, event_won = None, (), "", False
        with self.as_sess(s):
            lk = self.env.get("STATE_LOCK") or threading.RLock()
            with lk:
                st = self.env["load_state"]()
                p = PS.profile(st, PS.active_slot(st))
                xp = None
                if played:
                    xp = self._xp(PS, p, k, res, DFM, last, mres)   # antes de pvp_record: cuenta duelos y los títulos lo ven
                    pvp_record(PS, p, res, DFM, turns)
                    # Coste del duelo (persistencia NOTES §13): 1 DP a CADA jugador por duelo jugado (en un match, por
                    # partida: 2 o 3), también al rendirse, desconectarse o empatar (P); no si el duelo cae por un
                    # fallo nuestro (WHY_ABORT, D). Va en el mismo 0x4140 de abajo: el cliente nunca resta él solo (C).
                    dp = (PS.dp_charge(p) if why != getattr(DFM, "WHY_ABORT", "abort")
                          and getattr(PS, "DUEL_DP_COST", 0) and hasattr(PS, "dp_charge") else None)
                    ER = self.env.get("ER")
                    if ER is not None and hasattr(ER, "on_pvp_result"):
                        result_name = ("win" if res == DFM.RESULT_WIN else
                                       "lose" if res == DFM.RESULT_LOSE else "draw")
                        duel_no = p.get("rec", {}).get("duels", 0)
                        rewards = ER.on_pvp_result(
                            p, self._room_kind(s), result_name,
                            f"{self.name}:{duel_no}:{k}", played=True, last=last,
                            match=self._bo3(),
                            game_number=len(getattr(self, "games", ()) or ()) if self._bo3() else 1)
                        if rewards:
                            event_won = True
                            self.log(f"[eventos] {self.name}: jugador {k}, premios {rewards}")
                        event_won = event_won or any(
                            not bool(x.get("championship")) for x in p.get("event_rewards", ()))
                # En un milestone se manda 0x4800 vacío: la escena solicita 0x4910 y recibe el premio de evento.
                # La carta PvP normal no se mezcla con ese claim ni abre un segundo selector.
                if last and not event_won:
                    packs, card = self._card(PS, p, why, lob is not None, match=self._bo3())
                self.env["save_state"](st)
            pdu = PS.player_data_update(p)
        self.log(f"[pvp] {self.name}: fin para {k} ({s!r}): {'gana' if wv == 0 else 'pierde' if wv == 1 else 'empate'}"
                 f" ({why})" + ("" if last else " (partida del match)") + (f"; DP {dp[0]} -> {dp[1]}" if dp else "")
                 + (f"; puntuación {xp['delta']:+d} -> {xp['score']}, LV {xp['lv_old']} -> {xp['lv']}" if xp else "")
                 + (f"; {card}" if card else ""))
        if not on:
            return
        if lob is not None:
            (lob.post if hasattr(lob, "post") else lob.send)(DFM.M_PDU, pdu)   # antes del 0x4d (CResultScene)
        if svr and PVP_SVRINFO:
            self.put(k, [(0x649b, self._svrinfo(svr)), (None, lambda: time.sleep(SVRINFO_WAIT))])
        if ends:
            kw = {}
            if (why == getattr(DFM, "WHY_SURRENDER", "surrender") and SURRENDER_FIN_OFICIAL
                    and hasattr(DFM, "FINISH_SURRENDER")):     # como el oficial (C): sin 0x23, motivo 12, 0x4d P1 = 2
                kw["official_surrender"] = True
                if fin is None:
                    fin = DFM.FINISH_SURRENDER
            self.put(k, DFM.end_duel_msgs(wv, why, finish=DFM.FINISH_NORMAL if fin is None else fin, score=score, **kw))
        elif score is not None:
            self.put(k, [(DFM.M_DUELSCORE, DFM.duelscore(score[0], score[1]))])
        if last and PVP_DUELLOG_FINISH and not PVP_DUELLOG_RECORD:   # cabecera (n = 0) + cierre: ver duellog_msgs
            me, ot = self.sess[k], self.sess[1 - k]
            self.put(k, duellog_msgs(me.name, ot.name, me.pid, ot.pid)[:1])   # solo 0x6010; 0x6012 tras su 0x6013
        if last and lob is not None:         # con sobres (§14): «Siguiente» -> elector -> 0x4802 (run.py) -> 0x4803
            self.put(k, [(None, lambda: (lob.post if hasattr(lob, "post") else lob.send)(
                DFM.M_CARDPACKLIST, DFM.cardpacklist_notice(packs)))])

    def _xp(self, PS, p, k, res, DFM, last, mres):
        """Puntuación y LV de una partida PvP (PS.pvp_xp, rx/XP) y los contadores de duelo que el servidor conoce (LP final
        del duelcore, rendición: PS.apply_duel_stats). Modifica p (se guarda fuera). Un fallo solo va al log."""
        xp = None
        try:
            surr = getattr(self, "surr", None)
            lp = getattr(getattr(self, "ps", None), "last_lp", None)
            try:
                lp_left = max(0, int(lp[k])) if lp else 0
            except (TypeError, ValueError, IndexError):
                lp_left = 0
            if hasattr(PS, "apply_duel_stats"):
                tally = getattr(getattr(self, "ps", None), "stats", None)
                stats = tally.for_player(k) if tally is not None else None      # B51: tributos, Especiales, activaciones…
                PS.apply_duel_stats(p, stats, lp_left, None if surr is None else "me" if surr == k else "opp")
            if hasattr(PS, "pvp_xp"):
                name = "win" if res == DFM.RESULT_WIN else "lose" if res == DFM.RESULT_LOSE else "draw"
                lv = self._xp_levels()
                xp = PS.pvp_xp(p, lv[1 - k], name, in_match=self._bo3(), match_result=mres, last=last, my_lv=lv[k])
        except Exception as e:                            # noqa: BLE001  (la puntuación nunca tumba el fin del duelo)
            self.log(f"[EXC] {self.name}: puntuación: {e!r}")
        return xp

    def _card(self, PS, p, why, can_pick, match=False):
        """Carta tras el PvP (persistencia NOTES §14; YO2_PVP_CARTA=0 apaga): 1 por duelo terminado a cada jugador,
        también al rendirse, desconectarse o empatar; no si cae nuestro motor (WHY_ABORT, igual que el cobro de DP).
        Conectado: queda pendiente en el perfil y devuelve los sobres para 0x4800 (él elige). Sin conexión: n cartas
        de un sobre al azar directas al baúl. Modifica p (se guarda fuera). Devuelve (sobres, texto para el log)."""
        try:
            DFM = self.env["DFM"]
            if why == getattr(DFM, "WHY_ABORT", "abort") or not getattr(PS, "PVP_CARD_ON", False)                     or not hasattr(PS, "pvp_card_offer"):
                return (), ""
            if can_pick:
                e = PS.pvp_card_offer(p, match=match)  # match (PVP_MATCH): una vez al acabar, n = 3 (§14, P)
                return (tuple(e["packs"]), f"carta pendiente ({e['n']}): elige entre {len(e['packs'])} sobres")                     if e else ((), "")
            e = PS.pvp_card_entry(match)
            if e is None:
                return (), ""
            LP = self.env.get("LP")
            if LP is not None and hasattr(LP, "_cards"):
                draw = LP._cards
            else:
                draw = lambda pk, n: [cid for _, cid in self.env["shop_purchase"](1, pk, 1)][:n]   # noqa: E731
            pk, cards = PS.pvp_card_grant(p, e, draw)
            return (), f"sin conexión: carta directa al baúl (sobre {pk}: {cards})"
        except Exception as ex:                          # noqa: BLE001  (la carta nunca tumba el fin del duelo)
            self.log(f"[EXC] {self.name}: carta tras el PvP: {ex!r}")
            return (), ""


def _quita_rendirse(data):
    """0x6611 sin «Rendirse»: bit 10 de la máscara del mazo propio (jugador 0, zona 0xd). Formato (research/lockstep
    comid_field): por jugador, zonas 0..15 salvo la 11 (mano) -> u16 máscara + u8; la zona 0xd es la 13.ª entrada (12 con
    la 11 saltada), a los 36 B. Lo que no mida 110 B se deja tal cual."""
    if len(data) != 110:
        return data
    o = 12 * 3
    m = int.from_bytes(data[o:o + 2], "big")
    if not m & (1 << 10):
        return data
    return data[:o] + (m & ~(1 << 10)).to_bytes(2, "big") + data[o + 2:]


class _SurrTx:
    """Cola de envío de un jugador de PvP que, en una sala con «Rendirse» OFF, quita «Rendirse» de las máscaras 0x6611
    (el servidor decide qué entradas trae el menú del mazo). Lo demás pasa igual; el resto de atributos, de la cola."""

    def __init__(self, tx, match):
        self.tx, self.m = tx, match

    def put(self, msgs):
        if SURRENDER_ROOM_OPTION and self.m.room_surrender == 0:
            msgs = [(m[0], _quita_rendirse(m[1])) if isinstance(m, tuple) and m[0] == 0x6611 and isinstance(m[1], (bytes, bytearray))
                    else m for m in msgs]
        self.tx.put(msgs)

    def __getattr__(self, k):
        return getattr(self.tx, k)


class _RecTx:
    """Cola de envío de un jugador que además apunta cada 0x6202 (lado = índice del jugador) en el Recorder."""

    def __init__(self, tx, rec, side, cb=None):
        self.tx, self.rec, self.side, self.cb = tx, rec, side, cb

    def put(self, msgs):
        msgs = list(msgs)
        self.rec.note(self.side, msgs)
        self.tx.put(msgs)
        if self.cb is not None and self.side == 0:      # PVP_WATCH: espectadores esperando más 0x6202
            self.cb()


def _DL():
    """research/repeticion/duellog.py (solo con PVP_DUELLOG_RECORD)."""
    import sys
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research", "repeticion")
    if d not in sys.path:
        sys.path.insert(0, d)
    import duellog
    return duellog


def _RA():
    """research/repeticion/rep_archivo.py (archivo de repeticiones del servidor, REPLAY_ARCHIVE)."""
    _DL()                                                # pone research/repeticion en sys.path
    import rep_archivo
    return rep_archivo


def duellog_msgs(name_a, name_b, pid_a, pid_b, title=None):
    """[(0x6010, cabecera con n = 0), (0x6012, b"")]: ver PVP_DUELLOG_FINISH. 72 B + largo del título."""
    t = (title or time.strftime("pvp%Y%m%d%H%M%S")).encode("latin1", "replace")[:31]
    n16 = lambda x: str(x or "").encode("latin1", "replace")[:15].ljust(16, b"\0")
    head = (struct.pack(">HH", 0, 0) + t + b"\0" + struct.pack(">IIIII", 0, 0, 0, pid_a or 0, pid_b or 0)
            + n16(name_a) + n16(name_b) + struct.pack(">III", 0, 0, 0) + bytes(3))
    return [(0x6010, head), (0x6012, b"")]


def pvp_record(PS, p, res, DFM, turns=0):
    """Estadísticas de un duelo PvP en el perfil p (se modifica; guardar después). Sin NPCBIN, premios de NPC ni
    0x4237: solo duelos, turnos y victorias/derrotas/empates (las «wins»/«losses» del registro sí cuentan en PvP)."""
    r = p.setdefault("rec", {})
    PS.rec_inc(r, "duels")
    PS.rec_inc(r, "turns", max(0, int(turns or 0)))
    if res == DFM.RESULT_WIN:
        PS.rec_inc(r, "duels_won"); PS.rec_inc(r, "wins")
    elif res == DFM.RESULT_LOSE:
        PS.rec_inc(r, "duels_lost"); PS.rec_inc(r, "losses")
    else:
        PS.rec_inc(r, "duels_drawn")
    p.setdefault("pvp", {})["last"] = {"result": int(res), "t": int(time.time())}
