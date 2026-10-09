from alembic import context

from app import models  # noqa: F401  (rejestruje tabele w Base.metadata)
from app.db import Base, engine

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection):
    # render_as_batch: SQLite nie obsługuje większości ALTER TABLE – Alembic przebudowuje tabelę
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    given = context.config.attributes.get("connection")   # np. z testów – inna baza niż DATABASE_URL
    if given is not None:
        _run(given)
        return
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
