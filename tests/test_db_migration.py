"""init_db 轻量列迁移：旧库缺列时自动补齐（如 draft.attachments）。"""

import pytest
from sqlalchemy import create_engine, text

from autoemail import db as db_mod


def test_init_db_adds_missing_columns(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    # 手工还原一个"旧版"draft 表：没有 attachments 列，且已有历史数据
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE draft ("
                "id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, body TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO draft (id, case_id, body) VALUES (1, 1, '旧草稿')")
        )

    monkeypatch.setattr(db_mod, "engine", eng)
    db_mod.init_db()

    with eng.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(draft)"))}
        rows = conn.execute(
            text("SELECT id, attachments FROM draft WHERE id = 1")
        ).all()

    assert "attachments" in cols
    # 历史数据保留，新列落到 JSON 默认空列表
    assert rows == [(1, "[]")]


def test_init_db_fresh_db_still_works(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr(db_mod, "engine", eng)
    db_mod.init_db()
    with eng.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(draft)"))}
    assert "attachments" in cols
