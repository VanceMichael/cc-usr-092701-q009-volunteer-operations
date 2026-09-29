"""端到端业务测试的场景构造工具。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.volunteer_operations.service import Service
from src.volunteer_operations.store import Actor, Store


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.service = Service(Store(self.path))
        svc = self.service

        # 角色
        self.staff = Actor(id="staff1", role="staff", name="服务中心小王")
        self.committee = Actor(id="com1", role="committee", name="组委会老李")
        self.admin_a = Actor(id="admA", role="school", name="A校老师", school_id="SCH0001")
        self.admin_b = Actor(id="admB", role="school", name="B校老师", school_id="SCH0002")

        # 学校
        self.sch_a = svc.add_school("外国语大学")
        self.sch_b = svc.add_school("理工学院")
        # add_school 自增编号，按实际 id 绑定管理员
        self.admin_a = Actor(id="admA", role="school", name="A校老师",
                             school_id=self.sch_a.id)
        self.admin_b = Actor(id="admB", role="school", name="B校老师",
                             school_id=self.sch_b.id)

        # 志愿者
        self.lin = svc.register_volunteer(self.admin_a, "林小语", self.sch_a.id)
        self.chen = svc.register_volunteer(self.admin_a, "陈清晨", self.sch_a.id)
        self.zhao = svc.register_volunteer(self.admin_b, "赵理工", self.sch_b.id)

        # 岗位
        self.airport = svc.add_position(
            self.staff, "机场抵离服务", sensitive=True,
            required_credentials=["airport_pass"], require_training=True,
            min_rest_hours=11.0, early_morning=True)
        self.press = svc.add_position(
            self.staff, "涉外采访陪同", sensitive=True,
            required_credentials=["media_pass"],
            required_languages=["fr"], require_training=True)
        self.hotel = svc.add_position(
            self.staff, "媒体酒店引导", sensitive=False,
            require_training=True, min_rest_hours=10.0)

        # 林小语：法语核验 + 媒体证核验 + 两个敏感岗培训
        svc.declare_language(self.staff, self.lin.id, "fr", "法语", "C1")
        svc.verify_language(self.staff, self.lin.id, "fr")
        svc.verify_credential(self.staff, self.lin.id, "media_pass", "媒体中心通行证")
        svc.complete_training(self.staff, self.lin.id, self.press.id)
        svc.complete_training(self.staff, self.lin.id, self.airport.id)

        # 陈清晨：机场证 + 培训 + 阿拉伯语（自报未核验）
        svc.declare_language(self.staff, self.chen.id, "ar", "阿拉伯语", "B2")
        svc.verify_credential(self.staff, self.chen.id, "airport_pass", "机场隔离区证")
        svc.complete_training(self.staff, self.chen.id, self.airport.id)
        svc.complete_training(self.staff, self.chen.id, self.hotel.id)

        # 赵理工：酒店培训
        svc.complete_training(self.staff, self.zhao.id, self.hotel.id)

    def restart(self) -> Service:
        """模拟系统重启：用同一文件重新装载，未完成任务继续推进。"""
        self.service = Service(Store(self.path))
        return self.service

    def tearDown(self):
        self.tmp.cleanup()
