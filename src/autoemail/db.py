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


def _drop_draft_unique_constraint() -> None:
    """重建 draft 表，去掉 (source_message_id, intent) 唯一约束。

    该约束挡住「同一封邮件补发更正回复」的合法场景：已 sent 草稿占用
    (source_message_id, intent)，新更正草稿无法插入。SQLite 不支持直接 DROP
    表级 UNIQUE 约束，只能重建表。去重改由 drafts.generate_draft 的 existing
    检查（复用非 sent 草稿）负责。
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='draft'"
        ).fetchone()
        if row is None or "uq_draft_source_intent" not in (row[0] or ""):
            return  # 已去掉或表不存在
        conn.exec_driver_sql("""
            CREATE TABLE draft_new (
                id INTEGER NOT NULL PRIMARY KEY,
                case_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                source_message_id INTEGER,
                intent VARCHAR(40) NOT NULL,
                subject VARCHAR(600) NOT NULL,
                to_addrs JSON NOT NULL,
                cc_addrs JSON NOT NULL,
                body TEXT NOT NULL,
                template_id VARCHAR(80),
                controlled_fields JSON NOT NULL,
                status VARCHAR(16) NOT NULL,
                rule_hits JSON NOT NULL,
                notices JSON NOT NULL,
                model_id VARCHAR(80),
                input_hash VARCHAR(64),
                reviewer VARCHAR(120),
                edited_diff TEXT,
                version INTEGER NOT NULL,
                reviewed_at DATETIME,
                updated_at DATETIME NOT NULL,
                sent_at DATETIME,
                send_error TEXT,
                created_at DATETIME NOT NULL,
                attachments JSON DEFAULT '[]'
            )
        """)
        conn.exec_driver_sql("""
            INSERT INTO draft_new (
                id, case_id, thread_id, source_message_id, intent, subject,
                to_addrs, cc_addrs, body, template_id, controlled_fields, status,
                rule_hits, notices, model_id, input_hash, reviewer, edited_diff,
                version, reviewed_at, updated_at, sent_at, send_error, created_at,
                attachments
            )
            SELECT id, case_id, thread_id, source_message_id, intent, subject,
                to_addrs, cc_addrs, body, template_id, controlled_fields, status,
                rule_hits, notices, model_id, input_hash, reviewer, edited_diff,
                version, reviewed_at, updated_at, sent_at, send_error, created_at,
                attachments
            FROM draft
        """)
        conn.exec_driver_sql("DROP TABLE draft")
        conn.exec_driver_sql("ALTER TABLE draft_new RENAME TO draft")
        for idx_sql in (
            "CREATE INDEX ix_draft_case_id ON draft (case_id)",
            "CREATE INDEX ix_draft_thread_id ON draft (thread_id)",
            "CREATE INDEX ix_draft_source_message_id ON draft (source_message_id)",
            "CREATE INDEX ix_draft_status ON draft (status)",
        ):
            conn.exec_driver_sql(idx_sql)


def init_db() -> None:
    """建表并补齐旧表缺列。M1 之后由 Alembic 接管迁移。"""
    from . import models  # noqa: F401  导入以确保模型注册到 metadata

    Base.metadata.create_all(engine)
    _ensure_new_columns()
    _drop_draft_unique_constraint()


def get_session() -> Session:
    return SessionLocal()
