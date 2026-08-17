"""Cliente MQTT (aiomqtt) que conecta con Mosquitto y consume los topics IoT."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiomqtt

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _topic_str(topic: Any) -> str:
    return str(getattr(topic, "value", topic))


class MqttGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None

    def _build_client(self) -> aiomqtt.Client:
        tls_context = None
        if self.settings.mqtt_tls:
            import ssl

            tls_context = ssl.create_default_context()
        return aiomqtt.Client(
            hostname=self.settings.mqtt_host,
            port=self.settings.mqtt_port,
            username=self.settings.mqtt_username,
            password=self.settings.mqtt_password,
            client_id=self.settings.mqtt_client_id,
            tls_context=tls_context,
        )

    async def run(self) -> None:
        from app.mqtt.handlers import handle_message

        while True:
            try:
                async with self._build_client() as client:
                    self._client = client
                    await client.subscribe(self.settings.telemetry_topic, qos=1)
                    await client.subscribe(self.settings.heartbeat_topic, qos=1)
                    await client.subscribe(self.settings.status_topic, qos=1)
                    logger.info(
                        "MQTT conectado y suscrito a %s/{telemetry,heartbeat,status}",
                        self.settings.mqtt_topic_prefix,
                    )
                    async for message in client.messages:
                        await handle_message(_topic_str(message.topic), message.payload)
            except aiomqtt.MqttError as exc:
                self._client = None
                logger.error(
                    "Error MQTT (%s). Reintentando en %ss...",
                    exc,
                    self.settings.mqtt_reconnect_delay,
                )
                await asyncio.sleep(self.settings.mqtt_reconnect_delay)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        if self._client is None:
            from app.core.errors import MqttNotConnectedError

            raise MqttNotConnectedError()
        await self._client.publish(topic, payload, qos=qos)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


mqtt_gateway = MqttGateway(get_settings())
