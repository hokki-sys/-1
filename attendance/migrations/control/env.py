from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from app.config import CONTROL_SCHEMA, get_settings
from app.models.control import ControlBase

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = ControlBase.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """alembic 자기 버전 테이블은 마이그레이션 대상이 아닙니다.

    이걸 빼지 않으면 autogenerate 가 "메타데이터에 없는 테이블"로 보고
    upgrade() 안에 drop_table('alembic_version') 을 넣어버립니다.
    그러면 마이그레이션이 스스로 버전 기록을 지우고 실패합니다.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url, future=True)
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{CONTROL_SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=CONTROL_SCHEMA,
            include_schemas=True,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
