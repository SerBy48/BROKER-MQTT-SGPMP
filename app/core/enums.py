"""Espejo en Python de los enums definidos en la base de datos (schema modulo3).

Los valores deben coincidir exactamente con los labels de los tipos enum de
PostgreSQL, para que los parámetros sean aceptados por las funciones de ingesta.
"""

from __future__ import annotations

from enum import Enum


class OrigenTelemetria(str, Enum):
    TIEMPO_REAL = "TIEMPO_REAL"
    BUFFER_LOCAL = "BUFFER_LOCAL"
    EDGE_AGREGADO = "EDGE_AGREGADO"


class CategoriaVariable(str, Enum):
    AMBIENTAL = "AMBIENTAL"
    ANIMAL = "ANIMAL"
    HIDRICA = "HIDRICA"


class TipoDato(str, Enum):
    CRUDO = "CRUDO"
    AGREGADO = "AGREGADO"
    EVENTO_EDGE = "EVENTO_EDGE"


class TipoMensajeIot(str, Enum):
    HEARTBEAT = "HEARTBEAT"
    TELEMETRIA = "TELEMETRIA"
    ESTADO_CAMBIO = "ESTADO_CAMBIO"


class EstadoTransmision(str, Enum):
    EXITO = "EXITO"
    FALLIDO = "FALLIDO"
    PENDIENTE = "PENDIENTE"
    REINTENTADO = "REINTENTADO"
    TIMEOUT = "TIMEOUT"
