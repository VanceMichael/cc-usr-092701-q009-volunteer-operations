"""国际赛事志愿服务后台：报名、资质、排班、替班、签到复核、表彰与重启恢复。

核心规则：
- 一个志愿者不能被分到时间重叠的班次，班次间须满足最小休息间隔；
- 机场抵离、涉外采访等敏感岗位只接受证件与语言能力均已核验的志愿者；
- 替班保留原授权与新责任人，服务时长和奖励跟随实际出勤者；
- 漏签、迟到、补录一律进入复核，不直接改写出勤；封存后的签到不可修改；
- 高校管理员只能查看本校数据，组委会可跨队调度；
- 漏签扫描、到岗提醒、表彰计算以任务落库，重启后由 recover 继续推进。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .domain import (
    CHECK_IN_EARLY_MINUTES,
    EARLY_START_HOUR,
    LANGUAGE_LEVELS,
    LATE_GRACE_MINUTES,
    MAX_CONSECUTIVE_EARLY_DAYS,
    MIN_REST_HOURS,
    MISSED_SCAN_DELAY_MINUTES,
    REMINDER_LEAD_HOURS,
    ROLE_COMMITTEE,
    ROLE_SERVICE_CENTER,
    ROLE_UNIVERSITY_ADMIN,
    Actor,
    ConflictError,
    IneligibleAssignment,
    NotFound,
    PermissionDenied,
    SealedRecordError,
    ValidationError,
)
from .store import connect

STAFF = (ROLE_SERVICE_CENTER, ROLE_COMMITTEE)
READERS = (ROLE_SERVICE_CENTER, ROLE_COMMITTEE, ROLE_UNIVERSITY_ADMIN)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _hours_between(start: str, end: str) -> float:
    return (_parse(end) - _parse(start)).total_seconds() / 3600.0


class VolunteerService:
    """赛事服务中心使用的后台门面；所有状态写入 SQLite，重启后可恢复。"""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = connect(db_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def _now(at: datetime | None) -> datetime:
        return at if at is not None else datetime.now()

    @staticmethod
    def _require(actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied(f"角色 {actor.role} 无权执行该操作")

    def _one(self, sql: str, params: tuple, what: str):
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"{what}不存在")
        return row

    def _volunteer(self, volunteer_id: int):
        return self._one("SELECT * FROM volunteers WHERE id=?", (volunteer_id,), "志愿者")

    def _shift(self, shift_id: int):
        return self._one("SELECT * FROM shifts WHERE id=?", (shift_id,), "班次")

    def _post(self, post_id: int):
        return self._one("SELECT * FROM posts WHERE id=?", (post_id,), "岗位")

    def _assignment(self, assignment_id: int):
        return self._one("SELECT * FROM assignments WHERE id=?", (assignment_id,), "分配")

    def _audit(self, actor: Actor, action: str, detail: str, at: datetime | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit(actor, action, detail, at) VALUES (?,?,?,?)",
            (f"{actor.role}:{actor.name}", action, detail, _iso(self._now(at))),
        )

    def _enqueue(self, kind: str, payload: dict, run_after: datetime) -> None:
        self.conn.execute(
            "INSERT INTO jobs(kind, payload, run_after, state, attempts) VALUES (?,?,?,'pending',0)",
            (kind, json.dumps(payload, ensure_ascii=False), _iso(run_after)),
        )

    # ------------------------------------------------------------------
    # 报名、语言、证件、培训与岗位资质
    # ------------------------------------------------------------------
    def register_volunteer(
        self,
        actor: Actor,
        name: str,
        school: str,
        languages: tuple[tuple[str, str], ...] = (),
        credentials: tuple[str, ...] = (),
    ) -> int:
        self._require(actor, *STAFF)
        if not name.strip() or not school.strip():
            raise ValidationError("姓名与学校不能为空")
        cur = self.conn.execute("INSERT INTO volunteers(name, school) VALUES (?,?)", (name, school))
        volunteer_id = cur.lastrowid
        for language, level in languages:
            self._upsert_language(volunteer_id, language, level, verified=False)
        for kind in credentials:
            self.conn.execute(
                "INSERT OR IGNORE INTO credentials(volunteer_id, kind, verified) VALUES (?,?,0)",
                (volunteer_id, kind),
            )
        self._audit(actor, "register_volunteer", f"志愿者#{volunteer_id} {name}（{school}）")
        self.conn.commit()
        return volunteer_id

    def _upsert_language(self, volunteer_id: int, language: str, level: str, verified: bool) -> None:
        if level not in LANGUAGE_LEVELS:
            raise ValidationError(f"未知语言等级：{level}")
        self.conn.execute(
            "INSERT INTO languages(volunteer_id, language, level, verified) VALUES (?,?,?,?) "
            "ON CONFLICT(volunteer_id, language) DO UPDATE SET level=excluded.level",
            (volunteer_id, language, level, int(verified)),
        )

    def add_language(self, actor: Actor, volunteer_id: int, language: str, level: str) -> None:
        self._require(actor, *STAFF)
        self._volunteer(volunteer_id)
        self._upsert_language(volunteer_id, language, level, verified=False)
        self._audit(actor, "add_language", f"志愿者#{volunteer_id} {language}/{level}")
        self.conn.commit()

    def verify_language(self, actor: Actor, volunteer_id: int, language: str) -> None:
        """核验志愿者的语言能力，敏感岗位只接受核验过的语言。"""
        self._require(actor, *STAFF)
        row = self._one(
            "SELECT * FROM languages WHERE volunteer_id=? AND language=?",
            (volunteer_id, language),
            "语言记录",
        )
        self.conn.execute("UPDATE languages SET verified=1 WHERE id=?", (row["id"],))
        self._audit(actor, "verify_language", f"志愿者#{volunteer_id} {language}")
        self.conn.commit()

    def verify_credential(self, actor: Actor, volunteer_id: int, kind: str, at: datetime | None = None) -> None:
        """核验志愿者证件（如护照、外事服务证）。"""
        self._require(actor, *STAFF)
        self._volunteer(volunteer_id)
        self.conn.execute(
            "INSERT INTO credentials(volunteer_id, kind, verified, verified_at) VALUES (?,?,1,?) "
            "ON CONFLICT(volunteer_id, kind) DO UPDATE SET verified=1, verified_at=excluded.verified_at",
            (volunteer_id, kind, _iso(self._now(at))),
        )
        self._audit(actor, "verify_credential", f"志愿者#{volunteer_id} {kind}", at)
        self.conn.commit()

    def complete_training(
        self,
        actor: Actor,
        volunteer_id: int,
        course: str,
        grants_skill: str,
        at: datetime | None = None,
    ) -> None:
        """完成培训并授予对应岗位资质。"""
        self._require(actor, *STAFF)
        self._volunteer(volunteer_id)
        self.conn.execute(
            "INSERT INTO trainings(volunteer_id, course, completed_at) VALUES (?,?,?)",
            (volunteer_id, course, _iso(self._now(at))),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO qualifications(volunteer_id, skill, source) VALUES (?,?,?)",
            (volunteer_id, grants_skill, f"培训:{course}"),
        )
        self._audit(actor, "complete_training", f"志愿者#{volunteer_id} {course}→{grants_skill}", at)
        self.conn.commit()

    def add_qualification(self, actor: Actor, volunteer_id: int, skill: str, source: str = "manual") -> None:
        self._require(actor, *STAFF)
        self._volunteer(volunteer_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO qualifications(volunteer_id, skill, source) VALUES (?,?,?)",
            (volunteer_id, skill, source),
        )
        self._audit(actor, "add_qualification", f"志愿者#{volunteer_id} {skill}")
        self.conn.commit()

    # ------------------------------------------------------------------
    # 岗位、服务对象与班次
    # ------------------------------------------------------------------
    def create_post(
        self,
        actor: Actor,
        name: str,
        sensitive: bool = False,
        required_skill: str | None = None,
        required_language: str | None = None,
        min_language_level: str | None = None,
    ) -> int:
        self._require(actor, *STAFF)
        if min_language_level is not None and min_language_level not in LANGUAGE_LEVELS:
            raise ValidationError(f"未知语言等级：{min_language_level}")
        cur = self.conn.execute(
            "INSERT INTO posts(name, sensitive, required_skill, required_language, min_language_level) "
            "VALUES (?,?,?,?,?)",
            (name, int(sensitive), required_skill, required_language, min_language_level),
        )
        self._audit(actor, "create_post", f"岗位#{cur.lastrowid} {name}")
        self.conn.commit()
        return cur.lastrowid

    def create_service_target(self, actor: Actor, name: str, kind: str) -> int:
        self._require(actor, *STAFF)
        cur = self.conn.execute("INSERT INTO service_targets(name, kind) VALUES (?,?)", (name, kind))
        self._audit(actor, "create_service_target", f"服务对象#{cur.lastrowid} {name}")
        self.conn.commit()
        return cur.lastrowid

    def create_shift(
        self,
        actor: Actor,
        post_id: int,
        target_id: int,
        start: datetime,
        end: datetime,
        location: str = "",
    ) -> int:
        self._require(actor, *STAFF)
        self._post(post_id)
        self._one("SELECT * FROM service_targets WHERE id=?", (target_id,), "服务对象")
        if end <= start:
            raise ValidationError("班次结束必须晚于开始")
        cur = self.conn.execute(
            "INSERT INTO shifts(post_id, target_id, start, end, location) VALUES (?,?,?,?,?)",
            (post_id, target_id, _iso(start), _iso(end), location),
        )
        shift_id = cur.lastrowid
        # 班次结束后由系统扫描漏签；任务落库，重启后 recover 继续推进
        self._enqueue("missed_scan", {"shift_id": shift_id}, end + timedelta(minutes=MISSED_SCAN_DELAY_MINUTES))
        self._audit(actor, "create_shift", f"班次#{shift_id} {_iso(start)}~{_iso(end)}")
        self.conn.commit()
        return shift_id

    # ------------------------------------------------------------------
    # 可解释排班
    # ------------------------------------------------------------------
    @staticmethod
    def _check(ok: bool, code: str, detail: str) -> dict:
        return {"code": code, "passed": bool(ok), "detail": detail}

    def _active_shifts(self, volunteer_id: int):
        return self.conn.execute(
            "SELECT s.* FROM assignments a JOIN shifts s ON s.id=a.shift_id "
            "WHERE a.volunteer_id=? AND a.state='active'",
            (volunteer_id,),
        ).fetchall()

    def _early_streak(self, volunteer_id: int, before_date) -> int:
        """before_date 之前连续清晨出勤的天数。"""
        days = set()
        for shift in self._active_shifts(volunteer_id):
            start = _parse(shift["start"])
            if start.hour < EARLY_START_HOUR:
                days.add(start.date())
        streak = 0
        day = before_date - timedelta(days=1)
        while day in days:
            streak += 1
            day -= timedelta(days=1)
        return streak

    def evaluate_candidate(self, shift_id: int, volunteer_id: int) -> list[dict]:
        """逐项检查志愿者能否承担班次，结果用于排班解释与分配校验。"""
        shift = self._shift(shift_id)
        post = self._post(shift["post_id"])
        self._volunteer(volunteer_id)
        checks: list[dict] = []

        # 1. 岗位资质（可由培训获得）
        if post["required_skill"]:
            ok = self.conn.execute(
                "SELECT 1 FROM qualifications WHERE volunteer_id=? AND skill=?",
                (volunteer_id, post["required_skill"]),
            ).fetchone()
            checks.append(
                self._check(
                    ok is not None,
                    "QUALIFICATION",
                    f"岗位要求资质「{post['required_skill']}」" + ("，已具备" if ok else "，未具备"),
                )
            )

        # 2. 语言能力与等级
        if post["required_language"]:
            row = self.conn.execute(
                "SELECT * FROM languages WHERE volunteer_id=? AND language=?",
                (volunteer_id, post["required_language"]),
            ).fetchone()
            need = post["min_language_level"] or "basic"
            level_ok = row is not None and LANGUAGE_LEVELS[row["level"]] >= LANGUAGE_LEVELS[need]
            have = f"{row['level']}" if row else "无"
            checks.append(
                self._check(
                    level_ok,
                    "LANGUAGE_LEVEL",
                    f"岗位要求{post['required_language']}≥{need}，实际{have}",
                )
            )
            if post["sensitive"]:
                verified_ok = row is not None and row["verified"]
                checks.append(
                    self._check(
                        verified_ok,
                        "LANGUAGE_VERIFIED",
                        "敏感岗位要求语言能力已核验" + ("，已核验" if verified_ok else "，未核验"),
                    )
                )

        # 3. 敏感岗位：证件与语言必须均已核验
        if post["sensitive"]:
            cred = self.conn.execute(
                "SELECT 1 FROM credentials WHERE volunteer_id=? AND verified=1",
                (volunteer_id,),
            ).fetchone()
            checks.append(
                self._check(
                    cred is not None,
                    "CREDENTIAL_VERIFIED",
                    "敏感岗位要求证件已核验" + ("，已核验" if cred else "，未核验"),
                )
            )
            if not post["required_language"]:
                any_lang = self.conn.execute(
                    "SELECT 1 FROM languages WHERE volunteer_id=? AND verified=1",
                    (volunteer_id,),
                ).fetchone()
                checks.append(
                    self._check(
                        any_lang is not None,
                        "LANGUAGE_VERIFIED",
                        "敏感岗位要求至少一门语言已核验" + ("，已核验" if any_lang else "，未核验"),
                    )
                )

        # 4. 时间重叠与休息间隔
        start, end = _parse(shift["start"]), _parse(shift["end"])
        for other in self._active_shifts(volunteer_id):
            o_start, o_end = _parse(other["start"]), _parse(other["end"])
            if start < o_end and o_start < end:
                checks.append(
                    self._check(
                        False,
                        "OVERLAP",
                        f"与班次#{other['id']}（{other['start']}~{other['end']}）时间重叠",
                    )
                )
            else:
                gap = (start - o_end) if o_end <= start else (o_start - end)
                gap_hours = gap.total_seconds() / 3600.0
                ok = gap_hours >= MIN_REST_HOURS
                checks.append(
                    self._check(
                        ok,
                        "REST_INTERVAL",
                        f"与班次#{other['id']}间隔{gap_hours:.1f}小时（要求≥{MIN_REST_HOURS}小时）"
                        + ("" if ok else "，休息不足"),
                    )
                )

        # 5. 连续清晨出勤保护
        if start.hour < EARLY_START_HOUR:
            streak = self._early_streak(volunteer_id, start.date())
            ok = streak + 1 <= MAX_CONSECUTIVE_EARLY_DAYS
            checks.append(
                self._check(
                    ok,
                    "EARLY_STREAK",
                    f"此前已连续{streak}天清晨出勤（上限{MAX_CONSECUTIVE_EARLY_DAYS}天）"
                    + ("" if ok else "，超出保护上限"),
                )
            )
        return checks

    @staticmethod
    def _eligible(checks: list[dict]) -> bool:
        return all(c["passed"] for c in checks)

    def _fairness_score(self, volunteer_id: int, shift) -> dict:
        assigned_hours = sum(_hours_between(s["start"], s["end"]) for s in self._active_shifts(volunteer_id))
        return {
            "assigned_hours": round(assigned_hours, 2),
            "early_streak": self._early_streak(volunteer_id, _parse(shift["start"]).date()),
        }

    def plan_shift(self, shift_id: int) -> dict:
        """给出全班志愿者的可解释排班结果：谁可排、谁不可排、为什么。"""
        shift = self._shift(shift_id)
        post = self._post(shift["post_id"])
        candidates = []
        for vol in self.conn.execute("SELECT * FROM volunteers ORDER BY id").fetchall():
            checks = self.evaluate_candidate(shift_id, vol["id"])
            entry = {
                "volunteer_id": vol["id"],
                "name": vol["name"],
                "school": vol["school"],
                "eligible": self._eligible(checks),
                "checks": checks,
                "score": None,
            }
            if entry["eligible"]:
                entry["score"] = self._fairness_score(vol["id"], shift)
            candidates.append(entry)
        candidates.sort(
            key=lambda c: (
                not c["eligible"],
                c["score"]["assigned_hours"] if c["score"] else 0,
                c["score"]["early_streak"] if c["score"] else 0,
                c["volunteer_id"],
            )
        )
        return {
            "shift": dict(shift),
            "post": dict(post),
            "rules": [
                "同一志愿者不得承担时间重叠的班次",
                f"班次间最小休息间隔{MIN_REST_HOURS}小时",
                f"连续清晨出勤不超过{MAX_CONSECUTIVE_EARLY_DAYS}天",
                "敏感岗位（如机场抵离、涉外采访）须证件与语言能力均已核验",
                "岗位资质可由对应培训获得",
            ],
            "candidates": candidates,
        }

    def assign_shift(
        self,
        actor: Actor,
        shift_id: int,
        volunteer_id: int,
        reason: str = "",
        at: datetime | None = None,
    ) -> int:
        """把班次分配给志愿者；不满足要求时抛出携带逐项原因的异常。"""
        self._require(actor, *STAFF)
        shift = self._shift(shift_id)
        checks = self.evaluate_candidate(shift_id, volunteer_id)
        if not self._eligible(checks):
            raise IneligibleAssignment(volunteer_id, shift_id, checks)
        now = self._now(at)
        cur = self.conn.execute(
            "INSERT INTO assignments(shift_id, volunteer_id, state, supersedes_id, reason, explanation, created_by, created_at) "
            "VALUES (?,?, 'active', NULL, ?,?,?,?)",
            (
                shift_id,
                volunteer_id,
                reason,
                json.dumps(checks, ensure_ascii=False),
                actor.name,
                _iso(now),
            ),
        )
        assignment_id = cur.lastrowid
        remind_at = _parse(shift["start"]) - timedelta(hours=REMINDER_LEAD_HOURS)
        self._enqueue("reminder", {"assignment_id": assignment_id}, remind_at)
        self._audit(actor, "assign_shift", f"班次#{shift_id}→志愿者#{volunteer_id}（{reason or '手动排班'}）", now)
        self.conn.commit()
        return assignment_id

    def auto_assign(self, actor: Actor, shift_id: int, at: datetime | None = None) -> dict:
        """自动排班：在符合条件者中优先安排累计排班最少、清晨连班最少的人。"""
        self._require(actor, *STAFF)
        plan = self.plan_shift(shift_id)
        eligible = [c for c in plan["candidates"] if c["eligible"]]
        if not eligible:
            raise ConflictError(f"班次#{shift_id}没有满足条件的志愿者")
        best = min(
            eligible,
            key=lambda c: (c["score"]["assigned_hours"], c["score"]["early_streak"], c["volunteer_id"]),
        )
        assignment_id = self.assign_shift(actor, shift_id, best["volunteer_id"], reason="自动排班", at=at)
        return {
            "assignment_id": assignment_id,
            "volunteer_id": best["volunteer_id"],
            "reason": "符合条件者中累计排班时长最少、连续清晨出勤最少",
            "checks": best["checks"],
            "score": best["score"],
        }

    # ------------------------------------------------------------------
    # 替班：保留原授权与新责任人
    # ------------------------------------------------------------------
    def request_substitution(
        self,
        actor: Actor,
        assignment_id: int,
        to_volunteer_id: int,
        reason: str,
        at: datetime | None = None,
    ) -> int:
        self._require(actor, *STAFF)
        assignment = self._assignment(assignment_id)
        if assignment["state"] != "active":
            raise ConflictError("只有在岗分配可以发起替班")
        shift = self._shift(assignment["shift_id"])
        if self._now(at) >= _parse(shift["end"]):
            raise ConflictError("班次已结束，不能替班")
        checks = self.evaluate_candidate(assignment["shift_id"], to_volunteer_id)
        if not self._eligible(checks):
            raise IneligibleAssignment(to_volunteer_id, assignment["shift_id"], checks)
        cur = self.conn.execute(
            "INSERT INTO substitutions(shift_id, from_assignment_id, from_volunteer_id, to_volunteer_id, "
            "reason, state, requested_by, created_at) VALUES (?,?,?,?,?, 'pending', ?,?)",
            (
                assignment["shift_id"],
                assignment_id,
                assignment["volunteer_id"],
                to_volunteer_id,
                reason,
                actor.name,
                _iso(self._now(at)),
            ),
        )
        self._audit(
            actor,
            "request_substitution",
            f"分配#{assignment_id}：志愿者#{assignment['volunteer_id']}→#{to_volunteer_id}（{reason}）",
            at,
        )
        self.conn.commit()
        return cur.lastrowid

    def approve_substitution(self, actor: Actor, substitution_id: int, at: datetime | None = None) -> int:
        """批准替班：原分配置为 substituted，新责任人挂出继承分配，双方奖励重算。"""
        self._require(actor, *STAFF)
        sub = self._one("SELECT * FROM substitutions WHERE id=?", (substitution_id,), "替班申请")
        if sub["state"] != "pending":
            raise ConflictError("替班申请已处理")
        old = self._assignment(sub["from_assignment_id"])
        if old["state"] != "active":
            raise ConflictError("原分配已变更，替班失效")
        now = self._now(at)
        shift = self._shift(sub["shift_id"])
        if now >= _parse(shift["end"]):
            raise ConflictError("班次已结束，不能替班")
        checks = self.evaluate_candidate(sub["shift_id"], sub["to_volunteer_id"])
        if not self._eligible(checks):
            raise IneligibleAssignment(sub["to_volunteer_id"], sub["shift_id"], checks)
        self.conn.execute("UPDATE assignments SET state='substituted' WHERE id=?", (old["id"],))
        explanation = {
            "substitution_id": substitution_id,
            "原授权志愿者": sub["from_volunteer_id"],
            "新责任人": sub["to_volunteer_id"],
            "替班原因": sub["reason"],
        }
        cur = self.conn.execute(
            "INSERT INTO assignments(shift_id, volunteer_id, state, supersedes_id, reason, explanation, created_by, created_at) "
            "VALUES (?,?, 'active', ?,?,?,?,?)",
            (
                sub["shift_id"],
                sub["to_volunteer_id"],
                old["id"],
                f"替班：{sub['reason']}",
                json.dumps(explanation, ensure_ascii=False),
                actor.name,
                _iso(now),
            ),
        )
        new_assignment_id = cur.lastrowid
        self.conn.execute(
            "UPDATE substitutions SET state='approved', to_assignment_id=?, decided_by=?, decided_at=? WHERE id=?",
            (new_assignment_id, actor.name, _iso(now), substitution_id),
        )
        remind_at = _parse(shift["start"]) - timedelta(hours=REMINDER_LEAD_HOURS)
        self._enqueue("reminder", {"assignment_id": new_assignment_id}, remind_at)
        # 时长与奖励跟随实际出勤者：双方都需要重算
        for vid in (sub["from_volunteer_id"], sub["to_volunteer_id"]):
            self._enqueue("award_compute", {"volunteer_id": vid}, now)
        self._audit(
            actor,
            "approve_substitution",
            f"替班#{substitution_id}：分配#{old['id']}→#{new_assignment_id}",
            now,
        )
        self.conn.commit()
        return new_assignment_id

    def reject_substitution(self, actor: Actor, substitution_id: int, note: str = "", at: datetime | None = None) -> None:
        self._require(actor, *STAFF)
        sub = self._one("SELECT * FROM substitutions WHERE id=?", (substitution_id,), "替班申请")
        if sub["state"] != "pending":
            raise ConflictError("替班申请已处理")
        self.conn.execute(
            "UPDATE substitutions SET state='rejected', decided_by=?, decided_at=? WHERE id=?",
            (actor.name, _iso(self._now(at)), substitution_id),
        )
        self._audit(actor, "reject_substitution", f"替班#{substitution_id}（{note}）", at)
        self.conn.commit()

    # ------------------------------------------------------------------
    # 签到、复核与封存
    # ------------------------------------------------------------------
    def _attendance_of(self, assignment_id: int):
        return self.conn.execute(
            "SELECT * FROM attendance WHERE assignment_id=?", (assignment_id,)
        ).fetchone()

    def check_in(self, actor: Actor, assignment_id: int, at: datetime) -> int:
        """现场签到；超过宽限期记为迟到并进入复核，不直接记出勤。"""
        self._require(actor, *STAFF)
        assignment = self._assignment(assignment_id)
        if assignment["state"] != "active":
            raise ConflictError("分配不在岗，不能签到")
        shift = self._shift(assignment["shift_id"])
        start, end = _parse(shift["start"]), _parse(shift["end"])
        if at < start - timedelta(minutes=CHECK_IN_EARLY_MINUTES):
            raise ValidationError("未到签到时间")
        if at > end:
            raise ValidationError("班次已结束，不能签到")
        if self._attendance_of(assignment_id) is not None:
            raise ConflictError("已有签到记录")
        late = at > start + timedelta(minutes=LATE_GRACE_MINUTES)
        cur = self.conn.execute(
            "INSERT INTO attendance(assignment_id, volunteer_id, shift_id, check_in, state, origin) "
            "VALUES (?,?,?,?,?, 'self')",
            (
                assignment_id,
                assignment["volunteer_id"],
                assignment["shift_id"],
                _iso(at),
                "pending" if late else "effective",
            ),
        )
        attendance_id = cur.lastrowid
        if late:
            self._open_review(attendance_id, "late", {"check_in": _iso(at)}, "迟到签到，等待复核", at)
        self._audit(actor, "check_in", f"分配#{assignment_id} 签到 {_iso(at)}{'（迟到）' if late else ''}", at)
        self.conn.commit()
        return attendance_id

    def check_out(self, actor: Actor, assignment_id: int, at: datetime) -> None:
        self._require(actor, *STAFF)
        self._assignment(assignment_id)
        attendance = self._attendance_of(assignment_id)
        if attendance is None:
            raise NotFound("尚未签到")
        if attendance["sealed"]:
            raise SealedRecordError("签到已封存，不能修改")
        if attendance["state"] == "rejected":
            raise ConflictError("签到记录已被复核驳回")
        if attendance["check_out"]:
            raise ConflictError("已签退")
        if at <= _parse(attendance["check_in"]):
            raise ValidationError("签退必须晚于签到")
        self.conn.execute("UPDATE attendance SET check_out=? WHERE id=?", (_iso(at), attendance["id"]))
        if attendance["state"] == "effective":
            self._enqueue("award_compute", {"volunteer_id": attendance["volunteer_id"]}, at)
        self._audit(actor, "check_out", f"分配#{assignment_id} 签退 {_iso(at)}", at)
        self.conn.commit()

    def _open_review(self, attendance_id: int, kind: str, payload: dict, note: str, at: datetime | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO review_cases(attendance_id, kind, state, payload, note, created_at) "
            "VALUES (?,?, 'open', ?,?,?)",
            (attendance_id, kind, json.dumps(payload, ensure_ascii=False), note, _iso(self._now(at))),
        )
        return cur.lastrowid

    def backfill_attendance(
        self,
        actor: Actor,
        assignment_id: int,
        check_in: datetime,
        check_out: datetime,
        reason: str,
        at: datetime | None = None,
    ) -> int:
        """补录考勤：只生成复核单，批准前不改写出勤。返回复核单号。"""
        self._require(actor, *STAFF)
        assignment = self._assignment(assignment_id)
        if check_out <= check_in:
            raise ValidationError("补录的签退必须晚于签到")
        attendance = self._attendance_of(assignment_id)
        if attendance is not None and attendance["sealed"]:
            raise SealedRecordError("签到已封存，不能补录")
        if attendance is not None and attendance["state"] == "effective":
            raise ConflictError("已有有效考勤，无需补录")
        if attendance is None:
            cur = self.conn.execute(
                "INSERT INTO attendance(assignment_id, volunteer_id, shift_id, state, origin) "
                "VALUES (?,?,?, 'pending', 'backfill')",
                (assignment_id, assignment["volunteer_id"], assignment["shift_id"]),
            )
            attendance_id = cur.lastrowid
        else:
            attendance_id = attendance["id"]
        if self.conn.execute(
            "SELECT 1 FROM review_cases WHERE attendance_id=? AND state='open'", (attendance_id,)
        ).fetchone():
            raise ConflictError("已存在待复核记录")
        case_id = self._open_review(
            attendance_id,
            "backfill",
            {"check_in": _iso(check_in), "check_out": _iso(check_out), "reason": reason},
            f"补录申请：{reason}",
            at,
        )
        self._audit(actor, "backfill_attendance", f"分配#{assignment_id} 补录待复核（{reason}）", at)
        self.conn.commit()
        return case_id

    def review_case_decision(
        self,
        actor: Actor,
        case_id: int,
        approve: bool,
        note: str = "",
        at: datetime | None = None,
    ) -> None:
        """复核迟到、漏签、补录；封存后的签到任何人不能改写。"""
        self._require(actor, *STAFF)
        case = self._one("SELECT * FROM review_cases WHERE id=?", (case_id,), "复核单")
        if case["state"] != "open":
            raise ConflictError("复核单已处理")
        attendance = self._one("SELECT * FROM attendance WHERE id=?", (case["attendance_id"],), "考勤记录")
        if attendance["sealed"]:
            raise SealedRecordError("签到已封存，复核不能改写")
        now = self._now(at)
        if approve:
            if case["kind"] == "late":
                self.conn.execute("UPDATE attendance SET state='effective' WHERE id=?", (attendance["id"],))
            elif case["kind"] == "missed":
                self.conn.execute("UPDATE attendance SET state='absent' WHERE id=?", (attendance["id"],))
            elif case["kind"] == "backfill":
                payload = json.loads(case["payload"])
                self.conn.execute(
                    "UPDATE attendance SET check_in=?, check_out=?, state='effective', origin='backfill' WHERE id=?",
                    (payload["check_in"], payload["check_out"], attendance["id"]),
                )
            else:
                raise ValidationError(f"未知复核类型：{case['kind']}")
        else:
            self.conn.execute("UPDATE attendance SET state='rejected' WHERE id=?", (attendance["id"],))
        self.conn.execute(
            "UPDATE review_cases SET state=?, decided_by=?, decided_at=?, note=note||? WHERE id=?",
            ("approved" if approve else "rejected", actor.name, _iso(now), f" {note}".rstrip(), case_id),
        )
        self._enqueue("award_compute", {"volunteer_id": attendance["volunteer_id"]}, now)
        self._audit(
            actor,
            "review_case",
            f"复核单#{case_id}（{case['kind']}）{'批准' if approve else '驳回'}",
            now,
        )
        self.conn.commit()

    def seal_shift_attendance(self, actor: Actor, shift_id: int, at: datetime | None = None) -> int:
        """封存班次考勤：须无待复核记录；封存后组委会也不能修改。"""
        self._require(actor, ROLE_SERVICE_CENTER)
        self._shift(shift_id)
        open_cases = self.conn.execute(
            "SELECT COUNT(*) AS c FROM review_cases rc JOIN attendance a ON a.id=rc.attendance_id "
            "WHERE a.shift_id=? AND rc.state='open'",
            (shift_id,),
        ).fetchone()["c"]
        if open_cases:
            raise ConflictError(f"班次#{shift_id}仍有{open_cases}条待复核记录，不能封存")
        cur = self.conn.execute("UPDATE attendance SET sealed=1 WHERE shift_id=?", (shift_id,))
        self._audit(actor, "seal_attendance", f"班次#{shift_id} 封存{cur.rowcount}条考勤", at)
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # 表彰计算
    # ------------------------------------------------------------------
    def _volunteer_stats(self, volunteer_id: int) -> dict:
        rows = self.conn.execute(
            "SELECT a.*, p.required_language, p.sensitive FROM attendance a "
            "JOIN shifts s ON s.id=a.shift_id JOIN posts p ON p.id=s.post_id "
            "WHERE a.volunteer_id=? AND a.state='effective' AND a.check_in IS NOT NULL AND a.check_out IS NOT NULL",
            (volunteer_id,),
        ).fetchall()
        hours = round(sum(_hours_between(r["check_in"], r["check_out"]) for r in rows), 2)
        absent = self.conn.execute(
            "SELECT COUNT(*) AS c FROM attendance WHERE volunteer_id=? AND state='absent'",
            (volunteer_id,),
        ).fetchone()["c"]
        verified_languages = [
            r["language"]
            for r in self.conn.execute(
                "SELECT language FROM languages WHERE volunteer_id=? AND verified=1", (volunteer_id,)
            ).fetchall()
        ]
        return {
            "hours": hours,
            "shift_ids": sorted({r["shift_id"] for r in rows}),
            "language_shifts": sum(1 for r in rows if r["required_language"] or r["sensitive"]),
            "absent": absent,
            "verified_languages": verified_languages,
        }

    AWARD_RULES = (
        ("star_of_service", "服务之星", lambda s: s["hours"] >= 24, "累计有效服务满24小时"),
        (
            "language_pioneer",
            "语言服务先锋",
            lambda s: s["language_shifts"] >= 2 and bool(s["verified_languages"]),
            "持核验语言完成2个以上语言/敏感岗位班次",
        ),
        (
            "full_attendance",
            "全勤保障",
            lambda s: len(s["shift_ids"]) >= 4 and s["absent"] == 0,
            "完成4个以上班次且无缺勤",
        ),
    )

    def _compute_awards(self, volunteer_id: int, now: datetime) -> list[str]:
        stats = self._volunteer_stats(volunteer_id)
        granted = []
        for kind, label, ok, rule in self.AWARD_RULES:
            exists = self.conn.execute(
                "SELECT 1 FROM awards WHERE volunteer_id=? AND kind=?", (volunteer_id, kind)
            ).fetchone()
            if ok(stats) and not exists:
                basis = {"rule": rule, **stats}
                self.conn.execute(
                    "INSERT INTO awards(volunteer_id, kind, label, basis, granted_at) VALUES (?,?,?,?,?)",
                    (volunteer_id, kind, label, json.dumps(basis, ensure_ascii=False), _iso(now)),
                )
                granted.append(kind)
        return granted

    def compute_awards(self, actor: Actor, volunteer_id: int | None = None, at: datetime | None = None) -> dict:
        """按当前有效考勤重算表彰；已授予的不重复。返回 {志愿者: 新授予列表}。"""
        self._require(actor, *STAFF)
        now = self._now(at)
        if volunteer_id is not None:
            ids = [volunteer_id]
        else:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM volunteers").fetchall()]
        result = {vid: self._compute_awards(vid, now) for vid in ids}
        self._audit(actor, "compute_awards", f"重算表彰：{result}", now)
        self.conn.commit()
        return result

    # ------------------------------------------------------------------
    # 重启恢复：推进未完成的签到扫描、提醒与表彰计算
    # ------------------------------------------------------------------
    def recover(self, now: datetime) -> dict:
        """执行所有到期的待办任务；系统重启后调用即可继续推进。"""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE state='pending' AND run_after<=? ORDER BY id", (_iso(now),)
        ).fetchall()
        ran, failed = 0, 0
        for row in rows:
            try:
                self._run_job(row, now)
            except Exception as exc:  # noqa: BLE001 - 记录失败任务，避免阻塞其他任务
                self.conn.execute(
                    "UPDATE jobs SET attempts=attempts+1, state='failed' WHERE id=?", (row["id"],)
                )
                self.conn.execute(
                    "INSERT INTO audit(actor, action, detail, at) VALUES ('system', 'job_failed', ?, ?)",
                    (f"任务#{row['id']} {row['kind']}：{exc}", _iso(now)),
                )
                failed += 1
            else:
                self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (row["id"],))
                ran += 1
        self.conn.commit()
        return {"ran": ran, "failed": failed}

    def _run_job(self, row, now: datetime) -> None:
        payload = json.loads(row["payload"])
        if row["kind"] == "missed_scan":
            self._job_missed_scan(payload, now)
        elif row["kind"] == "reminder":
            self._job_reminder(payload, now)
        elif row["kind"] == "award_compute":
            self._compute_awards(payload["volunteer_id"], now)
        else:
            raise ValidationError(f"未知任务类型：{row['kind']}")

    def _job_missed_scan(self, payload: dict, now: datetime) -> None:
        """班次结束仍无签到记录的在岗分配 → 生成漏签复核单。"""
        rows = self.conn.execute(
            "SELECT a.* FROM assignments a WHERE a.shift_id=? AND a.state='active' "
            "AND NOT EXISTS (SELECT 1 FROM attendance t WHERE t.assignment_id=a.id)",
            (payload["shift_id"],),
        ).fetchall()
        for assignment in rows:
            cur = self.conn.execute(
                "INSERT INTO attendance(assignment_id, volunteer_id, shift_id, state, origin) "
                "VALUES (?,?,?, 'pending', 'missed')",
                (assignment["id"], assignment["volunteer_id"], assignment["shift_id"]),
            )
            self._open_review(cur.lastrowid, "missed", {}, "班次结束未签到，等待复核", now)

    def _job_reminder(self, payload: dict, now: datetime) -> None:
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE id=?", (payload["assignment_id"],)
        ).fetchone()
        if assignment is None or assignment["state"] != "active":
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO reminders(assignment_id, volunteer_id, shift_id, message, sent_at) "
            "VALUES (?,?,?,?,?)",
            (
                assignment["id"],
                assignment["volunteer_id"],
                assignment["shift_id"],
                f"班次#{assignment['shift_id']} 即将开始，请按时到岗",
                _iso(now),
            ),
        )

    # ------------------------------------------------------------------
    # 查询：高校数据隔离与志愿者全景
    # ------------------------------------------------------------------
    def _check_school_scope(self, actor: Actor, volunteer) -> None:
        if actor.role == ROLE_UNIVERSITY_ADMIN and actor.school != volunteer["school"]:
            raise PermissionDenied("高校管理员只能查看本校数据")

    def list_volunteers(self, actor: Actor) -> list[dict]:
        self._require(actor, *READERS)
        if actor.role == ROLE_UNIVERSITY_ADMIN:
            rows = self.conn.execute(
                "SELECT * FROM volunteers WHERE school=? ORDER BY id", (actor.school,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM volunteers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def open_review_cases(self, actor: Actor) -> list[dict]:
        self._require(actor, *STAFF)
        rows = self.conn.execute(
            "SELECT rc.*, a.volunteer_id, a.shift_id FROM review_cases rc "
            "JOIN attendance a ON a.id=rc.attendance_id WHERE rc.state='open' ORDER BY rc.id"
        ).fetchall()
        return [dict(r) for r in rows]

    def volunteer_report(self, actor: Actor, volunteer_id: int) -> dict:
        """按一名志愿者查询：真实班次、替班链、累计时长与奖励依据。"""
        self._require(actor, *READERS)
        volunteer = self._volunteer(volunteer_id)
        self._check_school_scope(actor, volunteer)

        assignments = []
        for row in self.conn.execute(
            "SELECT a.*, s.start, s.end, s.location, p.name AS post_name, p.sensitive, t.name AS target_name "
            "FROM assignments a JOIN shifts s ON s.id=a.shift_id "
            "JOIN posts p ON p.id=s.post_id JOIN service_targets t ON t.id=s.target_id "
            "WHERE a.volunteer_id=? ORDER BY s.start, a.id",
            (volunteer_id,),
        ).fetchall():
            item = dict(row)
            try:
                item["explanation"] = json.loads(item["explanation"])
            except (ValueError, TypeError):
                pass
            assignments.append(item)

        attendance = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM attendance WHERE volunteer_id=? ORDER BY id", (volunteer_id,)
            ).fetchall()
        ]
        substitutions = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM substitutions WHERE from_volunteer_id=? OR to_volunteer_id=? ORDER BY id",
                (volunteer_id, volunteer_id),
            ).fetchall()
        ]
        awards = [
            {**dict(r), "basis": json.loads(r["basis"])}
            for r in self.conn.execute(
                "SELECT * FROM awards WHERE volunteer_id=? ORDER BY id", (volunteer_id,)
            ).fetchall()
        ]
        stats = self._volunteer_stats(volunteer_id)
        return {
            "volunteer": dict(volunteer),
            "languages": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM languages WHERE volunteer_id=?", (volunteer_id,)
                ).fetchall()
            ],
            "credentials": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM credentials WHERE volunteer_id=?", (volunteer_id,)
                ).fetchall()
            ],
            "qualifications": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM qualifications WHERE volunteer_id=?", (volunteer_id,)
                ).fetchall()
            ],
            "assignments": assignments,
            "attendance": attendance,
            "substitutions": substitutions,
            "total_hours": stats["hours"],
            "awards": awards,
        }
