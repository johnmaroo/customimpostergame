"""SQLite persistence for the Imposter word bank and cached category clues."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "wordbank.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL,
    word_normalized TEXT NOT NULL UNIQUE,
    category_clue TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);
"""


def normalize_word(word: str) -> str:
    return " ".join(word.strip().split()).casefold()


class WordBank:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "WordBank":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def add_word(self, word: str) -> bool:
        """Insert a word. Returns True if it was new, False if it was already stored."""
        cleaned = " ".join(word.strip().split())
        if not cleaned:
            raise ValueError("word is required")
        try:
            self.conn.execute(
                "INSERT INTO words (word, word_normalized) VALUES (?, ?)",
                (cleaned, normalize_word(cleaned)),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def all_words(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT word FROM words ORDER BY word COLLATE NOCASE"
        ).fetchall()
        return [row["word"] for row in rows]

    def get_clue(self, word: str) -> str | None:
        row = self.conn.execute(
            "SELECT category_clue FROM words WHERE word_normalized = ?",
            (normalize_word(word),),
        ).fetchone()
        if row is None:
            return None
        return row["category_clue"]

    def set_clue(self, word: str, clue: str) -> None:
        cleaned = clue.strip()
        if not cleaned:
            raise ValueError("clue is required")
        cursor = self.conn.execute(
            "UPDATE words SET category_clue = ? WHERE word_normalized = ?",
            (cleaned, normalize_word(word)),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"word not in bank: {word}")

    def mark_used(self, word: str) -> None:
        self.conn.execute(
            "UPDATE words SET last_used_at = datetime('now') WHERE word_normalized = ?",
            (normalize_word(word),),
        )
        self.conn.commit()
