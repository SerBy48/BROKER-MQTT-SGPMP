"""Modelos de datos (payloads MQTT y contratos de la API)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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


# modulo3.heartbeats.estado_local_buffer es "char" (un byte): 'I'nactivo,
# 'A'ctivo, 'L'leno. El edge_agent manda la palabra completa; se aceptan ambas.
_ESTADOS_BUFFER = {"I": "I", "A": "A", "L": "L", "INACTIVO": "I", "ACTIVO": "A", "LLENO": "L"}


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

    @field_validator("estado_local_buffer")
    @classmethod
    def _codigo_buffer(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        codigo = _ESTADOS_BUFFER.get(valor.strip().upper())
        if codigo is None:
            raise ValueError("estado_local_buffer debe ser I/A/L o INACTIVO/ACTIVO/LLENO")
        return codigo


class CommandRequest(BaseModel):
    """Comando enviado por el servidor web para reconfigurar un dispositivo.

    `origen` identifica qué caso de uso del backend originó el comando:
    "configuracion" (RF-23, esta clase) o "umbral" (RF-17,
    `UmbralCommandRequest`). `ComandoRequest` es la unión que recibe
    `POST /v1/commands`.
    """

    origen: Literal["configuracion"]
    serial: str
    # RF-23 v1.1: un SENSOR lleva frecuencia_captura + intervalo_transmision;
    # una CAMARA lleva solo fps (1-60, RF-21). Nunca ambos juegos.
    frecuencia_captura: int | None = Field(default=None, gt=0)
    intervalo_transmision: int | None = Field(default=None, gt=0)
    fps: int | None = Field(default=None, ge=1, le=60)

    @model_validator(mode="after")
    def _un_solo_juego_de_parametros(self) -> CommandRequest:
        sensor = self.frecuencia_captura is not None and self.intervalo_transmision is not None
        sensor_parcial = (self.frecuencia_captura is None) != (self.intervalo_transmision is None)
        camara = self.fps is not None
        if sensor_parcial or sensor == camara:
            raise ValueError(
                "envíe frecuencia_captura e intervalo_transmision (sensor) o solo fps (cámara)"
            )
        return self


class NivelUmbral(BaseModel):
    nivel: Literal["normal", "precaucion", "critico"]
    limite_inferior: float
    limite_superior: float


class UmbralCommandRequest(BaseModel):
    """Umbral ambiental de una especie hacia un Gateway Edge (RF-17, INC-M09-104-G29).

    El backend resuelve a qué Gateway Edge le corresponde el umbral (los de las
    áreas de esa especie) y llama una vez por `serial` de Gateway. `version` es
    la `fecha_actualizacion` del umbral: deja que el Edge descarte una versión
    más vieja que llegue tarde. `variable` es el mismo nombre que el Edge usa en
    el campo `variable` de la telemetría.
    """

    origen: Literal["umbral"]
    serial: str
    id_umbral_ambiental: int = Field(gt=0)
    version: datetime | None = None
    variable: str = Field(min_length=1)
    unidad: str
    valor_min: float
    valor_max: float
    niveles: list[NivelUmbral] = Field(min_length=1)


ComandoRequest = Annotated[CommandRequest | UmbralCommandRequest, Field(discriminator="origen")]


class CommandResponse(BaseModel):
    serial: str
    topic: str | None = None
    estado: Literal["APLICADA", "PENDIENTE", "NO_CONF"] = "PENDIENTE"
    mensaje: str


# Mismo allow-list que SerialDispositivo en sgpmp-backend. Además es una barrera
# de seguridad acá: el serial termina dentro de nombres de rol y de topics de la
# ACL de dynamic-security, donde un '#', '+' o '/' abriría permisos ajenos.
PATRON_SERIAL = r"^[A-Za-z0-9_-]{1,50}$"


class CredencialResponse(BaseModel):
    usuario: str
    password: str
    seriales: list[str]


class EstadoCredencialResponse(BaseModel):
    usuario: str
    habilitada: bool
    conectada: bool
    seriales: list[str]
