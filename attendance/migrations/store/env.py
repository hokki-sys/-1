"""매장 스키마 마이그레이션.

같은 리비전 트리를 매장 수만큼 반복 실행합니다.

    alembic -c alembic_store.ini -x schema=store_gangnam upgrade head

버전 테이블도 그 매장 스키마 안에 두므로 매장마다 버전이 독립적으로 관리됩니다.
한 매장만 실패해 버전이 어긋나는 상황은 tools/migrate.py 가 보고합니다.

search_path 는 **접속 시점에** 지정합니다. 나중에 SET 으로 바꾸면 SQLAlchemy 가
접속할 때 캐시해 둔 기본 스키마와 어긋나서, 버전 테이블을 엉뚱한 곳에서 찾습니다.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import _assert_safe
from app.models.store import StoreBase

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = StoreBase.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """alembic 자기 버전 테이블은 마이그레이션 대상이 아닙니다.

    이걸 빼지 않으면 autogenerate 가 "메타데이터에 없는 테이블"로 보고
    upgrade() 안에 drop_table('alembic_version') 을 넣어버립니다.
    그러면 마이그레이션이 스스로 버전 기록을 지우고 실패합니다.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_online() -> None:
    schema = _assert_safe(context.get_x_argument(as_dictionary=True).get("schema", ""))
    url = get_settings().database_url

    bootstrap = create_engine(url, future=True, poolclass=None)
    with bootstrap.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.commit()
    bootstrap.dispose()

    engine = create_engine(
        url, future=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=False,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
