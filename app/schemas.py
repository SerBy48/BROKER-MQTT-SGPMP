"""Modelos de datos (payloads MQTT y contratos de la API)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import (
    CategoriaVariable,
    OrigenTelemetria,
    TipoDato,
    TipoMensajeIot,
)


class TelemetryPayload(BaseModel):
    """Payload esperado en el topic `<prefix>/<serial>/telemetry`.

    TODO: alinear los nombres de campos con el contrato real que envían los
    dispositivos edge; este es el contrato propuesto como esqueleto.
    """

    variable: str
    valor_crudo: float
    unidad: str
    timestamp_captura: datetime
    timestamp_envio: datetime | None = None
    sensor: str | None = None
    origen: OrigenTelemetria = OrigenTelemetria.TIEMPO_REAL
    categoria_variable: CategoriaVariable = CategoriaVariable.AMBIENTAL
    tipo_dato: TipoDato = TipoDato.CRUDO
    valor_agregado: bool = False
    ventana_agregacion_min: int | None = None
    latitud: float | None = None
    longitud: float | None = None
    nivel_bateria_pct: float | None = None
    calidad_senal_rssi: float | None = None
    calidad_senal_snr: float | None = None
    estado_conectividad: bool | None = None
    metadatos: dict[str, Any] = Field(default_factory=dict)


class HeartbeatPayload(BaseModel):
    """Payload esperado en el topic `<prefix>/<serial>/heartbeat`."""

    tipo_mensaje: TipoMensajeIot = TipoMensajeIot.HEARTBEAT
    nivel_bateria_pct: float | None = None
    calidad_senal_rssi: float | None = None
    calidad_senal_snr: float | None = None
    estado_local_buffer: str | None = None
    datos_pendientes_buffer: int = 0
    version_firmware: str | None = None
    coordenadas: dict[str, Any] | None = None
    fecha_registro: datetime | None = None
    reloj_sincronizado: bool = False


class CommandRequest(BaseModel):
    """Comando enviado por el servidor web para reconfigurar un dispositivo."""

    serial: str
    frecuencia_captura: int = Field(gt=0)
    intervalo_transmision: int = Field(gt=0)


class CommandResponse(BaseModel):
    serial: str
    topic: str
    estado: str = "PENDIENTE"
    id_configuracion_remota: int | None = None
