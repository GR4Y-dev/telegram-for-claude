"""Explicit approvals and transactional daily budgets in the existing SQLite store."""
from datetime import datetime, timedelta, timezone
import math
import uuid

from assistant_store import _owner


class GuestStore:
    def __init__(self, store, *, messages=30, voice_seconds=600, call_seconds=900, max_guests=10):
        self.store, self.db = store, store.db
        self.limits = {"messages":messages, "voice":voice_seconds, "calls":call_seconds}
        # Aggregate caps stop adding guests from multiplying spend without bound.
        self.total_limits = {"messages":100, "voice":1800, "calls":3600}
        self.max_guests = max_guests
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS guests (
                owner INTEGER NOT NULL, guest INTEGER NOT NULL, label TEXT NOT NULL,
                active INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
                session TEXT, introduced INTEGER NOT NULL DEFAULT 0, limit_day TEXT,
                PRIMARY KEY(owner,guest));
            CREATE TABLE IF NOT EXISTS guest_usage (
                owner INTEGER NOT NULL, guest INTEGER NOT NULL, day TEXT NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0, voice INTEGER NOT NULL DEFAULT 0,
                calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,guest,day));
            CREATE TABLE IF NOT EXISTS guest_call_reservations (
                token TEXT PRIMARY KEY, owner INTEGER NOT NULL, guest INTEGER NOT NULL,
                day TEXT NOT NULL, seconds INTEGER NOT NULL);
        """)
        self.db.execute("DELETE FROM guest_usage WHERE day < date('now','-32 day')")
        self.db.execute("DELETE FROM guest_call_reservations WHERE day < date('now','-2 day')")

    @staticmethod
    def day():
        return datetime.now(timezone.utc).date().isoformat()

    def get(self, owner, guest):
        row = self.db.execute("SELECT * FROM guests WHERE owner=? AND guest=? AND active=1",
                              (_owner(owner), _owner(guest))).fetchone()
        return dict(row) if row else None

    def current(self, owner, guest, generation):
        row = self.get(owner, guest)
        return row is not None and row["generation"] == generation

    def list(self, owner):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM guests WHERE owner=? AND active=1 ORDER BY guest", (_owner(owner),))]

    def allow(self, owner, guest, label):
        owner, guest = _owner(owner), _owner(guest)
        if owner == guest:
            raise ValueError("The owner already has access.")
        label = str(label).strip()[:120] or str(guest)
        with self.store._write():
            if self.get(owner, guest):
                return False
            if len(self.list(owner)) >= self.max_guests:
                raise ValueError("Guest list is full. Remove someone before adding another person.")
            if self.db.execute("SELECT COUNT(*) FROM guests WHERE owner=?", (owner,)).fetchone()[0] >= 1000:
                raise ValueError("Guest history is full; ask the administrator to review it.")
            self.db.execute("""INSERT INTO guests(owner,guest,label,active) VALUES (?,?,?,1)
                ON CONFLICT(owner,guest) DO UPDATE SET label=excluded.label, active=1,
                generation=guests.generation+1, session=NULL, introduced=0""", (owner,guest,label))
            return True

    def deny(self, owner, guest):
        with self.store._write():
            return self.db.execute("""UPDATE guests SET active=0, generation=generation+1,
                session=NULL WHERE owner=? AND guest=? AND active=1""",
                (_owner(owner), _owner(guest))).rowcount > 0

    def reset(self, owner, guest):
        with self.store._write():
            self.db.execute("UPDATE guests SET generation=generation+1,session=NULL WHERE owner=? AND guest=? AND active=1",
                            (_owner(owner), _owner(guest)))

    def save_session(self, owner, guest, generation, session):
        import re
        if session is not None and (not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", session)):
            raise ValueError("Invalid session ID")
        self.db.execute("UPDATE guests SET session=? WHERE owner=? AND guest=? AND generation=? AND active=1",
                        (session, owner, guest, generation))

    def introduced(self, owner, guest, generation):
        self.db.execute("UPDATE guests SET introduced=1 WHERE owner=? AND guest=? AND generation=? AND active=1",
                        (owner, guest, generation))

    def notice(self, owner, guest):
        """At most one limit notification per guest/day, including across re-grants."""
        with self.store._write():
            return self.db.execute("""UPDATE guests SET limit_day=? WHERE owner=? AND guest=?
                AND active=1 AND (limit_day IS NULL OR limit_day!=?)""",
                (self.day(), owner, guest, self.day())).rowcount > 0

    def _remaining(self, owner, guest, kind):
        # kind is selected by this module, never interpolated from user input.
        today = self.day()
        own = self.db.execute(f"SELECT {kind} FROM guest_usage WHERE owner=? AND guest=? AND day=?",
                              (owner, guest, today)).fetchone()
        total = self.db.execute(f"SELECT COALESCE(SUM({kind}),0) FROM guest_usage WHERE owner=? AND day=?",
                                (owner, today)).fetchone()[0]
        return max(0, min(self.limits[kind] - (own[0] if own else 0), self.total_limits[kind] - total))

    def _add(self, owner, guest, kind, amount):
        self.db.execute(f"""INSERT INTO guest_usage(owner,guest,day,{kind}) VALUES (?,?,?,?)
            ON CONFLICT(owner,guest,day) DO UPDATE SET {kind}={kind}+excluded.{kind}""",
            (owner, guest, self.day(), amount))

    def charge(self, owner, guest, generation, *, messages=0, voice=0):
        amounts = {"messages":messages, "voice":voice}
        if any(type(n) is not int or n < 0 for n in amounts.values()):
            raise ValueError("Invalid usage charge")
        with self.store._write():
            if not self.current(owner, guest, generation):
                return False
            if any(value > self._remaining(owner, guest, kind) for kind,value in amounts.items()):
                return False
            for kind,value in amounts.items():
                if value:
                    self._add(owner, guest, kind, value)
            return True

    def reserve_call(self, owner, guest, generation):
        """Reserve the full remaining budget before accepting; a crash keeps the charge."""
        with self.store._write():
            if not self.current(owner, guest, generation) or self._remaining(owner, guest, "messages") < 1:
                return None
            seconds = self._remaining(owner, guest, "calls")
            now = datetime.now(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            seconds = min(seconds, max(0, int((midnight - now).total_seconds())))
            if seconds < 1:
                return None
            token = uuid.uuid4().hex
            self._add(owner, guest, "calls", seconds)
            self.db.execute("INSERT INTO guest_call_reservations VALUES (?,?,?,?,?)",
                            (token, owner, guest, self.day(), seconds))
            return token, seconds

    def finish_call(self, token, elapsed):
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Invalid elapsed time")
        with self.store._write():
            row = self.db.execute("SELECT * FROM guest_call_reservations WHERE token=?", (token,)).fetchone()
            if row:
                refund = max(0, row["seconds"] - math.ceil(elapsed))
                self.db.execute("UPDATE guest_usage SET calls=MAX(0,calls-?) WHERE owner=? AND guest=? AND day=?",
                                (refund, row["owner"], row["guest"], row["day"]))
                self.db.execute("DELETE FROM guest_call_reservations WHERE token=?", (token,))
