from logging.config import fileConfig
from alembic import context
from app.db import Base, engine
config=context.config
if config.config_file_name:fileConfig(config.config_file_name)
target_metadata=Base.metadata
if context.is_offline_mode():
    from app.config import get_settings
    context.configure(url=get_settings().database_url,target_metadata=target_metadata,literal_binds=True)
    with context.begin_transaction():context.run_migrations()
else:
    with engine.connect() as connection:
        context.configure(connection=connection,target_metadata=target_metadata)
        with context.begin_transaction():context.run_migrations()
