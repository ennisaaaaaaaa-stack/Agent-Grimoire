-- 山海 / Agent Grimoire — 工具层分区 (v0.3, da1840e 提案落地)
-- 判据: 按知识类型不按协议。tools 表记能力级事实 (capability),
--       tools_telemetry 记 entry 级遥测 (照照: 死 server 不被忙的 CLI 孪生救活)。
--       MCP: config.yaml 是事实源, 山海只做镜像读脸 (被动 sync, 主权在外面)。
--       CLI: 无被动事实源, 走登记制 (tool.register, 同 skill.submit 自报+巡山核验)。

-- 能力级: 一个 capability = 一件事 (如 "darkzone-report")。
-- 正字 = capability; mcp/cli/api 是同能力的不同门牌, 互为 cross-link。
CREATE TABLE IF NOT EXISTS tools (
  capability   TEXT PRIMARY KEY,           -- 正字能力名 (kebab-case, 如 patrol-report-head)
  note         TEXT,                       -- 一句话: 这能力是干什么的
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- entry 级: 一行 = 一个可调用的门牌 (mcp server / cli 脚本 / api endpoint)。
-- 遥测落这里 (entry 级), 能力视图从这聚合。disable 判据也在这层:
-- 一个 entry 死了不该由别的 entry 顶活——照照的分层判据。
CREATE TABLE IF NOT EXISTS tool_entries (
  entry_id     TEXT PRIMARY KEY,           -- te:<uuid>
  capability   TEXT NOT NULL REFERENCES tools(capability),
  kind         TEXT NOT NULL CHECK(kind IN ('mcp','cli','api')),
  ref          TEXT NOT NULL,              -- mcp: server名; cli: repo相对路径; api: url模板
  status       TEXT NOT NULL DEFAULT 'active'
               CHECK(status IN ('active','disabled','retired')),
  note         TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE(capability, kind, ref)
);

-- entry 遥测: 只记不动, 与 skill 遥测同律。call_count/last_seen 聚合读面算。
CREATE TABLE IF NOT EXISTS tool_telemetry (
  event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  entry_id  TEXT NOT NULL REFERENCES tool_entries(entry_id),
  caller    TEXT NOT NULL,                 -- 谁调的 (operator scope, plugin hook 传 session/operator)
  ok        INTEGER NOT NULL DEFAULT 1,    -- 1=成功 0=失败
  payload   TEXT                           -- JSON: args 摘要/错误类型等
);

CREATE INDEX IF NOT EXISTS idx_tool_tel_entry ON tool_telemetry(entry_id, ts);
