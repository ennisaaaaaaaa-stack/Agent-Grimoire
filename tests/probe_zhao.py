#!/usr/bin/env python3
"""照照审阅包·超套件探测 v1 — 只打作者测试外的边界（全部对着活服务）。"""
import json, urllib.request, urllib.error, urllib.parse, sys

BASE = "http://127.0.0.1:8730"

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
    if cond: ok += 1
    else: fail += 1
    print(f"{ok_or} {name}" + (f"  | {detail}" if detail else ""))

NAME = "归还术-扫描修复流程"

# 1) rewrite 双花: A改完, B拿改前hash不能过; A自己重复提交自己刚提交的也不能静默过
s, b = get("/skill/" + NAME)
bh = b.split("baseline_hash: ")[1].splitlines()[0].strip()
s1, r1 = post("/event", {"kind": "skill.description.rewrite", "operator": "A",
                          "skill_id": NAME, "trigger": "A的改写v1", "baseline_hash": bh})
check("A用新鲜hash改写成功", s1 == 200, r1[:80])
s2, r2 = post("/event", {"kind": "skill.description.rewrite", "operator": "B",
                          "skill_id": NAME, "trigger": "B拿同一旧hash覆盖", "baseline_hash": bh})
check("B拿A改前hash被409挡(双花防护)", s2 == 409, r2[:80])
# A再拿同一个旧hash重放自己刚成功的请求
s3, r3 = post("/event", {"kind": "skill.description.rewrite", "operator": "A",
                          "skill_id": NAME, "trigger": "A重放同一请求", "baseline_hash": bh})
check("A重放旧hash也被409挡(重放防护)", s3 == 409, r3[:80])
# 但hash本身没轮换——GET确认
s, b = get("/skill/" + NAME)
bh2 = b.split("baseline_hash: ")[1].splitlines()[0].strip()
check("改写后baseline_hash未轮换(观察项,非阻断)", bh2 == bh)

# 2) tag注入攻击: 提交带script/img标签的tag, 经图是纯文本注入面吗
s, b = post("/skill", {"name": "注入探针书", "operator": "照照审",
                        "tags": ["<script>alert(1)</script>", "维修"],
                        "trigger": "x", "body": "y"})
d = json.loads(b)
sid = d.get("skill_id", "")
check("带script tag的skill照收(只记不拦)", s == 200, sid)
s, b = get("/map")
check("经图纯文本无转义(script原文进经图)", "<script>" in b)
s, b = get("/tag/<script>alert(1)</script>")
check("URL含<>仍404或200, 服务不炸", s in (200, 404), f"status={s}")

# 3) name唯一性大小写/unicode
s, b = post("/skill", {"name": " 归还术-扫描修复流程 ", "body": "x"})
check("前后空格变体绕过同名挡", s in (200, 409), f"status={s}")

# 4) skill_id参数注入SQL
s, b = get("/skill/x%27%20OR%20%271%3D%271")
check("SQL注入路径安全(参数化)", s in (200, 404), f"status={s}")
s, b = post("/event", {"kind": "skill.pool.review", "operator": "x",
                        "skill_id": "x' OR '1'='1", "decision": "promoted"})
check("POST侧参数化安全", s in (200, 400, 404), f"status={s}")

# 5) 未鉴权治理面: 谁都能当巡山使
s, b = post("/event", {"kind": "skill.roster.update", "operator": "任意人",
                        "skill_id": sid, "layer": "archive"})
check("无鉴权,任意operator可archive任意书(观察项)", s == 200, b[:80])

# 6) tag.merge会把别名山连根删
post("/event", {"kind": "skill.tag.merge", "operator": "审",
                "old_tag": "维修", "new_tag": "维修2"})
s, b = get("/tag/repair")
check("merge后别名随山消失(repair 404)", s == 404)
s, b = get("/tag/维修2")
check("正字已改名", s == 200 and "归还术" in b)

# 7) events只追加不更新的硬核验证: 事件总数只增不减
s, b = get("/stats")
n1 = int(b.split("事件总数: ")[1].splitlines()[0])
post("/skill", {"name": "临时书-计数", "body": "t"})
s, b = get("/stats")
n2 = int(b.split("事件总数: ")[1].splitlines()[0])
check("事件单调递增", n2 > n1)

print(f"\n{ok}/{ok+fail} 探测完成 (FAIL含真实缺陷, 观察项单独看)")
sys.exit(0)
