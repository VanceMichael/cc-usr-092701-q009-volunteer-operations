"""测试共用的造数工具。"""

from datetime import datetime

from src.volunteer_operations.domain import (
    ROLE_COMMITTEE,
    ROLE_SERVICE_CENTER,
    ROLE_UNIVERSITY_ADMIN,
    Actor,
)
from src.volunteer_operations.service import VolunteerService

CENTER = Actor(ROLE_SERVICE_CENTER, "中心干事")
COMMITTEE = Actor(ROLE_COMMITTEE, "组委会调度")
ADMIN_A = Actor(ROLE_UNIVERSITY_ADMIN, "高校管理员甲", school="临湖大学")
ADMIN_B = Actor(ROLE_UNIVERSITY_ADMIN, "高校管理员乙", school="远山学院")

SCHOOL_A = "临湖大学"
SCHOOL_B = "远山学院"


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def new_service(db_path: str = ":memory:") -> VolunteerService:
    return VolunteerService(db_path)


def add_volunteer(
    service: VolunteerService,
    name: str,
    school: str = SCHOOL_A,
    languages=(),
    credentials=(),
    trainings=(),
) -> int:
    """languages: [(语言, 等级, 是否已核验)]；credentials: [(证件, 是否已核验)]；trainings: [(课程, 授予资质)]"""
    vid = service.register_volunteer(
        CENTER,
        name,
        school,
        languages=[(lang, level) for lang, level, _ in languages],
        credentials=[kind for kind, _ in credentials],
    )
    for lang, _level, verified in languages:
        if verified:
            service.verify_language(CENTER, vid, lang)
    for kind, verified in credentials:
        if verified:
            service.verify_credential(CENTER, vid, kind)
    for course, skill in trainings:
        service.complete_training(CENTER, vid, course, skill)
    return vid


def make_posts(service: VolunteerService) -> dict:
    """常用岗位：媒体酒店（普通）、制证（要资质）、机场抵离（敏感+法语）。"""
    return {
        "hotel": service.create_post(CENTER, "媒体酒店服务"),
        "badge": service.create_post(CENTER, "制证服务", required_skill="制证核验"),
        "airport": service.create_post(
            CENTER,
            "机场抵离服务",
            sensitive=True,
            required_language="法语",
            min_language_level="advanced",
        ),
        "press": service.create_post(CENTER, "涉外采访协助", sensitive=True),
    }


def make_targets(service: VolunteerService) -> dict:
    return {
        "hotel": service.create_service_target(CENTER, "媒体酒店", "住宿保障"),
        "airport": service.create_service_target(CENTER, "机场抵离", "交通保障"),
        "badge": service.create_service_target(CENTER, "制证中心", "证件制作"),
        "gala": service.create_service_target(CENTER, "欢迎招待会", "礼宾活动"),
        "party": service.create_service_target(CENTER, "告别派对", "礼宾活动"),
    }


def make_shift(service, post_id, target_id, start, end, location="") -> int:
    return service.create_shift(CENTER, post_id, target_id, dt(start), dt(end), location)
