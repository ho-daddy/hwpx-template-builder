"""SQLite 기반 템플릿 저장소.

이름표(label)까지 붙은 템플릿과 원본 hwpx 원본 바이트를 함께 저장해두면,
이후 generate 단계는 이 레코드 하나만으로(원본 파일을 다시 업로드할 필요 없이)
{라벨: 새값} 딕셔너리만 받아 새 hwpx를 생성할 수 있다.
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
    conn.commit()
    conn.close()


def save_template(name: str, source_filename: str, template: dict, original_hwpx: bytes) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO templates (name, source_filename, created_at, template_json, original_hwpx) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            name,
            source_filename,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(template, ensure_ascii=False),
            original_hwpx,
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
        "SELECT id, name, source_filename, created_at, template_json, original_hwpx "
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
