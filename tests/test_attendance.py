"""签到考勤：迟到、漏签、补录一律复核；封存后不可改写。"""

import unittest

import helpers  # noqa: F401
from helpers import CENTER, COMMITTEE, add_volunteer, dt, make_posts, make_shift, make_targets, new_service
from src.volunteer_operations.domain import ConflictError, SealedRecordError, ValidationError


class AttendanceTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)
        self.vid = add_volunteer(self.service, "志愿者甲")
        self.shift = make_shift(
            self.service, self.posts["hotel"], self.targets["hotel"],
            "2026-09-29T08:00", "2026-09-29T16:00",
        )
        self.aid = self.service.assign_shift(COMMITTEE, self.shift, self.vid)

    def tearDown(self):
        self.service.close()

    def test_normal_checkin_out_is_effective(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:05"))
        att = self.service.conn.execute("SELECT * FROM attendance WHERE assignment_id=?", (self.aid,)).fetchone()
        self.assertEqual(att["state"], "effective")
        self.assertEqual(att["origin"], "self")
        self.service.check_out(CENTER, self.aid, dt("2026-09-29T16:00"))

    def test_late_checkin_opens_review_not_effective(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:20"))  # 超15分钟宽限
        att = self.service.conn.execute("SELECT * FROM attendance WHERE assignment_id=?", (self.aid,)).fetchone()
        self.assertEqual(att["state"], "pending")
        cases = self.service.open_review_cases(CENTER)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["kind"], "late")
        # 批准前累计时长为0
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 0)
        self.service.review_case_decision(CENTER, cases[0]["id"], approve=True, note="摆渡车延误")
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 0)  # 还未签退
        self.service.check_out(CENTER, self.aid, dt("2026-09-29T16:00"))

    def test_late_checkin_rejected(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T09:00"))
        case = self.service.open_review_cases(CENTER)[0]
        self.service.review_case_decision(CENTER, case["id"], approve=False)
        with self.assertRaises(ConflictError):
            self.service.check_out(CENTER, self.aid, dt("2026-09-29T16:00"))

    def test_duplicate_checkin_blocked(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:00"))
        with self.assertRaises(ConflictError):
            self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:10"))

    def test_too_early_checkin_blocked(self):
        with self.assertRaises(ValidationError):
            self.service.check_in(CENTER, self.aid, dt("2026-09-29T06:30"))  # 提前90分钟

    def test_backfill_does_not_rewrite_until_approved(self):
        case_id = self.service.backfill_attendance(
            CENTER, self.aid,
            dt("2026-09-29T08:00"), dt("2026-09-29T16:00"),
            "设备故障未签到", at=dt("2026-09-29T17:00"),
        )
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 0)
        self.service.review_case_decision(CENTER, case_id, approve=True, at=dt("2026-09-29T18:00"))
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 8.0)
        att = self.service.conn.execute("SELECT * FROM attendance WHERE assignment_id=?", (self.aid,)).fetchone()
        self.assertEqual(att["origin"], "backfill")

    def test_backfill_rejected_leaves_no_hours(self):
        case_id = self.service.backfill_attendance(
            CENTER, self.aid,
            dt("2026-09-29T08:00"), dt("2026-09-29T16:00"),
            "理由存疑", at=dt("2026-09-29T17:00"),
        )
        self.service.review_case_decision(CENTER, case_id, approve=False)
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 0)

    def test_sealed_attendance_cannot_be_modified(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:00"))
        self.service.check_out(CENTER, self.aid, dt("2026-09-29T16:00"))
        sealed = self.service.seal_shift_attendance(CENTER, self.shift)
        self.assertEqual(sealed, 1)
        with self.assertRaises(SealedRecordError):
            self.service.check_out(CENTER, self.aid, dt("2026-09-29T16:30"))
        with self.assertRaises(SealedRecordError):
            self.service.backfill_attendance(
                CENTER, self.aid,
                dt("2026-09-29T08:00"), dt("2026-09-29T16:30"),
                "试图改长时长",
            )

    def test_seal_blocked_with_open_review(self):
        self.service.check_in(CENTER, self.aid, dt("2026-09-29T08:30"))  # 迟到挂起复核
        with self.assertRaises(ConflictError):
            self.service.seal_shift_attendance(CENTER, self.shift)

    def test_committee_cannot_seal(self):
        from src.volunteer_operations.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.seal_shift_attendance(COMMITTEE, self.shift)

    def test_missed_scan_creates_review_after_shift(self):
        # 班次结束后无人签到；由恢复任务扫描
        result = self.service.recover(dt("2026-09-29T16:31"))
        self.assertGreaterEqual(result["ran"], 1)
        cases = self.service.open_review_cases(CENTER)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["kind"], "missed")
        att = self.service.conn.execute("SELECT * FROM attendance WHERE assignment_id=?", (self.aid,)).fetchone()
        self.assertEqual(att["state"], "pending")
        self.assertEqual(att["origin"], "missed")
        # 漏签复核批准 → 记缺勤；志愿者可申请补录救济
        self.service.review_case_decision(CENTER, cases[0]["id"], approve=True)
        self.assertEqual(
            self.service.conn.execute("SELECT state FROM attendance WHERE id=?", (att["id"],)).fetchone()["state"],
            "absent",
        )

    def test_missed_then_backfill_relief(self):
        self.service.recover(dt("2026-09-29T16:31"))
        missed_case = self.service.open_review_cases(CENTER)[0]
        self.service.review_case_decision(CENTER, missed_case["id"], approve=True)  # 先记缺勤
        # 志愿者实际在岗，事后补录仍可走复核救济
        case_id = self.service.backfill_attendance(
            CENTER, self.aid,
            dt("2026-09-29T08:00"), dt("2026-09-29T16:00"),
            "忘记刷卡，有带队老师证明", at=dt("2026-09-30T09:00"),
        )
        self.service.review_case_decision(CENTER, case_id, approve=True)
        self.assertEqual(self.service.volunteer_report(CENTER, self.vid)["total_hours"], 8.0)


if __name__ == "__main__":
    unittest.main()
