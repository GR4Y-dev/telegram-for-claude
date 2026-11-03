"""Persistence and recurrence checks; no Telegram, model or network access."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
from assistant_store import Store


UTC = timezone.utc


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "assistant.sqlite3"
        self.store = Store(self.path)
        self.now = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reminder(self, **kwargs):
        return self.store.add_reminder(7, "Study", kwargs.pop("due", self.now),
                                       kwargs.pop("timezone", "UTC"), **kwargs)

    def test_reopen_preserves_memory_and_reminder(self):
        memory = self.store.remember(7, "  Exam is December 10  ")
        reminder = self.reminder()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.memories(7), [{"id": memory, "text": "Exam is December 10"}])
        self.assertEqual(self.store.due_reminders(7, self.now)[0]["id"], reminder)
        self.assertTrue(self.store.forget(7, memory))
        self.assertFalse(self.store.forget(7, memory))

    def test_owner_isolation_for_reads_and_mutations(self):
        memory = self.store.remember(7, "Private fact")
        reminder = self.reminder()
        self.assertEqual(self.store.memories(8), [])
        self.assertEqual(self.store.reminders(8), [])
        self.assertEqual(self.store.due_reminders(8, self.now), [])
        self.assertFalse(self.store.forget(8, memory))
        self.assertFalse(self.store.delivered(8, reminder, self.now))
        self.assertFalse(self.store.snooze(8, reminder, 20, self.now))
        self.assertFalse(self.store.complete(8, reminder))
        self.assertEqual(len(self.store.memories(7)), 1)
        self.assertEqual(len(self.store.due_reminders(7, self.now)), 1)

    def test_delivery_keeps_failed_attempt_due_and_one_off_available_for_snooze(self):
        reminder = self.reminder()
        self.assertEqual(self.store.due_reminders(7, self.now - timedelta(seconds=1)), [])
        self.assertFalse(self.store.delivered(7, reminder, self.now - timedelta(seconds=1)))
        self.assertEqual(self.store.due_reminders(7, self.now), self.store.due_reminders(7, self.now))
        self.assertTrue(self.store.delivered(7, reminder, self.now))
        self.assertEqual(self.store.due_reminders(7, self.now + timedelta(days=1)), [])
        self.assertIsNone(self.store.reminders(7)[0]["next_due"])
        self.assertTrue(self.store.snooze(7, reminder, 20, self.now))
        self.assertEqual(self.store.due_reminders(7, self.now + timedelta(minutes=19)), [])
        self.assertEqual(len(self.store.due_reminders(7, self.now + timedelta(minutes=20))), 1)
        self.assertTrue(self.store.complete(7, reminder))
        self.assertFalse(self.store.complete(7, reminder))
        self.assertFalse(self.store.snooze(7, reminder, 20, self.now))
        self.assertEqual(self.store.reminders(7), [])

    def test_missed_daily_reminders_do_not_create_backlog(self):
        reminder = self.reminder(repeat="daily")
        late = self.now + timedelta(days=12, hours=4)
        self.assertEqual(len(self.store.due_reminders(7, late)), 1)
        self.store.delivered(7, reminder, late)
        self.assertEqual(self.store.reminders(7)[0]["next_due"],
                         (self.now + timedelta(days=13)).timestamp())
        self.assertEqual(self.store.due_reminders(7, late), [])

    def test_weekdays_skip_weekend(self):
        friday = datetime(2026, 11, 6, 8, tzinfo=UTC)
        reminder = self.reminder(due=friday, repeat="weekdays")
        self.store.delivered(7, reminder, friday)
        self.assertEqual(self.store.reminders(7)[0]["next_due"],
                         datetime(2026, 11, 9, 8, tzinfo=UTC).timestamp())

    def test_weekly_snooze_restores_original_weekday_and_time(self):
        reminder = self.reminder(repeat="weekly")
        self.store.delivered(7, reminder, self.now)
        self.store.snooze(7, reminder, 60, self.now + timedelta(days=1))
        self.store.delivered(7, reminder, self.now + timedelta(days=1, hours=1))
        self.assertEqual(self.store.reminders(7)[0]["next_due"],
                         (self.now + timedelta(days=7)).timestamp())

    def test_daily_keeps_wall_clock_across_dst(self):
        zone = ZoneInfo("America/New_York")
        saturday = datetime(2026, 3, 7, 8, tzinfo=zone)
        reminder = self.reminder(due=saturday, timezone=zone.key, repeat="daily")
        self.store.delivered(7, reminder, saturday)
        following = self.store.reminders(7)[0]["next_due"]
        self.assertEqual(datetime.fromtimestamp(following, zone), datetime(2026, 3, 8, 8, tzinfo=zone))
        self.assertEqual(following - saturday.timestamp(), 23 * 60 * 60)

    def test_nonexistent_dst_time_moves_forward_and_returns_to_original_time(self):
        zone = ZoneInfo("America/New_York")
        saturday = datetime(2026, 3, 7, 2, 30, tzinfo=zone)
        reminder = self.reminder(due=saturday, timezone=zone.key, repeat="daily")
        self.store.delivered(7, reminder, saturday)
        sunday = datetime.fromtimestamp(self.store.reminders(7)[0]["next_due"], zone)
        self.assertEqual(sunday, datetime(2026, 3, 8, 3, 30, tzinfo=zone))
        self.store.delivered(7, reminder, sunday)
        self.assertEqual(datetime.fromtimestamp(self.store.reminders(7)[0]["next_due"], zone),
                         datetime(2026, 3, 9, 2, 30, tzinfo=zone))

    def test_ambiguous_hour_is_not_sent_twice(self):
        zone = ZoneInfo("America/New_York")
        saturday = datetime(2026, 10, 31, 1, 30, tzinfo=zone)
        reminder = self.reminder(due=saturday, timezone=zone.key, repeat="daily")
        self.store.delivered(7, reminder, saturday)
        first_sunday = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        self.assertEqual(self.store.reminders(7)[0]["next_due"], first_sunday.timestamp())
        self.store.delivered(7, reminder, first_sunday)
        self.assertEqual(self.store.due_reminders(7, first_sunday.replace(fold=1)), [])

    def test_duplicate_memory_and_limits_release_capacity(self):
        for i in range(100):
            self.store.remember(7, f"Fact {i}")
        self.assertEqual(self.store.remember(7, "Fact 0"), self.store.memories(7)[0]["id"])
        with self.assertRaisesRegex(ValueError, "100 saved memories"):
            self.store.remember(7, "Too many")
        self.store.forget(7, self.store.memories(7)[0]["id"])
        self.store.remember(7, "Replacement")
        self.store.remember(8, "Different owner")
        ids = [self.reminder() for _ in range(100)]
        with self.assertRaisesRegex(ValueError, "100 active reminders"):
            self.reminder()
        self.store.complete(7, ids[0])
        self.reminder()

    def test_validation_does_not_write_invalid_rows(self):
        for text in ["", "  ", "x" * 2001, None]:
            with self.subTest(text=repr(text)[:30]), self.assertRaises(ValueError):
                self.store.remember(7, text)
            with self.assertRaises(ValueError):
                self.store.add_reminder(7, text, self.now, "UTC")
        for minutes in [0, -1, 10081, 1.5, True, "20"]:
            with self.assertRaises(ValueError):
                self.store.snooze(7, 1, minutes, self.now)
        for kwargs in [{"repeat": "monthly"}, {"timezone": "Invalid/Zone"},
                       {"due": datetime(2026, 1, 1)}, {"due": "tomorrow"},
                       {"repeat": "weekdays", "due": datetime(2026, 11, 7, tzinfo=UTC)}]:
            with self.assertRaises(ValueError):
                self.reminder(**kwargs)
        self.assertEqual(self.store.memories(7), [])
        self.assertEqual(self.store.reminders(7), [])

    def test_two_connections_see_committed_updates(self):
        other = Store(self.path)
        try:
            memory = self.store.remember(7, "Shared")
            self.assertEqual(other.memories(7)[0]["id"], memory)
            other.forget(7, memory)
            self.assertEqual(self.store.memories(7), [])
            reminder = self.reminder()
            other.complete(7, reminder)
            self.assertEqual(self.store.due_reminders(7, self.now), [])
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
