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


class DeviceInactiveError(SgpmpError):
    def __init__(self, serial: str) -> None:
        super().__init__(f"Dispositivo inactivo: {serial}")


class DeviceDependsOnGatewayError(SgpmpError):
    def __init__(self, serial: str, serial_gateway: str) -> None:
        super().__init__(
            f"{serial} se comunica a través de su Gateway Edge {serial_gateway}: "
            "la credencial MQTT es la del Edge"
        )


class DynsecError(SgpmpError):
    """Mosquitto rechazó o no respondió un comando de dynamic-security."""


class UsuarioReservadoError(SgpmpError):
    def __init__(self, serial: str) -> None:
        super().__init__(f"El serial {serial} coincide con un usuario MQTT reservado")
