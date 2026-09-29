"""替班链：保留原授权、新责任人、时长与奖励跟随实际出勤者。"""

import unittest

import helpers  # noqa: F401
from helpers import CENTER, COMMITTEE, add_volunteer, dt, make_posts, make_shift, make_targets, new_service
from src.volunteer_operations.domain import ConflictError, IneligibleAssignment


class SubstitutionTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)
        self.v1 = add_volunteer(self.service, "原责任人")
        self.v2 = add_volunteer(self.service, "新责任人")
        self.shift = make_shift(
            self.service, self.posts["hotel"], self.targets["gala"],
            "2026-09-29T18:00", "2026-09-29T22:00",
        )
        self.assignment = self.service.assign_shift(COMMITTEE, self.shift, self.v1)

    def tearDown(self):
        self.service.close()

    def test_substitution_keeps_chain(self):
        sub_id = self.service.request_substitution(
            COMMITTEE, self.assignment, self.v2, "课程冲突", at=dt("2026-09-29T12:00")
        )
        new_id = self.service.approve_substitution(CENTER, sub_id, at=dt("2026-09-29T13:00"))

        old = self.service.conn.execute("SELECT * FROM assignments WHERE id=?", (self.assignment,)).fetchone()
        new = self.service.conn.execute("SELECT * FROM assignments WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(old["state"], "substituted")
        self.assertEqual(old["volunteer_id"], self.v1)       # 原授权保留
        self.assertEqual(new["state"], "active")
        self.assertEqual(new["volunteer_id"], self.v2)      # 新责任人
        self.assertEqual(new["supersedes_id"], self.assignment)
        sub = self.service.conn.execute("SELECT * FROM substitutions WHERE id=?", (sub_id,)).fetchone()
        self.assertEqual(sub["from_volunteer_id"], self.v1)
        self.assertEqual(sub["to_volunteer_id"], self.v2)
        self.assertEqual(sub["state"], "approved")

    def test_cannot_substitute_with_ineligible_volunteer(self):
        shift = make_shift(
            self.service, self.posts["airport"], self.targets["airport"],
            "2026-09-30T05:30", "2026-09-30T13:30",
        )
        assignment = self.service.assign_shift(
            COMMITTEE, shift,
            add_volunteer(self.service, "机场主力", languages=[("法语", "advanced", True)], credentials=[("护照", True)]),
        )
        with self.assertRaises(IneligibleAssignment):
            self.service.request_substitution(COMMITTEE, assignment, self.v2, "临时有事")

    def test_cannot_substitute_after_shift_ended(self):
        sub_id = self.service.request_substitution(COMMITTEE, self.assignment, self.v2, "生病")
        # 班次已结束再批准会触发重新校验（当前时间晚于班次结束）
        with self.assertRaises(ConflictError):
            self.service.approve_substitution(CENTER, sub_id, at=dt("2026-09-29T23:30"))

    def test_rejected_substitution_leaves_assignment_active(self):
        sub_id = self.service.request_substitution(
            COMMITTEE, self.assignment, self.v2, "想换班", at=dt("2026-09-29T12:00")
        )
        self.service.reject_substitution(CENTER, sub_id, note="新责任人资格存疑", at=dt("2026-09-29T13:00"))
        with self.assertRaises(ConflictError):
            self.service.approve_substitution(CENTER, sub_id)
        old = self.service.conn.execute("SELECT * FROM assignments WHERE id=?", (self.assignment,)).fetchone()
        self.assertEqual(old["state"], "active")

    def test_hours_follow_actual_worker(self):
        sub_id = self.service.request_substitution(
            COMMITTEE, self.assignment, self.v2, "课程冲突", at=dt("2026-09-29T12:00")
        )
        self.service.approve_substitution(CENTER, sub_id, at=dt("2026-09-29T13:00"))
        new_assignment = self.service.conn.execute(
            "SELECT id FROM assignments WHERE shift_id=? AND state='active'", (self.shift,)
        ).fetchone()["id"]
        # 新责任人完成签到签退，原责任人无出勤
        self.service.check_in(CENTER, new_assignment, dt("2026-09-29T18:05"))
        self.service.check_out(CENTER, new_assignment, dt("2026-09-29T22:00"))
        report_old = self.service.volunteer_report(CENTER, self.v1)
        report_new = self.service.volunteer_report(CENTER, self.v2)
        self.assertEqual(report_old["total_hours"], 0)
        self.assertEqual(report_new["total_hours"], 3.92)
        # 双方报告都能看见这条替班链
        self.assertTrue(report_old["substitutions"])
        self.assertTrue(report_new["substitutions"])


if __name__ == "__main__":
    unittest.main()
