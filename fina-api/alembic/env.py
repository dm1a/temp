import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from fina.config import DatabaseSettings

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silently disables
    # every logger that already existed at this point (e.g. fina's own
    # module loggers, if this runs via command.upgrade() in-process rather
    # than as its own migration process/container -- as the integration
    # test suite's database_url fixture does) for the rest of that
    # process. A one-shot migration process exiting right after doesn't
    # notice; anything sharing a process with it would silently lose its
    # own logging.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if "connection" not in config.attributes:
    settings = DatabaseSettings()
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
