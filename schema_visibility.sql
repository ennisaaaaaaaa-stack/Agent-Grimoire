-- 三层门 (v0.5): 可见性分档 + 名单
-- 档位: public(任何人) / family(家人名单) / private(库主+audience白名单)
-- 语义: private 对无权者装404(与真404字节一致)——不泄露存在性
-- 防误看不防冒领: 身份自报(X-Operator/body.operator), 冒领归 R2 身份地基堵。
-- 名单内容 = 部署态数据(库主拍板), 机制先上, 表留空起步。
-- skills.visibility 列走 init() 里的 ALTER(先查后加, 幂等)——
-- SQLite 的 ADD COLUMN 不能 IF NOT EXISTS, executescript 不适合它。

CREATE TABLE IF NOT EXISTS visibility_rosters (
  scope    TEXT NOT NULL,             -- 'family' | 'skill:<skill_id>'
  operator TEXT NOT NULL,             -- 谁可见
  note     TEXT,
  added_at TEXT NOT NULL,
  PRIMARY KEY (scope, operator)
);
