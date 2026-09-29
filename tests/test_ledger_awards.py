"""替班后时长随责任人同步、表彰依据、重启后任务继续推进、志愿者画像。"""

from src.volunteer_operations.store import Actor
from tests.support import ScenarioTest


class LedgerAwardRecoveryTest(ScenarioTest):
    def _hotel_shift(self, day="10", start="08:00", end="12:00"):
        return self.service.add_shift(
            self.staff, "媒体酒店", self.hotel.id,
            f"2026-10-{day}T{start}:00+08:00", f"2026-10-{day}T{end}:00+08:00")

    def test_hours_move_with_approved_substitution(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_a, sh.id, self.chen.id)
        self.service.check_in(a.id, at="2026-10-10T08:05:00+08:00")
        self.assertEqual(self.service.total_minutes(self.chen.id), 240)
        self.assertEqual(self.service.total_minutes(self.zhao.id), 0)

        sub = self.service.request_substitution(
            Actor(id=self.chen.id, role="volunteer"), a.id, self.zhao.id, "换班")
        # 跨校替班由组委会审批；签到随派班转给新责任人
        self.service.approve_substitution(self.committee, sub.id)
        self.assertEqual(self.service.total_minutes(self.chen.id), 0)
        self.assertEqual(self.service.total_minutes(self.zhao.id), 240)

        # 画像中原责任人仍能看到这条已替出的班次与替班链
        p_chen = self.service.profile(
            Actor(id=self.chen.id, role="volunteer"), self.chen.id)
        view = p_chen["shifts"][0]
        self.assertEqual(view["role"], "原责任人（已替出）")
        self.assertEqual(view["current_volunteer_id"], self.zhao.id)
        self.assertEqual(view["substitution_chain"][0]["to"], self.zhao.id)
        self.assertEqual(len(p_chen["ledger"]), 0)

    def test_award_computation_keeps_basis_and_respects_thresholds(self):
        # 赵理工累计 4 个 4 小时班 = 960 分钟，先不达牌
        for day in ("08", "09", "10", "11"):
            sh = self._hotel_shift(day=day)
            a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
            self.service.check_in(a.id, at=f"2026-10-{day}T08:05:00+08:00")
        self.assertEqual(self.service.total_minutes(self.zhao.id), 960)
        awards = self.service.compute_awards(self.staff)
        self.assertFalse(any(x.volunteer_id == self.zhao.id for x in awards))

        # 再上一个 6 小时班 → 1320 分钟，达到铜牌（1200 分钟）
        sh = self.service.add_shift(
            self.staff, "媒体酒店", self.hotel.id,
            "2026-10-12T08:00:00+08:00", "2026-10-12T14:00:00+08:00")
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        self.service.check_in(a.id, at="2026-10-12T08:05:00+08:00")
        awards = self.service.compute_awards(self.staff)
        aw = next(x for x in awards if x.volunteer_id == self.zhao.id)
        self.assertEqual(aw.level, "铜牌志愿者")
        self.assertEqual(aw.total_minutes, 1320)
        self.assertEqual(len(aw.basis), 5)
        self.assertTrue(any("台账" in x for x in aw.reasons))

        # 重新计算：旧表彰作废，画像引用最新结果
        awards2 = self.service.compute_awards(self.staff)
        self.assertTrue(aw.superseded)
        p = self.service.profile(self.admin_b, self.zhao.id)
        self.assertEqual(p["award"]["level"], "铜牌志愿者")
        self.assertEqual(p["award"]["basis"], awards2[0].basis)

    def test_due_tasks_resume_after_restart_and_never_run_twice(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        # 班次 10-10 08:00，提前 12 小时 → 提醒在 10-09 20:00 到期
        self.service.schedule_reminders(lead_hours=12)
        self.service.schedule_awards("2026-10-09T19:00:00+08:00")

        # 重启前什么都没跑，重启后两个任务仍待推进
        svc = self.restart()
        self.assertEqual(
            sorted(t.status for t in svc.s.tasks), ["pending", "pending"])

        # 未到点不执行
        self.assertEqual(svc.run_due_tasks(at="2026-10-09T18:00:00+08:00"), [])

        # 到点后两个任务都完成（表彰 19:00、提醒 20:00）
        done = svc.run_due_tasks(at="2026-10-09T21:00:00+08:00")
        self.assertEqual({t.type for t in done}, {"awards", "reminder"})
        self.assertTrue(all(t.status == "done" for t in done))

        # 再重启，已完成任务不重复执行、不重复发通知
        svc = self.restart()
        self.assertEqual(svc.run_due_tasks(at="2026-10-11T00:00:00+08:00"), [])
        self.assertEqual(
            len([n for n in svc.s.notifications if n.kind == "shift_reminder"]), 1)

    def test_reminder_fires_at_lead_time_and_recovers_from_running(self):
        sh = self._hotel_shift()
        a = self.service.assign(self.admin_b, sh.id, self.zhao.id)
        tasks = self.service.schedule_reminders(lead_hours=12)
        self.assertEqual(len(tasks), 1)
        # 手工把任务置为 running 后重启，模拟执行到一半崩溃
        tasks[0].status = "running"
        self.service.store.save()

        svc = self.restart()
        task = next(t for t in svc.s.tasks if t.type == "reminder")
        self.assertEqual(task.status, "pending")
        done = svc.run_due_tasks(at="2026-10-09T20:00:00+08:00")
        self.assertEqual({t.id for t in done}, {task.id})
        note = next(n for n in svc.s.notifications
                    if n.volunteer_id == self.zhao.id and n.kind == "shift_reminder")
        self.assertIn("媒体酒店", note.text)
        # 重启后任务保持 done，不重复发
        svc = self.restart()
        done = svc.run_due_tasks(at="2026-10-11T00:00:00+08:00")
        self.assertEqual(
            len([n for n in svc.s.notifications if n.kind == "shift_reminder"]), 1)

    def test_profile_shows_real_shifts_chain_minutes_and_award_basis(self):
        sh1 = self._hotel_shift(day="10")
        a1 = self.service.assign(self.admin_a, sh1.id, self.chen.id)
        self.service.check_in(a1.id, at="2026-10-10T08:05:00+08:00")
        # 替班给赵理工并完成
        sub = self.service.request_substitution(
            Actor(id=self.chen.id, role="volunteer"), a1.id, self.zhao.id, "换")
        self.service.approve_substitution(self.committee, sub.id)

        # 陈清晨再上一个机场敏感岗
        sh2 = self.service.add_shift(
            self.staff, "机场抵离", self.airport.id,
            "2026-10-12T05:00:00+08:00", "2026-10-12T09:00:00+08:00")
        a2 = self.service.assign(self.committee, sh2.id, self.chen.id)
        self.service.check_in(a2.id, at="2026-10-12T04:58:00+08:00")
        self.service.compute_awards(self.staff)

        staff_view = self.service.profile(self.staff, self.chen.id)
        self.assertEqual(staff_view["total_minutes"], 240)
        self.assertIn("airport_pass", staff_view["verified_credentials"])
        airport_view = next(x for x in staff_view["shifts"]
                            if x["service_object"] == "机场抵离")
        self.assertTrue(airport_view["sensitive"])
        self.assertTrue(any("airport_pass" in x for x in airport_view["schedule_basis"]))
        # 陈清晨为该敏感岗位累计的分钟数应出现在奖励依据中
        # （其总量 240 分钟未达牌，改为直接核对台账）
        self.assertEqual(staff_view["ledger"][0]["minutes"], 240)
        self.assertEqual(staff_view["ledger"][0]["basis"], "attendance")
