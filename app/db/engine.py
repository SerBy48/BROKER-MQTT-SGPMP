# | Fecha      | Nombre   | Que se hizo                                                        |
# |------------|----------|--------------------------------------------------------------------|
# | 2026-10-29 | Arekkazu | RLS M03 (#808): cada transacción declara el usuario de servicio     |
"""Motor y sesiones de base de datos (SQLAlchemy async + asyncpg).

SGPMP no ejecuta migraciones: la base ya está versionada con Alembic en el
proyecto principal. Aquí solo se conecta y consume los esquemas existentes.

Identidad para RLS: `modulo3` y `modulo9` tienen FORCE ROW LEVEL SECURITY y
ningún rol de la app usa BYPASSRLS (D1 del DBA, PR #485 del backend). Las
políticas leen `app.current_user_id` / `app.current_role`; sin ellos la
ingesta no ve dispositivos ni puede insertar. El broker siempre actúa como el
usuario de servicio, así que toda transacción lo declara al empezar con
`set_config(..., true)` (equivale a SET LOCAL: muere con la transacción y no
se filtra entre conexiones de un pooler en modo transacción).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from app.config import get_settings

_settings = get_settings()

# Mismo usuario y rol que `src/shared/database.py` del backend. Lo crea la
# migración 5c3e9b1d7a20; su id difiere entre bases, por eso se resuelve por
# correo (función SECURITY DEFINER, la única que puede hacerlo sin identidad).
CORREO_USUARIO_SERVICIO = "servicio.sistema@sgpmp.local"
_ROL_SERVICIO = "Administrador"
_id_usuario_servicio: int | None = None

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
)


class _SesionServicio(Session):
    """Sesión síncrona bajo cada AsyncSession del broker (ver `_declarar_servicio`)."""


def _id_servicio(conexion) -> int:
    global _id_usuario_servicio
    if _id_usuario_servicio is None:
        _id_usuario_servicio = conexion.execute(
            text("SELECT modulo1.fn_id_usuario_por_correo(:correo)"),
            {"correo": CORREO_USUARIO_SERVICIO},
        ).scalar()
        if _id_usuario_servicio is None:
            raise RuntimeError(
                f"No existe el usuario de servicio {CORREO_USUARIO_SERVICIO}: "
                "falta aplicar las migraciones del backend (alembic upgrade head)."
            )
    return _id_usuario_servicio


@event.listens_for(_SesionServicio, "after_begin")
def _declarar_servicio(_session, _transaccion, conexion) -> None:
    conexion.execute(
        text(
            "SELECT set_config('app.current_user_id', :uid, true), "
            "set_config('app.current_role', :rol, true)"
        ),
        {"uid": str(_id_servicio(conexion)), "rol": _ROL_SERVICIO},
    )


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    sync_session_class=_SesionServicio,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
