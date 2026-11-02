from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.types import TypeDecorator

import app.models  # noqa: F401  - registers all tables on Base.metadata
from app.db.base import Base
from app.db.session import create_db_engine

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests (and app.db.migrations) pass the URL explicitly; otherwise use app settings.
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.core.config import DatabaseSettings

    return DatabaseSettings().resolved_database_url


def _render_item(type_: str, obj: object, autogen_context) -> str | bool:  # type: ignore[no-untyped-def]
    # Render app TypeDecorators as their storage type so migrations never import app code.
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl_instance!r}"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=_render_item,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Batch mode rebuilds a table (copy, DROP, rename). With foreign keys on, the DROP
            # of a parent table would fire ON DELETE CASCADE / SET NULL on its children, so
            # they are off while migrating and every reference is verified afterwards.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
            render_item=_render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            if broken:
                raise RuntimeError(f"foreign key violations after migration: {broken[:10]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
