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


class SensorOperativo(BaseModel):
    # `sensor`: el mismo identificador que viaja en el campo `sensor` de la telemetría.
    sensor: str = Field(min_length=1)
    operativo: bool


class HeartbeatPayload(BaseModel):
    """Payload esperado en el topic `<prefix>/<serial>/heartbeat`.

    `sensores_operativos` y `temperatura_dispositivo` son de D11 (AIOT-DEV-03,
    BRK-07): opcionales, porque el firmware que aún no los manda debe seguir
    funcionando, y solo se guardan si la BD ya tiene sus columnas (DB-03).
    """

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
    sensores_operativos: list[SensorOperativo] | None = None
    temperatura_dispositivo: float | None = Field(default=None, allow_inf_nan=False)  # °C

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


# BRK-05 (D7 de AIOT-DEV-03): tres comandos nuevos por el mismo patrón de RF-17.
# El broker fija el sobre (a qué nodo, qué versión, cómo se confirma); el
# contenido de la regla y de la configuración del motor lo arma el backend
# desde su tabla y lo interpreta el firmware, así que aquí viaja tal cual.


class ReglaCompuestaCommandRequest(BaseModel):
    """Regla compuesta de `modulo3.reglas_alertas` hacia un Gateway Edge (RF-55/RF-57).

    El nodo la guarda junto a sus umbrales y la evalúa con prevalencia sobre la
    regla simple (RF-57 E14/E15). `version_regla` deja que descarte una versión
    más vieja que llegue tarde; `es_activa=False` le pide retirarla.
    """

    origen: Literal["regla_compuesta"]
    serial: str
    id_regla_alerta: int = Field(gt=0)
    version_regla: int = Field(ge=1)
    es_activa: bool = True
    regla: dict[str, Any] = Field(min_length=1)


class ConfigMotorCommandRequest(BaseModel):
    """Configuración del motor de IA de `modulo4.configuraciones_motor_ia` (RF-65).

    El nodo confirma con `ACK_CONFIG_MOTOR` y el `config_version` que quedó
    activo; el backend lo compara con el suyo (`sincronizacion_nodos_edge`).
    """

    origen: Literal["config_motor"]
    serial: str
    tipo_modelo: str = Field(min_length=1)
    config_version: int = Field(ge=1)
    configuracion: dict[str, Any] = Field(min_length=1)


class TipoModeloCommandRequest(BaseModel):
    """`tipo_modelo_asignado` de un área (M09·RF-20, D-06).

    El nodo reinicia su módulo de procesamiento con el esquema nuevo y conserva
    la calibración activa.
    """

    origen: Literal["tipo_modelo"]
    serial: str
    id_infraestructura: int = Field(gt=0)
    tipo_modelo_asignado: str = Field(min_length=1)


ComandoRequest = Annotated[
    CommandRequest
    | UmbralCommandRequest
    | ReglaCompuestaCommandRequest
    | ConfigMotorCommandRequest
    | TipoModeloCommandRequest,
    Field(discriminator="origen"),
]


class CommandResponse(BaseModel):
    serial: str
    topic: str | None = None
    estado: Literal["APLICADA", "PENDIENTE", "NO_CONF"] = "PENDIENTE"
    mensaje: str
    # BRK-05: el `config_version` que el nodo informa en su ACK (CONFIG_MOTOR_IA).
    # None en los demás comandos o si el ACK no lo trae.
    config_version: int | None = None


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
