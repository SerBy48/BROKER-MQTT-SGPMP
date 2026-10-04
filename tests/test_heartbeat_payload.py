"""estado_local_buffer va a una columna "char" (un byte): se normaliza a I/A/L."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import HeartbeatPayload


@pytest.mark.parametrize(
    ("enviado", "guardado"),
    [("INACTIVO", "I"), ("activo", "A"), (" LLENO ", "L"), ("I", "I"), ("a", "A"), (None, None)],
)
def test_estado_buffer_se_normaliza_a_una_letra(enviado, guardado):
    assert HeartbeatPayload(estado_local_buffer=enviado).estado_local_buffer == guardado


def test_estado_buffer_desconocido_se_rechaza():
    with pytest.raises(ValidationError):
        HeartbeatPayload(estado_local_buffer="VACIO")
