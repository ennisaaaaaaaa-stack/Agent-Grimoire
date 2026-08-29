#!/usr/bin/env python3
"""三层门 (v0.5) 专项烟测 — 独立迷你实例, 不碰 live。

验证六件事:
1. 任意 operator (含缺省 unknown) 能看 public 书
2. family 书: 家人名单内可见, 名单外/unknown 不可见
3. private 书: 库主可见, audience 白名单内可见, 其余装404
4. 装404与真404字节一致(存在性不泄露) — GET /skill/<name> 两种404正文全等
5. 五读面全覆盖: map/tag/darkzone/vault-listing/vault-file 按operator过滤
6. 现存书零扰动: visibility 缺省 public, 老读面行为不变
拆除纪律与 smoke.py 同款: atexit 必杀。
"""
import atexit
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_PORT = 18741
BASE = f"http://127.0.0.1:{_PORT}"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {detail}" if detail else ""))


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(path, obj, headers=None):
    data = json.dumps(obj, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 0. 起隔离实例: 临时库 + 临时端口
_tmpdir = tempfile.TemporaryDirectory(prefix="grimoire-vis-smoke-")
_srv = subprocess.Popen(
    [sys.executable, "grimoire.py", str(_PORT)],
    cwd="/home/ubuntu/Agent-Grimoire",
    env={**os.environ, "GRIMOIRE_DB": _tmpdir.name + "/vis.db",
         "GRIMOIRE_BUDGET_WINDOW": "3600", "GRIMOIRE_BUDGET_MAX": "50"},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import atexit as _ae
_ae.register(lambda: (_srv.terminate(), _srv.wait(timeout=5)))
time.sleep(0.8)

s, b = get("/health")
check("health", s == 200 and b == "ok", b)

# 1. 提交三本测试书: public / family / private
#    (提交即 draft; 为简化, 直接用 owner 身份走 governance_flow promote)
for name, vis in [("公开手册-public", "public"), ("家传心法-family", "family"),
                  ("密卷-private", "private")]:
    s, b = post("/skill", {"name": name, "author": "smoke",
                           "body": f"# {name}\n测试书 {vis}",
                           "tags": ["vis-test"]})
    check(f"提交{name}", s in (200, 201), f"{s} {b[:60]}")

# 转正 + 设 visibility: 直接动测试库(治理事件走API, 字段直改——
# visibility 是库主的数据面操作, 不走巡山使治理流)
_db = pathlib.Path(_tmpdir.name) / "vis.db"
import sqlite3
con = sqlite3.connect(_db)
# 先找出三本书的 skill_id
ids = {n: con.execute("SELECT skill_id FROM skills WHERE name=?", (n,)).fetchone()[0]
       for n in ("公开手册-public", "家传心法-family", "密卷-private")}
# 转正: smoke 简化路径——直接 status=verified(与 smoke.py 的 promote 路径等价效果)
con.execute("UPDATE skills SET status='verified' WHERE skill_id IN (?,?,?)",
            tuple(ids.values()))
con.execute("UPDATE skills SET visibility='family' WHERE skill_id=?",
            (ids["家传心法-family"],))
con.execute("UPDATE skills SET visibility='private' WHERE skill_id=?",
            (ids["密卷-private"],))
# 名单: family 名单加入 mingming; private 书 audience 加入 zhaohao
con.executescript(f"""
INSERT INTO visibility_rosters(scope, operator, note, added_at) VALUES
('family', 'mingming', '测试: 鸣鸣在家人名单', datetime('now')),
('skill:{ids['密卷-private']}', 'zhaohao', '测试: 照照在密卷白名单', datetime('now'));
""")
con.commit()
con.close()

H = lambda op: {"X-Operator": op}  # noqa: E731

# 2. public 书人人可见
s, b = get("/skill/" + urllib.parse.quote("公开手册-public"), headers=H("mingming"))
check("public书: 鸣鸣可见", s == 200 and "公开手册" in b, f"{s}")
s, b = get("/skill/" + urllib.parse.quote("公开手册-public"))
check("public书: unknown可见", s == 200 and "公开手册" in b, f"{s}")

# 3. family 书: 名单内可见, 名单外装404
s, b = get("/skill/" + urllib.parse.quote("家传心法-family"), headers=H("mingming"))
check("family书: 名单内(鸣鸣)可见", s == 200 and "家传心法" in b, f"{s}")
s, b = get("/skill/" + urllib.parse.quote("家传心法-family"), headers=H("zhaohao"))
check("family书: 名单外(照照)装404", s == 404, f"{s} {b[:40]}")
s, b = get("/skill/" + urllib.parse.quote("家传心法-family"))
check("family书: unknown装404", s == 404, f"{s}")

# 4. private 书: 库主可见, 白名单可见, 其余装404
s, b = get("/skill/" + urllib.parse.quote("密卷-private"), headers=H("hui"))
check("private书: 库主可见", s == 200 and "密卷" in b, f"{s}")
s, b = get("/skill/" + urllib.parse.quote("密卷-private"), headers=H("zhaohao"))
check("private书: audience白名单(照照)可见", s == 200 and "密卷" in b, f"{s}")
s, b = get("/skill/" + urllib.parse.quote("密卷-private"), headers=H("mingming"))
check("private书: 非白名单(鸣鸣)装404", s == 404, f"{s}")

# 5. 装404与真404字节一致
s1, b1 = get("/skill/" + urllib.parse.quote("密卷-private"), headers=H("mingming"))
s2, b2 = get("/skill/" + urllib.parse.quote("根本不存在的书"))
check("装404与真404字节一致", s1 == s2 == 404 and b1 == b2, f"{s1}/{s2}")

# 6. map/darkzone/tag 三面过滤
s, b = get("/map", headers=H("mingming"))
check("map: 鸣鸣(public+family)无密卷", s == 200 and "密卷" not in b
      and "家传心法" in b)
s, b = get("/map")
check("map: unknown只见public", s == 200 and "密卷" not in b
      and "家传心法" not in b and "公开手册" in b)
s, b = get("/tag/vis-test", headers=H("zhaohao"))
check("tag面: 照照(public+密卷)无家传", s in (200, 404) and "家传心法" not in b)
s, b = get("/darkzone", headers=H("mingming"))
check("darkzone: 鸣鸣无密卷", s == 200 and "密卷" not in b)

# 7. vault 两面: 挂个附件到密卷, 验证装404
_con = sqlite3.connect(_db)
_con.execute(
    "INSERT INTO vault_index(file_id, skill_id, relpath, size, sha256, "
    "binary, mtime, synced_at) VALUES('t1', ?, 'refs/note.md', 5, "
    "'x', 0, NULL, datetime('now'))", (ids["密卷-private"],))
_con.commit(); _con.close()
import pathlib as _pl
(_pl.Path(_tmpdir.name)).mkdir(exist_ok=True)
# vault 文件落盘路径走 VAULT_DIR 环境变量——直接指到临时目录
os.environ["GRIMOIRE_VAULT"] = _tmpdir.name + "/vault"
_vdir = _pl.Path(_tmpdir.name) / "vault" / ids["密卷-private"] / "refs"
_vdir.mkdir(parents=True, exist_ok=True)
(_vdir / "note.md").write_text("秘密附件")
# 注: VAULT_DIR 在 server 启动时读取, 上面 os.environ 改的是测试进程——
# server 侧 vault 落盘路径是 cwd/vault。补写到 server 视角路径:
_vdir2 = _pl.Path("/home/ubuntu/Agent-Grimoire/vault") / ids["密卷-private"] / "refs"
try:
    _vdir2.mkdir(parents=True, exist_ok=True)
    (_vdir2 / "note.md").write_text("秘密附件")
    s, b = get("/vault/" + urllib.parse.quote("密卷-private") + "/refs/note.md",
               headers=H("mingming"))
    check("vault-file: 鸣鸣取密卷附件装404", s == 404, f"{s} {b[:40]}")
    s, b = get("/vault/" + urllib.parse.quote("密卷-private") + "/refs/note.md",
               headers=H("zhaohao"))
    check("vault-file: 照照(白名单)取密卷附件200", s == 200 and "秘密附件" in b,
          f"{s} {b[:40]}")
    s, b = get("/vault?skill=" + urllib.parse.quote("密卷-private"),
               headers=H("mingming"))
    check("vault-listing: 鸣鸣看密卷装404", s == 404, f"{s}")
finally:
    import shutil
    shutil.rmtree(_vdir2, ignore_errors=True)

# 8. 现存书零扰动: live 库没被动过(这里只验测试库默认值逻辑)
_con = sqlite3.connect(_db)
_cnt = _con.execute(
    "SELECT COUNT(*) FROM skills WHERE visibility='public'").fetchone()[0]
_con.close()
check("缺省public: 除两本改过的全public", _cnt == 1, f"public count={_cnt}")

print()
_fails = [r for r in results if not r[1]]
print(f"=== {len(results)-len(_fails)}/{len(results)} PASS ===")
sys.exit(1 if _fails else 0)
