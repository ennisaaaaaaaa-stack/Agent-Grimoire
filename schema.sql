-- 山海 / Agent Grimoire — library schema
-- 依据: portalk-contract-skill-lifecycle v0.2 (tags[] 正字; 开场注入四件套;
--       baseline_hash 绑定; 只记不动遥测; 写入扫描坐藏书阁门口)

-- 技能条目（异兽志的每一兽）
CREATE TABLE IF NOT EXISTS skills (
  skill_id      TEXT PRIMARY KEY,          -- sk:<uuid>，契约v0.2正字格式
  name          TEXT NOT NULL UNIQUE,
  layer         TEXT NOT NULL DEFAULT 'index'
                -- 对齐域6 layer枚举; pinned=描述行常驻(core=index之间的注水层,
                -- 每session必知但正文太重的书: 描述行进经图, 正文按需拉)
                CHECK(layer IN ('core','pinned','index','archive')),
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','verified','retired')),
  author        TEXT NOT NULL,             -- 提交者身份
  source        TEXT,                      -- 出处: 'self' | 来源url | repo引用
  imported_at   TEXT,                      -- 外来导入时间（自梳理为NULL）
  body          TEXT NOT NULL,
  baseline_hash TEXT NOT NULL,             -- 最近一次被接受的正文hash，改写绑它
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 结构化字段: tags / trigger / boundary / why / aliases
-- tags[] 与 submit 事件的 tags 同源同名（审账对暗号）
CREATE TABLE IF NOT EXISTS skill_fields (
  skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  field    TEXT NOT NULL,
  value    TEXT NOT NULL,                  -- tags/aliases 存JSON数组，其余存文本
  PRIMARY KEY (skill_id, field)
);

-- tag树（山系）。包含关系 = parent缩进，树在渲染时长出来。
CREATE TABLE IF NOT EXISTS tags (
  tag     TEXT PRIMARY KEY,
  parent  TEXT REFERENCES tags(tag),        -- NULL = 山根
  aliases TEXT,                             -- JSON数组; 查询侧归并到正字(repair≈维修), 不占树位, 可逆
  note    TEXT
);

-- 前置表: 左边是右边的前提。requires 可以是skill_id也可以是tag。
CREATE TABLE IF NOT EXISTS prereqs (
  target   TEXT NOT NULL,                  -- ⇒ 右侧（skill或tag）
  requires TEXT NOT NULL,                  -- ⇒ 左侧（skill或tag）
  reason   TEXT NOT NULL,                  -- 理由：违反会出什么事
  PRIMARY KEY (target, requires)
);

-- 遥测账本：只记不动。事件本身不触发任何变更，巡山使每周读账裁决。
-- operator = 操作者scope，一座馆藏，账本人手一本。
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  operator TEXT NOT NULL,
  kind     TEXT NOT NULL,                  -- skill.submit / skill.push / skill.expand / skill.description.rewrite
  payload  TEXT NOT NULL                   -- JSON; rewrite 事件携带 baseline_hash
);

-- 写入扫描报告：三面（static/pattern/duplicate），只记不拦。
-- 带red报告的draft到不了verified——阻断由状态机完成，不由扫描完成。
CREATE TABLE IF NOT EXISTS scan_reports (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id  TEXT NOT NULL REFERENCES skills(skill_id),
  face      TEXT NOT NULL CHECK(face IN ('static','pattern','duplicate')),
  severity  TEXT NOT NULL CHECK(severity IN ('red','yellow','green')),
  findings  TEXT NOT NULL,                 -- JSON数组
  ts        TEXT NOT NULL
);

-- 工具层分区 (v0.3): 见 schema_tools.sql。init_db 幂等并跑两份 schema。
CREATE INDEX IF NOT EXISTS idx_events_operator ON events(operator, kind);
CREATE INDEX IF NOT EXISTS idx_fields_field ON skill_fields(field);
CREATE INDEX IF NOT EXISTS idx_scan_skill ON scan_reports(skill_id);
