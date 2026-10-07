"""Gateway Edge que avisaron que se desconectaron (TC-M09-63).

`dynsec getClient` no sirve para saber si un Edge está apagado: el `edge_agent`
conecta con sesión persistente y Mosquitto sigue listando su conexión después
de que se cae. Sin otra señal, el broker publicaba y esperaba los 30 s del ACK
para terminar en NO_CONF.

El Edge publica `{"tipo_mensaje": "DESCONEXION"}` en su topic `status`: como
Last Will si la conexión se corta, y él mismo antes de un cierre ordenado. Su
siguiente heartbeat lo vuelve a dar por conectado.

En memoria, como las esperas de ACK (`correlacion`): tras reiniciar el broker
no hay marcas y se vuelve al chequeo por dynsec, que ante la duda publica.
"""

from __future__ import annotations

TIPO_DESCONEXION = "DESCONEXION"

_desconectados: set[str] = set()


def marcar_desconectado(serial: str) -> None:
    _desconectados.add(serial)


def marcar_conectado(serial: str) -> None:
    _desconectados.discard(serial)


def desconectado(serial: str) -> bool:
    return serial in _desconectados
