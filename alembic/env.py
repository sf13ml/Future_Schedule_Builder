import os
import sys
from os.path import abspath, dirname
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

# 1. Load environment variables from .env
load_dotenv()

# 2. Add root project directory to sys.path
sys.path.insert(0, dirname(dirname(abspath(__file__))))

# 3. Import Base & models
from database import Base
import models

config = context.config

if config.config_file_name:
    fileConfig(config.config_file_name)

# 4. Set target metadata
target_metadata = Base.metadata

# 5. Build Neon PostgreSQL connection string dynamically
user = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")
host = os.getenv("DB_HOST")
port = os.getenv("DB_PORT", "5432")
db_name = os.getenv("DB_NAME")

database_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}?sslmode=require"
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()