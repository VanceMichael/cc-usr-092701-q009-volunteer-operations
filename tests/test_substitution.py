"""替班链：保留原授权与新责任人，替班人资格复核，封存后禁止替班。"""

from src.volunteer_operations.service import BusinessError
from src.volunteer_operations.store import Actor
from tests.support import ScenarioTest


class SubstitutionTest(ScenarioTest):
    def _press_shift(self):
        return self.service.add_shift(
            self.staff, "涉外采访团陪同", self.press.id,
            "2026-10-10T14:00:00+08:00", "2026-10-10T18:00:00+08:00",
            language_needs=["fr"])

    def test_chain_keeps_original_assignment_and_new_owner(self):
        sh = self._press_shift()
        a = self.service.assign(self.committee, sh.id, self.lin.id)

        sub = self.service.request_substitution(
            Actor(id=self.lin.id, role="volunteer", name="林小语"),
            a.id, self.zhao.id, "临时有课")
        self.assertEqual(sub.seq, 1)
        self.assertEqual(sub.status, "pending")
        # 待批期间责任人不变
        self.assertEqual(self.service._assignment(a.id).volunteer_id, self.lin.id)

        # 赵理工不具备法语核验和媒体证，替班审批应被拒绝且责任人不变
        with self.assertRaisesRegex(BusinessError, "不符合要求"):
            self.service.approve_substitution(self.committee, sub.id)
        self.assertEqual(sub.status, "rejected")
        self.assertEqual(self.service._assignment(a.id).volunteer_id, self.lin.id)
        self.assertIsNotNone(sub.eligibility)
        self.assertFalse(sub.eligibility["eligible"])

        # 第二次替班：给陈清晨补齐涉外采访资质后接手
        self.service.declare_language(self.staff, self.chen.id, "fr", "法语", "B2")
        self.service.verify_language(self.staff, self.chen.id, "fr")
        self.service.verify_credential(self.staff, self.chen.id, "media_pass", "媒体证")
        self.service.complete_training(self.staff, self.chen.id, self.press.id)
        sub2 = self.service.request_substitution(
            Actor(id=self.lin.id, role="volunteer", name="林小语"),
            a.id, self.chen.id, "课程冲突，改由陈清晨接手")
        self.assertEqual(sub2.seq, 2)
        self.service.approve_substitution(self.committee, sub2.id)

        a2 = self.service._assignment(a.id)
        self.assertEqual(a2.volunteer_id, self.chen.id)          # 新责任人
        self.assertEqual(a2.original_volunteer_id, self.lin.id)  # 原责任人保留
        self.assertEqual(a2.original_authorized_by, self.committee.id)

        chain = self.service.substitution_chain(a.id)
        self.assertEqual([x.seq for x in chain], [1, 2])
        self.assertEqual([x.status for x in chain], ["rejected", "approved"])
        self.assertEqual(chain[1].authorized_by, self.committee.id)

    def test_sealed_checkin_blocks_substitution(self):
        sh = self._press_shift()
        a = self.service.assign(self.committee, sh.id, self.lin.id)
        self.service.check_in(a.id, at="2026-10-10T14:05:00+08:00")
        self.service.seal_checkins(self.staff, assignment_ids=[a.id])
        sub = self.service.request_substitution(
            Actor(id=self.lin.id, role="volunteer"), a.id, self.chen.id, "不适")
        with self.assertRaisesRegex(BusinessError, "已封存"):
            self.service.approve_substitution(self.committee, sub.id)

    def test_school_admin_cannot_cross_school_approve(self):
        sh = self._press_shift()
        a = self.service.assign(self.committee, sh.id, self.lin.id)
        sub = self.service.request_substitution(
            Actor(id=self.lin.id, role="volunteer"), a.id, self.zhao.id, "有事")
        # B 校老师不能审批 A 校志愿者的替班（赵也是 B 校）
        from src.volunteer_operations.store import PermissionError_
        with self.assertRaises(PermissionError_):
            self.service.approve_substitution(self.admin_b, sub.id)
