"""Excepciones de dominio de SGPMP."""

from __future__ import annotations


class SgpmpError(Exception):
    """Error base de la aplicación."""


class DeviceNotFoundError(SgpmpError):
    def __init__(self, serial: str) -> None:
        super().__init__(f"Dispositivo no encontrado: {serial}")


class VariableNotFoundError(SgpmpError):
    def __init__(self, variable: str) -> None:
        super().__init__(f"Variable ambiental no encontrada: {variable}")


class SensorNotFoundError(SgpmpError):
    def __init__(self, serial: str, variable: str) -> None:
        super().__init__(f"Sensor no resuelto para dispositivo {serial} y variable {variable}")


class IngestError(SgpmpError):
    """Error durante la ingesta de telemetría/heartbeat."""


class CommandError(SgpmpError):
    """Error al despachar un comando hacia un dispositivo."""


class MqttNotConnectedError(SgpmpError):
    def __init__(self) -> None:
        super().__init__("MQTT no está conectado")
