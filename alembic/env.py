"""Runs migrations against DATABASE_URL, comparing the database with app.models."""

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import get_settings
from app.models import Base

target_metadata = Base.metadata


def database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set in backend/.env")
    # Use the psycopg 3 driver the API already depends on.
    return url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "postgres://", "postgresql+psycopg://", 1
    )


def include_name(name, type_, parent_names) -> bool:
    # Only our tables in public; Supabase owns auth, storage and the rest.
    if type_ == "schema":
        return name in (None, "public")
    return True


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        # auth.users is declared only for foreign keys; Supabase owns it.
        if obj.schema == "auth":
            return False
        # Never drop a table that has no model (e.g. items_legacy); remove those by hand.
        if reflected and compare_to is None:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
