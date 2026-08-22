#!/usr/bin/env python3
"""probe适配层: 照照的探针搬到烟测自隔离环境 — 同一探针, 自带临时DB+临时口+拆除。
v2差异: BASE走18731(烟测隔离区口段), 网络层照样真HTTP不mock。
fixture自种: 探针需要'归还术-扫描修复流程'+别名repair — 走治理面事件, 不碰live。"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:18731"
PORT = "18731"


def get(path):
    path = urllib.parse.quote(path, safe="/?=&")
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    ok_or = "PASS" if cond else "FAIL"
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"{ok_or} {name}" + (f"  | {detail}" if detail else ""))


# 0. 隔离环境: 临时DB + 18731口 + atexit拆除
import atexit
import tempfile

_tmp = tempfile.TemporaryDirectory(prefix="grimoire-probe-")
_srv = subprocess.Popen(
    [sys.executable, "grimoire.py", PORT],
    cwd="/home/ubuntu/Agent-Grimoire",
    env={**os.environ, "GRIMOIRE_DB": _tmp.name + "/probe.db"},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(lambda: (_srv.terminate(), _srv.wait(timeout=5)))
time.sleep(0.8)

NAME = "归还术-扫描修复流程"

# 自种fixture: 探针依赖的书+别名 (治理面, 不碰live)
s, b = post("/skill", {"name": NAME, "author": "probe", "tags": ["维修"],
                       "trigger": "刚修完bug想存修法", "body": "# 归还术\nx"})
check("fixture: 书入库", s == 200)
s, b = post("/event", {"kind": "skill.tag.alias.add", "operator": "probe",
                       "tag": "维修", "aliases": ["repair"]})
check("fixture: 别名登记", s == 200)

# ============ 以下为照照原版探针 v1, 探测逻辑未动 ============

# 1) rewrite双花+重放
s, b = get("/skill/" + NAME)
bh = b.split("baseline_hash: ")[1].splitlines()[0].strip()
s1, r1 = post("/event", {"kind": "skill.description.rewrite", "operator": "A",
                          "skill_id": NAME, "trigger": "A的改写v1", "baseline_hash": bh})
check("A用新鲜hash改写成功", s1 == 200, r1[:80])
s2, r2 = post("/event", {"kind": "skill.description.rewrite", "operator": "B",
                          "skill_id": NAME, "trigger": "B拿同一旧hash覆盖", "baseline_hash": bh})
check("B拿A改前hash被409挡(双花防护)", s2 == 409, r2[:80])
s3, r3 = post("/event", {"kind": "skill.description.rewrite", "operator": "A",
                          "skill_id": NAME, "trigger": "A重放同一请求", "baseline_hash": bh})
check("A重放旧hash也被409挡(重放防护)", s3 == 409, r3[:80])
s, b = get("/skill/" + NAME)
bh2 = b.split("baseline_hash: ")[1].splitlines()[0].strip()
check("改写后baseline_hash已轮换(本轮已修)", bh2 != bh)

# 2) tag注入
s, b = post("/skill", {"name": "注入探针书", "operator": "照照审",
                        "tags": ["<script>alert(1)</script>", "维修"],
                        "trigger": "x", "body": "y"})
d = json.loads(b)
sid = d.get("skill_id", "")
check("带script tag的skill照收(只记不拦)", s == 200, sid)
s, b = get("/map")
check("经图纯文本无转义(script原文进经图) — 观察项③", "<script>" in b)
s, b = get("/tag/<script>alert(1)</script>")
check("URL含<>仍404或200, 服务不炸", s in (200, 404), f"status={s}")

# 3) name空格变体
s, b = post("/skill", {"name": " 归还术-扫描修复流程 ", "body": "x"})
check("前后空格变体strip后撞同名挡(409)", s == 409, f"status={s}")

# 4) SQL注入
s, b = get("/skill/x%27%20OR%20%271%3D%271")
check("SQL注入路径安全(参数化)", s in (200, 404), f"status={s}")
s, b = post("/event", {"kind": "skill.pool.review", "operator": "x",
                        "skill_id": "x' OR '1'='1", "decision": "promoted"})
check("POST侧参数化安全", s in (200, 400, 404), f"status={s}")

# 5) 未鉴权治理面 — 观察项②
s, b = post("/event", {"kind": "skill.roster.update", "operator": "任意人",
                        "skill_id": sid, "layer": "archive"})
check("无鉴权,任意operator可archive任意书(观察项)", s == 200, b[:80])

# 6) merge别名迁移 (本轮已修: 先种山再迁别名)
post("/event", {"kind": "skill.tag.merge", "operator": "审",
                "old_tag": "维修", "new_tag": "维修2"})
s, b = get("/tag/repair")
check("merge后别名随书迁到新山(repair仍可查)", s == 200 and NAME in b)
s, b = get("/tag/维修2")
check("正字已改名", s == 200 and NAME in b)

# 7) events只追加不更新
s, b = get("/stats")
n1 = int(b.split("事件总数: ")[1].splitlines()[0])
post("/skill", {"name": "临时书-计数", "body": "t"})
s, b = get("/stats")
n2 = int(b.split("事件总数: ")[1].splitlines()[0])
check("事件单调递增", n2 > n1)

print(f"\n{ok}/{ok+fail} 探测完成 (FAIL含真实缺陷, 观察项单独看)")
sys.exit(1 if fail else 0)
