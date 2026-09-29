"""国际赛事志愿服务排班的领域对象。

覆盖报名、语言与岗位资质、班次、派班、替班链、签到、复核、
服务时长台账、表彰、通知与可持久化任务，所有对象均可序列化为 JSON 数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------- 基础档案

@dataclass
class School:
    """高校。"""
    id: str
    name: str


@dataclass
class LanguageAbility:
    """志愿者的语言能力；sensitive 岗位只承认 verified=True 的能力。"""
    code: str            # 如 fr、ar、ru
    name: str            # 如 法语
    level: str           # 如 A2 / B2 / C1
    verified: bool = False


@dataclass
class Credential:
    """证件/准入资质，例如制证中心出入证、机场隔离区通行证。"""
    type: str
    name: str
    verified: bool = False
    verified_at: Optional[str] = None


@dataclass
class Training:
    """岗位培训记录。"""
    position_id: str
    completed_at: str


@dataclass
class Volunteer:
    id: str
    name: str
    school_id: str
    languages: list[LanguageAbility] = field(default_factory=list)
    credentials: list[Credential] = field(default_factory=list)
    trainings: list[Training] = field(default_factory=list)

    def verified_languages(self) -> set[str]:
        return {lang.code for lang in self.languages if lang.verified}

    def verified_credential_types(self) -> set[str]:
        return {cred.type for cred in self.credentials if cred.verified}

    def trained_positions(self) -> set[str]:
        return {t.position_id for t in self.trainings}


# ---------------------------------------------------------------- 岗位班次

@dataclass
class Position:
    """岗位定义。

    sensitive=True 表示机场服务、涉外采访等敏感岗位：
    只能由证件与语言能力均核验通过的人承担。
    """
    id: str
    name: str
    sensitive: bool = False
    required_credentials: list[str] = field(default_factory=list)
    required_languages: list[str] = field(default_factory=list)
    require_training: bool = True
    min_rest_hours: float = 11.0
    early_morning: bool = False   # 是否属于清晨班（用于连续清晨出勤提示）


@dataclass
class Shift:
    """一个班次，挂靠在某个服务对象（媒体酒店、机场抵离、制证、招待会等）上。"""
    id: str
    service_object: str
    position_id: str
    start: str
    end: str
    headcount: int = 1
    language_needs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 派班替班

@dataclass
class Assignment:
    """派班记录（一个岗位名额）。

    发生替班时 volunteer_id 更新为新责任人，但 original_volunteer_id 与
    original_authorized_by 永久保留，体现“原授权 + 新责任人”。
    """
    id: str
    shift_id: str
    volunteer_id: str
    original_volunteer_id: str
    original_authorized_by: str
    created_at: str
    created_by: str
    matched: list[str] = field(default_factory=list)   # 排班依据（可解释）
    cancelled: bool = False


@dataclass
class Substitution:
    """替班申请/决定，按 seq 串成替班链。"""
    id: str
    assignment_id: str
    seq: int
    from_volunteer_id: str
    to_volunteer_id: str
    requested_by: str
    reason: str
    status: str = "pending"                 # pending / approved / rejected
    authorized_by: Optional[str] = None
    eligibility: Optional[dict] = None      # 替班人资格复核快照
    created_at: str = ""
    decided_at: Optional[str] = None


# ---------------------------------------------------------------- 签到复核

@dataclass
class CheckIn:
    """签到记录。sealed=True 后任何角色都不得改写。"""
    id: str
    assignment_id: str
    shift_id: str
    volunteer_id: str
    scheduled_start: str
    scheduled_end: str
    check_in_at: Optional[str] = None
    status: str = "normal"                  # normal / late / missed / backfill
    source: str = "self"                    # self / review
    sealed: bool = False
    review_id: Optional[str] = None
    created_at: str = ""


@dataclass
class Review:
    """漏签、迟到申诉、补录均进入复核，不直接改写出勤。"""
    id: str
    kind: str                               # missed / late_appeal / backfill
    assignment_id: str
    shift_id: str
    volunteer_id: str
    evidence: str
    raised_by: str
    status: str = "pending"                 # pending / approved / rejected
    reviewer: Optional[str] = None
    decision_note: Optional[str] = None
    claimed_check_in_at: Optional[str] = None
    created_at: str = ""
    decided_at: Optional[str] = None


@dataclass
class LedgerEntry:
    """服务时长台账。正数为计入，负数或对冲条目注明依据，表彰只认台账。"""
    id: str
    volunteer_id: str
    shift_id: str
    minutes: int
    basis: str                              # attendance / substitution / review
    ref_id: str                             # 签到或复核记录 id
    note: str = ""
    created_at: str = ""


# ---------------------------------------------------------------- 表彰通知

@dataclass
class Award:
    """表彰结果与逐条奖励依据。"""
    id: str
    volunteer_id: str
    level: str
    total_minutes: int
    created_at: str
    basis: list[str] = field(default_factory=list)   # 台账条目 id
    reasons: list[str] = field(default_factory=list)
    superseded: bool = False


@dataclass
class Notification:
    id: str
    volunteer_id: str
    kind: str                               # shift_reminder / review / award
    text: str
    created_at: str
    delivered: bool = False


@dataclass
class DurableTask:
    """持久化任务；重启后 pending/running 的任务继续推进。"""
    id: str
    type: str                               # reminder / missed_scan / awards
    run_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"                 # pending / running / done / failed
    attempts: int = 0
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""


# ---------------------------------------------------------------- 整体状态

@dataclass
class Settings:
    grace_minutes: int = 15                 # 超过计划开始时间多少分钟算迟到
    default_lead_hours: float = 12.0        # 提醒提前量
    award_levels: list[dict[str, Any]] = field(default_factory=lambda: [
        {"level": "铜牌志愿者", "min_minutes": 1200},
        {"level": "银牌志愿者", "min_minutes": 2400},
        {"level": "金牌志愿者", "min_minutes": 3600},
    ])


@dataclass
class State:
    schools: list[School] = field(default_factory=list)
    volunteers: list[Volunteer] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    substitutions: list[Substitution] = field(default_factory=list)
    checkins: list[CheckIn] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    awards: list[Award] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    tasks: list[DurableTask] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    counters: dict[str, int] = field(default_factory=dict)

    def to_data(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "State":
        if not data:
            return cls()
        data = dict(data)
        data["schools"] = [School(**x) for x in data.get("schools", [])]
        data["volunteers"] = [
            Volunteer(
                id=v["id"], name=v["name"], school_id=v["school_id"],
                languages=[LanguageAbility(**x) for x in v.get("languages", [])],
                credentials=[Credential(**x) for x in v.get("credentials", [])],
                trainings=[Training(**x) for x in v.get("trainings", [])],
            )
            for v in data.get("volunteers", [])
        ]
        data["positions"] = [Position(**x) for x in data.get("positions", [])]
        data["shifts"] = [Shift(**x) for x in data.get("shifts", [])]
        data["assignments"] = [Assignment(**x) for x in data.get("assignments", [])]
        data["substitutions"] = [Substitution(**x) for x in data.get("substitutions", [])]
        data["checkins"] = [CheckIn(**x) for x in data.get("checkins", [])]
        data["reviews"] = [Review(**x) for x in data.get("reviews", [])]
        data["ledger"] = [LedgerEntry(**x) for x in data.get("ledger", [])]
        data["awards"] = [Award(**x) for x in data.get("awards", [])]
        data["notifications"] = [Notification(**x) for x in data.get("notifications", [])]
        data["tasks"] = [DurableTask(**x) for x in data.get("tasks", [])]
        data["settings"] = Settings(**data.get("settings", {}))
        return cls(**data)
