"""TC-M09-252 (anti-replay): solo el ACK que devuelve el `id_comando` emitido
resuelve la espera de un comando.

Antes se correlacionaba solo por `serial`: un ACK capturado y reenviado, o uno
forjado por cualquier cliente con la credencial compartida de dispositivos (que
puede escribir en `status` de cualquier serial), resolvía como APLICADA el
comando que estuviera en vuelo.
"""

from __future__ import annotations

import pytest

from app.mqtt import correlacion


@pytest.fixture(autouse=True)
def _limpiar():
    correlacion._pending_acks.clear()
    yield
    correlacion._pending_acks.clear()


async def test_ack_con_el_id_emitido_resuelve_la_espera() -> None:
    future = correlacion.crear_espera("S1", "id-1")

    assert correlacion.resolver_ack("S1", "id-1") is True
    assert future.done()


async def test_ack_con_otro_id_se_ignora_y_no_consume_la_espera() -> None:
    """El replay de un ACK viejo no debe resolver el comando actual, ni gastar su espera."""
    future = correlacion.crear_espera("S1", "id-actual")

    assert correlacion.resolver_ack("S1", "id-viejo-capturado") is False
    assert not future.done()

    # el ACK legítimo, que llega después, todavía resuelve
    assert correlacion.resolver_ack("S1", "id-actual") is True
    assert future.done()


async def test_ack_sin_id_se_acepta_por_compatibilidad_por_defecto() -> None:
    future = correlacion.crear_espera("S1", "id-1")

    assert correlacion.resolver_ack("S1", None) is True
    assert future.done()


async def test_ack_sin_id_se_rechaza_si_se_exige_el_id() -> None:
    future = correlacion.crear_espera("S1", "id-1")

    assert correlacion.resolver_ack("S1", None, exigir_id=True) is False
    assert not future.done()


async def test_exigir_id_no_afecta_a_un_ack_con_el_id_correcto() -> None:
    future = correlacion.crear_espera("S1", "id-1")

    assert correlacion.resolver_ack("S1", "id-1", exigir_id=True) is True
    assert future.done()


async def test_ack_sin_espera_activa_devuelve_false() -> None:
    assert correlacion.resolver_ack("S1", "id-1") is False


async def test_ack_no_resuelve_dos_veces() -> None:
    correlacion.crear_espera("S1", "id-1")

    assert correlacion.resolver_ack("S1", "id-1") is True
    assert correlacion.resolver_ack("S1", "id-1") is False


async def test_las_esperas_de_seriales_distintos_no_se_mezclan() -> None:
    f1 = correlacion.crear_espera("S1", "id-1")
    f2 = correlacion.crear_espera("S2", "id-2")

    # el id de S2 enviado en el topic de S1 (o al revés) no resuelve nada
    assert correlacion.resolver_ack("S1", "id-2") is False
    assert not f1.done() and not f2.done()

    assert correlacion.resolver_ack("S2", "id-2") is True
    assert f2.done() and not f1.done()


async def test_limpiar_espera_quita_la_espera() -> None:
    correlacion.crear_espera("S1", "id-1")

    correlacion.limpiar_espera("S1")

    assert correlacion.resolver_ack("S1", "id-1") is False
