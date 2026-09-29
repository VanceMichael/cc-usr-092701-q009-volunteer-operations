# 国际赛事志愿服务排班

国际赛事临近闭幕，媒体酒店、机场抵离、制证、欢迎招待会和告别派对班次互相挤占。本项目是赛事服务中心使用的志愿服务**后台管理**：管理报名、语言与岗位资质、班次、签到、替班、服务对象、培训和表彰，并根据休息间隔与岗位要求给出**可解释**的排班结果。数据均落 SQLite，系统重启后自动推进未完成任务。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `src/volunteer_operations/domain.py` | 参与角色（赛事服务中心、组委会调度员、高校管理员、志愿者）、排班考勤常量、业务异常 |
| `src/volunteer_operations/store.py` | SQLite 建表与连接（志愿者、语言、证件、资质、培训、岗位、班次、分配、替班、考勤、复核、表彰、任务、审计） |
| `src/volunteer_operations/service.py` | 后台门面 `VolunteerService`：报名资质、可解释排班、替班链、签到复核、封存、表彰、重启恢复、权限隔离、志愿者全景查询 |
| `src/volunteer_operations/context.py` | 领域资料读取与校验 |

## 业务规则

- **重叠禁止**：同一志愿者不能被分到时间重叠的班次。
- **休息间隔**：相邻班次之间至少 8 小时（`MIN_REST_HOURS`）。
- **清晨保护**：早于 7:00 的班次连续出勤不超过 3 天。
- **资质上岗**：岗位可要求资质（如「制证核验」），资质来自培训或手工授予。
- **敏感岗位**：机场抵离、涉外采访等岗位只接受**证件已核验**且**语言能力已核验**（并达到等级门槛）的志愿者。
- **可解释排班**：`plan_shift` / `evaluate_candidate` 返回逐项检查结果（`OVERLAP`、`REST_INTERVAL`、`QUALIFICATION`、`LANGUAGE_LEVEL`、`LANGUAGE_VERIFIED`、`CREDENTIAL_VERIFIED`、`EARLY_STREAK`）；`auto_assign` 在合格者中优先选累计排班最少、清晨连班最少者。
- **替班链**：替班申请保留**原授权**（`substituted`）与**新责任人**（新分配 `supersedes_id` 指向原分配）；批准时重校资格，服务时长和奖励跟随**实际出勤者**，双方触发表彰重算。
- **考勤复核**：迟到（超 15 分钟宽限）、班次结束漏签、人工补录一律进入 `review_cases`，复核前不写出有效出勤；补录批准后才生效，可驳回。
- **封存**：封存须无待复核记录，仅赛事服务中心可执行；封存后任何人（含组委会）不能改签到或补录。
- **权限**：高校管理员只能查看本校志愿者数据，不能排班；组委会可跨校调度但不能改封存记录。
- **表彰**：服务之星（≥24 小时）、语言服务先锋（核验语言完成 ≥2 个语言/敏感班次）、全勤保障（≥4 班次且无缺勤）；每条奖励记录依据（规则、时长、班次）。
- **重启恢复**：漏签扫描、开班前 2 小时提醒、表彰计算以任务落库，重启后 `recover(now)` 继续推进，任务只执行一次。

## 用法示例

```python
from datetime import datetime
from src.volunteer_operations.domain import Actor, ROLE_SERVICE_CENTER, ROLE_COMMITTEE
from src.volunteer_operations.service import VolunteerService

svc = VolunteerService("event.db")  # 或 ":memory:"
center = Actor(ROLE_SERVICE_CENTER, "中心干事")
committee = Actor(ROLE_COMMITTEE, "组委会调度")

# 报名小语种志愿者并核验语言与证件
vid = svc.register_volunteer(center, "陈合格", "临湖大学",
                             languages=(("法语", "advanced"),), credentials=("护照",))
svc.verify_language(center, vid, "法语")
svc.verify_credential(center, vid, "护照")

# 建敏感岗位与班次
airport = svc.create_post(center, "机场抵离服务", sensitive=True,
                          required_language="法语", min_language_level="advanced")
target = svc.create_service_target(center, "机场抵离", "交通保障")
shift = svc.create_shift(center, airport, target,
                         datetime.fromisoformat("2026-09-29T05:30"),
                         datetime.fromisoformat("2026-09-29T13:30"))

# 可解释排班：看每个候选人为什么能/不能排
plan = svc.plan_shift(shift)
# 或自动选择最空闲的合格者
result = svc.auto_assign(committee, shift)

# 系统重启后继续推进未完成任务
svc.recover(datetime.now())

# 按一名志愿者查询：真实班次、替班链、累计时长、奖励依据
report = svc.volunteer_report(committee, vid)
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖 43 个用例：排班规则（重叠/休息/清晨连班/资质/敏感岗位/可解释/自动排班）、替班链与时长归属、迟到/漏签/补录复核、封存不可改、高校数据隔离、跨校调度、重启恢复、表彰依据。

## 编译与命令行检查

```bash
python3 -m compileall -q src tests
python3 -m src.volunteer_operations.context fixtures/context.json
```
