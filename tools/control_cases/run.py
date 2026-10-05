#!/usr/bin/env python3
"""山海路由层 control case 测试集 — runner。

方法论（KiroCrew eval harness 移植）：每个 skill 配「不该触发」的反例，
改指纹/改地图前后各跑一遍，反例失守=触发器松了，收紧再改。

被试协议（与 shanhai-route-bench BENCH.md 同源，判分加严）：
- 被试 = 干净上下文的 LLM，只看到经图 + 一条 query，不知道 ground truth
- 只准 GET（/map /tag /skill），禁止 POST（不写库不扫描）
- 答案格式强制：ROUTE: <书名|NONE> | 置信 | 理由
- 判分：
    PASS       命中 should_route
    SOFT       命中 can-reject 邻居（D3 对 content-platform-research 的现管口径）
    FAIL-LOOSE 落进 should_not_route（触发器过松，本测试集要抓的病）
    FAIL-TIGHT 该想起的没想起（NONE 或格式崩）
    FAIL-WILD  路由到期望+反例之外的第三者（地图里有本不相干的书被拽进来）

判分边界：被试可用的书 = FIXTURES 全集（含诱饵书）。第三者指 FIXTURES 内
非 should_route 非 should_not_route 的书——诱饵书被拽进阴性对照按 FAIL 算。

三种跑法：
  python3 tools/control_cases/run.py fingerprint   # 抓 live 路由指纹 → 与基线 diff（零API零LLM）
  python3 tools/control_cases/run.py baseline      # 真跑一轮基线（需 API）
  python3 tools/control_cases/run.py diff <A.json> <B.json>   # 比对两轮结果

fingerprint 直读 live DB（mode=ro，不碰服务不记账）——因为基线的语义就是
「live 的指纹变了要报警」，隔离种副本会把这个信号源掐掉。ro 连接只读不写，
对 live 零侵入。
baseline 真调 LLM（默认 huanapi/claude-opus-4-6，--api-base/--api-model/--api-key 可覆盖），
被试跑在隔离实例里（种同一套 fixture），结果落 results/<UTC时间戳>.json，
附当轮指纹哈希——每份结果永远可追到「跑在什么指纹上」。

流程纪律（「不改不测」）：任何动指纹/动地图/动 tag 的 commit，前后各跑一次
fingerprint（必跑，零成本）+ baseline（可跑），commit message 带两轮结果。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LIVE_DB = REPO / "grimoire.db"

CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
BASELINE_FP = HERE / "baseline_fingerprints.json"
RESULTS = HERE / "results"
PORT = "18745"  # 隔离区口段（18731 probe / 18745 control，不撞烟测）

# 被测技能集 = 三簇成员 + 阴性对照的诱饵书。trigger 基线由 fingerprint 命令
# 从 live 抓取（fixtures_live_pull），baseline 种库时也用 live 当前值——保证
# 测的就是现管地图，不是副本快照。body 故意短：路由只看描述行，正文短=逼被试靠指纹判。
MONITORED = [
    "activity-forensics", "delivery-forensics", "memory-retrieval-forensics",
    "varia-content", "xhs-content-production", "content-platform-research",
    "platform-account-research", "category-competitive-research",
    "community-user-analysis", "systematic-debugging", "vibe-diagnose",
    "归还术-扫描修复流程", "dev-disciplines",
    # 阴性对照诱饵（N1/N2 的 should_not_route 目标）
    "humanizer", "exploration-cron", "dayjob-support",
]

# D3 的现管口径：content-platform-research 与 platform-account-research 在「拆大号」
# 字面重叠（已报巡山使待收口），收口前它算 can-reject 邻居，不算 hard-fail。
CAN_REJECT = {("D3", "content-platform-research")}


def validate_cases(fixtures):
    """引用闭包校验：cases 引用的每本书必须在监控集且在馆——
    反例引用被试看不见的书=空钉（首轮验收真踩过：N1 引了不在监控集的
    creative-design，被试地图上没有这本书，「不该路由到它」永远假绿）。"""
    problems = []
    live_names = {n for n, m in fixtures.items() if not m.get("missing")}
    for c in CASES["cases"]:
        refs = ([c["should_route"]] if c["should_route"] != "NONE" else []) \
            + list(c["should_not_route"])
        for r in refs:
            if r not in live_names:
                problems.append(f"{c['id']} 引用 {r}：不在馆/不在监控集（空钉）")
        if c["should_route"] in c["should_not_route"]:
            problems.append(f"{c['id']}：should_route 与反例重叠")
    if problems:
        raise SystemExit("cases.json 引用闭包破损:\n  " + "\n  ".join(problems))


def fixtures_live_pull():
    """从 live 库拉 MONITORED 每本书的当前 (tags, trigger, layer, status)。
    ro 连接，零服务交互，零账本污染。书不在/已退役如实报。"""
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for name in MONITORED:
            r = con.execute(
                "SELECT layer, status FROM skills WHERE name=?", (name,)).fetchone()
            if not r:
                out[name] = {"missing": True}
                continue
            f = {}
            for x in con.execute(
                    "SELECT field, value FROM skill_fields WHERE skill_id="
                    "(SELECT skill_id FROM skills WHERE name=?)", (name,)):
                try:
                    f[x["field"]] = json.loads(x["value"])
                except (ValueError, TypeError):
                    f[x["field"]] = x["value"]
            out[name] = {
                "layer": r["layer"], "status": r["status"],
                "tags": f.get("tags", []), "trigger": f.get("trigger", ""),
            }
        return out
    finally:
        con.close()


# ---------------- fingerprint ----------------

def fingerprint_cmd():
    live = fixtures_live_pull()
    validate_cases(live)
    # 只对「在馆且 verified」的书取指纹——retired/missing 是地图事件，单独报
    fp, events = {}, []
    for name, meta in live.items():
        if meta.get("missing"):
            events.append(f"- {name} 不在馆（监控集里它没了——退役？改名？要查）")
        elif meta["status"] != "verified":
            events.append(f"? {name} status={meta['status']}（非 verified，读面不亮，路由面已变）")
        else:
            fp[name] = {"tags": meta["tags"], "trigger": meta["trigger"]}
    h = hashlib.sha256(
        json.dumps(fp, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    short = h[:16]
    print(f"指纹 sha256:{short}…  覆盖 {len(fp)}/{len(MONITORED)} 本在馆 verified 书")
    if events:
        print("\n地图事件（监控集成员状态变化）：")
        for e in events:
            print("  " + e)

    base = json.loads(BASELINE_FP.read_text(encoding="utf-8"))
    bskills = base.get("skills", {})
    drift = []
    for name in sorted(set(fp) | set(bskills)):
        cur, old = fp.get(name), bskills.get(name)
        if old and not cur:
            drift.append(f"- {name}：基线有、现管无")
        elif cur and not old:
            drift.append(f"+ {name}：现管新入监控（基线没盖到它）")
        elif cur and old:
            if cur["trigger"] != old["trigger"]:
                drift.append(f"~ {name}：trigger 漂移")
                drift.append(f"      旧: {old['trigger'][:70]}")
                drift.append(f"      新: {cur['trigger'][:70]}")
            if cur["tags"] != old["tags"]:
                drift.append(f"~ {name}：tags 漂移 {old['tags']} → {cur['tags']}")
    if drift:
        print("\n⚠ 指纹漂移（改指纹/地图后属预期；无缘无故漂移=要查）：")
        for d in drift:
            print("  " + d)
        print("\n指纹变了 → 这正是本测试集存在的理由：先跑 baseline 看新指纹的路由质量，")
        print("结果过了再 fingerprint --rebase 重新盖章。")
        return 2
    print("✓ 与基线一致（trigger+tags 零漂移）")
    return 0


def fingerprint_rebase():
    live = fixtures_live_pull()
    fp = {n: {"tags": m["tags"], "trigger": m["trigger"]}
          for n, m in live.items() if not m.get("missing") and m["status"] == "verified"}
    blob = json.dumps(fp, ensure_ascii=False, sort_keys=True, indent=1)
    h = hashlib.sha256(blob.encode()).hexdigest()
    BASELINE_FP.write_text(json.dumps({
        "comment": "基线指纹：control cases 覆盖的书本的路由指纹（trigger+tags）。"
                   "改指纹/改地图后有意变更时，跑 baseline 验证新指纹路由质量，"
                   "过了再 fingerprint --rebase 重新盖章。",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fingerprint_sha256": h,
        "capture_command": "python3 tools/control_cases/run.py fingerprint --rebase",
        "skills": fp,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 重新盖章：{len(fp)} 本书，sha256:{h[:16]}… → {BASELINE_FP.name}")
    return 0


# ---------------- isolated grimoire（baseline 用） ----------------

def http(method, path, obj=None, timeout=15):
    url = f"http://127.0.0.1:{PORT}{urllib.parse.quote(path, safe='/?=&')}"
    data = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class IsolatedGrimoire:
    """隔离实例：临时 DB + 18745 口 + 种 live 当前指纹 + 拆除。不碰 live。"""

    def __init__(self, fixtures):
        self.tmp = tempfile.TemporaryDirectory(prefix="grimoire-ctrl-")
        env = {**os.environ,
               "GRIMOIRE_DB": self.tmp.name + "/ctrl.db",
               "GRIMOIRE_BUDGET_WINDOW": "3600", "GRIMOIRE_BUDGET_MAX": "500",
               "GRIMOIRE_OWNER": "ctrl-runner"}
        self.srv = subprocess.Popen(
            [sys.executable, "grimoire.py", PORT], cwd=str(REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                if http("GET", "/health")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("isolated grimoire failed to start")
        self.frozen = {n: m for n, m in fixtures.items() if not m.get("missing")}

    def seed(self):
        """种库：全部按 index 层种（core/pinned 层差异会改变注入形态，
        是另一层变量——本测试集钉的是 trigger 指纹，控制变量法把层归一）。"""
        for name, m in self.frozen.items():
            s, b = http("POST", "/skill", {
                "name": name, "author": "ctrl-runner", "tags": m["tags"],
                "trigger": m["trigger"], "body": f"# {name}\n(fixture 正文)"})
            if s != 200:
                raise RuntimeError(f"seed {name}: {s} {b[:120]}")
            s, b = http("POST", "/event", {
                "kind": "skill.pool.review", "operator": "ctrl-runner",
                "skill_id": name, "decision": "promoted"})
            if s != 200:
                raise RuntimeError(f"promote {name}: {s} {b[:120]}")

    def map_text(self):
        s, b = http("GET", "/map")
        if s != 200:
            raise RuntimeError(f"map: {s}")
        return b

    def close(self):
        self.srv.terminate()
        try:
            self.srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.srv.kill()
        self.tmp.cleanup()


# ---------------- baseline（真调 LLM） ----------------

SYSTEM_PROMPT = """你是agent，刚被唤醒。你有一张技能经图（tag树+描述行）。用户来了一句话，你的唯一任务：判断该不该翻某本技能书。

