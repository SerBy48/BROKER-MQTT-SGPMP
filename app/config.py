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
    # siempre para que las Raspberry sin credencial propia sigan conectando.
    # Quitarla de Dokploy cuando todas migraron: el gateway borra ese cliente.
    mqtt_device_username: str | None = None
    mqtt_device_password: str | None = None

    # Convención de topics
    mqtt_topic_prefix: str = "sgpmp"
    mqtt_topic_telemetry: str = "telemetry"
    mqtt_topic_heartbeat: str = "heartbeat"
    mqtt_topic_command: str = "command"
    mqtt_topic_status: str = "status"

    # RF-23: segundos que /v1/commands espera el ACK del dispositivo antes
    # de responder NO_CONF.
    mqtt_ack_timeout_seconds: int = 30

    # TC-M09-252 (anti-replay): cada comando lleva `id_comando` y el ACK debe
    # devolverlo. Con False (default) un ACK SIN id todavía se acepta, para no
    # romper firmware que aún no lo devuelve; un ACK con un id DISTINTO se
    # ignora siempre. Pasar a True cuando IoT confirme que ya lo devuelve: así
    # un ACK capturado y reenviado (o forjado sin id) deja de resolver comandos.
    mqtt_ack_requiere_id_comando: bool = False

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
