# Eventos de YO2 Ver.080912

Este módulo separa cuatro conceptos que el cliente no separa por sí solo:

1. `0x5000 EVENTINFO` anuncia el inicio o fin.
2. El tipo de sala (`b2`) determina qué regla se aplica.
3. El servidor conserva el progreso de cada edición.
4. `0x4910` y `0x4915` solicitan una recompensa ya obtenida.

## Reglas habilitadas

- **DUELIST TRIAL** (`b2` 9/16, EVENTINFO 5/15): Single Duel,
  victorias acumuladas, premios en 4 y 5; el lote se repite cada cinco
  victorias. Fuentes históricas de YO2 describen explícitamente “4ème étoile”
  y “5ème étoile”.
- **GUNSLINGER** (`b2` 7/15, EVENTINFO 1/3/9/13): Single Duel, racha;
  premios especiales en 5, 6 y 7. Perder o empatar corta la racha.
- **ANNIVERSARY TRIAL** (`b2` 10, EVENTINFO 7): Single Duel, victorias
  acumuladas. Las ediciones usaron catálogos diferentes, por lo que las
  cartas se configuran por milestone.
- **CHAMPIONSHIP** (`b2` 8, EVENTINFO 11): cuadro eliminatorio; premios de
  campeón y subcampeón.
- **EVOLUTION MATCH** (`b2` 17, EVENTINFO 17): Match al mejor de tres. Una
  victoria en la primera, segunda o tercera ronda suma 2, 4 o 6 estrellas,
  respectivamente. Perder una ronda o el match no elimina las estrellas ya
  obtenidas; se entrega una carta al cruzar 10, 20 y 30.

## Reglas visibles pero bloqueadas

- **ULTIMATE MATCH / LAST BATTLE** (`b2` 20, EVENTINFO 21): están
  documentados los tiers hasta 100, pero no se ha confirmado que el anuncio
  ULTIMATE y la sala LAST BATTLE sean el mismo flujo en Ver.080912.

No se habilitan estas dos familias hasta demostrar su fórmula en el cliente o
en una fuente contemporánea de YO2. El launcher debe explicar el motivo, no
presentar una reconstrucción como regla oficial.

## Protocolo

- Estados impares de `EVENTINFO`: inicio; el par siguiente: final.
- En salas 7/9/10/15/16/17/20, si `0x4800` está vacío, el cliente solicita
  `0x4910 REQGUNSLINGCARD` y espera `0x4803`.
- Championship usa `0x4915 REQCHAMPSCARDGET`, también resuelto con `0x4803`.
- Una petición sin premio debe recibir un `0x4803` vacío para que la escena no
  quede esperando.

Las cartas se guardan en el baúl antes de enviar la respuesta y cada premio
lleva una clave `familia:serial:milestone` para impedir duplicados.

Cuando coincide un milestone con la carta PvP ordinaria, el premio de evento
tiene prioridad: se envía `0x4800` vacío para provocar `0x4910` y no se abre un
segundo selector. Un claim que quedó pendiente por desconexión usa el siguiente
fin de duelo de evento del jugador.
