"""重启恢复：未完成的提醒、漏签扫描、表彰计算在 recover 后继续推进。"""

import os
import tempfile
import unittest

import helpers  # noqa: F401
from helpers import CENTER, COMMITTEE, add_volunteer, dt, make_posts, make_shift, make_targets


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def test_reminder_sent_on_recover(self):
        service = new_service_file(self.path)
        posts = make_posts(service)
        targets = make_targets(service)
        vid = add_volunteer(service, "待提醒")
        shift = make_shift(service, posts["hotel"], targets["gala"], "2026-09-29T18:00", "2026-09-29T22:00")
        aid = service.assign_shift(COMMITTEE, shift, vid)
        # 开班前2小时之前：不应发送
        self.assertEqual(service.recover(dt("2026-09-29T15:00"))["ran"], 0)
        self.assertEqual(service.conn.execute("SELECT COUNT(*) AS c FROM reminders").fetchone()["c"], 0)
        # 到期后：发送提醒
        result = service.recover(dt("2026-09-29T16:00"))
        self.assertEqual(result["ran"], 1)
        reminder = service.conn.execute("SELECT * FROM reminders WHERE assignment_id=?", (aid,)).fetchone()
        self.assertEqual(reminder["volunteer_id"], vid)
        service.close()

    def test_restart_resumes_pending_jobs_once(self):
        service = new_service_file(self.path)
        posts = make_posts(service)
        targets = make_targets(service)
        vid = add_volunteer(service, "跨重启")
        shift = make_shift(service, posts["hotel"], targets["hotel"], "2026-09-29T08:00", "2026-09-29T12:00")
        service.assign_shift(COMMITTEE, shift, vid)
        service.close()

        # 模拟系统重启：新实例打开同一文件，继续推进漏签扫描
        service = new_service_file(self.path)
        result = service.recover(dt("2026-09-29T12:31"))
        self.assertGreaterEqual(result["ran"], 1)
        cases = service.open_review_cases(CENTER)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["kind"], "missed")
        # 再次 recover 不重复生成
        again = service.recover(dt("2026-09-29T13:00"))
        self.assertEqual(again["ran"], 0)
        self.assertEqual(len(service.open_review_cases(CENTER)), 1)
        service.close()

    def test_award_compute_resumes_after_restart(self):
        service = new_service_file(self.path)
        posts = make_posts(service)
        targets = make_targets(service)
        vid = add_volunteer(service, "重启表彰")
        for day in (27, 28):
            shift = make_shift(service, posts["hotel"], targets["hotel"], f"2026-09-{day}T08:00", f"2026-09-{day}T20:00")
            aid = service.assign_shift(COMMITTEE, shift, vid)
            service.check_in(CENTER, aid, dt(f"2026-09-{day}T08:00"))
            service.check_out(CENTER, aid, dt(f"2026-09-{day}T20:00"))
        pending = service.conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE kind='award_compute' AND state='pending'").fetchone()["c"]
        self.assertGreaterEqual(pending, 1)
        service.close()

        service = new_service_file(self.path)
        service.recover(dt("2026-09-28T20:05"))
        awards = service.conn.execute("SELECT kind FROM awards WHERE volunteer_id=?", (vid,)).fetchall()
        self.assertIn("star_of_service", [r["kind"] for r in awards])
        service.close()


def new_service_file(path: str):
    from src.volunteer_operations.service import VolunteerService
    return VolunteerService(path)


if __name__ == "__main__":
    unittest.main()
