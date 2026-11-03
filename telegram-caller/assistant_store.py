"""Durable, owner-scoped memory and reminders; no model or network needed.

Reminder timestamps are UTC epochs. Repeating reminders keep their original local
clock time even after snoozing. A missed repeat produces one notification, then
advances beyond the delivery time. During a DST gap the scheduled time moves
forward by the gap; during a repeated hour only its first occurrence is used.
"""

import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_ITEMS = 100
MAX_TEXT = 2000
REPEATS = {"once", "daily", "weekdays", "weekly"}


def _owner(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("An owner ID must be a positive integer.")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TEXT:
        raise ValueError("Please use between 1 and 2,000 characters.")
    return value.strip()


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Please supply a date and time with a timezone.")
    try:
        stamp = value.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("That date is outside the supported range.") from exc
    if not math.isfinite(stamp):
        raise ValueError("Please supply a finite date and time.")
    return stamp


def _zone(value):
    try:
        return ZoneInfo(value)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Please use a valid timezone, such as Europe/Istanbul.") from exc


def _next_repeat(reminder, now):
    """First original wall-clock occurrence strictly after now, without a backlog."""
    zone = _zone(reminder["timezone"])
    stamp = _timestamp(now)
    day = datetime.fromtimestamp(stamp, zone).date()
    for offset in range(15):
        candidate_day = day + timedelta(days=offset)
        if reminder["repeat"] == "weekdays" and candidate_day.weekday() >= 5:
            continue
        if reminder["repeat"] == "weekly" and candidate_day.weekday() != reminder["weekday"]:
            continue
        candidate = datetime.combine(
            candidate_day, time(reminder["hour"], reminder["minute"]), tzinfo=zone)
        # For nonexistent wall times, fold=0 maps forward through the DST gap.
        # Compare epochs rather than local datetimes so repeated hours are unambiguous.
        if candidate.timestamp() > stamp:
            return candidate.timestamp()
    raise ValueError("Couldn't find the next reminder date.")


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS memories_owner ON memories(owner);
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner INTEGER NOT NULL,
                text TEXT NOT NULL,
                next_due REAL,
                timezone TEXT NOT NULL,
                repeat TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                weekday INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reminders_due
                ON reminders(owner, status, next_due);
        """)

    @contextmanager
    def _write(self):
        # Lock before checking limits so two connections cannot exceed a quota.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def remember(self, owner: int, text: str) -> int:
        owner, text = _owner(owner), _text(text)
        with self._write():
            existing = self.db.execute(
                "SELECT id FROM memories WHERE owner=? AND text=?", (owner, text)).fetchone()
            if existing:
                return existing["id"]
            count = self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE owner=?", (owner,)).fetchone()[0]
            if count >= MAX_ITEMS:
                raise ValueError("You have 100 saved memories. Forget one before adding another.")
            return self.db.execute(
                "INSERT INTO memories(owner, text) VALUES (?, ?)", (owner, text)).lastrowid

    def memories(self, owner: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id, text FROM memories WHERE owner=? ORDER BY id", (_owner(owner),))]

    def forget(self, owner: int, id: int) -> bool:
        with self._write():
            return self.db.execute(
                "DELETE FROM memories WHERE owner=? AND id=?", (_owner(owner), id)).rowcount > 0

    def add_reminder(self, owner: int, text: str, due: datetime, timezone: str,
                     repeat: str = "once") -> int:
        owner, text, stamp, zone = _owner(owner), _text(text), _timestamp(due), _zone(timezone)
        if not isinstance(repeat, str) or repeat not in REPEATS:
            raise ValueError("Repeat must be once, daily, weekdays, or weekly.")
        local = due.astimezone(zone)
        if repeat == "weekdays" and local.weekday() >= 5:
            raise ValueError("A weekday reminder must first start on a weekday.")
        with self._write():
            count = self.db.execute(
                "SELECT COUNT(*) FROM reminders WHERE owner=? AND status='active'", (owner,)).fetchone()[0]
            if count >= MAX_ITEMS:
                raise ValueError("You have 100 active reminders. Complete one before adding another.")
            return self.db.execute("""INSERT INTO reminders
                (owner, text, next_due, timezone, repeat, hour, minute, weekday)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner, text, stamp, zone.key, repeat, local.hour, local.minute, local.weekday())).lastrowid

    def reminders(self, owner: int) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM reminders
            WHERE owner=? AND status='active'
            ORDER BY next_due IS NULL, next_due, id""", (_owner(owner),))]

    def due_reminders(self, owner: int, now: datetime) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM reminders
            WHERE owner=? AND status='active' AND next_due<=?
            ORDER BY next_due, id""", (_owner(owner), _timestamp(now)))]

    def delivered(self, owner: int, id: int, now: datetime) -> bool:
        owner, stamp = _owner(owner), _timestamp(now)
        with self._write():
            row = self.db.execute("""SELECT * FROM reminders
                WHERE owner=? AND id=? AND status='active' AND next_due<=?""",
                (owner, id, stamp)).fetchone()
            if not row:
                return False
            due = None if row["repeat"] == "once" else _next_repeat(row, now)
            self.db.execute("UPDATE reminders SET next_due=? WHERE owner=? AND id=?", (due, owner, id))
            return True

    def snooze(self, owner: int, id: int, minutes: int, now: datetime) -> bool:
        owner, stamp = _owner(owner), _timestamp(now)
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 10080:
            raise ValueError("Snooze must be between 1 and 10,080 minutes (one week).")
        with self._write():
            return self.db.execute("""UPDATE reminders SET next_due=?
                WHERE owner=? AND id=? AND status='active'""",
                (stamp + minutes * 60, owner, id)).rowcount > 0

    def complete(self, owner: int, id: int) -> bool:
        with self._write():
            # Completed tasks need no stored text; avoid accumulating a private task archive.
            return self.db.execute("DELETE FROM reminders WHERE owner=? AND id=?",
                                   (_owner(owner), id)).rowcount > 0

    def close(self):
        self.db.close()
