#!/usr/bin/env python3
"""Smoke-test translations: 20 main-menu strings of game/gui -> db.strings.uk_default (status='draft').

Every row is addressed by (table, string_id) AND checked against the expected English text, so a changed
game build can never silently put a translation on the wrong string.
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "db" / "avowed_l10n.db"

# (string_id, expected en_default, uk_default)
SMOKE = [
    (4, "Continue", "Продовжити"),
    (7, "Back", "Назад"),
    (8, "Options", "Параметри"),
    (9, "Quit", "Вийти"),
    (13, "Save", "Зберегти"),
    (14, "Load", "Завантажити"),
    (15, "Save Game", "Зберегти гру"),
    (25, "Close", "Закрити"),
    (82, "Start New Game", "Почати нову гру"),
    (85, "Settings", "Налаштування"),
    (86, "Quit", "Вийти"),
    (87, "Continue", "Продовжити"),
    (258, "Continue", "Продовжити"),
    (373, "Resume Game", "Продовжити гру"),
    (375, "Quit To Main Menu", "Вийти до головного меню"),
    (552, "Accessibility", "Доступність"),
    (606, "Language", "Мова"),
    (860, "Credits", "Титри"),
    (905, "Overwrite Save Game?", "Перезаписати збережену гру?"),
    (988, "Load Game", "Завантажити гру"),
]


def main():
    con = sqlite3.connect(DB)
    with con:
        for sid, en, uk in SMOKE:
            cur = con.execute(
                "UPDATE strings SET uk_default=?, status='draft' "
                "WHERE source_file='BaseGame' AND table_name='game/gui' AND string_id=? AND en_default=?",
                (uk, sid, en))
            if cur.rowcount != 1:
                sys.exit(f"string_id {sid}: expected English {en!r} not found")
    n = con.execute("SELECT COUNT(*) FROM strings WHERE uk_default!=''").fetchone()[0]
    print(f"{len(SMOKE)} smoke translations applied; rows with uk_default in DB: {n}")


if __name__ == "__main__":
    main()
