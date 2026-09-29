"""领域基础：参与角色、业务错误与排班考勤常量。"""

from __future__ import annotations

from dataclasses import dataclass

# ---- 排班与考勤常量（可按赛事运行手册调整） ----
MIN_REST_HOURS = 8              # 同一志愿者两个班次之间的最小休息间隔（小时）
LATE_GRACE_MINUTES = 15         # 签到宽限分钟数，超过记为迟到并进入复核
EARLY_START_HOUR = 7            # 早于该时刻开班视为清晨班次
MAX_CONSECUTIVE_EARLY_DAYS = 3  # 连续清晨出勤天数上限
CHECK_IN_EARLY_MINUTES = 60     # 允许提前签到的分钟数
MISSED_SCAN_DELAY_MINUTES = 30  # 班次结束后多久判定漏签
REMINDER_LEAD_HOURS = 2         # 开班前几小时推送到岗提醒

# ---- 语言等级（用于岗位语言门槛比较） ----
LANGUAGE_LEVELS = {"basic": 1, "intermediate": 2, "advanced": 3, "native": 4}

# ---- 参与角色 ----
ROLE_SERVICE_CENTER = "service_center"      # 赛事服务中心
ROLE_COMMITTEE = "committee"                # 组委会调度员
ROLE_UNIVERSITY_ADMIN = "university_admin"  # 高校管理员
ROLE_VOLUNTEER = "volunteer"                # 志愿者本人


@dataclass(frozen=True)
class Actor:
    """一次操作的执行者；高校管理员必须带本校校名用于数据隔离。"""

    role: str
    name: str
    school: str | None = None


class DomainError(Exception):
    """业务规则错误的基类。"""


class NotFound(DomainError):
    """引用的业务对象不存在。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作或越权访问他校数据。"""


class ValidationError(DomainError):
    """输入不满足领域格式要求。"""


class ConflictError(DomainError):
    """当前状态不允许该操作（如重复签到、班次已结束）。"""


class SealedRecordError(DomainError):
    """签到已封存，任何人（包括组委会）都不能改写。"""


class IneligibleAssignment(DomainError):
    """志愿者不满足班次要求；携带逐项检查结果以便解释。"""

    def __init__(self, volunteer_id: int, shift_id: int, checks: list[dict]):
        self.volunteer_id = volunteer_id
        self.shift_id = shift_id
        self.checks = checks
        failed = "；".join(c["detail"] for c in checks if not c["passed"])
        super().__init__(f"志愿者#{volunteer_id} 不能承担班次#{shift_id}：{failed}")
