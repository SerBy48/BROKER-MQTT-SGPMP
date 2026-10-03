"""Modelos de datos (payloads MQTT y contratos de la API)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

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
    """Comando enviado por el servidor web para reconfigurar un dispositivo.

    `origen` identifica qué caso de uso del backend originó el comando.
    Hoy solo existe "configuracion" (RF-23); se deja como Literal para poder
    agregar "telemetria"/"prediccion" el día que esos flujos envíen comandos
    reales por este mismo punto de entrada — no se construye nada de eso
    todavía.
    """

    origen: Literal["configuracion"]
    serial: str
    frecuencia_captura: int = Field(gt=0)
    intervalo_transmision: int = Field(gt=0)


class CommandResponse(BaseModel):
    serial: str
    topic: str | None = None
    estado: Literal["APLICADA", "PENDIENTE", "NO_CONF"] = "PENDIENTE"
    mensaje: str


# Mismo allow-list que SerialDispositivo en sgpmp-backend. Además es una barrera
# de seguridad acá: el serial termina dentro de nombres de rol y de topics de la
# ACL de dynamic-security, donde un '#', '+' o '/' abriría permisos ajenos.
PATRON_SERIAL = r"^[A-Za-z0-9_-]{1,50}$"
Serial = Annotated[str, StringConstraints(pattern=PATRON_SERIAL)]


class CredencialRequest(BaseModel):
    """Alta o rotación de la credencial MQTT de una Raspberry (SEG-BROKER-03).

    El serial principal (el del path) es el usuario MQTT. `seriales_adicionales`
    son los otros seriales que esa misma Raspberry transmite (modelo "serial por
    ESP32"); vacío en el modelo "serial por sitio".
    """

    seriales_adicionales: list[Serial] = Field(default_factory=list, max_length=50)


class CredencialResponse(BaseModel):
    usuario: str
    password: str
    seriales: list[str]


class EstadoCredencialResponse(BaseModel):
    usuario: str
    habilitada: bool
    conectada: bool
    seriales: list[str]
