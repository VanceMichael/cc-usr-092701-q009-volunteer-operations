"""表彰计算：累计时长、语言岗位、全勤，奖励依据可追溯；任务可重入。"""

import unittest

import helpers  # noqa: F401
from helpers import CENTER, add_volunteer, dt, make_posts, make_shift, make_targets, new_service


def serve(service, aid, start, end):
    service.check_in(CENTER, aid, dt(start))
    service.check_out(CENTER, aid, dt(end))


class AwardsTest(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.posts = make_posts(self.service)
        self.targets = make_targets(self.service)

    def tearDown(self):
        self.service.close()

    def test_star_of_service_after_24_hours_with_basis(self):
        vid = add_volunteer(self.service, "劳模")
        for i in range(3):
            shift = make_shift(
                self.service, self.posts["hotel"], self.targets["hotel"],
                f"2026-09-{27 + i}T08:00", f"2026-09-{27 + i}T20:00",
            )
            aid = self.service.assign_shift(CENTER, shift, vid)
            serve(self.service, aid, f"2026-09-{27 + i}T08:00", f"2026-09-{27 + i}T20:00")
        result = self.service.compute_awards(CENTER, vid)
        self.assertIn("star_of_service", result[vid])
        report = self.service.volunteer_report(CENTER, vid)
        award = next(a for a in report["awards"] if a["kind"] == "star_of_service")
        self.assertEqual(award["basis"]["hours"], 36.0)
        self.assertEqual(award["basis"]["rule"], "累计有效服务满24小时")
        # 幂等：重复计算不重复授奖
        self.service.compute_awards(CENTER, vid)
        self.assertEqual(len(self.service.volunteer_report(CENTER, vid)["awards"]), 1)

    def test_language_pioneer_requires_verified_language_shifts(self):
        vid = add_volunteer(
            self.service, "小语种",
            languages=[("法语", "advanced", True)],
            credentials=[("护照", True)],
        )
        for day in (27, 28):
            shift = make_shift(
                self.service, self.posts["airport"], self.targets["airport"],
                f"2026-09-{day}T05:30", f"2026-09-{day}T13:30",
            )
            aid = self.service.assign_shift(CENTER, shift, vid)
            serve(self.service, aid, f"2026-09-{day}T05:35", f"2026-09-{day}T13:25")
        result = self.service.compute_awards(CENTER, vid)
        self.assertIn("language_pioneer", result[vid])

    def test_full_attendance(self):
        vid = add_volunteer(self.service, "全勤")
        for i in range(4):
            shift = make_shift(
                self.service, self.posts["hotel"], self.targets["gala"],
                f"2026-09-{27 + i}T10:00", f"2026-09-{27 + i}T14:00",
            )
            aid = self.service.assign_shift(CENTER, shift, vid)
            serve(self.service, aid, f"2026-09-{27 + i}T10:00", f"2026-09-{27 + i}T14:00")
        result = self.service.compute_awards(CENTER, vid)
        self.assertIn("full_attendance", result[vid])

    def test_absent_blocks_full_attendance(self):
        vid = add_volunteer(self.service, "缺席者")
        # 3个正常完成 + 1个漏签记缺勤
        for i in range(3):
            shift = make_shift(
                self.service, self.posts["hotel"], self.targets["gala"],
                f"2026-09-{27 + i}T10:00", f"2026-09-{27 + i}T14:00",
            )
            aid = self.service.assign_shift(CENTER, shift, vid)
            serve(self.service, aid, f"2026-09-{27 + i}T10:00", f"2026-09-{27 + i}T14:00")
        missed_shift = make_shift(
            self.service, self.posts["hotel"], self.targets["party"],
            "2026-09-30T10:00", "2026-09-30T14:00",
        )
        self.service.assign_shift(CENTER, missed_shift, vid)
        self.service.recover(dt("2026-09-30T14:31"))
        case = self.service.open_review_cases(CENTER)[0]
        self.service.review_case_decision(CENTER, case["id"], approve=True)  # 缺勤
        result = self.service.compute_awards(CENTER, vid)
        self.assertNotIn("full_attendance", result[vid])


if __name__ == "__main__":
    unittest.main()
