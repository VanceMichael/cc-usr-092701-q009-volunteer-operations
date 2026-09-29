"""闭幕周场景演示：报名资质 → 排班 → 替班 → 签到复核 → 表彰 → 画像。

用法：
    python3 -m src.volunteer_operations.demo [状态文件.json]

不指定文件时使用临时数据；指定文件后状态持久化，重启可继续推进任务。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .service import Service
from .store import Actor, Store


def build(path: Path) -> tuple[Service, dict[str, str]]:
    svc = Service(Store(path))
    staff = Actor(id="staff1", role="staff", name="服务中心小王")
    committee = Actor(id="com1", role="committee", name="组委会老李")

    # 复用已持久化的演示数据（同一文件重启不重复建档）
    if svc.s.volunteers:
        lin = next(v for v in svc.s.volunteers if v.name == "林小语")
        chen = next(v for v in svc.s.volunteers if v.name == "陈清晨")
        return svc, {"lin": lin.id, "chen": chen.id}

    school = svc.add_school("外国语大学")
    admin = Actor(id="adm1", role="school", name="高校张老师", school_id=school.id)

    lin = svc.register_volunteer(admin, "林小语", school.id)
    chen = svc.register_volunteer(admin, "陈清晨", school.id)
    ids = {"lin": lin.id, "chen": chen.id}

    airport = svc.add_position(
        staff, "机场抵离服务", sensitive=True,
        required_credentials=["airport_pass"], early_morning=True)
    press = svc.add_position(
        staff, "涉外采访陪同", sensitive=True,
        required_credentials=["media_pass"], required_languages=["fr"])
    hotel = svc.add_position(staff, "媒体酒店引导", min_rest_hours=10.0)

    for v in (lin.id, chen.id):
        svc.complete_training(staff, v, hotel.id)
    svc.declare_language(staff, lin.id, "fr", "法语", "C1")
    svc.verify_language(staff, lin.id, "fr")
    svc.verify_credential(staff, lin.id, "media_pass", "媒体中心通行证")
    svc.complete_training(staff, lin.id, press.id)
    svc.verify_credential(staff, chen.id, "airport_pass", "机场隔离区证")
    svc.complete_training(staff, chen.id, airport.id)

    # 闭幕周互相挤占的五类班次
    shifts = [
        ("媒体酒店", hotel.id, "2026-10-15T08:00:00+08:00", "2026-10-15T12:00:00+08:00", {}),
        ("机场抵离（清晨）", airport.id, "2026-10-16T05:00:00+08:00", "2026-10-16T09:00:00+08:00", {}),
        ("制证中心", hotel.id, "2026-10-16T10:00:00+08:00", "2026-10-16T14:00:00+08:00", {}),
        ("欢迎招待会", press.id, "2026-10-17T17:00:00+08:00", "2026-10-17T21:00:00+08:00",
         {"language_needs": ["fr"]}),
        ("告别派对", hotel.id, "2026-10-18T18:00:00+08:00", "2026-10-18T23:00:00+08:00", {}),
        ("制证中心（闭幕长班）", hotel.id, "2026-10-17T08:00:00+08:00", "2026-10-17T16:00:00+08:00", {}),
    ]
    created = []
    for name, pos_id, start, end, kw in shifts:
        created.append((name, svc.add_shift(staff, name, pos_id, start, end, **kw)))

    # 林小语：酒店（后替出）+ 制证 + 招待会；陈清晨：机场清晨 + 闭幕长班 + 派对
    a_hotel = svc.assign(committee, created[0][1].id, lin.id)
    a_cert = svc.assign(committee, created[2][1].id, lin.id)
    a_press = svc.assign(committee, created[3][1].id, lin.id)
    a_air = svc.assign(committee, created[1][1].id, chen.id)
    a_party = svc.assign(committee, created[4][1].id, chen.id)
    a_long = svc.assign(committee, created[5][1].id, chen.id)

    # 酒店班当天林小语身体不适，临时替给陈清晨（跨班调剂，组委会审批）
    sub = svc.request_substitution(
        Actor(id=lin.id, role="volunteer", name="林小语"),
        a_hotel.id, chen.id, "突发不适")
    svc.approve_substitution(committee, sub.id)

    # 签到：酒店替班后由陈清晨正常签到；机场迟到；其余正常签到
    svc.check_in(a_hotel.id, at="2026-10-15T07:58:00+08:00")
    svc.check_in(a_cert.id, at="2026-10-16T09:58:00+08:00")
    svc.check_in(a_press.id, at="2026-10-17T16:55:00+08:00")
    svc.check_in(a_long.id, at="2026-10-17T07:59:00+08:00")
    late = svc.check_in(a_air.id, at="2026-10-16T05:20:00+08:00")
    assert late.status == "late"
    appeal = svc.raise_review(
        Actor(id=chen.id, role="volunteer", name="陈清晨"),
        "late_appeal", a_air.id, "首班地铁延误，附运营公告")
    svc.decide_review(staff, appeal.id, approve=True, note="情况属实")

    # 告别派对陈清晨漏签，班后扫描自动登记复核，服务中心补录确认
    svc.schedule_missed_scan()
    svc.run_due_tasks(at="2026-10-18T23:16:00+08:00")
    missed_review = next(r for r in svc.s.reviews
                         if r.assignment_id == a_party.id and r.kind == "missed")
    svc.decide_review(staff, missed_review.id, approve=True, note="门岗录像确认")

    svc.compute_awards(staff)
    return svc, ids


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__)
        return 2
    if len(argv) == 2:
        path = Path(argv[1])
        cleanup = None
    else:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "demo.json"
        cleanup = tmp.cleanup
    try:
        svc, ids = build(path)
        staff = Actor(id="staff1", role="staff")
        profile = svc.profile(staff, ids["chen"])
        print(json.dumps(profile, ensure_ascii=False, indent=2))
    finally:
        if cleanup:
            cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
