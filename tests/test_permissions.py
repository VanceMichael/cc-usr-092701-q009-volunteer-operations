"""权限边界：高校管理员只能看本校数据；组委会可跨队调度但不能改封存签到。"""

import unittest

import helpers  # noqa: F401
from helpers import (
    ADMIN_A,
    ADMIN_B,
    CENTER,
    COMMITTEE,
    SCHOOL_A,
    SCHOOL_B,
    add_volunteer,
    dt,
    make_posts,
    make_shift,
    make_targets,
    new_service,
)
from src.volunteer_operations.domain import PermissionDenied, SealedRecordError


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)
        self.v_a = add_volunteer(self.service, "本校同学", school=SCHOOL_A)
        self.v_b = add_volunteer(self.service, "外校同学", school=SCHOOL_B)

    def tearDown(self):
        self.service.close()

    def test_university_admin_scoped_list(self):
        names = {v["name"] for v in self.service.list_volunteers(ADMIN_A)}
        self.assertEqual(names, {"本校同学"})
        names_all = {v["name"] for v in self.service.list_volunteers(COMMITTEE)}
        self.assertEqual(names_all, {"本校同学", "外校同学"})

    def test_university_admin_cannot_read_other_school_report(self):
        with self.assertRaises(PermissionDenied):
            self.service.volunteer_report(ADMIN_A, self.v_b)
        report = self.service.volunteer_report(ADMIN_A, self.v_a)
        self.assertEqual(report["volunteer"]["school"], SCHOOL_A)
        # 另一所学校的管理员恰好相反
        with self.assertRaises(PermissionDenied):
            self.service.volunteer_report(ADMIN_B, self.v_a)
        self.service.volunteer_report(ADMIN_B, self.v_b)

    def test_university_admin_cannot_assign(self):
        shift = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T08:00", "2026-09-29T12:00")
        with self.assertRaises(PermissionDenied):
            self.service.assign_shift(ADMIN_A, shift, self.v_a)

    def test_committee_can_cross_school_dispatch(self):
        shift_a = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-28T08:00", "2026-09-28T12:00")
        shift_b = make_shift(self.service, self.posts["hotel"], self.targets["gala"], "2026-09-29T08:00", "2026-09-29T12:00")
        self.service.assign_shift(COMMITTEE, shift_a, self.v_a)
        self.service.assign_shift(COMMITTEE, shift_b, self.v_b)

    def test_committee_cannot_modify_sealed_attendance(self):
        shift = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T08:00", "2026-09-29T12:00")
        aid = self.service.assign_shift(COMMITTEE, shift, self.v_a)
        self.service.check_in(CENTER, aid, dt("2026-09-29T08:00"))
        self.service.check_out(CENTER, aid, dt("2026-09-29T12:00"))
        self.service.seal_shift_attendance(CENTER, shift)
        with self.assertRaises(SealedRecordError):
            self.service.backfill_attendance(
                COMMITTEE, aid,
                dt("2026-09-29T08:00"), dt("2026-09-29T13:00"),
                "组委会想改长时长",
            )
        with self.assertRaises(SealedRecordError):
            self.service.check_out(COMMITTEE, aid, dt("2026-09-29T13:00"))


class VolunteerReportTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)

    def tearDown(self):
        self.service.close()

    def test_report_shows_real_shifts_chain_hours_and_award_basis(self):
        v1 = add_volunteer(self.service, "记录可查者")
        shift = make_shift(
            self.service, self.posts["hotel"], self.targets["gala"],
            "2026-09-29T18:00", "2026-09-29T22:00",
        )
        aid = self.service.assign_shift(COMMITTEE, shift, v1)
        self.service.check_in(CENTER, aid, dt("2026-09-29T18:00"))
        self.service.check_out(CENTER, aid, dt("2026-09-29T22:00"))
        self.service.compute_awards(CENTER, v1)

        report = self.service.volunteer_report(COMMITTEE, v1)
        self.assertEqual(len(report["assignments"]), 1)
        self.assertEqual(report["assignments"][0]["post_name"], "媒体酒店服务")
        self.assertEqual(report["assignments"][0]["target_name"], "欢迎招待会")
        self.assertEqual(report["total_hours"], 4.0)
        self.assertEqual(report["attendance"][0]["state"], "effective")
        # 4小时不足以拿奖，但奖励依据结构对真实得奖者可追溯
        self.assertEqual(report["awards"], [])

    def test_report_includes_languages_credentials_qualifications(self):
        vid = add_volunteer(
            self.service, "资质齐全",
            languages=[("西班牙语", "native", True)],
            credentials=[("外事服务证", True)],
            trainings=[("制证岗前培训", "制证核验")],
        )
        report = self.service.volunteer_report(CENTER, vid)
        self.assertEqual(report["languages"][0]["language"], "西班牙语")
        self.assertEqual(report["languages"][0]["verified"], 1)
        self.assertEqual(report["credentials"][0]["kind"], "外事服务证")
        self.assertEqual(report["qualifications"][0]["skill"], "制证核验")


if __name__ == "__main__":
    unittest.main()
