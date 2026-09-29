"""JSON 文件持久化与角色权限边界。

所有写操作立即落盘（崩溃后状态不丢）；重启后由服务层恢复未完成任务。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import State


class PermissionError_(Exception):
    """越权操作（与内置 PermissionError 区分，便于业务层捕获）。"""


@dataclass(frozen=True)
class Actor:
    """当前操作人员。

    role=committee 组委会调度员：可跨高校调度，但不能修改已封存签到。
    role=school    高校管理员：只能查看/操作本校志愿者数据。
    role=staff     赛事服务中心工作人员：可发起复核、计算表彰、查全局。
    role=volunteer 志愿者本人：只能看自己的画像。
    """
    id: str
    role: str
    name: str = ""
    school_id: str | None = None

    @property
    def cross_team(self) -> bool:
        return self.role in ("committee", "staff")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.state = State()
        if self.path.exists():
            self.reload()

    def reload(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.state = State.from_data(raw)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.state.to_data(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 编号

    def next_id(self, prefix: str) -> str:
        n = self.state.counters.get(prefix, 0) + 1
        self.state.counters[prefix] = n
        return f"{prefix}{n:04d}"

    # ------------------------------------------------------------ 可见性

    def visible_volunteer_ids(self, actor: Actor) -> set[str] | None:
        """返回该角色可见的志愿者 id 集合；None 表示全部可见。"""
        if actor.cross_team:
            return None
        if actor.role == "school":
            if not actor.school_id:
                raise PermissionError_("高校管理员必须绑定学校")
            return {
                v.id for v in self.state.volunteers if v.school_id == actor.school_id
            }
        if actor.role == "volunteer":
            return {actor.id}
        raise PermissionError_("未知角色")

    def assert_can_see_volunteer(self, actor: Actor, volunteer_id: str) -> None:
        ids = self.visible_volunteer_ids(actor)
        if ids is not None and volunteer_id not in ids:
            raise PermissionError_("无权访问外校志愿者数据")

    def assert_can_dispatch(self, actor: Actor, volunteer_id: str) -> None:
        """调度/派班/替班审批：组委会可跨队，高校管理员只限本校。"""
        if actor.role not in ("committee", "school"):
            raise PermissionError_("该角色不能调度班次")
        self.assert_can_see_volunteer(actor, volunteer_id)
