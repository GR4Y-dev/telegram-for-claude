"""Deterministic request parsing, with no model or external calls."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
from assistant_commands import handle_command


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.remember.return_value = 12
        self.store.memories.return_value = []
        self.store.add_reminder.return_value = 4
        self.store.reminders.return_value = []
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)  # Monday, 13:00 Istanbul

    def handle(self, text):
        return handle_command(self.store, 7, text, self.tz, self.now)

    def added(self):
        args = self.store.add_reminder.call_args
        self.assertEqual(args.args[0], 7)
        return args.args[1], args.args[2], args.kwargs["repeat"]

    def test_remember_explicit_fact_and_list(self):
        response = self.handle("Remember that my exam is December 10")
        self.store.remember.assert_called_once_with(7, "my exam is December 10")
        self.assertIn("#12", response)
        self.store.memories.return_value = [{"id": 12, "text": "my exam is December 10"}]
        self.assertIn("12. my exam", self.handle("What do you remember about me?"))

    def test_not_all_remember_mentions_are_commands(self):
        for text in ["How do I remember things?", "Remember when we talked?", "Remember how to do this", "Hello", "I remember the date", "remember this?"]:
            self.assertIsNone(self.handle(text), text)
        self.store.remember.assert_not_called()

    def test_empty_and_long_memories_do_not_write(self):
        for text in ["/remember", "Remember that", "/remember " + "a" * 2001]:
            self.handle(text)
        # 'Remember that' is a fragment, not a meaningful fact.
        self.assertEqual(self.store.remember.call_count, 0)

    def test_forget_scoped_id(self):
        self.store.forget.return_value = True
        response = self.handle("forget memory #12")
        self.store.forget.assert_called_once_with(7, 12)
        self.assertIn("doesn't delete old Telegram", response)

    def test_forget_requires_id_and_existing_memory(self):
        self.assertIn("memory ID", self.handle("/forget my birthday"))
        self.store.forget.assert_not_called()
        self.store.forget.return_value = False
        self.assertIn("couldn't find", self.handle("/forget 99"))

    def test_relative_delay(self):
        response = self.handle("Remind me in 20 minutes to take a break")
        task, due, repeat = self.added()
        self.assertEqual(task, "take a break")
        self.assertEqual(due.isoformat(), "2026-11-02T13:20:00+03:00")
        self.assertEqual(repeat, "once")
        self.assertIn("Europe/Istanbul", response)

    def test_tomorrow_clock_and_midnight(self):
        for clock, expected in [("8am", 8), ("8:30pm", 20), ("12am", 0), ("12pm", 12), ("20:00", 20)]:
            self.handle(f"remind me tomorrow at {clock} to study")
            _, due, _ = self.added()
            self.assertEqual((due.day, due.hour), (3, expected))

    def test_absolute_date_pipe_preserves_task(self):
        self.handle("/remind 2026-12-10 09:30 | Bring my passport | blue bag")
        task, due, _ = self.added()
        self.assertEqual(task, "Bring my passport | blue bag")
        self.assertEqual(due.isoformat(), "2026-12-10T09:30:00+03:00")

    def test_daily_next_occurrence(self):
        self.handle("Remind me every day at 8 to study")
        _, due, repeat = self.added()
        self.assertEqual((due.day, due.hour, repeat), (3, 8, "daily"))

    def test_weekdays_skip_weekend(self):
        self.now = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)  # Friday after 8
        self.handle("/remind weekdays 08:00 | Study")
        _, due, repeat = self.added()
        self.assertEqual((due.day, due.weekday(), repeat), (9, 0, "weekdays"))

    def test_explicit_weekday_and_weekly(self):
        self.handle("Remind me every Wednesday at 8am to exercise")
        _, due, repeat = self.added()
        self.assertEqual((due.day, due.weekday(), repeat), (4, 2, "weekly"))
        self.handle("/remind weekly 08:00 | Review tasks")
        _, due, repeat = self.added()
        self.assertEqual((due.day, due.weekday(), repeat), (9, 0, "weekly"))

    def test_invalid_or_unspecified_dates_never_write(self):
        for text in ["remind me tomorrow to study", "remind me in 0 minutes to study",
                     "remind me today at 8am to study", "remind me tomorrow at 25:00 to study",
                     "remind me tomorrow at 0am to study", "/remind 2026-02-30 10:00 | exam",
                     "/remind 2026-12-10 10:00 |", "/remind yesterday | exam",
                     "remind me in 999999 days to study", "remind me tomorrow", "/remind"]:
            self.assertIsInstance(self.handle(text), str, text)
        self.store.add_reminder.assert_not_called()

    def test_long_reminder_never_writes(self):
        self.handle("remind me in 20 minutes to " + "x" * 2001)
        self.store.add_reminder.assert_not_called()

    def test_dst_nonexistent_and_ambiguous_rejected(self):
        self.tz = ZoneInfo("America/New_York")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIn("skipped", self.handle("/remind 2026-03-08 02:30 | test"))
        self.assertIn("occurs twice", self.handle("/remind 2026-11-01 01:30 | test"))
        self.store.add_reminder.assert_not_called()

    def test_relative_delay_crosses_dst_by_elapsed_time(self):
        self.tz = ZoneInfo("America/New_York")
        self.now = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
        self.handle("remind me in 1 hour to study")
        _, due, _ = self.added()
        self.assertEqual(due.isoformat(), "2026-03-08T03:30:00-04:00")

    def test_snooze(self):
        self.store.snooze.return_value = True
        response = self.handle("snooze #4 for 20 minutes")
        self.store.snooze.assert_called_once_with(7, 4, 20, self.now)
        self.assertIn("13:20", response)

    def test_snooze_invalid_asks_instead_of_guessing(self):
        for text in ["snooze", "/snooze 4", "snooze 4 for twenty minutes", "/snooze 4 0", "/snooze 4 10081"]:
            self.assertIsInstance(self.handle(text), str)
        self.store.snooze.assert_not_called()

    def test_done_and_missing_id(self):
        self.store.complete.return_value = True
        self.assertIn("won't send any more", self.handle("done #4"))
        self.store.complete.assert_called_once_with(7, 4)
        self.assertIn("Which reminder", self.handle("done"))

    def test_list_notifications_and_recurrence(self):
        self.store.reminders.return_value = [
            {"id": 4, "text": "Study", "next_due": self.now.timestamp(), "timezone": self.tz.key, "repeat": "weekdays"},
            {"id": 5, "text": "Break", "next_due": None, "timezone": self.tz.key, "repeat": "once"},
        ]
        response = self.handle("what are my tasks?")
        self.assertIn("#4: Study", response)
        self.assertIn("repeats weekdays", response)
        self.assertIn("awaiting Done", response)
        self.assertIn("stops repeats", response)

    def test_store_validation_returns_helpful_error(self):
        self.store.remember.side_effect = ValueError("Memory limit reached")
        self.assertEqual(self.handle("/remember a fact"), "Memory limit reached")


if __name__ == "__main__":
    unittest.main()
