"""Configuración de logging de la aplicación.

Emitimos siempre a stdout en formato JSON estructurado (una línea por evento),
para que Dokploy y la terminal puedan ver y filtrar los logs sin depender de
ninguna variable de entorno. El nivel queda fijo en DEBUG para que toda la
actividad (MQTT, BD y API) sea visible en todo momento.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_LOGGERS_EXTERNOS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "aiomqtt",
)


class JsonFormatter(logging.Formatter):
    """Formatea cada registro como un objeto JSON de una sola línea."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.DEBUG) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Uvicorn y aiomqtt configuran sus propios handlers al arrancar; los
    # quitamos y dejamos que propaguen al root para unificar el formato JSON.
    for name in _LOGGERS_EXTERNOS:
        external = logging.getLogger(name)
        external.handlers = []
        external.propagate = True
        external.setLevel(level)
