import datetime as dt
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import band_bot as bot

class StudylogTests(unittest.TestCase):
    def test_weekly_posts(self):
        self.assertEqual(bot.weekly_posts(dt.date(2026, 11, 1)), [
            "26/11/02 월", "26/11/03 화", "26/11/04 수",
            "26/11/05 목", "26/11/06 금", "26/11/07 토",
        ])

    def test_only_sunday_is_valid(self):
        with self.assertRaises(ValueError):
            bot.weekly_posts(dt.date(2026, 11, 2))

    def test_sunday_for_any_weekday(self):
        for i in range(7):
            day = dt.date(2026, 11, 1) + dt.timedelta(days=i)
            self.assertEqual(bot.sunday_for(day), dt.date(2026, 11, 1))

    def test_schedule(self):
        tz = ZoneInfo("Asia/Seoul")
        self.assertTrue(bot.scheduled_now(dt.datetime(2026, 11, 1, 9, 0, tzinfo=tz)))
        self.assertFalse(bot.scheduled_now(dt.datetime(2026, 11, 1, 8, 59, tzinfo=tz)))
        self.assertFalse(bot.scheduled_now(dt.datetime(2026, 11, 2, 9, 0, tzinfo=tz)))

    def test_create_six_and_mark_posted(self):
        writer = Mock()
        ledger = Mock()
        ledger.load.return_value = {}
        created, skipped = bot.publish(dt.date(2026,11,1), writer, ledger)
        self.assertEqual((created, skipped), (6, 0))
        self.assertEqual(writer.create_post.call_count, 6)
        self.assertEqual(ledger.save.call_count, 12)

    def test_respects_attempted_items(self):
        writer = Mock()
        ledger = Mock()
        ledger.load.return_value = {"26/11/02 월": "attempted"}
        created, skipped = bot.publish(dt.date(2026,11,1), writer, ledger)
        self.assertEqual((created, skipped), (5, 1))

    def test_no_comment_member_read_interfaces(self):
        self.assertFalse(hasattr(bot.BandWriter, "comments"))
        self.assertFalse(hasattr(bot.BandWriter, "create_comment"))
        self.assertFalse(hasattr(bot.BandWriter, "members"))

if __name__ == "__main__":
    unittest.main()
