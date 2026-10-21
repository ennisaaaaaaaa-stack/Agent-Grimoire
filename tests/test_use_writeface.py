#!/usr/bin/env python3
"""use/inject 写面 (T23) 专项烟测 — 独立迷你实例, 不碰 live。

病根: events 表全史零 skill.use/skill.inject — 转正判据挂 use 层但记录仪没装。
修法 (洄洄 10/20, 堂屋 #1107): get_skill 命中落 use, render_map 落 inject。
use 语义钉死: 取用代理指标, 非用上了 (见 docs/use-writeface.md)。

验证六件事:
1. get_skill 命中(200) → events 落 skill.use 行 (operator/skill_id/name 对得上)
2. 门拒零事件: 404(不存在) 与 403(draft门) 不落任何事件 (阴性对照)
3. GET /map → events 落 skill.inject 行 (operator对上, 带map_bytes)
4. 落账不碰响应体: 响应与构造的期望体逐字节全等; 连取两次响应全等 (只添账不改脸)
5. 同书连取两次 → 两行 skill.use (不合并不幂等 — use计数就是要数次数)
另: POST /event 伪造 skill.use/skill.inject 必须被拒 (use只能读面内部落账);
     /stats 内部渲染不算注入 (量尺不虚增inject账)。
拆除纪律与 smoke.py 同款: atexit 必杀。
"""
import atexit
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_PORT = 18743
BASE = f"http://127.0.0.1:{_PORT}"
results = []

NAME = "取用记账书"            # verified 书: 命中路径
BODY = "# 取用记账\n正文第一行。"
DRAFT = "门内草稿书"            # draft 书: 403 门路径
AUTO = "auto/取用新兵"          # auto/ 书: 免审阅头直读路径(转正判据正主)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {detail}" if detail else ""))


def get(path, headers=None):
    path = urllib.parse.quote(path, safe="/")
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
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


