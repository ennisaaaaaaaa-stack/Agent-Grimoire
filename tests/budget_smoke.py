#!/usr/bin/env python3
"""one-mutation budget 专项烟测 — 独立迷你实例(MAX=1小窗口), 不碰 live。

验证四件事:
1. 巡山使第1笔 govern 动作放行
2. 第2笔起 429 拒绝, 响应带 countdown/hint
3. 豁免 operator(hui) 不受限
4. 遥测事件不占预算 (429状态下 telemetry 仍可落账)
拆除纪律与 smoke.py 同款: atexit 必杀。
"""
import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_PORT = 18739
BASE = f"http://127.0.0.1:{_PORT}"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {detail}" if detail else ""))


def post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


_tmpdir = tempfile.TemporaryDirectory(prefix="grimoire-budget-")
_srv = subprocess.Popen(
    [sys.executable, "grimoire.py", str(_PORT)],
    cwd="/home/ubuntu/Agent-Grimoire",
    env={**os.environ, "GRIMOIRE_DB": _tmpdir.name + "/budget.db",
         "GRIMOIRE_BUDGET_WINDOW": "3600", "GRIMOIRE_BUDGET_MAX": "1"},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(lambda: (_srv.terminate(), _srv.wait(timeout=5)))
time.sleep(0.8)

# 0. health
try:
    with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
        s, b = r.status, r.read().decode()
except Exception as e:
    s, b = 0, str(e)
check("health", s == 200, b)

# 1. 种两本测试书 (submit 是库主特权路径, 不占预算)
s, b = post("/skill", {"name": "budget-test-a", "tags": ["预算"], "body": "A"})
check("种书a", s == 200, b[:80])
s, b = post("/skill", {"name": "budget-test-b", "tags": ["预算"], "body": "B"})
check("种书b", s == 200, b[:80])

# 2. 巡山使第1笔 govern: 转正 budget-test-a → 200
s, b = post("/event", {"kind": "skill.pool.review", "operator": "巡山使",
                       "skill_id": "budget-test-a", "decision": "promoted"})
check("第1笔放行", s == 200, b[:100])

# 3. 第2笔 govern: 转正 budget-test-b → 429
s, b = post("/event", {"kind": "skill.pool.review", "operator": "巡山使",
                       "skill_id": "budget-test-b", "decision": "promoted"})
d = json.loads(b) if s == 429 else {}
check("第2笔429", s == 429, b[:120])
check("429带countdown", "window_resets_at" in d, str(d.get("window_resets_at")))
check("429带契约hint", "单次巡视至多一个" in d.get("hint", ""), d.get("hint", "")[:60])

# 3b. 429 时动作本身未执行 (书b仍是draft)
try:
    with urllib.request.urlopen(BASE + "/skill/budget-test-b", timeout=5) as r:
        body = r.read().decode()
except urllib.error.HTTPError as e:
    body = e.read().decode()
check("被拒动作未生效(b仍draft)", '"status": "draft"' in body or "draft" in body,
      body[:80])

# 4. 豁免: hui 第2笔直接放行
s, b = post("/event", {"kind": "skill.pool.review", "operator": "hui",
                       "skill_id": "budget-test-b", "decision": "promoted"})
check("豁免operator(hui)放行", s == 200, b[:100])

# 5. 遥测不占预算: 巡山使已被拦, 但 telemetry 仍可落账
s, b = post("/event", {"kind": "skill.telemetry.expand", "operator": "巡山使",
                       "skill_id": "budget-test-a"})
check("遥测不占预算仍可落账", s == 200, b[:80])

# 6. 新 operator 有自己的预算 (不共享)
s, b = post("/event", {"kind": "skill.pool.review", "operator": "别的使",
                       "skill_id": "budget-test-a", "decision": "promoted"})
check("预算按operator隔离", s == 200, b[:100])

fails = sum(1 for _, ok, _ in results if not ok)
print(f"\n{len(results) - fails}/{len(results)} PASS")
sys.exit(1 if fails else 0)
