"""数据库连接与会话。生产环境用 PostgreSQL，本地骨架默认 SQLite 以便直接跑通。"""

from sqlalchemy import JSON, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.db_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# 非空新列加到已有数据的旧表时，SQLite 要求带 DEFAULT；按类型给安全默认值
_SERVER_DEFAULTS = {JSON: "'[]'"}


def _ensure_new_columns() -> None:
    """轻量列迁移：create_all 不会给已有表补新列，这里按模型定义补齐缺的列。

    仅 SQLite 生效（本地骨架）；PostgreSQL 上线后由 Alembic 接管迁移。
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            # case 等保留字表名必须加引号
            existing = {
                row[1]
                for row in conn.execute(
                    text(f'PRAGMA table_info("{table.name}")')
                )
            }
            if not existing:
                continue  # 新表由 create_all 负责
            for column in table.columns:
                if column.name in existing:
                    continue
                col_ddl = column.type.compile(engine.dialect)
                if not column.nullable:
                    default = _SERVER_DEFAULTS.get(type(column.type), "''")
                    col_ddl = f"{col_ddl} DEFAULT {default}"
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN '
                        f'"{column.name}" {col_ddl}'
                    )
                )


def init_db() -> None:
    """建表并补齐旧表缺列。M1 之后由 Alembic 接管迁移。"""
    from . import models  # noqa: F401  导入以确保模型注册到 metadata

    Base.metadata.create_all(engine)
    _ensure_new_columns()


def get_session() -> Session:
    return SessionLocal()
