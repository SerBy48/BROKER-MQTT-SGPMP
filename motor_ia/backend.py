# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: el motor entrega sus resultados al backend (M2)           |
# | 2026-10-31 | SerBy48  | RF-66: 403 y 404 de ruta o proxy se reintentan, no se consumen   |
# | 2026-11-02 | Arekkazu | RF-66/RF-69: cliente del catálogo del motor (rutas /prediccion/motor) |
# | 2026-11-02 | Arekkazu | AJF-14 RF-71: rutas de reentrenamiento y registro de versión (RF-69) |
"""Cliente motor → backend: ``POST {BACKEND_URL}/prediccion/inferencias``.

``BACKEND_URL`` es la URL base del backend en cualquier dominio, con su prefijo si lo tiene
(``https://api.inmero.co/api-sgpmp-test``, ``https://api.arekkazu.top``,
``https://1-2-3-4.sslip.io``). Solo ``https://``: el token viaja en la cabecera.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


class BackendNoDisponibleError(RuntimeError):
    """El backend no confirmó: el motor responde 503 y el backend reenvía el lote."""


def normalizar_url_base(url: str) -> str:
    """Valida y quita la barra final. Cualquier dominio, puerto o prefijo; solo https."""
    url = url.strip().rstrip("/")
    partes = urlsplit(url)
    if partes.scheme != "https" or not partes.hostname:
        raise ValueError(f"BACKEND_URL debe ser una URL https:// con dominio, no {url!r}.")
    if partes.query or partes.fragment:
        raise ValueError(
            "BACKEND_URL no lleva query ni fragmento: solo esquema, dominio y prefijo."
        )
    return url


def _es_rechazo_de_datos(r: httpx.Response) -> bool:
    """4xx que decidió la lógica del backend: reenviar el resultado no cambia nada.

    401, 403, 408 y 429 se resuelven solos o con configuración. Este endpoint no responde 403, y un
    404 sin ``error_code`` tampoco es suyo: vienen de un proxy o de un ``BACKEND_URL`` con prefijo
    errado, y consumirlos dejaría los paquetes en ENVIADO sin haber entregado nada.
    """
    if r.status_code == 404:
        try:
            cuerpo = r.json()
        except ValueError:
            return False
        return isinstance(cuerpo, dict) and bool(cuerpo.get("error_code"))
    return 400 <= r.status_code < 500 and r.status_code not in (401, 403, 408, 429)


class BackendResultadosClient:
    def __init__(
        self, url_base: str, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._url = f"{normalizar_url_base(url_base)}/prediccion/inferencias"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    @property
    def url(self) -> str:
        return self._url

    def registrar(self, resultado: dict) -> bool:
        """True si el backend lo guardó (201) o ya lo tenía (200). False si lo rechazó por datos
        (ver ``_es_rechazo_de_datos``). Cualquier otro caso → BackendNoDisponibleError."""
        try:
            with httpx.Client(
                timeout=_TIMEOUT_S, transport=self._transport, follow_redirects=False
            ) as c:
                r = c.post(self._url, json=resultado, headers=self._headers)
        except httpx.HTTPError as exc:
            raise BackendNoDisponibleError(
                f"sin respuesta del backend ({type(exc).__name__})"
            ) from exc
        if r.is_success:
            return True
        if _es_rechazo_de_datos(r):
            logger.warning(
                "RF-66: el backend rechazó el resultado %s del activo %s: HTTP %s %s",
                resultado["id_resultado_inferencia"],
                resultado["id_activo_biologico"],
                r.status_code,
                r.text[:300],
            )
            return False
        # Token desalineado, proxy, ruta errada, 408, 429 y 5xx: que el backend reintente.
        raise BackendNoDisponibleError(f"el backend respondió HTTP {r.status_code}")


class BackendCatalogoClient:
    """Motor → backend para armar el catálogo solo: ``{BACKEND_URL}/prediccion/motor/*``.

    Cualquier fallo sale como excepción de httpx: quien sincroniza conserva lo que ya tenía.
    """

    def __init__(
        self, url_base: str, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._base = f"{normalizar_url_base(url_base)}/prediccion/motor"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    def _pedir(self, metodo: str, ruta: str, **kw) -> httpx.Response:
        with httpx.Client(
            timeout=_TIMEOUT_S * 3, transport=self._transport, follow_redirects=False
        ) as c:
            r = c.request(metodo, f"{self._base}{ruta}", headers=self._headers, **kw)
        r.raise_for_status()
        return r

    def catalogo(self) -> list[dict]:
        """Animales a atender: ``{id_activo_biologico, tipo_modelo, version | None}``."""
        return self._pedir("GET", "/catalogo").json()["animales"]

    def asignar_version(self, id_activo: int) -> dict:
        """Versión propia en SOMBRA para un animal que no tiene (RFC-010); idempotente."""
        return self._pedir("POST", f"/activos/{id_activo}/version").json()

    def artefacto(self, id_version: int) -> bytes:
        return self._pedir("GET", f"/versiones/{id_version}/artefacto").content

    def lecturas(self, id_activo: int, horas: float) -> list[dict]:
        return self._pedir(
            "GET", f"/activos/{id_activo}/lecturas", params={"horas": horas}
        ).json()["lecturas"]

    # ── AJF-14: reentrenamiento ───────────────────────────────────────────────
    def tomar_reentrenamiento(self) -> dict | None:
        """El proceso PENDIENTE más antiguo, ya EN_CURSO para este motor; None si no hay (204)."""
        r = self._pedir("POST", "/reentrenamientos/tomar")
        return None if r.status_code == 204 else r.json()

    def reportar_reentrenamiento(self, id_proceso: str, **campos) -> dict:
        """Latido (``fase``, ``progreso``, ``cpu``, ``ram_mb``) o cierre (``estado`` COMPLETADO/FALLIDO)."""
        cuerpo = {k: v for k, v in campos.items() if v is not None}
        return self._pedir("PATCH", f"/reentrenamientos/{id_proceso}", json=cuerpo).json()

    def retroalimentaciones(self, id_activo: int) -> list[dict]:
        """Diagnósticos clínicos vigentes del animal con la ventana de su inferencia (RF-72)."""
        return self._pedir("GET", f"/activos/{id_activo}/retroalimentaciones").json()["retroalimentaciones"]


class BackendRegistroModelosClient:
    """Motor → backend: ``POST {BACKEND_URL}/prediccion/modelos`` (RF-69, ``X-RF71-Internal-Key``).

    Lo usa el reentrenador (AJF-14) para entregar la versión que produjo; el backend la evalúa y
    decide su estado. ``SINTETICA`` y ``MIXTA`` solo pueden quedar en sombra (regla del backend).
    """

    def __init__(self, url_base: str, clave_rf71: str, transport: httpx.BaseTransport | None = None) -> None:
        if not clave_rf71:
            raise ValueError("Falta la clave interna de RF-71 (MOTOR_IA_RF71_KEY).")
        self._url = f"{normalizar_url_base(url_base)}/prediccion/modelos"
        self._headers = {"X-RF71-Internal-Key": clave_rf71}
        self._transport = transport

    def registrar(self, formulario: dict[str, str], onnx: bytes, nombre_archivo: str) -> dict:
        with httpx.Client(timeout=_TIMEOUT_S * 6, transport=self._transport, follow_redirects=False) as c:
            r = c.post(self._url, data=formulario, headers=self._headers,
                       files={"archivo_modelo": (nombre_archivo, onnx, "application/octet-stream")})
        r.raise_for_status()
        return r.json()

