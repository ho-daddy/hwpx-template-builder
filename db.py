"""SQLite 기반 템플릿 저장소.

템플릿은 "원본과 동일한 형식(xml 포함 패키지)"으로 저장된다. build_template_package()
가 원본 hwpx의 각 채울 자리를 라벨 placeholder로 치환한 템플릿화된 패키지(template_hwpx)
를 만들어주므로, 여기서는 이를 원본(template_json 메타 + original_hwpx 원본 바이트)과
함께 보관한다. 이후 generate 단계는 template_hwpx 하나만으로 {라벨: 새값}만 받아
placeholder를 치환해 새 hwpx를 만들 수 있다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "templates.db"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_filename TEXT,
            created_at TEXT NOT NULL,
            template_json TEXT NOT NULL,
            original_hwpx BLOB NOT NULL
        )
    """)
    # 마이그레이션: template_hwpx(템플릿화된 패키지) 컬럼 추가
    cols = {r[1] for r in conn.execute("PRAGMA table_info(templates)").fetchall()}
    if "template_hwpx" not in cols:
        conn.execute("ALTER TABLE templates ADD COLUMN template_hwpx BLOB")
    conn.commit()
    conn.close()


def save_template(name: str, source_filename: str, template: dict,
                  original_hwpx: bytes, template_hwpx: bytes) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO templates (name, source_filename, created_at, template_json, original_hwpx, template_hwpx) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            name,
            source_filename,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(template, ensure_ascii=False),
            original_hwpx,
            template_hwpx,
        ),
    )
    conn.commit()
    template_id = cur.lastrowid
    conn.close()
    return template_id


def list_templates() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, source_filename, created_at FROM templates ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_template(template_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, source_filename, created_at, template_json, original_hwpx, template_hwpx "
        "FROM templates WHERE id = ?",
        (template_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["template"] = json.loads(d.pop("template_json"))
    return d


def delete_template(template_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

