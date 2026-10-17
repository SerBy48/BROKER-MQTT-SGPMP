"""Configuración central de SGPMP, cargada desde variables de entorno (.env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL existente
    database_url: str
    db_schema_ingest: str = "modulo3"
    db_schema_registry: str = "modulo9"

    # Broker MQTT (Mosquitto)
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    mqtt_client_id: str = "sgpmp"
    mqtt_reconnect_delay: int = 5

    # Credencial COMPARTIDA legacy de los dispositivos (TC-M09-250/251). Mientras
    # esté definida, el gateway la mantiene en dynamic-security con la ACL de
    # siempre para que los Gateway Edge sin credencial propia sigan conectando.
    # Quitarla de Dokploy cuando todas migraron: el gateway borra ese cliente.
    mqtt_device_username: str | None = None
    mqtt_device_password: str | None = None

    # Convención de topics
    mqtt_topic_prefix: str = "sgpmp"
    mqtt_topic_telemetry: str = "telemetry"
    mqtt_topic_heartbeat: str = "heartbeat"
    mqtt_topic_command: str = "command"
    mqtt_topic_status: str = "status"
    # BRK-01 (D2 de AIOT-DEV-03): aviso al nodo del resultado de cada ingesta de
    # telemetría, para que libere o retenga su buffer. Topic propio (broker -> nodo):
    # el firmware actual trata todo `command` sin tipo_comando como configuración
    # RF-23. Formato propuesto por Desarrollo; AIoT lo confirma en FW-01.
    mqtt_topic_resultado: str = "resultado"
    # D4/D5/D6 de AIOT-DEV-03 (nodo -> broker -> endpoint interno del backend):
    # vector de visión (BRK-03), eventos Edge de RF-55 (BRK-04) y OTA de modelos
    # (BRK-08: `ota/notify` broker -> nodo, `ota/<evento>` nodo -> broker).
    mqtt_topic_vision: str = "vision"
    mqtt_topic_evento: str = "evento"
    mqtt_topic_ota: str = "ota"

    # RF-23: segundos que /v1/commands espera el ACK del dispositivo antes
    # de responder NO_CONF.
    mqtt_ack_timeout_seconds: int = 30

    # TC-M09-252 (anti-replay): cada comando lleva `id_comando` y el ACK debe
    # devolverlo. Con False (default) un ACK SIN id todavía se acepta, para no
    # romper firmware que aún no lo devuelve; un ACK con un id DISTINTO se
    # ignora siempre. Pasar a True cuando IoT confirme que ya lo devuelve: así
    # un ACK capturado y reenviado (o forjado sin id) deja de resolver comandos.
    mqtt_ack_requiere_id_comando: bool = False

    # BRK-02 (P3 de AIOT-DEV-03): cliente HTTP broker -> sgpmp-backend para los
    # eventos de control de bajo volumen (buffer, visión, eventos Edge, OTA). El
    # token es el mismo BROKER_SERVICE_TOKEN del backend; va solo en el entorno.
    backend_url: str = ""
    broker_service_token: str = ""
    backend_timeout_seconds: float = 10.0

    # API HTTPS (0.0.0.0 requerido dentro de contenedor Docker)
    api_host: str = "0.0.0.0"  # nosec B104
    api_port: int = 8000

    @property
    def telemetry_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_telemetry}"

    @property
    def heartbeat_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_heartbeat}"

    @property
    def status_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_status}"

    @property
    def vision_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_vision}"

    @property
    def evento_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_evento}"

    @property
    def ota_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/{self.mqtt_topic_ota}/+"


@lru_cache
def get_settings() -> Settings:
    return Settings()
