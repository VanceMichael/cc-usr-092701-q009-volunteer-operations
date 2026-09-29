"""可解释排班：资质、证件语言核验、重叠、休息间隔、清晨连续、名额。"""

from src.volunteer_operations.service import BusinessError
from tests.support import ScenarioTest


class SchedulingTest(ScenarioTest):
    def test_qualified_volunteer_gets_positive_matched_basis(self):
        sh = self.service.add_shift(
            self.staff, "涉外采访团陪同", self.press.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00",
            language_needs=["fr"])
        decision = self.service.evaluate(self.lin.id, sh.id)
        self.assertTrue(decision.eligible, decision.reasons)
        self.assertTrue(any("培训" in x for x in decision.matched))
        self.assertTrue(any("media_pass" in x for x in decision.matched))
        self.assertTrue(any("fr" in x for x in decision.matched))

    def test_declared_but_unverified_language_rejected_for_sensitive_post(self):
        qian = self.service.register_volunteer(self.admin_a, "钱小译", self.sch_a.id)
        self.service.declare_language(self.staff, qian.id, "fr", "法语", "B2")
        self.service.verify_credential(self.staff, qian.id, "media_pass", "媒体证")
        self.service.complete_training(self.staff, qian.id, self.press.id)
        sh = self.service.add_shift(
            self.staff, "采访陪同", self.press.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00")
        decision = self.service.evaluate(qian.id, sh.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("语言能力未核验" in x for x in decision.reasons))

    def test_missing_credential_and_training_both_listed(self):
        sh = self.service.add_shift(
            self.staff, "机场早班", self.airport.id,
            "2026-10-10T05:00:00+08:00", "2026-10-10T09:00:00+08:00")
        decision = self.service.evaluate(self.zhao.id, sh.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("培训" in x for x in decision.reasons))
        self.assertTrue(any("airport_pass" in x for x in decision.reasons))

    def test_overlapping_shifts_forbidden(self):
        sh1 = self.service.add_shift(
            self.staff, "采访陪同", self.press.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00",
            language_needs=["fr"])
        self.service.assign(self.committee, sh1.id, self.lin.id)
        sh2 = self.service.add_shift(
            self.staff, "采访加场", self.press.id,
            "2026-10-10T10:00:00+08:00", "2026-10-10T12:00:00+08:00")
        decision = self.service.evaluate(self.lin.id, sh2.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("时间重叠" in x for x in decision.reasons))
        with self.assertRaisesRegex(BusinessError, "时间重叠"):
            self.service.assign(self.committee, sh2.id, self.lin.id)

    def test_rest_interval_enforced_with_explanation(self):
        early = self.service.add_shift(
            self.staff, "机场抵离", self.airport.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00")
        self.service.assign(self.committee, early.id, self.chen.id)
        evening = self.service.add_shift(
            self.staff, "媒体酒店", self.hotel.id,
            "2026-10-10T18:00:00+08:00", "2026-10-10T22:00:00+08:00")
        decision = self.service.evaluate(self.chen.id, evening.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("小时间隔" in x for x in decision.reasons))

    def test_consecutive_early_morning_is_warning_not_block(self):
        for day in ("08", "09"):
            sh = self.service.add_shift(
                self.staff, "机场清晨", self.airport.id,
                f"2026-10-{day}T05:00:00+08:00", f"2026-10-{day}T09:00:00+08:00")
            self.service.assign(self.admin_a, sh.id, self.chen.id)
        third = self.service.add_shift(
            self.staff, "机场清晨", self.airport.id,
            "2026-10-10T05:00:00+08:00", "2026-10-10T09:00:00+08:00")
        decision = self.service.evaluate(self.chen.id, third.id)
        self.assertTrue(decision.eligible, decision.reasons)
        self.assertTrue(any("连续 2 天清晨" in x for x in decision.warnings))

    def test_headcount_full_blocks_assignment(self):
        sh = self.service.add_shift(
            self.staff, "酒店夜班", self.hotel.id,
            "2026-10-11T20:00:00+08:00", "2026-10-12T02:00:00+08:00",
            headcount=1)
        self.service.assign(self.admin_b, sh.id, self.zhao.id)
        decision = self.service.evaluate(self.chen.id, sh.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("名额已满" in x for x in decision.reasons))

    def test_language_gap_must_be_covered_by_verified_speaker(self):
        sh = self.service.add_shift(
            self.staff, "机场抵离", self.airport.id,
            "2026-10-10T05:00:00+08:00", "2026-10-10T11:00:00+08:00",
            headcount=2, language_needs=["ar"])
        # 陈清晨的阿拉伯语只是自报未核验，无法补缺口
        decision = self.service.evaluate(self.chen.id, sh.id)
        self.assertFalse(decision.eligible)
        self.assertTrue(any("ar" in x and "缺口" in x for x in decision.reasons))
        # 核验后可覆盖缺口
        self.service.verify_language(self.staff, self.chen.id, "ar")
        decision = self.service.evaluate(self.chen.id, sh.id)
        self.assertTrue(decision.eligible, decision.reasons)
        self.assertTrue(any("缺口" in x for x in decision.matched))

    def test_suggest_ranks_eligible_first(self):
        sh = self.service.add_shift(
            self.staff, "采访陪同", self.press.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00",
            language_needs=["fr"])
        ranked = self.service.suggest(sh.id)
        self.assertEqual(ranked[0].volunteer_id, self.lin.id)
        self.assertTrue(ranked[0].eligible)
        self.assertFalse(ranked[-1].eligible)

    def test_assignment_records_schedule_basis(self):
        sh = self.service.add_shift(
            self.staff, "采访陪同", self.press.id,
            "2026-10-10T09:00:00+08:00", "2026-10-10T13:00:00+08:00",
            language_needs=["fr"])
        a = self.service.assign(self.committee, sh.id, self.lin.id)
        self.assertTrue(a.matched)
        self.assertEqual(a.original_volunteer_id, self.lin.id)
        self.assertEqual(a.original_authorized_by, self.committee.id)
