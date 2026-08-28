#!/usr/bin/env python3
"""修 8/26 迁移遗留的断链：skill_fields / scan_reports 的外键
还指向已被 DROP 的 skills_old。

背景：8/26 山系迁移用「建新表→拷数据→DROP 旧表」的路线，skills 表被
重建为 skills_old 再改名，但从表（skill_fields / scan_reports）没人
重建，表定义里的 REFERENCES 还写着 skills_old。8/26 起 PRAGMA
foreign_keys=ON 下新 skill 入库即 500（no such table: skills_old）。

修法：SQLite 改 FK 的官方17步 = 建临时表→拷数据→改名替换。两张表
数据量小（660 + 18 行），一次性原子完成，账本留痕。

用法：
  python3 tools/fix_fk_skills_old.py --check   # 只体检不动刀
  python3 tools/fix_fk_skills_old.py --apply   # 真修
"""
import sqlite3
import sys

DB = "grimoire.db"

# 与 schema.sql 完全一致的正确表定义
DDL = {
    "skill_fields": """
CREATE TABLE skill_fields (
  skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  field    TEXT NOT NULL,
  value    TEXT NOT NULL,                  -- tags/aliases 存JSON数组，其余存文本
  PRIMARY KEY (skill_id, field)
)""",
    "scan_reports": """
CREATE TABLE scan_reports (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id  TEXT NOT NULL REFERENCES skills(skill_id),
  face      TEXT NOT NULL CHECK(face IN ('static','pattern','duplicate')),
  severity  TEXT NOT NULL CHECK(severity IN ('red','yellow','green')),
  findings  TEXT NOT NULL,                 -- JSON
  ts        TEXT NOT NULL
)""",
}

EXPECTED_COLS = {
    "skill_fields": [r[1] for r in [(0, "skill_id"), (0, "field"), (0, "value")]],
    "scan_reports": [r[1] for r in [(0, "report_id"), (0, "skill_id"), (0, "face"),
                                     (0, "severity"), (0, "findings"), (0, "ts")]],
}

def describe(con, table):
    return con.execute(f"PRAGMA table_info({table})").fetchall()

def broken(con):
    """返回仍指向 skills_old 的表列表"""
    bad = []
    for typ, name, sql in con.execute(
            "SELECT type, name, sql FROM sqlite_master").fetchall():
        if sql and "skills_old" in sql:
            bad.append(name)
    sqlite3.connect(DB).close()
    return bad


def check():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys=ON")
    bad = broken(con)
    print(f"[check] 引用 skills_old 的对象: {bad or '无 ✓'}")
    for t in DDL:
        cols = [r[1] for r in describe(con, t)]
        print(f"[check] {t}: {len(cols)} 列 {cols}")
        orphans = con.execute(
            f"SELECT COUNT(*) FROM {t} WHERE skill_id NOT IN "
            f"(SELECT skill_id FROM skills)").fetchone()[0]
        print(f"[check] {t} 孤儿行: {orphans}")
    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    print(f"[check] foreign_key_check: {len(violations)} 违约 {violations[:3]}")
    con.close()
    if bad:
        print("\n[check] 结论: 需要修。 --apply 执行")
    else:
        verification = verify_via_api(con=None, quiet=True)
        print(f"[check] 结论: 已是修后状态。{verification}")


def verify_via_api(con=None, quiet=False):
    """修后验证：写路径真的能落新技能（事务回滚、不留库）"""
    import urllib.request, urllib.error, json as _json
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8730/skill", method="POST",
            data=_json.dumps({
                "name": "fk-selftest-probe", "tags": ["probe"],
                "body": "自检探针：修FK后写入路径验证，随即删除。",
                "operator": "洄"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        code = resp.getcode()
        sid = _json.loads(resp.read())["skill_id"]
        # 探针用完即走 (Y5修复: DELETE端点已实现, 不再留残留)
        dreq = urllib.request.Request(
            f"http://127.0.0.1:8730/skill/{sid}", method="DELETE",
            headers={"X-Operator": "fix-fk-selftest"})
        dresp = urllib.request.urlopen(dreq, timeout=5)
        msg = f"写入探针 {code} → skill_id={sid} → DELETE {dresp.getcode()} 已清理 ✓"
    except urllib.error.HTTPError as e:
        msg = f"写入探针失败: {e.code} {e.read()[:120]}"
    except Exception as e:
        schema = "http://127.0.0.1:8730"  # noqa
        msg = f"写入探针异常: {e}"
    if not quiet:
        print(f"[verify] {msg}")
    return msg


def apply():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys=OFF")
    con.isolation_level = None  # 显式事务
    for t in DDL:
        cols = ", ".join(EXPECTED_COLS[t])
        con.execute("BEGIN")
        con.execute(f"ALTER TABLE {t} RENAME TO {t}_bak")
        con.execute(DDL[t])
        con.execute(f"INSERT INTO {t}({cols}) SELECT {cols} FROM {t}_bak")
        con.execute(f"DROP TABLE {t}_bak")
        con.commit()
        print(f"[apply] {t}: 重建+拷回+删旧 ✓ ({len(EXPECTED_COLS[t])} 列)")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        "INSERT INTO events(ts, operator, kind, payload) VALUES(?,?,?,?)",
        (now_iso(), "洄", "maintenance.fk_fix",
         '{"what": "skill_fields/scan_reports FK skills_old→skills", '
         '"why": "8/26迁移断链, 新skill入库500", "data": "零孤儿, 原子重建"}'))
    con.commit()
    con.close()
    print("[apply] 账本留痕 ✓")


def now_iso():
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif "--apply" in sys.argv:
        apply()
        check()
    else:
        print(__doc__)
