"""签到、漏签扫描、迟到与补录复核、封存不可改、角色数据边界。"""

from src.volunteer_operations.service import BusinessError
from src.volunteer_operations.store import Actor, PermissionError_
from tests.support import ScenarioTest


class CheckInReviewTest(ScenarioTest):
    def _hotel_shift(self, day="10"):
        return self.service.add_shift(
            self.staff, "媒体酒店", self.hotel.id,
            f"2026-10-{day}T08:00:00+08:00", f"2026-10-{day}T12:00:00+08:00")

    def test_late_marked_after_grace_period(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        # 宽限期（15 分钟）内签到为正常
        c = self.service.check_in(a.id, at="2026-10-10T08:05:00+08:00")
        self.assertEqual(c.status, "normal")
        # 之后再扫码不会把首次正常记录改坏，也不会重复建记录
        c2 = self.service.check_in(a.id, at="2026-10-10T08:40:00+08:00")
        self.assertEqual(c2.id, c.id)
        self.assertEqual(c2.status, "normal")
        self.assertEqual(c2.check_in_at, c.check_in_at)

    def test_checkin_after_grace_is_late(self):
        sh = self._hotel_shift(day="11")
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        c = self.service.check_in(a.id, at="2026-10-11T08:20:00+08:00")
        self.assertEqual(c.status, "late")

    def test_missed_scan_creates_review_not_direct_attendance(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        tasks = self.service.schedule_missed_scan()
        self.assertEqual(len(tasks), 1)
        # 宽限期内不执行，也不产生任何签到
        self.service.run_due_tasks(at="2026-10-10T12:10:00+08:00")
        self.assertFalse(any(x.assignment_id == a.id
                             for x in self.service.s.checkins))
        # 班后宽限期过后：漏签 + 复核
        done = self.service.run_due_tasks(at="2026-10-10T12:16:00+08:00")
        self.assertTrue(done)
        c = next(x for x in self.service.s.checkins if x.assignment_id == a.id)
        self.assertEqual(c.status, "missed")
        self.assertEqual(c.source, "system")
        self.assertIsNone(c.check_in_at)
        self.assertEqual(self.service.total_minutes(self.zhao.id), 0)
        review = next(x for x in self.service.s.reviews if x.assignment_id == a.id)
        self.assertEqual(review.status, "pending")
        self.assertEqual(review.kind, "missed")

        # 复核驳回：维持漏签，不计时长
        self.service.decide_review(self.staff, review.id, approve=False, note="无凭证")
        self.assertEqual(self.service.total_minutes(self.zhao.id), 0)

    def test_approved_backfill_counts_time_and_becomes_reward_basis(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        self.service.schedule_missed_scan()
        self.service.run_due_tasks(at="2026-10-10T12:16:00+08:00")
        review = next(x for x in self.service.s.reviews if x.assignment_id == a.id)

        self.service.decide_review(
            self.staff, review.id, approve=True, note="门岗录像确认已到岗",
            )
        c = next(x for x in self.service.s.checkins if x.assignment_id == a.id)
        self.assertEqual(c.status, "backfill")
        self.assertEqual(c.source, "review")
        self.assertEqual(self.service.total_minutes(self.zhao.id), 240)
        entry = next(e for e in self.service.s.ledger
                     if e.volunteer_id == self.zhao.id)
        self.assertEqual(entry.basis, "review")
        self.assertEqual(entry.ref_id, review.id)

    def test_late_appeal_clears_late_only_after_approval(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        c = self.service.check_in(a.id, at="2026-10-10T08:40:00+08:00")
        self.assertEqual(c.status, "late")
        review = self.service.raise_review(
            Actor(id=self.zhao.id, role="volunteer"), "late_appeal", a.id,
            "班车故障，附停运通知")
        # 结论作出前状态不变
        self.assertEqual(c.status, "late")
        self.service.decide_review(self.staff, review.id, approve=True)
        self.assertEqual(c.status, "normal")
        self.assertEqual(c.source, "review")

    def test_sealed_checkin_cannot_be_changed_by_anyone(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        self.service.check_in(a.id, at="2026-10-10T08:05:00+08:00")
        self.service.seal_checkins(self.staff, assignment_ids=[a.id])
        # 志愿者不能补刷
        with self.assertRaisesRegex(BusinessError, "已封存"):
            self.service.check_in(a.id, at="2026-10-10T09:00:00+08:00")
        # 组委会不能修改（不能靠替班改写）
        sub = self.service.request_substitution(
            Actor(id=self.zhao.id, role="volunteer"), a.id, self.chen.id, "x")
        with self.assertRaisesRegex(BusinessError, "已封存"):
            self.service.approve_substitution(self.committee, sub.id)
        # 也不能再发起复核
        with self.assertRaisesRegex(BusinessError, "已封存"):
            self.service.raise_review(
                Actor(id=self.zhao.id, role="volunteer"), "late_appeal", a.id, "x")
        # 非服务中心角色不能封存
        with self.assertRaises(PermissionError_):
            self.service.seal_checkins(self.committee, assignment_ids=[a.id])

    def test_seal_waits_for_pending_review(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        self.service.check_in(a.id, at="2026-10-10T08:05:00+08:00")
        self.service.raise_review(
            Actor(id=self.zhao.id, role="volunteer"), "late_appeal", a.id, "申诉")
        sealed = self.service.seal_checkins(self.staff, assignment_ids=[a.id])
        self.assertEqual(sealed, 0)
        c = next(x for x in self.service.s.checkins if x.assignment_id == a.id)
        self.assertFalse(c.sealed)

    def test_school_admin_sees_only_own_school(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        self.service.check_in(a.id, at="2026-10-10T08:05:00+08:00")
        # A 校管理员不能查看 B 校志愿者
        with self.assertRaises(PermissionError_):
            self.service.profile(self.admin_a, self.zhao.id)
        # B 校管理员可以
        p = self.service.profile(self.admin_b, self.zhao.id)
        self.assertEqual(p["volunteer_id"], self.zhao.id)
        # A 校管理员不能把 B 校志愿者派进班
        sh2 = self.service.add_shift(
            self.staff, "酒店", self.hotel.id,
            "2026-10-11T08:00:00+08:00", "2026-10-11T12:00:00+08:00")
        with self.assertRaises(PermissionError_):
            self.service.assign(self.admin_a, sh2.id, self.zhao.id)
        # 组委会可跨校查看
        p = self.service.profile(self.committee, self.zhao.id)
        self.assertEqual(p["name"], "赵理工")
        # 志愿者只能看自己
        with self.assertRaises(PermissionError_):
            self.service.profile(Actor(id=self.zhao.id, role="volunteer"), self.lin.id)

    def test_committee_cannot_register_or_verify(self):
        with self.assertRaises(PermissionError_):
            self.service.register_volunteer(self.committee, "某人", self.sch_a.id)
        with self.assertRaises(PermissionError_):
            self.service.verify_language(self.committee, self.lin.id, "fr")
