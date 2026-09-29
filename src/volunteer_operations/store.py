"""SQLite 持久化：所有业务状态落库，系统重启后由任务表继续推进。"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS volunteers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  school TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  language TEXT NOT NULL,
  level TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  UNIQUE(volunteer_id, language)
);

CREATE TABLE IF NOT EXISTS credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  kind TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  UNIQUE(volunteer_id, kind)
);

CREATE TABLE IF NOT EXISTS trainings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  course TEXT NOT NULL,
  completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qualifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  skill TEXT NOT NULL,
  source TEXT NOT NULL,
  UNIQUE(volunteer_id, skill)
);

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  sensitive INTEGER NOT NULL DEFAULT 0,
  required_skill TEXT,
  required_language TEXT,
  min_language_level TEXT
);

CREATE TABLE IF NOT EXISTS service_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  target_id INTEGER NOT NULL REFERENCES service_targets(id),
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  location TEXT NOT NULL DEFAULT ''
);

-- 分配链：替班不删除原授权，只把原分配置为 substituted 并挂出新分配
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  state TEXT NOT NULL DEFAULT 'active',      -- active / substituted / cancelled
  supersedes_id INTEGER REFERENCES assignments(id),
  reason TEXT NOT NULL DEFAULT '',
  explanation TEXT NOT NULL DEFAULT '',      -- 排班依据（逐项检查结果）
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS substitutions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  from_assignment_id INTEGER NOT NULL REFERENCES assignments(id),
  to_assignment_id INTEGER REFERENCES assignments(id),
  from_volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),  -- 原授权
  to_volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),    -- 新责任人
  reason TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',     -- pending / approved / rejected
  requested_by TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

-- 考勤：漏签、迟到、补录一律先 pending 进入复核，不直接改写出勤
CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assignment_id INTEGER NOT NULL REFERENCES assignments(id),
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  check_in TEXT,
  check_out TEXT,
  state TEXT NOT NULL DEFAULT 'pending',     -- pending / effective / rejected / absent
  origin TEXT NOT NULL DEFAULT 'self',       -- self / missed / backfill
  sealed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attendance_id INTEGER NOT NULL REFERENCES attendance(id),
  kind TEXT NOT NULL,                        -- late / missed / backfill
  state TEXT NOT NULL DEFAULT 'open',        -- open / approved / rejected
  payload TEXT NOT NULL DEFAULT '',          -- 补录的起止时间等
  note TEXT NOT NULL DEFAULT '',
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS awards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  volunteer_id INTEGER NOT NULL REFERENCES volunteers(id),
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  basis TEXT NOT NULL,                       -- 奖励依据（时长、班次、规则）
  granted_at TEXT NOT NULL,
  UNIQUE(volunteer_id, kind)
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assignment_id INTEGER NOT NULL UNIQUE REFERENCES assignments(id),
  volunteer_id INTEGER NOT NULL,
  shift_id INTEGER NOT NULL,
  message TEXT NOT NULL,
  sent_at TEXT NOT NULL
);

-- 待办任务：漏签扫描、到岗提醒、表彰计算；重启后 recover 继续推进
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  run_after TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',     -- pending / done / failed
  attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL,
  at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证表结构存在。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