你不是执行者。不要做任务，不要给方案，不要写清单——只做路由判断。

规则：
- 只根据经图里的描述行判断，不猜不编不脑补技能内容
- 经图里没有合适的书就答 NONE（答 NONE 不是失败）
- 输出必须严格只有一行，格式：ROUTE: <书名或NONE> | 置信(高/中/低) | 一句话理由
- 书名必须逐字抄经图里存在的名字，不要发明"""


def call_llm(api_base, api_key, api_model, system, user):
    payload = {"model": api_model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": 0.2, "max_tokens": 200}
    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def normalize_route(route, frozen):
    """被试偶尔把书名包进 GET /skill/<name> 之类的动作句里（模型把
    「宣布路由」写成「执行取书」）。规范化：图上书名在路由串里逐字出现
    即视为路由到该书；NONE 识别同理。多个命中取最长书名（更具体者胜）。"""
    r = route.strip()
    if not r or r.upper().startswith("NONE"):
        return "NONE"
    hits = [n for n in frozen if n in r]
    if hits:
        return max(hits, key=len)
    return r


def parse_route(raw):
    for line in raw.splitlines():
        line = line.strip()
        if line.upper().startswith("ROUTE:"):
            body = line[6:].strip()
            parts = [p.strip() for p in body.split("|")]
            route = parts[0] if parts else "NONE"
            if route.upper() in ("NONE", ""):
                route = "NONE"
            return {"route": route,
                    "conf": parts[1] if len(parts) > 1 else "",
                    "reason": parts[2] if len(parts) > 2 else ""}
    return {"route": "(unparsed)", "conf": "", "reason": raw.strip()[:120]}


def judge(case, route, frozen):
    want, fOrbidden = case["should_route"], case["should_not_route"]
    if route == want:
        return "PASS"
    if route in fOrbidden:
        return "SOFT" if (case["id"], route) in CAN_REJECT else "FAIL-LOOSE"
    if route in ("NONE", "(unparsed)"):
        return "FAIL-TIGHT"
    if route in frozen:
        return "FAIL-WILD"
    return "FAIL-WILD"  # 书名不在图上（幻觉书）也按野处理，raw 里留着证据


def summarize(results):
    n = {}
    for r in results:
        n[r["verdict"]] = n.get(r["verdict"], 0) + 1
    hard = n.get("FAIL-LOOSE", 0) + n.get("FAIL-TIGHT", 0) + n.get("FAIL-WILD", 0)
    line = (f"共{len(results)}题：PASS {n.get('PASS',0)} / SOFT {n.get('SOFT',0)} / "
            f"FAIL-LOOSE(松) {n.get('FAIL-LOOSE',0)} / FAIL-TIGHT(紧) {n.get('FAIL-TIGHT',0)} / "
            f"FAIL-WILD(野) {n.get('FAIL-WILD',0)}")
    return {"counts": n, "hard_fail": hard, "line": line}


def _huanapi_from_hermes():
    """hermes auth.json credential pool 里有 huanapi token（本机部署态，loopback 部署细节）。"""
    try:
        pool = json.loads(Path("/home/ubuntu/.hermes/auth.json").read_text())
        for item in pool.get("credential_pool", {}).get("custom:huanapi", []):
            tok = item.get("access_token")
            if tok:
                return tok
    except Exception:
        pass
    return ""


def baseline_cmd(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="https://api.huanapi.com/v1")
    ap.add_argument("--api-model", default="claude-opus-4-6")
    ap.add_argument("--api-key", default="")
    args = ap.parse_args(argv)
    api_key = args.api_key or os.environ.get("HUANAPI_API_KEY") or _huanapi_from_hermes()
    if not api_key:
        print("缺 API key：--api-key 或 HUANAPI_API_KEY")
        return 1

    fixtures = fixtures_live_pull()
    validate_cases(fixtures)
    missing = [n for n, m in fixtures.items() if m.get("missing")]
    if missing:
        print(f"⚠ 监控集里这些书不在馆，本轮 baseline 跳过它们：{missing}")

    iso = IsolatedGrimoire(fixtures)
    import atexit
    reg = atexit.register(iso.close)
    results = []
    try:
        iso.seed()
        map_text = iso.map_text()
        for case in CASES["cases"]:
            user = (f"## 经图\n{map_text}\n\n## 用户的话\n{case['query']}\n\n"
                    f"（记住：只输出一行 ROUTE: ... ，不要执行任务）")
            raw, attempt = "", 0
            while attempt < 3:
                attempt += 1
                try:
                    raw = call_llm(args.api_base, api_key, args.api_model,
                                   SYSTEM_PROMPT, user)
                except Exception as e:
                    raw = f"API错误: {e}"
                    continue
                if parse_route(raw)["route"] not in ("(unparsed)",):
                    break  # 格式合格才收
                # 空回复/格式崩：重试（空回复在本 API 上偶发，重试即恢复）
                time.sleep(1)
            parsed = parse_route(raw)
            parsed["route_raw"] = parsed["route"]
            parsed["route"] = normalize_route(parsed["route"], iso.frozen)
            verdict = judge(case, parsed["route"], iso.frozen)
            results.append({"id": case["id"], "cluster": case["cluster"],
                            "query": case["query"], "raw": raw.strip()[:300],
                            "parsed": parsed, "verdict": verdict,
                            "expect": case["should_route"],
                            "fOrbidden": case["should_not_route"]})
            mark = {"PASS": "✓", "SOFT": "◐", "FAIL-LOOSE": "✗松",
                    "FAIL-TIGHT": "✗紧", "FAIL-WILD": "✗野"}.get(verdict, "?")
            print(f"  {mark} {case['id']:3s} {verdict:10s} → {parsed['route'][:44]}"
                  f"  (期望 {case['should_route']})")
    finally:
        atexit.unregister(reg)
        iso.close()

    summary = summarize(results)
    RESULTS.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = RESULTS / f"baseline-{ts}.json"
    blob = json.dumps(
        {"ran_at_utc": ts, "api_model": args.api_model,
         "fixtures_frozen": {n: {"tags": m["tags"], "trigger": m["trigger"]}
                             for n, m in iso.frozen.items()},
         "n_cases": len(results), "summary": summary, "results": results},
        ensure_ascii=False, indent=1)
    fp_hash = hashlib.sha256(blob.encode()).hexdigest()[:16]
    out.write_text(blob, encoding="utf-8")
    print(f"\n{summary['line']}")
    print(f"结果落 {out.name}（含当轮冻结指纹，全文哈希 {fp_hash}…）")
    return 0 if summary["hard_fail"] == 0 else 3


# ---------------- diff ----------------

def diff_cmd(argv):
    if len(argv) < 2:
        print("用法: run.py diff <A.json> <B.json>")
        return 1
    a = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    b = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    ra = {r["id"]: r for r in a["results"]}
    rb = {r["id"]: r for r in b["results"]}
    print(f"A: {a.get('ran_at_utc')} {a['summary']['line']}")
    print(f"B: {b.get('ran_at_utc')} {b['summary']['line']}")
    print()
    changed = 0
    for cid in sorted(set(ra) | set(rb)):
        va = ra.get(cid, {}).get("verdict", "(缺)")
        vb = rb.get(cid, {}).get("verdict", "(缺)")
        if va != vb:
            changed += 1
            if vb in ("FAIL-LOOSE", "FAIL-WILD") and va in ("PASS", "SOFT"):
                arrow = "🔴松了"
            elif vb == "FAIL-TIGHT" and va in ("PASS", "SOFT"):
                arrow = "🔵紧了"
            else:
                arrow = "🔄"
            pa = ra.get(cid, {}).get("parsed", {}).get("route", "?")
            pb = rb.get(cid, {}).get("parsed", {}).get("route", "?")
            print(f"  {arrow} {cid}: {va} → {vb}   ({pa} → {pb})")
    if changed == 0:
        print("  （零漂移：两轮判分完全一致）")
    else:
        print(f"\n共 {changed} 题判分漂移——改指纹的 commit 请对照本 diff 说明每一处")
    return 0


# ---------------- main ----------------

def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "fingerprint":
        if "--rebase" in rest:
            return fingerprint_rebase()
        return fingerprint_cmd()
    if cmd == "baseline":
        return baseline_cmd(rest)
    if cmd == "diff":
        return diff_cmd(rest)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
