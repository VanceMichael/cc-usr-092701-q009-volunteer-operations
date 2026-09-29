"""排班规则：重叠、休息间隔、清晨连班、资质与敏感岗位核验、可解释性。"""

import unittest

import helpers  # noqa: F401 - 确保 tests 目录在导入路径中
from helpers import CENTER, COMMITTEE, add_volunteer, make_posts, make_shift, make_targets, new_service
from src.volunteer_operations.domain import IneligibleAssignment


def failed_codes(checks):
    return {c["code"] for c in checks if not c["passed"]}


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)

    def tearDown(self):
        self.service.close()

    def test_overlap_rejected(self):
        vid = add_volunteer(self.service, "王晨")
        s1 = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T08:00", "2026-09-29T12:00")
        s2 = make_shift(self.service, self.posts["hotel"], self.targets["gala"], "2026-09-29T11:00", "2026-09-29T15:00")
        self.service.assign_shift(COMMITTEE, s1, vid)
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, s2, vid)
        self.assertIn("OVERLAP", failed_codes(ctx.exception.checks))

    def test_rest_interval_enforced(self):
        vid = add_volunteer(self.service, "李婧")
        s1 = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T06:00", "2026-09-29T14:00")
        s2 = make_shift(self.service, self.posts["hotel"], self.targets["party"], "2026-09-29T20:00", "2026-09-29T23:00")
        s3 = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-30T08:00", "2026-09-30T12:00")
        self.service.assign_shift(COMMITTEE, s1, vid)
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, s2, vid)  # 间隔仅6小时
        self.assertIn("REST_INTERVAL", failed_codes(ctx.exception.checks))
        self.service.assign_shift(COMMITTEE, s3, vid)  # 间隔18小时，可以排

    def test_qualification_from_training(self):
        vid = add_volunteer(self.service, "赵可")
        shift = make_shift(self.service, self.posts["badge"], self.targets["badge"], "2026-09-29T09:00", "2026-09-29T17:00")
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, shift, vid)
        self.assertIn("QUALIFICATION", failed_codes(ctx.exception.checks))
        self.service.complete_training(CENTER, vid, "制证岗前培训", "制证核验")
        self.service.assign_shift(COMMITTEE, shift, vid)

    def test_sensitive_post_requires_verified_language_and_credential(self):
        shift = make_shift(self.service, self.posts["airport"], self.targets["airport"], "2026-09-29T05:30", "2026-09-29T13:30")
        weak = add_volunteer(
            self.service, "孙未核验",
            languages=[("法语", "advanced", False)],
            credentials=[("护照", False)],
        )
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, shift, weak)
        self.assertIn("LANGUAGE_VERIFIED", failed_codes(ctx.exception.checks))
        self.assertIn("CREDENTIAL_VERIFIED", failed_codes(ctx.exception.checks))

        low_level = add_volunteer(
            self.service, "周等级不足",
            languages=[("法语", "basic", True)],
            credentials=[("护照", True)],
        )
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, shift, low_level)
        self.assertIn("LANGUAGE_LEVEL", failed_codes(ctx.exception.checks))

        ready = add_volunteer(
            self.service, "陈合格",
            languages=[("法语", "advanced", True)],
            credentials=[("护照", True)],
        )
        self.service.assign_shift(COMMITTEE, shift, ready)

    def test_sensitive_post_without_required_language_needs_any_verified_language(self):
        shift = make_shift(self.service, self.posts["press"], self.targets["hotel"], "2026-09-29T14:00", "2026-09-29T20:00")
        no_lang = add_volunteer(self.service, "吴无证", credentials=[("护照", True)])
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, shift, no_lang)
        self.assertIn("LANGUAGE_VERIFIED", failed_codes(ctx.exception.checks))
        ok = add_volunteer(
            self.service, "郑有证",
            languages=[("日语", "intermediate", True)],
            credentials=[("护照", True)],
        )
        self.service.assign_shift(COMMITTEE, shift, ok)

    def test_early_streak_limited(self):
        vid = add_volunteer(self.service, "钱早起")
        shifts = [
            make_shift(self.service, self.posts["hotel"], self.targets["hotel"], f"2026-09-{26 + i}T05:30", f"2026-09-{26 + i}T13:30")
            for i in range(4)
        ]
        for shift in shifts[:3]:
            self.service.assign_shift(COMMITTEE, shift, vid)
        with self.assertRaises(IneligibleAssignment) as ctx:
            self.service.assign_shift(COMMITTEE, shifts[3], vid)  # 第4个连续清晨班
        self.assertIn("EARLY_STREAK", failed_codes(ctx.exception.checks))

    def test_plan_shift_is_explainable(self):
        add_volunteer(self.service, "普通志愿者")
        add_volunteer(
            self.service, "小语种志愿者",
            languages=[("法语", "advanced", True)],
            credentials=[("护照", True)],
        )
        shift = make_shift(self.service, self.posts["airport"], self.targets["airport"], "2026-09-29T05:30", "2026-09-29T13:30")
        plan = self.service.plan_shift(shift)
        self.assertTrue(plan["rules"])
        by_name = {c["name"]: c for c in plan["candidates"]}
        self.assertFalse(by_name["普通志愿者"]["eligible"])
        self.assertTrue(by_name["小语种志愿者"]["eligible"])
        self.assertIn("CREDENTIAL_VERIFIED", failed_codes(by_name["普通志愿者"]["checks"]))
        # 合格者的检查项全部通过且带公平性评分
        self.assertTrue(all(c["passed"] for c in by_name["小语种志愿者"]["checks"]))
        self.assertIsNotNone(by_name["小语种志愿者"]["score"])

    def test_auto_assign_prefers_least_loaded(self):
        v1 = add_volunteer(self.service, "已排一天")
        v2 = add_volunteer(self.service, "空闲")
        s1 = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T08:00", "2026-09-29T16:00")
        s2 = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-30T08:00", "2026-09-30T16:00")
        first = self.service.auto_assign(COMMITTEE, s1)
        second = self.service.auto_assign(COMMITTEE, s2)
        self.assertNotEqual(first["volunteer_id"], second["volunteer_id"])
        self.assertIn("累计排班时长最少", second["reason"])

    def test_assignment_keeps_explanation(self):
        vid = add_volunteer(self.service, "留痕")
        shift = make_shift(self.service, self.posts["hotel"], self.targets["hotel"], "2026-09-29T08:00", "2026-09-29T12:00")
        self.service.assign_shift(COMMITTEE, shift, vid)
        report = self.service.volunteer_report(CENTER, vid)
        self.assertIsInstance(report["assignments"][0]["explanation"], list)


if __name__ == "__main__":
    unittest.main()
