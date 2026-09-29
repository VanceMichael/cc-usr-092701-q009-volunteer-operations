"""赛事志愿服务后台主服务。

一个 Service 绑定一个 Store，方法即业务用例：
报名与资质、可解释排班、替班链、签到复核、时长台账、表彰与持久化任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import timeutil
from .models import (
    Award,
    Assignment,
    CheckIn,
    Credential,
    DurableTask,
    LanguageAbility,
    LedgerEntry,
    Notification,
    Position,
    Review,
    School,
    Shift,
    Substitution,
    Training,
    Volunteer,
)
from .store import Actor, PermissionError_, Store


class BusinessError(Exception):
    """业务规则不满足（区别于越权）。"""


@dataclass
class Eligibility:
    """单个候选人对单个班次的可解释排班结论。"""
    volunteer_id: str
    shift_id: str
    eligible: bool
    matched: list[str] = field(default_factory=list)    # 满足的硬性依据
    reasons: list[str] = field(default_factory=list)    # 不予排岗的原因
    warnings: list[str] = field(default_factory=list)  # 可排但需关注

    def as_dict(self) -> dict:
        return {
            "volunteer_id": self.volunteer_id,
            "shift_id": self.shift_id,
            "eligible": self.eligible,
            "matched": self.matched,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


class Service:
    def __init__(self, store: Store):
        self.store = store
        self.s = store.state
        # 崩溃恢复：上次未落为 done 的 running 任务视为未完成，重启继续。
        recovered = False
        for task in self.s.tasks:
            if task.status == "running":
                task.status = "pending"
                recovered = True
        if recovered:
            store.save()

    # ============================================================ 辅助查找

    def _volunteer(self, volunteer_id: str) -> Volunteer:
        for v in self.s.volunteers:
            if v.id == volunteer_id:
                return v
        raise BusinessError(f"志愿者不存在：{volunteer_id}")

    def _shift(self, shift_id: str) -> Shift:
        for sh in self.s.shifts:
            if sh.id == shift_id:
                return sh
        raise BusinessError(f"班次不存在：{shift_id}")

    def _position(self, position_id: str) -> Position:
        for p in self.s.positions:
            if p.id == position_id:
                return p
        raise BusinessError(f"岗位不存在：{position_id}")

    def _assignment(self, assignment_id: str) -> Assignment:
        for a in self.s.assignments:
            if a.id == assignment_id:
                return a
        raise BusinessError(f"派班记录不存在：{assignment_id}")

    def active_assignments_for(self, volunteer_id: str) -> list[Assignment]:
        """该志愿者当前负责（含替班接手后）的全部有效派班。"""
        return [
            a for a in self.s.assignments
            if not a.cancelled and a.volunteer_id == volunteer_id
        ]

    def active_assignments_for_shift(self, shift_id: str) -> list[Assignment]:
        return [
            a for a in self.s.assignments
            if not a.cancelled and a.shift_id == shift_id
        ]

    # ============================================================ 档案与资质

    def add_school(self, name: str) -> School:
        school = School(id=self.store.next_id("SCH"), name=name)
        self.s.schools.append(school)
        self.store.save()
        return school

    def register_volunteer(self, actor: Actor, name: str, school_id: str) -> Volunteer:
        if actor.role not in ("staff", "school"):
            raise PermissionError_("只有赛事服务中心或高校管理员可以报名登记")
        if actor.role == "school" and actor.school_id != school_id:
            raise PermissionError_("高校管理员只能登记本校志愿者")
        if not any(sch.id == school_id for sch in self.s.schools):
            raise BusinessError("学校不存在")
        v = Volunteer(id=self.store.next_id("V"), name=name, school_id=school_id)
        self.s.volunteers.append(v)
        self.store.save()
        return v

    def _require_staff(self, actor: Actor) -> None:
        if actor.role != "staff":
            raise PermissionError_("资质核验与培训登记由赛事服务中心执行")

    def declare_language(self, actor: Actor, volunteer_id: str, code: str,
                         name: str, level: str) -> LanguageAbility:
        """志愿者自报语言能力（未核验），敏感岗位不予承认。"""
        self._require_staff(actor)
        v = self._volunteer(volunteer_id)
        for lang in v.languages:
            if lang.code == code:
                lang.name, lang.level = name, level
                self.store.save()
                return lang
        lang = LanguageAbility(code=code, name=name, level=level, verified=False)
        v.languages.append(lang)
        self.store.save()
        return lang

    def verify_language(self, actor: Actor, volunteer_id: str, code: str) -> None:
        self._require_staff(actor)
        v = self._volunteer(volunteer_id)
        for lang in v.languages:
            if lang.code == code:
                lang.verified = True
                self.store.save()
                return
        raise BusinessError(f"未找到语言申报：{code}，请先登记再核验")

    def verify_credential(self, actor: Actor, volunteer_id: str, cred_type: str,
                          cred_name: str) -> None:
        self._require_staff(actor)
        v = self._volunteer(volunteer_id)
        for cred in v.credentials:
            if cred.type == cred_type:
                cred.verified, cred.name = True, cred_name
                cred.verified_at = timeutil.iso(timeutil.now())
                self.store.save()
                return
        v.credentials.append(Credential(
            type=cred_type, name=cred_name, verified=True,
            verified_at=timeutil.iso(timeutil.now()),
        ))
        self.store.save()

    def complete_training(self, actor: Actor, volunteer_id: str,
                          position_id: str, at: str | None = None) -> None:
        self._require_staff(actor)
        self._position(position_id)
        v = self._volunteer(volunteer_id)
        if position_id in v.trained_positions():
            return
        v.trainings.append(Training(
            position_id=position_id,
            completed_at=at or timeutil.iso(timeutil.now()),
        ))
        self.store.save()

    def add_position(self, actor: Actor, name: str, *, sensitive: bool = False,
                     required_credentials: list[str] | None = None,
                     required_languages: list[str] | None = None,
                     require_training: bool = True,
                     min_rest_hours: float = 11.0,
                     early_morning: bool = False) -> Position:
        self._require_staff(actor)
        pos = Position(
            id=self.store.next_id("P"), name=name, sensitive=sensitive,
            required_credentials=required_credentials or [],
            required_languages=required_languages or [],
            require_training=require_training,
            min_rest_hours=min_rest_hours, early_morning=early_morning,
        )
        self.s.positions.append(pos)
        self.store.save()
        return pos

    def add_shift(self, actor: Actor, service_object: str, position_id: str,
                  start: str, end: str, *, headcount: int = 1,
                  language_needs: list[str] | None = None) -> Shift:
        if actor.role not in ("staff", "committee"):
            raise PermissionError_("只有服务中心或组委会可以开班")
        self._position(position_id)
        if timeutil.parse(end) <= timeutil.parse(start):
            raise BusinessError("班次结束时间必须晚于开始时间")
        sh = Shift(
            id=self.store.next_id("S"), service_object=service_object,
            position_id=position_id, start=start, end=end,
            headcount=headcount, language_needs=language_needs or [],
        )
        self.s.shifts.append(sh)
        self.store.save()
        return sh

    # ============================================================ 可解释排班

    def _active_shift_intervals(self, volunteer_id: str,
                                exclude_shift_id: str | None = None,
                                exclude_assignment_id: str | None = None
                                ) -> list[tuple[Shift, Assignment]]:
        result = []
        for a in self.active_assignments_for(volunteer_id):
            if exclude_assignment_id and a.id == exclude_assignment_id:
                continue
            if exclude_shift_id and a.shift_id == exclude_shift_id:
                continue
            result.append((self._shift(a.shift_id), a))
        return result

    def evaluate(self, volunteer_id: str, shift_id: str,
                 exclude_assignment_id: str | None = None) -> Eligibility:
        """评估候选人能否上该班次，逐条给出匹配依据或拒绝原因。

        exclude_assignment_id 用于替班审批：被替换的派班不再占用名额、
        其语种覆盖也相应移除（替班是“换人”而非“加人”）。
        """
        v = self._volunteer(volunteer_id)
        sh = self._shift(shift_id)
        pos = self._position(sh.position_id)
        result = Eligibility(volunteer_id=volunteer_id, shift_id=shift_id,
                             eligible=True)

        # 1. 培训
        if pos.require_training:
            if pos.id in v.trained_positions():
                result.matched.append(f"已完成「{pos.name}」岗位培训")
            else:
                result.eligible = False
                result.reasons.append(f"缺少「{pos.name}」岗位培训")

        # 2. 证件（敏感岗位逐项核验）
        missing_creds = [c for c in pos.required_credentials
                         if c not in v.verified_credential_types()]
        if pos.sensitive and missing_creds:
            result.eligible = False
            result.reasons.append(
                "敏感岗位要求的证件未核验：" + "、".join(missing_creds))
        elif pos.required_credentials and not missing_creds:
            result.matched.append("岗位证件均已核验：" + "、".join(pos.required_credentials))

        # 3. 语言能力（敏感岗位只认核验通过的小语种）
        missing_langs = [c for c in pos.required_languages
                         if c not in v.verified_languages()]
        if pos.sensitive and missing_langs:
            result.eligible = False
            result.reasons.append(
                "敏感岗位要求的语言能力未核验：" + "、".join(missing_langs))
        elif pos.required_languages and not missing_langs:
            result.matched.append("岗位语言要求已核验：" + "、".join(pos.required_languages))

        # 4. 班次小语种缺口：候选人需补上尚未覆盖的语种（非敏感也适用）
        if sh.language_needs:
            current = [a for a in self.active_assignments_for_shift(shift_id)
                       if a.id != exclude_assignment_id]
            covered = set()
            for a in current:
                covered |= self._volunteer(a.volunteer_id).verified_languages()
            uncovered = [c for c in sh.language_needs if c not in covered]
            can_cover = [c for c in uncovered if c in v.verified_languages()]
            if uncovered and can_cover:
                result.matched.append("可补上班次小语种缺口：" + "、".join(can_cover))
            elif uncovered:
                result.eligible = False
                result.reasons.append(
                    "班次小语种缺口仍含 " + "、".join(uncovered)
                    + "，候选人的核验语种无法覆盖")
            else:
                result.matched.append("班次小语种需求已由团队覆盖")

        # 5. 班次时间冲突（一人不得重叠班次）
        for other, _a in self._active_shift_intervals(
                volunteer_id, shift_id, exclude_assignment_id):
            if timeutil.overlap_minutes(sh.start, sh.end, other.start, other.end) > 0:
                result.eligible = False
                result.reasons.append(
                    f"与班次 {other.id}（{other.service_object} "
                    f"{timeutil.shift_label(other.start, other.end)}）时间重叠")

        # 6. 休息间隔（岗前/岗后都要满足）
        for other, _a in self._active_shift_intervals(
                volunteer_id, shift_id, exclude_assignment_id):
            if timeutil.overlap_minutes(sh.start, sh.end, other.start, other.end) > 0:
                continue  # 重叠已在上面记录
            if other.end <= sh.start:
                gap = timeutil.gap_hours(other.end, sh.start)
                if gap < pos.min_rest_hours:
                    result.eligible = False
                    result.reasons.append(
                        f"距上一班 {other.id} 结束仅 {gap:.1f} 小时，"
                        f"不足 {pos.min_rest_hours:g} 小时间隔")
            elif sh.end <= other.start:
                other_min = self._position(other.position_id).min_rest_hours
                gap = timeutil.gap_hours(sh.end, other.start)
                if gap < other_min:
                    result.eligible = False
                    result.reasons.append(
                        f"距下一班 {other.id} 开始仅 {gap:.1f} 小时，"
                        f"不足 {other_min:g} 小时间隔")

        # 7. 连续清晨出勤（柔性警示，不阻断）
        if pos.early_morning:
            streak = self._early_morning_streak(v.id, sh.start)
            if streak >= 2:
                result.warnings.append(
                    f"此前已连续 {streak} 天清晨出勤，请关注疲劳与轮换")

        # 8. 名额（替班换人不占新名额）
        current = [a for a in self.active_assignments_for_shift(shift_id)
                   if a.id != exclude_assignment_id]
        if len(current) >= sh.headcount:
            result.eligible = False
            result.reasons.append(f"班次 {sh.id} 名额已满（{sh.headcount} 人）")

        return result

    def _early_morning_streak(self, volunteer_id: str, before_start: str) -> int:
        """统计 before_start 之前连续多少天排了清晨班。"""
        days: set[str] = set()
        start_dt = timeutil.parse(before_start)
        for other, _a in self._active_shift_intervals(volunteer_id):
            op = self._position(other.position_id)
            if op.early_morning and timeutil.parse(other.start) < start_dt:
                days.add(timeutil.parse(other.start).date().isoformat())
        if not days:
            return 0
        streak = 0
        day = start_dt.date()
        from datetime import timedelta
        day = day - timedelta(days=1)
        while day.isoformat() in days:
            streak += 1
            day -= timedelta(days=1)
        return streak

    def assign(self, actor: Actor, shift_id: str, volunteer_id: str,
               *, force: bool = False) -> Assignment:
        """派班。force=False 时硬性条件不满足直接拒绝。"""
        self.store.assert_can_dispatch(actor, volunteer_id)
        decision = self.evaluate(volunteer_id, shift_id)
        if not decision.eligible and not force:
            raise BusinessError("；".join(decision.reasons) or "不符合排班条件")
        sh = self._shift(shift_id)
        at = timeutil.iso(timeutil.now())
        a = Assignment(
            id=self.store.next_id("A"), shift_id=shift_id,
            volunteer_id=volunteer_id, original_volunteer_id=volunteer_id,
            original_authorized_by=actor.id, created_at=at, created_by=actor.id,
            matched=decision.matched + (
                ["【强制派班】" + "；".join(decision.reasons)] if force and decision.reasons else []
            ),
        )
        self.s.assignments.append(a)
        self.store.save()
        return a

    def suggest(self, shift_id: str) -> list[Eligibility]:
        """为班次返回全部候选人的评估，按“合格优先、依据数量”排序。"""
        sh = self._shift(shift_id)
        results = [self.evaluate(v.id, shift_id) for v in self.s.volunteers]
        results.sort(key=lambda e: (not e.eligible, -len(e.matched),
                                    len(e.reasons), e.volunteer_id))
        return results

    # ============================================================ 替班链

    def request_substitution(self, actor: Actor, assignment_id: str,
                             to_volunteer_id: str, reason: str) -> Substitution:
        a = self._assignment(assignment_id)
        if a.cancelled:
            raise BusinessError("派班已取消，不能替班")
        if actor.role == "volunteer" and actor.id != a.volunteer_id:
            raise PermissionError_("只能为自己的派班申请替班")
        if actor.role not in ("volunteer", "committee", "school", "staff"):
            raise PermissionError_("该角色不能申请替班")
        if actor.role in ("school", "committee"):
            self.store.assert_can_dispatch(actor, to_volunteer_id)
        self._volunteer(to_volunteer_id)
        seq = max((x.seq for x in self.s.substitutions
                   if x.assignment_id == assignment_id), default=0) + 1
        sub = Substitution(
            id=self.store.next_id("SUB"), assignment_id=assignment_id, seq=seq,
            from_volunteer_id=a.volunteer_id, to_volunteer_id=to_volunteer_id,
            requested_by=actor.id, reason=reason,
            created_at=timeutil.iso(timeutil.now()),
        )
        self.s.substitutions.append(sub)
        self.store.save()
        return sub

    def approve_substitution(self, actor: Actor, substitution_id: str) -> Substitution:
        if actor.role not in ("committee", "school"):
            raise PermissionError_("替班须由组委会或高校管理员审批")
        sub = next((x for x in self.s.substitutions if x.id == substitution_id), None)
        if sub is None:
            raise BusinessError("替班申请不存在")
        if sub.status != "pending":
            raise BusinessError(f"替班申请已{_status_cn(sub.status)}，不能重复审批")
        a = self._assignment(sub.assignment_id)
        self.store.assert_can_dispatch(actor, sub.from_volunteer_id)
        self.store.assert_can_dispatch(actor, sub.to_volunteer_id)

        # 已封存签到不可随替班改动（先于资格判断，封存数据绝不被改写）
        sealed = [c for c in self.s.checkins
                  if c.assignment_id == a.id and c.sealed]
        if sealed:
            raise BusinessError("该派班存在已封存签到，不能替班")

        # 替班人按相同岗位要求重新核验（换人不占新名额），结论随审批留档
        decision = self.evaluate(sub.to_volunteer_id, a.shift_id,
                                 exclude_assignment_id=a.id)
        sub.eligibility = decision.as_dict()
        if not decision.eligible:
            sub.status = "rejected"
            sub.authorized_by = actor.id
            sub.decided_at = timeutil.iso(timeutil.now())
            self.store.save()
            raise BusinessError("替班人不符合要求：" + "；".join(decision.reasons))

        # 转移未封存签到（实际服务人尚未锁定），挂在派班上的复核仍可追溯
        for c in self.s.checkins:
            if c.assignment_id == a.id and not c.sealed:
                c.volunteer_id = sub.to_volunteer_id

        a.volunteer_id = sub.to_volunteer_id
        sub.status = "approved"
        sub.authorized_by = actor.id
        sub.decided_at = timeutil.iso(timeutil.now())
        self.rebuild_ledger()
        self.store.save()
        return sub

    def reject_substitution(self, actor: Actor, substitution_id: str,
                            note: str = "") -> Substitution:
        if actor.role not in ("committee", "school"):
            raise PermissionError_("替班须由组委会或高校管理员审批")
        sub = next((x for x in self.s.substitutions if x.id == substitution_id), None)
        if sub is None or sub.status != "pending":
            raise BusinessError("替班申请不存在或已处理")
        sub.status = "rejected"
        sub.authorized_by = actor.id
        sub.decision_note = note
        sub.decided_at = timeutil.iso(timeutil.now())
        self.store.save()
        return sub

    def substitution_chain(self, assignment_id: str) -> list[Substitution]:
        return sorted(
            (x for x in self.s.substitutions if x.assignment_id == assignment_id),
            key=lambda x: x.seq,
        )

    # ============================================================ 签到与复核

    def check_in(self, assignment_id: str, at: str | None = None) -> CheckIn:
        """志愿者签到（自助）。超过宽限时间记迟到；重复签到直接返回原记录。"""
        a = self._assignment(assignment_id)
        if a.cancelled:
            raise BusinessError("派班已取消")
        existing = next((c for c in self.s.checkins
                         if c.assignment_id == assignment_id), None)
        if existing:
            if existing.sealed:
                raise BusinessError("签到已封存，不能补刷")
            if existing.status == "missed":
                raise BusinessError("该班次已登记漏签复核，请等待复核结论，不能自行补刷")
            if existing.check_in_at is not None:
                return existing
        at_dt = timeutil.parse(at) if at else timeutil.now()
        sh = self._shift(a.shift_id)
        late_after = timeutil.parse(sh.start)
        from datetime import timedelta
        late_after = late_after + timedelta(minutes=self.s.settings.grace_minutes)
        status = "late" if at_dt > late_after else "normal"
        if existing:
            c = existing
            # 自助补刷只承认更早的扫码记录，不能靠再刷一次抹掉迟到
            if c.check_in_at is not None and timeutil.parse(c.check_in_at) <= at_dt:
                return c
            c.check_in_at = timeutil.iso(at_dt)
            c.status = status
            c.source = "self"
        else:
            c = CheckIn(
                id=self.store.next_id("C"), assignment_id=assignment_id,
                shift_id=a.shift_id, volunteer_id=a.volunteer_id,
                scheduled_start=sh.start, scheduled_end=sh.end,
                check_in_at=timeutil.iso(at_dt), status=status, source="self",
                created_at=timeutil.iso(timeutil.now()),
            )
            self.s.checkins.append(c)
        self.rebuild_ledger()
        self.store.save()
        return c

    def raise_review(self, actor: Actor, kind: str, assignment_id: str,
                     evidence: str, claimed_check_in_at: str | None = None) -> Review:
        """漏签申诉/迟到申诉/补录都进入复核队列，不直接改写出勤。"""
        if kind not in ("missed", "late_appeal", "backfill"):
            raise BusinessError("复核类型必须是 missed/late_appeal/backfill")
        if actor.role not in ("staff", "school", "volunteer", "committee"):
            raise PermissionError_("该角色不能发起复核")
        a = self._assignment(assignment_id)
        if actor.role == "volunteer" and actor.id != a.volunteer_id:
            raise PermissionError_("只能为自己的签到发起复核")
        if actor.role == "school":
            self.store.assert_can_see_volunteer(actor, a.volunteer_id)
        c = next((x for x in self.s.checkins if x.assignment_id == assignment_id), None)
        if c and c.sealed:
            raise BusinessError("签到已封存，不能再发起复核")
        review = Review(
            id=self.store.next_id("R"), kind=kind, assignment_id=assignment_id,
            shift_id=a.shift_id, volunteer_id=a.volunteer_id, evidence=evidence,
            raised_by=actor.id, claimed_check_in_at=claimed_check_in_at,
            created_at=timeutil.iso(timeutil.now()),
        )
        self.s.reviews.append(review)
        self.store.save()
        return review

    def decide_review(self, actor: Actor, review_id: str, approve: bool,
                      note: str = "") -> Review:
        """复核结论由赛事服务中心作出；通过才回写出勤与时长。"""
        if actor.role != "staff":
            raise PermissionError_("只有赛事服务中心可以作出复核结论")
        review = next((x for x in self.s.reviews if x.id == review_id), None)
        if review is None:
            raise BusinessError("复核记录不存在")
        if review.status != "pending":
            raise BusinessError("复核已作出结论")
        c = next((x for x in self.s.checkins
                  if x.assignment_id == review.assignment_id), None)
        if c and c.sealed:
            raise BusinessError("签到已封存，复核结论不能回写")

        review.status = "approved" if approve else "rejected"
        review.reviewer = actor.id
        review.decision_note = note
        review.decided_at = timeutil.iso(timeutil.now())

        if approve:
            sh = self._shift(review.shift_id)
            if c is None:
                # 漏签/补录经复核通过后才生成有效签到
                c = CheckIn(
                    id=self.store.next_id("C"),
                    assignment_id=review.assignment_id, shift_id=review.shift_id,
                    volunteer_id=review.volunteer_id,
                    scheduled_start=sh.start, scheduled_end=sh.end,
                    check_in_at=review.claimed_check_in_at or sh.start,
                    status="backfill", source="review", review_id=review.id,
                    created_at=timeutil.iso(timeutil.now()),
                )
                self.s.checkins.append(c)
            else:
                c.source = "review"
                c.review_id = review.id
                if review.kind == "missed":
                    c.status = "backfill"
                    c.check_in_at = review.claimed_check_in_at or c.check_in_at or sh.start
                elif review.kind == "backfill":
                    c.status = "backfill"
                elif review.kind == "late_appeal":
                    c.status = "normal"  # 迟到申诉成立，撤销迟到标记
        else:
            if c is not None and review.kind == "missed" and c.source == "system":
                c.status = "missed"  # 维持漏签，不计时长
        review.volunteer_id = self._assignment(review.assignment_id).volunteer_id
        self.rebuild_ledger()
        self._notify(review.volunteer_id, "review",
                     f"复核 {review.id} 结论：{'通过' if approve else '驳回'}（{review.kind}）")
        self.store.save()
        return review

    def seal_checkins(self, actor: Actor, assignment_ids: list[str] | None = None,
                      day: str | None = None) -> int:
        """封存签到：封存后组委会及任何角色都不能再改写。

        不限定 assignment_ids/day 时，封存所有已结束班次的签到；
        尚有未结复核的签到暂不封存。
        """
        if actor.role != "staff":
            raise PermissionError_("只有赛事服务中心可以封存签到")
        targets = set(assignment_ids or [])
        now_dt = timeutil.now()
        count = 0
        for c in self.s.checkins:
            if c.sealed:
                continue
            if day is not None and not c.scheduled_start.startswith(day):
                continue
            if targets and c.assignment_id not in targets:
                continue
            if not targets and day is None and timeutil.parse(c.scheduled_end) > now_dt:
                continue
            pending = [r for r in self.s.reviews
                       if r.assignment_id == c.assignment_id and r.status == "pending"]
            if pending:
                continue  # 尚有复核未结，先不封存
            c.sealed = True
            count += 1
        self.store.save()
        return count

    # ============================================================ 时长台账

    def rebuild_ledger(self) -> None:
        """依据签到/复核现状重建台账（稳定编号，供表彰引用）。

        有效出勤 = 有签到时间且状态不为 missed；时长计入实际服务人，
        从而替班、漏签补认、驳回都能与奖励记录保持同步。
        """
        effective: dict[str, LedgerEntry] = {}
        for c in self.s.checkins:
            if c.check_in_at is None or c.status == "missed":
                continue
            review = next((r for r in self.s.reviews
                           if r.id == c.review_id and r.status == "approved"), None)
            if c.source == "review" and review is None and c.status == "backfill":
                continue
            minutes = timeutil.duration_minutes(c.scheduled_start, c.scheduled_end)
            if c.status == "backfill" or review is not None:
                basis, note = "review", f"经复核 {c.review_id} 确认的服务时长"
            else:
                basis, note = "attendance", f"班次 {c.shift_id} 签到服务时长"
            effective[c.id] = LedgerEntry(
                id=f"L-{c.id}", volunteer_id=c.volunteer_id, shift_id=c.shift_id,
                minutes=minutes, basis=basis, ref_id=c.review_id or c.id, note=note,
                created_at=c.created_at or timeutil.iso(timeutil.now()),
            )
        self.s.ledger = sorted(effective.values(), key=lambda e: e.id)

    def total_minutes(self, volunteer_id: str) -> int:
        return sum(e.minutes for e in self.s.ledger if e.volunteer_id == volunteer_id)

    # ============================================================ 表彰

    def compute_awards(self, actor: Actor | None = None) -> list[Award]:
        """按台账累计时长计算表彰，逐条保留奖励依据；重复计算作废旧结果。"""
        if actor is not None and actor.role not in ("staff", "committee"):
            raise PermissionError_("该角色不能计算表彰")
        by_volunteer: dict[str, list[LedgerEntry]] = {}
        for e in self.s.ledger:
            by_volunteer.setdefault(e.volunteer_id, []).append(e)
        awards: list[Award] = []
        for volunteer_id, entries in by_volunteer.items():
            total = sum(e.minutes for e in entries)
            level = None
            for rule in sorted(self.s.settings.award_levels,
                               key=lambda r: -r["min_minutes"]):
                if total >= rule["min_minutes"]:
                    level = rule["level"]
                    break
            if level is None:
                continue
            v = self._volunteer(volunteer_id)
            reasons = [
                f"累计服务 {total} 分钟（{total / 60:.1f} 小时）"
                f"，达到「{level}」门槛",
                f"依据 {len(entries)} 条台账：" + "、".join(e.id for e in entries),
                f"其中含小语种/敏感岗位服务 {self._sensitive_minutes(entries)} 分钟",
            ]
            awards.append(Award(
                id=self.store.next_id("AW"), volunteer_id=volunteer_id,
                level=level, total_minutes=total,
                basis=[e.id for e in entries], reasons=reasons,
                created_at=timeutil.iso(timeutil.now()),
            ))
        for old in self.s.awards:
            old.superseded = True
        self.s.awards.extend(awards)
        for aw in awards:
            self._notify(aw.volunteer_id, "award",
                         f"恭喜获得「{aw.level}」，累计服务 {aw.total_minutes} 分钟")
        self.store.save()
        return awards

    def _sensitive_minutes(self, entries: list[LedgerEntry]) -> int:
        total = 0
        for e in entries:
            sh = next((x for x in self.s.shifts if x.id == e.shift_id), None)
            if sh and self._position(sh.position_id).sensitive:
                total += e.minutes
        return total

    # ============================================================ 通知与持久化任务

    def _notify(self, volunteer_id: str, kind: str, text: str) -> Notification:
        n = Notification(
            id=self.store.next_id("N"), volunteer_id=volunteer_id, kind=kind,
            text=text, created_at=timeutil.iso(timeutil.now()),
        )
        self.s.notifications.append(n)
        return n

    def schedule_reminders(self, actor: Actor | None = None,
                           lead_hours: float | None = None) -> list[DurableTask]:
        """为全部有效派班排定岗前提醒（幂等：每班仅排一次）。"""
        lead = lead_hours if lead_hours is not None else self.s.settings.default_lead_hours
        existing = {t.payload.get("assignment_id") for t in self.s.tasks
                    if t.type == "reminder"}
        tasks = []
        for a in self.s.assignments:
            if a.cancelled or a.id in existing:
                continue
            sh = self._shift(a.shift_id)
            run_at = timeutil.add_hours(sh.start, -lead)
            t = DurableTask(
                id=self.store.next_id("T"), type="reminder", run_at=run_at,
                payload={"assignment_id": a.id, "volunteer_id": a.volunteer_id,
                         "shift_id": a.shift_id},
                created_at=timeutil.iso(timeutil.now()),
            )
            self.s.tasks.append(t)
            tasks.append(t)
        self.store.save()
        return tasks

    def schedule_missed_scan(self) -> list[DurableTask]:
        """班后宽限期一过即扫描漏签（幂等）。"""
        existing = {t.payload.get("shift_id") for t in self.s.tasks
                    if t.type == "missed_scan"}
        tasks = []
        from datetime import timedelta
        for sh in self.s.shifts:
            if sh.id in existing:
                continue
            run_dt = timeutil.parse(sh.end) + timedelta(
                minutes=self.s.settings.grace_minutes)
            t = DurableTask(
                id=self.store.next_id("T"), type="missed_scan",
                run_at=timeutil.iso(run_dt), payload={"shift_id": sh.id},
                created_at=timeutil.iso(timeutil.now()),
            )
            self.s.tasks.append(t)
            tasks.append(t)
        self.store.save()
        return tasks

    def schedule_awards(self, run_at: str) -> DurableTask:
        t = DurableTask(
            id=self.store.next_id("T"), type="awards", run_at=run_at,
            created_at=timeutil.iso(timeutil.now()),
        )
        self.s.tasks.append(t)
        self.store.save()
        return t

    def _execute_task(self, task: DurableTask) -> None:
        if task.type == "reminder":
            a = self._assignment(task.payload["assignment_id"])
            if not a.cancelled:
                sh = self._shift(a.shift_id)
                self._notify(
                    a.volunteer_id, "shift_reminder",
                    f"班次提醒：{sh.service_object} "
                    f"{timeutil.shift_label(sh.start, sh.end)}，请按时签到")
            task.result = "reminder-sent"
        elif task.type == "missed_scan":
            shift_id = task.payload["shift_id"]
            found = 0
            for a in self.active_assignments_for_shift(shift_id):
                c = next((x for x in self.s.checkins
                          if x.assignment_id == a.id), None)
                if c and c.check_in_at:
                    continue
                if c is None:
                    sh = self._shift(shift_id)
                    c = CheckIn(
                        id=self.store.next_id("C"), assignment_id=a.id,
                        shift_id=shift_id, volunteer_id=a.volunteer_id,
                        scheduled_start=sh.start, scheduled_end=sh.end,
                        status="missed", source="system",
                        created_at=timeutil.iso(timeutil.now()),
                    )
                    self.s.checkins.append(c)
                else:
                    c.status, c.source = "missed", "system"
                self.raise_review(
                    Actor(id="system", role="staff"), "missed", a.id,
                    evidence="班后扫描未发现签到，自动登记漏签复核")
                found += 1
            self.rebuild_ledger()
            task.result = f"missed:{found}"
        elif task.type == "awards":
            awards = self.compute_awards()
            task.result = f"awards:{len(awards)}"
        else:
            raise BusinessError(f"未知任务类型：{task.type}")

    def run_due_tasks(self, at: str | None = None) -> list[DurableTask]:
        """推进所有到期且未完成的任务；每条任务独立落盘，崩溃可续跑。"""
        now_dt = timeutil.parse(at) if at else timeutil.now()
        done = []
        for task in sorted(self.s.tasks, key=lambda t: t.run_at):
            if task.status not in ("pending", "failed", "running"):
                continue
            if timeutil.parse(task.run_at) > now_dt:
                continue
            task.status = "running"
            task.attempts += 1
            self.store.save()
            try:
                self._execute_task(task)
                task.status = "done"
            except Exception as exc:  # 失败留痕，下次重启继续重试
                task.status = "failed"
                task.error = str(exc)
            self.store.save()
            if task.status == "done":
                done.append(task)
        return done

    # ============================================================ 志愿者画像

    def profile(self, actor: Actor, volunteer_id: str) -> dict:
        """一名志愿者的真实班次、替班链、累计时长与奖励依据。"""
        self.store.assert_can_see_volunteer(actor, volunteer_id)
        v = self._volunteer(volunteer_id)
        shifts_view = []
        # 作为原责任人或当前责任人出现的派班都要呈现
        for a in self.s.assignments:
            if volunteer_id not in (a.original_volunteer_id, a.volunteer_id):
                continue
            sh = self._shift(a.shift_id)
            pos = self._position(sh.position_id)
            chain = [
                {
                    "id": x.id, "seq": x.seq,
                    "from": x.from_volunteer_id, "to": x.to_volunteer_id,
                    "status": x.status, "authorized_by": x.authorized_by,
                    "reason": x.reason,
                }
                for x in self.substitution_chain(a.id)
            ]
            checkin = next((c for c in self.s.checkins
                            if c.assignment_id == a.id), None)
            shifts_view.append({
                "assignment_id": a.id,
                "_start": sh.start,
                "service_object": sh.service_object,
                "position": pos.name,
                "sensitive": pos.sensitive,
                "time": timeutil.shift_label(sh.start, sh.end),
                "original_volunteer_id": a.original_volunteer_id,
                "current_volunteer_id": a.volunteer_id,
                "original_authorized_by": a.original_authorized_by,
                "role": "当前责任人" if a.volunteer_id == volunteer_id else "原责任人（已替出）",
                "substitution_chain": chain,
                "checkin": None if checkin is None else {
                    "status": checkin.status,
                    "check_in_at": checkin.check_in_at,
                    "source": checkin.source,
                    "sealed": checkin.sealed,
                    "review_id": checkin.review_id,
                },
                "schedule_basis": a.matched,
            })
        shifts_view.sort(key=lambda x: x.pop("_start"))
        entries = [e for e in self.s.ledger if e.volunteer_id == volunteer_id]
        award = next((x for x in reversed(self.s.awards)
                      if x.volunteer_id == volunteer_id and not x.superseded), None)
        return {
            "volunteer_id": v.id,
            "name": v.name,
            "school_id": v.school_id,
            "verified_languages": sorted(v.verified_languages()),
            "verified_credentials": sorted(v.verified_credential_types()),
            "trained_positions": sorted(v.trained_positions()),
            "shifts": shifts_view,
            "ledger": [
                {"id": e.id, "shift_id": e.shift_id, "minutes": e.minutes,
                 "basis": e.basis, "note": e.note}
                for e in entries
            ],
            "total_minutes": sum(e.minutes for e in entries),
            "award": None if award is None else {
                "level": award.level,
                "total_minutes": award.total_minutes,
                "basis": award.basis,
                "reasons": award.reasons,
            },
        }


def _status_cn(status: str) -> str:
    return {"approved": "通过", "rejected": "驳回", "pending": "待批"}.get(status, status)
