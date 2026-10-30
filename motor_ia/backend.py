# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: el motor entrega sus resultados al backend (M2)           |
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
        (4xx): reenviarlo no cambia nada. Error de red o 5xx → BackendNoDisponibleError."""
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
        if 400 <= r.status_code < 500 and r.status_code not in (401, 408, 429):
            logger.warning(
                "RF-66: el backend rechazó el resultado %s del activo %s: HTTP %s %s",
                resultado["id_resultado_inferencia"],
                resultado["id_activo_biologico"],
                r.status_code,
                r.text[:300],
            )
            return False
        # 401 (token desalineado), 408, 429 y 5xx se resuelven solos: que el backend reintente.
        raise BackendNoDisponibleError(f"el backend respondió HTTP {r.status_code}")
