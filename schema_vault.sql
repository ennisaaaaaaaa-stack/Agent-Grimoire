-- 附件库 (vault, v0.4): skill目录下的配套资产（references/scripts/templates等）
-- 设计: BLOB不进库（库保持轻、备份快），文件落vault/<skill_id前8位>/树，SQLite只存索引。
-- 依据: 契约v0.2主权边界——"sync 是被动镜像"；附件随SKILL.md同律入馆。
-- 幂等: CREATE TABLE IF NOT EXISTS, 与主schema同init路径。

CREATE TABLE IF NOT EXISTS vault_index (
  file_id   TEXT PRIMARY KEY,          -- <skill_id>:<relpath> 的sha前16位, 稳定ID
  skill_id  TEXT NOT NULL REFERENCES skills(skill_id),
  relpath   TEXT NOT NULL,             -- 相对skill目录的路径 (references/api.md)
  size      INTEGER NOT NULL,
  sha256    TEXT NOT NULL,             -- 内容hash: 幂等重放靠它判变
  binary    INTEGER NOT NULL DEFAULT 0,-- 1=二进制(ttf/png/pack), GET时base64
  mtime     TEXT,
  synced_at TEXT NOT NULL,
  UNIQUE(skill_id, relpath)
);
