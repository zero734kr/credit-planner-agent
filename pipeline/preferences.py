"""
User preference CRUD — general-purpose key-value store persisted in SQLite.
"""

import sqlite3
from datetime import datetime
from typing import Dict


def set_preference(
    db_path: str, user_id: str, key: str, value: str,
    description: str = "",
):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO user_preferences (user_id, pref_key, pref_value, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, pref_key) DO UPDATE SET
            pref_value = excluded.pref_value,
            description = excluded.description,
            updated_at = excluded.updated_at
    """, (user_id, key, value, description, now, now))
    conn.commit()
    conn.close()


def get_preferences(db_path: str, user_id: str) -> Dict[str, Dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='user_preferences'
    """)
    if not cur.fetchone():
        conn.close()
        return {}

    cur.execute("""
        SELECT pref_key, pref_value, description, updated_at
        FROM user_preferences WHERE user_id = ?
    """, (user_id,))

    prefs = {}
    for key, value, desc, updated in cur.fetchall():
        prefs[key] = {"value": value, "description": desc, "updated_at": updated}
    conn.close()
    return prefs
