import datetime as dt
import unittest
from zoneinfo import ZoneInfo
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import band_bot as bot

class StudylogTests(unittest.TestCase):
    def test_weekly_posts(self):
        dates = bot.weekly_dates(dt.date(2026, 11, 1))
        self.assertEqual([bot.post_text(x) for x in dates], [
            "26/11/02 월", "26/11/03 화", "26/11/04 수",
            "26/11/05 목", "26/11/06 금", "26/11/07 토",
        ])

    def test_week_start_sunday(self):
        self.assertEqual(bot.sunday_for(dt.date(2026,11,5)), dt.date(2026,11,1))

    def test_weekday_schedule(self):
        tz=ZoneInfo("Asia/Seoul")
        self.assertTrue(bot.validate_schedule("publish", dt.datetime(2026,11,1,9,0,tzinfo=tz)))
        self.assertFalse(bot.validate_schedule("publish", dt.datetime(2026,11,1,8,59,tzinfo=tz)))
        self.assertTrue(bot.validate_schedule("collect", dt.datetime(2026,11,5,7,20,tzinfo=tz)))
        self.assertFalse(bot.validate_schedule("collect", dt.datetime(2026,11,5,7,19,tzinfo=tz)))
        self.assertFalse(bot.validate_schedule("collect", dt.datetime(2026,11,1,7,20,tzinfo=tz)))

    def test_aggregate_before_cutoff_only(self):
        tz=ZoneInfo("Asia/Seoul")
        def c(n,text,h,m):
            return {"content":text,"author":{"name":n,"user_key":n},
                    "created_at":int(dt.datetime(2026,11,5,h,m,tzinfo=tz).timestamp()*1000)}
        comments=[c("김철수","반휴",7,10),c("홍길동","월휴",7,15),c("이영희","시험휴",7,21),c("김철수","반휴",7,10)]
        self.assertEqual(bot.aggregate(comments,dt.date(2026,11,5)),
                         "반휴: 김철수\\n월휴: 홍길동\\n시험휴: -".replace("\\n","\n"))

    def test_summary_detection(self):
        self.assertTrue(bot.is_summary("반휴: 김철수\n월휴: 홍길동\n시험휴: -"))
        self.assertFalse(bot.is_summary("반휴"))

    def test_duplicate_post_error(self):
        tz=ZoneInfo("Asia/Seoul")
        item={"content":"26/11/05 목","post_key":"a",
              "created_at":int(dt.datetime(2026,11,1,9,0,tzinfo=tz).timestamp()*1000)}
        with self.assertRaises(bot.StudylogError):
            bot.matching_post([item,{**item,"post_key":"b"}],dt.date(2026,11,5))

if __name__ == "__main__":
    unittest.main()