# 0. 起隔离实例: 临时库 + 临时端口
_tmpdir = tempfile.TemporaryDirectory(prefix="grimoire-useface-")
_DB = _tmpdir.name + "/useface.db"
_srv = subprocess.Popen(
    [sys.executable, "grimoire.py", str(_PORT)],
    cwd=str(Path(__file__).resolve().parent.parent),
    env={**os.environ, "GRIMOIRE_DB": _DB,
         "GRIMOIRE_BUDGET_WINDOW": "3600", "GRIMOIRE_BUDGET_MAX": "50"},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(lambda: (_srv.terminate(), _srv.wait(timeout=5)))
time.sleep(0.8)


def events(kind=None):
    """直读临时库 events 表 — 账本断言不打HTTP读面, 不依赖任何聚合端点。"""
    con = sqlite3.connect(_DB)
    if kind:
        r = con.execute(
            "SELECT event_id, operator, kind, payload FROM events "
            "WHERE kind=? ORDER BY event_id", (kind,)).fetchall()
    else:
        r = con.execute(
            "SELECT event_id, operator, kind, payload FROM events "
            "ORDER BY event_id").fetchall()
    con.close()
    return r


def payloads(rows):
    return [json.loads(p) for _, _, _, p in rows]


s, b = get("/health")
check("health", s == 200, b)

# fixture 三本: verified(命中路径) + draft(门路径) + auto/(免审直读路径)
s, b = post("/skill", {"name": NAME, "author": "useface", "body": BODY})
check("fixture: verified书入库", s == 200, b[:80])
_sid = json.loads(b)["skill_id"] if s == 200 else ""
s, b = post("/event", {"kind": "skill.pool.review", "operator": "巡山使",
                       "skill_id": NAME, "decision": "promoted"})
check("fixture: 转正", s == 200, b[:60])
s, b = post("/skill", {"name": DRAFT, "author": "useface", "body": "# 草稿\n门内。"})
check("fixture: draft书入库", s == 200, b[:80])
s, b = post("/skill/auto", {"operator": "useface", "name": AUTO,
                            "tags": ["维修"], "body": "# 新兵正文\n取我就算use。"})
check("fixture: auto书入库", s == 200, b[:80])
_asid = json.loads(b)["skill_id"] if s == 200 else ""

# ── 阴性对照先行: 门拒零事件 (任何成功GET之前做, 账面干净才咬得住) ──
_total0 = len(events())
s, b = get("/skill/根本不存在的书")
check("404(不存在)", s == 404, b[:60])
s, b = get(f"/skill/{DRAFT}")
check("403(draft门)", s == 403, b[:60])
check("门拒零事件(404/403后events总数不变, 零skill.use)",
      len(events()) == _total0 and not events("skill.use"),
      f"total {_total0}->{len(events())} use={len(events('skill.use'))}")

# ── 命中: verified书取用 → skill.use 落账 + 响应逐字节全等 ──
s, b1 = get(f"/skill/{NAME}", headers={"X-Operator": "zhaozhao"})
_h = hashlib.sha256(BODY.encode()).hexdigest()
# 注: POST /skill 把 trigger/boundary/why 也按 json.dumps 落库, 缺省空串
# 存成两字节文本 '\"\"' — 渲染时非tags/aliases字段不解析, 原样出引号。
_expected = (f"# {NAME}\n"
             f"layer: index  status: verified\n"
             f"tags: []\n"
             f"trigger: \"\"\n"
             f"boundary: \"\"\n"
             f"why: \"\"\n"
             f"baseline_hash: {_h}\n\n"
             + BODY +
             f"\n\n---\n用完顺手记一笔: POST /event "
             '{"kind":"skill.telemetry.expand","operator":"<你>","skill_id":"'
             + NAME + '"} — 好用/没用, 你判断.)')
check("命中200+响应逐字节不变(与构造期望体全等)", s == 200 and b1 == _expected,
      "" if b1 == _expected else b1[:120])
_use = events("skill.use")
_up = payloads(_use)
check("命中落skill.use行(operator/skill_id/name对得上)",
      len(_use) == 1 and _use[0][1] == "zhaozhao"
      and _up[0]["skill_id"] == _sid and _up[0]["name"] == NAME,
      f"rows={len(_use)} op={_use[0][1] if _use else '-'} "
      f"payload={json.dumps(_up[0], ensure_ascii=False) if _up else '-'}")

# ── 连取两次: 两行(不合并不幂等) + 两次响应逐字节全等 ──
s, b2 = get(f"/skill/{NAME}", headers={"X-Operator": "zhaozhao"})
check("同书连取两次=两行skill.use(use计数就是要数次数)",
      len(events("skill.use")) == 2, f"rows={len(events('skill.use'))}")
check("两次响应逐字节全等(只添账不改脸)", b1 == b2)

# ── 缺省operator: 不带X-Operator头 → operator=unknown (与render_map签名一致) ──
s, b = get(f"/skill/{NAME}")
check("缺省operator=unknown", s == 200
      and [r for r in events("skill.use") if r[1] == "unknown"],
      f"rows={len(events('skill.use'))}")

# ── inject: GET /map 落账; /stats内部渲染不算注入; 再/map再一笔 ──
s, b = get("/map", headers={"X-Operator": "zhaozhao"})
_inj = events("skill.inject")
_ip = payloads(_inj)
check("GET /map落skill.inject行(operator+map_bytes)",
      s == 200 and len(_inj) == 1 and _inj[0][1] == "zhaozhao"
      and isinstance(_ip[0].get("map_bytes"), int)
      and _ip[0]["map_bytes"] == len(b.encode()),
      f"rows={len(_inj)} payload={json.dumps(_ip[0], ensure_ascii=False) if _ip else '-'}")
s, b = get("/stats")
check("/stats内部渲染不落inject(量尺不是注入)",
      len(events("skill.inject")) == 1, f"rows={len(events('skill.inject'))}")
s, b = get("/map", headers={"X-Operator": "zhaozhao"})
check("再拉/map再落一笔(inject也不幂等)", len(events("skill.inject")) == 2,
      f"rows={len(events('skill.inject'))}")

# ── 门后200的另两条路径: 审阅态draft / auto免审直读 (200即use, 待照照裁注记见docs) ──
s, b = get(f"/skill/{DRAFT}", headers={"X-Review-Draft": "1",
                                       "X-Operator": "reviewer"})
_duse = [r for r in events("skill.use") if r[1] == "reviewer"]
check("审阅头读draft=200→记use(观察项: 审阅读算不算use, docs留档待裁)",
      s == 200 and len(_duse) == 1
      and payloads(_duse)[0]["name"] == DRAFT,
      f"status={s} reviewer_rows={len(_duse)}")
s, b = get(f"/skill/{AUTO}", headers={"X-Operator": "zhaozhao"})
_ause = [r for r in events("skill.use")
         if payloads([r])[0]["name"] == AUTO]
check("auto/免审直读=200→记use(转正判据正主路径)",
      s == 200 and "# 新兵正文" in b and len(_ause) == 1
      and payloads(_ause)[0]["skill_id"] == _asid,
      f"status={s} rows={len(_ause)}")

# ── 反伪造: use/inject只能读面内部落账, POST /event 一律拒 ──
s, b = post("/event", {"kind": "skill.use", "operator": "evil",
                       "skill_id": NAME})
check("POST /event伪造skill.use被拒(400)", s == 400, f"{s} {b[:80]}")
s, b = post("/event", {"kind": "skill.inject", "operator": "evil"})
check("POST /event伪造skill.inject被拒(400)", s == 400, f"{s} {b[:80]}")
check("伪造被拒后use账未虚增",
      len(events("skill.use")) == 5, f"rows={len(events('skill.use'))}")

fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} 绿")
sys.exit(1 if fails else 0)
