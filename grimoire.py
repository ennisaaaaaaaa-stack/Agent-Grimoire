#!/usr/bin/env python3
"""山海 / Agent Grimoire — skill manager service.

读脸（session开局拉）: GET /map, GET /tag/<tag>, GET /skill/<id>
写脸（遥测落账）:      POST /event
提交（draft起步）:     POST /skill — 三面扫描报告挂上, 只记不拦

依据: portalk-contract-skill-lifecycle v0.2
- 开场注入四件套: tag树 + 前置表 + tag↔skill映射(短名) + 查询语法说明; 描述行检索期才拉
- tags[] 与 submit 事件的 tags 同源同名
- 只记不动: events 表只追加, 不触发任何变更
- 扫描坛建藏书阁门口: 所有写入统一过(含自梳理)
"""
import json
import os
import re
import shutil
import sqlite3
import hashlib
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("GRIMOIRE_DB", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "grimoire.db"))
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
VAULT_DIR = os.environ.get(
    "GRIMOIRE_VAULT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault"))
LISTEN_HOST = "127.0.0.1"

# ── one-mutation budget (契约v0.2 §策展人: 单次巡视至多一个落盘动作) ──
# 服务端强制: 同一 operator 在滚动窗口内 governance 事件达到上限后, 第二笔起 429。
# 窗口/上限可用 GRIMOIRE_BUDGET_WINDOW(秒)/GRIMOIRE_BUDGET_MAX 覆盖 (烟测用小窗)。
# 豁免: 库主 hui 不受限 (手动批处理是库主特权); 遥测事件不占预算。
BUDGET_WINDOW = int(os.environ.get("GRIMOIRE_BUDGET_WINDOW", 24 * 3600))
BUDGET_MAX = int(os.environ.get("GRIMOIRE_BUDGET_MAX", 2))
BUDGET_EXEMPT = {"hui"}

# ── 三层门 (v0.5): 库主身份。读面全开(含family/private全部)。 ──
# 与 BUDGET_EXEMPT 同人但语义不同: 那边是治理豁免, 这边是可见性豁免。
LIBRARY_OWNER = os.environ.get("GRIMOIRE_OWNER", "hui")

# 契约 v0.2 域6全量事件（skill.checkin.scan 由提交路径内部落账，不经 POST /event）
TELEMETRY_KINDS = {          # 只记不动：遥测永远不触发变更
    "skill.telemetry.push",
    "skill.telemetry.expand",
}
GOVERNANCE_KINDS = {         # 写脸：策展动作，动作+落账一起（契约"落盘动作走管理工具，全部产生账本事件"）
    "skill.pool.review",     # decision: promoted|discarded
    "skill.roster.update",   # layer: core|index|archive
    "skill.tag.merge",       # old_tag -> new_tag
    "skill.tag.alias.add",   # 登记别名 (查询侧折叠用)
    "skill.prereq.update",   # 边增删
    "skill.description.rewrite",  # 改写绑 baseline_hash，不符→409 stale
    "skill.pool.withdraw",   # draft物理撤回(探针/误提交清理); verified走review
}
EVENT_KINDS = TELEMETRY_KINDS | GOVERNANCE_KINDS

_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _ts_epoch(ts: str) -> float:
    """ISO文本 → epoch秒 (解析失败返回0, 预算宁可漏拦不可误拦)。"""
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return 0.0


def budget_check(con, operator: str):
    """one-mutation budget: 窗口内 governance 事件已达上限则返回拒绝信息。

    返回 None = 放行; 返回 dict = 拒绝(HTTP 429)。
    豁免: BUDGET_EXEMPT 内的 operator (库主手动批处理)。
    """
    if operator in BUDGET_EXEMPT:
        return None
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.gmtime(time.time() - BUDGET_WINDOW))
    gov_ph = ",".join("?" for _ in GOVERNANCE_KINDS)
    n = con.execute(
        f"SELECT COUNT(*) FROM events WHERE operator=? AND kind IN "
        f"({gov_ph}) AND ts >= ?",
        (operator, *GOVERNANCE_KINDS, cutoff)).fetchone()[0]
    if n < BUDGET_MAX:
        return None
    oldest = con.execute(
        f"SELECT MIN(ts) FROM events WHERE operator=? AND kind IN "
        f"({gov_ph}) AND ts >= ?",
        (operator, *GOVERNANCE_KINDS, cutoff)).fetchone()[0]
    reset_at = (_ts_epoch(oldest) + BUDGET_WINDOW
                if oldest else time.time() + BUDGET_WINDOW)
    return {
        "error": f"one-mutation budget: 窗口{BUDGET_WINDOW}s内已有{n}笔落盘动作, "
                 f"上限{BUDGET_MAX}",
        "governance_events_in_window": n,
        "budget_max": BUDGET_MAX,
        "window_seconds": BUDGET_WINDOW,
        "window_resets_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.gmtime(reset_at)),
        "hint": "契约v0.2: 单次巡视至多一个落盘动作。想动第二笔写进策展日志下周再动。",
    }


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def slugify(name):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", name.strip().lower())
    return s.strip("-")[:60] or "unnamed"


def rows(rows_or_none):
    return rows_or_none or []


# ---------------- 经图 (opening map) ----------------

def render_map(operator: str = "unknown"):
    con = db()
    try:
        tags = con.execute(
            "SELECT tag, parent, note FROM tags ORDER BY tag").fetchall()
        # R3(外聘审计): 默认经图只亮verified书点亮的山 — draft-only山不进开场注入
        # (山照常长: tags表submit时照种; 没进馆的书不点亮山头。巡山使走/darkzone看全量)
        vis_clause, vis_args = visible_skills_clause(operator)
        mapping = con.execute(
            "SELECT s.name, s.layer, f.value AS tags "
            "FROM skills s LEFT JOIN skill_fields f "
            "ON f.skill_id = s.skill_id AND f.field = 'tags' "
            "WHERE s.status = 'verified' AND s.layer != 'archive'" + vis_clause +
            " ORDER BY s.name", vis_args).fetchall()
        by_tag = {}
        for m in rows(mapping):
            for t in (json.loads(m["tags"]) if m["tags"] else []):
                by_tag.setdefault(t, []).append(m["name"])
        lit = set(by_tag)
        grown = True
        while grown:  # 被点亮山的父山跟着亮(链式上溯)
            grown = False
            for t in rows(tags):
                if t["tag"] in lit and t["parent"] and t["parent"] not in lit:
                    lit.add(t["parent"])
                    grown = True
        tags = [t for t in rows(tags) if t["tag"] in lit]
        children = {}
        roots = []
        for t in rows(tags):
            if t["parent"] and any(x["tag"] == t["parent"] for x in tags):
                children.setdefault(t["parent"], []).append(t)
            elif not t["parent"]:
                roots.append(t)

        lines = [
            "# 经图 (Skill Map)",
            "这是你的技能树。tag是山系, skill是异兽条目。有兽焉, 用时召来。",
            "查询语法: GET /tag/<tag> 拉某山下条目(含首行描述); "
            "GET /skill/<id> 拉正文。用不用、何时用, 你判断。",
            "",
        ]

        def walk(tag, depth):
            note = f"  # {tag['note']}" if tag["note"] else ""
            lines.append(f"{'  ' * depth}{tag['tag']}{note}")
            for c in children.get(tag["tag"], []):
                walk(c, depth + 1)

        for r in rows(roots):
            walk(r, 0)
        if roots:
            lines.append("")

        pre = con.execute(
            "SELECT target, requires, reason FROM prereqs ORDER BY target").fetchall()
        if pre:
            lines.append("## 前置表")
            for p in rows(pre):
                lines.append(f"{p['requires']} ⇒ {p['target']}（理由：{p['reason']}）")
            lines.append("")

        # core层全文常驻 (契约: core=保底常驻, 每session全文)
        vis_clause, vis_args = visible_skills_clause(operator)  # 同一把门
        core_rows = con.execute(
            "SELECT s.skill_id, s.name, s.body FROM skills s "
            "WHERE s.layer = 'core' AND s.status = 'verified'"
            + vis_clause + " ORDER BY s.name", vis_args).fetchall()
        if core_rows:
            lines.append("## core层全文常驻")
            for c in rows(core_rows):
                lines.append(f"### {c['name']}")
                lines.append(c["body"])
                lines.append("")
        # pinned层描述行常驻 (core=index之间的注水层: 每session必知但正文太重;
        # 经图里带一行描述, 正文按需拉。描述行读trigger字段。)
        pinned_rows = con.execute(
            "SELECT s.name, f.value AS trigger FROM skills s "
            "JOIN skill_fields f ON f.skill_id = s.skill_id "
            "AND f.field = 'trigger' "
            "WHERE s.layer = 'pinned' AND s.status = 'verified'"
            + vis_clause + " ORDER BY s.name", vis_args).fetchall()
        if pinned_rows:
            lines.append("## pinned层描述行")
            for p in rows(pinned_rows):
                raw = p["trigger"] or '""'
                try:
                    raw = json.loads(raw)  # trigger按json.dumps存, 解包引号
                except (ValueError, TypeError):
                    pass
                desc = (raw or "").strip().splitlines()[0] if raw else ""
                lines.append(f"{p['name']}: {desc}")

        mapping = con.execute(
            "SELECT s.name, s.layer, f.value AS tags "
            "FROM skills s LEFT JOIN skill_fields f "
            "ON f.skill_id = s.skill_id AND f.field = 'tags' "
            "WHERE s.status = 'verified' AND s.layer != 'archive'"
            + vis_clause +
            " ORDER BY s.name", vis_args).fetchall()
        if mapping:
            by_tag = {}
            for m in rows(mapping):
                for t in (json.loads(m["tags"]) if m["tags"] else []):
                    by_tag.setdefault(t, []).append(m["name"])
            lines.append("## tag↔skill 映射")
            for t in sorted(by_tag):
                lines.append(f"{t}: {', '.join(by_tag[t])}")
            lines.append("")
        # R3: draft不在默认经图露脸 — 想看draft(含red横幅)走X-Review-Draft头
        drafts = con.execute(
            "SELECT name FROM skills WHERE status='draft' "
            "ORDER BY name").fetchall()
        if drafts:
            # R3补丁: 只报数量不列名字 — 经图是每session注入面,
            # 列名字=攻击者可用skill名往所有agent上下文里塞任意串
            lines.append(
                f"## 待审区({len(drafts)}本draft, 不进默认经图; "
                "名单走/darkzone, 审阅带X-Review-Draft头GET /skill/<name>)")
            lines.append("")
        return "\n".join(lines)
    finally:
        con.close()


# ---------------- 写入扫描 (三面, 只记不拦) ----------------

STATIC_RULES = [
    (r"eval\s*\(", "yellow", "eval调用—需巡山使判断用途"),
    (r"exec\s*\(", "yellow", "exec调用—需巡山使判断用途"),
    (r"base64\.?b64decode|__import__\(\s*['\"]zlib", "yellow", "混淆解码链"),
    (r"/etc/(?:passwd|shadow)|\.ssh/|\.aws/credentials", "red", "越界读:系统凭证路径"),
    (r"os\.system|shell\s*=\s*True", "yellow", "shell执行—需判断命令内容"),
]

PATTERN_RULES = [
    (r"ignore (?:all )?(?:previous|prior) instructions", "red", "注入话术:重置指令"),
    (r"disregard .{0,40}above", "red", "注入话术:绕过上文"),
    (r"https?://(?!127\.0\.0\.1|localhost)[\w.-]+/(?:webhook|collect|track|exfil)",
     "red", "外发URL:webhook/收集端点"),
    (r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}",
     "red", "疑似明文凭证"),
]


def _slot_norm(s):
    """槽位词规范化: 组合tag(类型·场景·关键词)的槽内清空格/统一宽窄."""
    return s.strip().replace(" ", "").replace("．", ".").replace("　", "")


def _levenshtein(a, b):
    """编辑距离, tag查重提示用."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def tag_dup_check(con, incoming):
    """写入侧tag查重 (只提示+精确折叠, 不拦写入 — 契约库门纪律):
    1) 单tag精确命中正字/别名 → 自动折到正字, 提示;
    2) 组合tag折槽不折串: 槽内折到正字+去重保序, 整串规范化;
    3) 模糊近似(编辑距离≤2, len≥3) → 只提示不折, 提交者选旧tag或确认新立.
    返回 (folded_tags, hints): folded实际入库, hints进submit响应."""
    known = {}
    for r in rows(con.execute(
            "SELECT tag, aliases FROM tags").fetchall()):
        known[r["tag"]] = (json.loads(r["aliases"])
                           if r["aliases"] else [])
    norm = lambda s: _slot_norm(s).lower()
    canon = {}
    for t, al in known.items():
        canon[norm(t)] = t
        for a in al:
            canon[norm(a)] = t
    folded, hints = [], []
    for t in incoming:
        nt = norm(t)
        if "·" in t:
            slots = [_slot_norm(x) for x in t.split("·") if x.strip()]
            slots = [canon.get(norm(s), s) for s in slots]
            seen, dedup = set(), []
            for s in slots:
                if s not in seen:
                    seen.add(s)
                    dedup.append(s)
            folded_tag = "·".join(dedup)
            if folded_tag != t:
                hints.append(f"组合tag规范化: '{t}' → '{folded_tag}' "
                             f"(槽位查重: 槽内折到正字/去重保序)")
            folded.append(folded_tag)
            continue
        if nt in canon:
            c = canon[nt]
            if c != t:
                hints.append(f"tag查重: '{t}' 已有正字 '{c}' — 已折叠写入 "
                             f"(后续查询自动归并)")
            folded.append(c)
            continue
        near = []
        for c_tag in canon:
            ok_len = (len(nt) >= 3 or bool(re.search(r"[\u4e00-\u9fff]", nt))) \
                and (len(c_tag) >= 3 or bool(re.search(r"[\u4e00-\u9fff]", c_tag)))
            if ok_len and abs(len(c_tag) - len(nt)) <= 2 \
                    and _levenshtein(nt, c_tag) <= 2:
                near.append(c_tag)
        if near:
            hints.append(f"tag近似提示: '{t}' 与既有正字 {'/'.join(near)} "
                         f"编辑距离≤2 — 是否选旧tag? (只提示未折叠; "
                         f"请改用旧tag或确认新立)")
        folded.append(t)
    return folded, hints


def scan_skill(con, name, body, exclude_id=None):
    findings = []
    all_text = f"{name}\n{body}"

    for pat, sev, msg in STATIC_RULES:
        if re.search(pat, all_text, re.IGNORECASE):
            findings.append({"face": "static", "severity": sev,
                             "msg": msg, "match": pat})

    # 文面扫不看代码块里的示例(那里出现这些词是教学, 不是攻击)
    prose = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    for pat, sev, msg in PATTERN_RULES:
        if re.search(pat, f"{name}\n{prose}", re.IGNORECASE):
            findings.append({"face": "pattern", "severity": sev,
                             "msg": msg, "match": pat})

    # 重复检测: 归一化后比对全部在馆条目(排除自己——刚落库的自己是假重复)
    norm = re.sub(r"\s+", " ", body).strip().lower()
    if norm:
        seen = con.execute(
            "SELECT skill_id, body FROM skills WHERE skill_id != ?",
            (exclude_id,)).fetchall() if exclude_id else \
            con.execute("SELECT skill_id, body FROM skills").fetchall()
        for r in rows(seen):
            other = re.sub(r"\s+", " ", r["body"]).strip().lower()
            if not other:
                continue
            if norm == other:
                findings.append({"face": "duplicate", "severity": "red",
                                 "msg": f"与 {r['skill_id']} 正文完全重复(归一化后)"})
                break
            if len(norm) > 200 and (norm in other or other in norm):
                findings.append({"face": "duplicate", "severity": "yellow",
                                 "msg": f"与 {r['skill_id']} 正文高度重叠(包含关系)"})
                break
    return findings


# ---------------- handler ----------------

def skill_visible(con, skill_id: str, operator: str) -> bool:
    """三层门 (v0.5) 单本判定: get_skill / vault 两面用。
    private 无权→调用方装404(字节与真404一致), 这里只答可见与否。"""
    row = con.execute(
        "SELECT visibility FROM skills WHERE skill_id = ?",
        (skill_id,)).fetchone()
    if not row:
        return True  # 行不在(不该发生)——归404主路径, 不在此装
    vis = row["visibility"] or "public"
    if vis == "public":
        return True
    if operator == LIBRARY_OWNER:
        return True
    ok = con.execute(
        "SELECT 1 FROM visibility_rosters WHERE "
        "(scope = 'family' AND operator = ? AND ? = 'family') OR "
        "(scope = ? AND operator = ?)",
        (operator, vis, f"skill:{skill_id}", operator)).fetchone()
    return bool(ok)


def visible_skills_clause(operator: str, alias: str = "s") -> tuple:
    """三层门 (v0.5) SQL 片段: 返回 (WHERE 片段, 参数) 过滤不可见 skill。

    public = 任何人; family = 家人名单(visibility_rosters scope='family');
    private = 库主(LIBRARY_OWNER)或该skill的 audience 白名单(scope='skill:<id>')。
    名单为空时 family/private 全隐(白名单制, 不是黑名单制)。
    """
    if operator == LIBRARY_OWNER:
        return ("", [])
    return (
        f" AND ({alias}.visibility = 'public'"
        f" OR ({alias}.visibility = 'family' AND EXISTS ("
        f"  SELECT 1 FROM visibility_rosters vr WHERE vr.scope = 'family'"
        f"  AND vr.operator = ?))"
        f" OR ({alias}.visibility = 'private' AND EXISTS ("
        f"  SELECT 1 FROM visibility_rosters vr WHERE vr.scope = 'skill:' || {alias}.skill_id"
        f"  AND vr.operator = ?)))",
        [operator, operator],
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def operator(self) -> str:
        """读面身份: X-Operator 头, 缺省 unknown。防误看不防冒领(R2 管后者)。"""
        return self.headers.get("X-Operator") or "unknown"

    def do_GET(self):
        if self.path == "/health":
            return self.send_text(200, "ok")
        if self.path == "/map":
            return self.send_text(200, render_map(operator=self.operator))
        if self.path == "/darkzone":
            return self.get_darkzone()
        if self.path.split("?")[0] == "/vault":
            return self.get_vault_listing()
        if self.path.split("?")[0] == "/tools":
            return self.get_tools_map()
        if self.path.split("?")[0] == "/stats":
            since = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("since", [None])[0]
            return self.send_text(200, self.get_stats(since))
        m = re.match(r"^/vault/([^/]+)/(.+)$", self.path.split("?")[0])
        if m:
            return self.get_vault_file(
                urllib.parse.unquote(m.group(1)),
                urllib.parse.unquote(m.group(2)))
        m = re.match(r"^/tag/([^/]+)$", self.path)
        if m:
            return self.get_tag(urllib.parse.unquote(m.group(1)))
        m = re.match(r"^/skill/([^/]+)$", self.path)
        if m:
            return self.get_skill(urllib.parse.unquote(m.group(1)))
        return self.send_text(404, "not found")

    def canonical_tag(self, con, tag):
        """别名归并: 查询侧把别名折叠到正字tag(repair≈维修)。可逆, 树不长胖。"""
        for r in rows(con.execute(
                "SELECT tag, aliases FROM tags WHERE aliases IS NOT NULL"
                ).fetchall()):
            if tag == r["tag"]:
                return r["tag"]
            for a in (json.loads(r["aliases"]) if r["aliases"] else []):
                if tag == a:
                    return r["tag"]
        return tag

    def get_darkzone(self):
        """暗区点名(机械半边): 从未被push/expand过的skill名单。
        archive层除外——它们已被巡山使判过, 点名是给还没被判断的书的保底。
        出口必须带全量名单——暗区skill是唯一没有遥测数据替它们说话的,
        名单是它们的保底。逐本判断仍是巡山使的活。
        三层门(v0.5): 按operator过滤——不该看的人连"有这本书"都不知道。"""
        con = db()
        try:
            names = set()
            for r in rows(con.execute(
                    "SELECT payload FROM events WHERE kind IN "
                    "('skill.telemetry.push','skill.telemetry.expand')"
                    ).fetchall()):
                p = json.loads(r["payload"])
                v = p.get("skill_id") or p.get("name")
                if v:
                    names.add(v)
            vis_clause, vis_args = visible_skills_clause(self.operator)
            out = []
            for r in rows(con.execute(
                    "SELECT s.skill_id, s.name, s.layer, s.status "
                    "FROM skills s WHERE s.status != 'retired' "
                    "AND s.layer != 'archive'" + vis_clause +
                    " ORDER BY s.name", vis_args).fetchall()):
                if r["name"] not in names and r["skill_id"] not in names:
                    out.append(f"{r['name']}  [{r['layer']}/{r['status']}]")
            body = "暗区点名(从未被push/expand):\n" + ("\n".join(out)
                    if out else "(空——没有暗区skill)")
            body += "\n\n出口提醒: 名单是机械的, 逐本判断仍是巡山使的活。"
            return self.send_text(200, body)
        finally:
            con.close()

    def get_stats(self, since=None):
        """巡逻统计读面(收编自 tools/patrol_stats.py):
        四数字全部端点化, 跨工具读数从此同源。
        since=UTC时刻时加算上轮以来新增事件数。弱trigger判据与历次报告一致。"""
        con = db()
        try:
            seen = set()
            for r in rows(con.execute(
                    "SELECT payload FROM events WHERE kind IN "
                    "('skill.telemetry.push','skill.telemetry.expand')"
                    ).fetchall()):
                p = json.loads(r["payload"])
                v = p.get("skill_id") or p.get("name")
                if v:
                    seen.add(v)
            dark = weak = 0
            for r in rows(con.execute(
                    """SELECT s.name, s.skill_id, f.value t FROM skills s
                    LEFT JOIN skill_fields f ON f.skill_id=s.skill_id
                    AND f.field='trigger'
                    WHERE s.layer!='archive' AND s.status!='retired'""")):
                if r["name"] not in seen and r["skill_id"] not in seen:
                    dark += 1
                t = r["t"] or ""
                if t.startswith('"'):
                    t = json.loads(t)
                has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
                if len(t) < 25 or (not has_cjk and len(t) < 60):
                    weak += 1
            total = con.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0]
            out = [f"暗区数量: {dark}",
                   f"经图字节数: {len(render_map().encode())}",
                   f"弱trigger数: {weak}",
                   f"事件总数: {total}"]
            if since:
                n = con.execute(
                    "SELECT COUNT(*) FROM events WHERE ts > ?",
                    (since,)).fetchone()[0]
                out.append(f"since({since} UTC)新增事件: {n}")
            return "\n".join(out)
        finally:
            con.close()

    def get_tag(self, tag):
        con = db()
        try:
            tag = self.canonical_tag(con, tag)  # 查询侧归并: 别名折叠到正字
            out = []
            vis_clause, vis_args = visible_skills_clause(self.operator)
            for r in rows(con.execute(
                    "SELECT s.skill_id, s.name, s.layer, s.status, f.value AS tags "
                    "FROM skills s LEFT JOIN skill_fields f "
                    "ON f.skill_id = s.skill_id AND f.field = 'tags' "
                    "WHERE s.status = 'verified' AND s.layer != 'archive'"
                    + vis_clause +
                    " ORDER BY s.name", vis_args).fetchall()):
                taglist = json.loads(r["tags"]) if r["tags"] else []
                if tag in taglist or tag in [t.lower() for t in taglist]:
                    trig = con.execute(
                        "SELECT value FROM skill_fields "
                        "WHERE skill_id = ? AND field = 'trigger'",
                        (r["skill_id"],)).fetchone()
                    desc = ""
                    if trig and trig["value"]:
                        desc = trig["value"].splitlines()[0][:120]
                    out.append(f"{r['name']}  [{r['layer']}/{r['status']}]  {desc}")
            if not out:
                return self.send_text(404, f"tag '{tag}' 下暂无在馆条目")
            return self.send_text(
                200, f"山下条目（首行描述）:\n" + "\n".join(out))
        finally:
            con.close()

    def get_vault_listing(self):
        """GET /vault?skill=<name> — 附件索引清单。无参数=全馆附件统计。"""
        con = db()
        try:
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            skill = (q.get("skill") or [None])[0]
            if skill:
                row = con.execute(
                    "SELECT skill_id FROM skills WHERE name=?",
                    (skill,)).fetchone()
                if not row:
                    return self.send_text(404, f"skill不存在: {skill}")
                # 三层门(v0.5): 不可见→装404(三个vault面同律)
                if not skill_visible(con, row["skill_id"], self.operator):
                    return self.send_text(404, f"skill不存在: {skill}")
                fl = rows(con.execute(
                    "SELECT relpath, size, sha256, binary, synced_at "
                    "FROM vault_index WHERE skill_id=? ORDER BY relpath",
                    (row["skill_id"],)).fetchall())
                lines = [f"# vault: {skill} ({len(fl)} files)"]
                for f in fl:
                    flag = " [binary]" if f["binary"] else ""
                    lines.append(
                        f"  {f['relpath']}  ({f['size']}B, "
                        f"{f['sha256'][:12]}{flag})")
                lines.append("\nGET /vault/<skill>/<relpath> 取文件; "
                             "binary以base64返回")
                return self.send_text(200, "\n".join(lines))
            # 全馆统计
            vis_clause, vis_args = visible_skills_clause(self.operator)
            st = con.execute(
                "SELECT count(*) n, sum(size) sz FROM vault_index v "
                "JOIN skills s ON s.skill_id = v.skill_id WHERE 1=1"
                + vis_clause, vis_args).fetchone()
            top = rows(con.execute(
                "SELECT s.name, count(*) n, sum(v.size) sz "
                "FROM vault_index v JOIN skills s ON s.skill_id=v.skill_id "
                "WHERE 1=1" + vis_clause +
                " GROUP BY v.skill_id ORDER BY sz DESC LIMIT 10",
                vis_args).fetchall())
            lines = [f"# vault 全馆: {st['n']} files, "
                     f"{(st['sz'] or 0)/1024/1024:.1f} MB", "", "## 最重的10位:"]
            for t in top:
                lines.append(f"  {t['name']}: {t['n']} files, "
                             f"{t['sz']/1024:.0f} KB")
            return self.send_text(200, "\n".join(lines))
        finally:
            con.close()

    def get_vault_file(self, skill, relpath):
        """GET /vault/<skill>/<relpath> — 取附件内容。binary以base64返回。"""
        con = db()
        try:
            row = con.execute(
                "SELECT skill_id FROM skills WHERE name=?", (skill,)).fetchone()
            if not row:
                return self.send_text(404, f"skill不存在: {skill}")
            # 三层门(v0.5): 不可见→装404(与get_skill同律, 不泄露存在性)
            if not skill_visible(con, row["skill_id"], self.operator):
                return self.send_text(404, f"skill不存在: {skill}")
            # R3: draft/retired附件默认不外发 — 审阅者带X-Review-Draft取
            # (照照四审·两把钥匙: 审阅态统一X-Review-Draft一把钥匙
            #  X-Review-Draft一把钥匙管审阅态, 消息里写清这把钥匙开什么)
            status = con.execute(
                "SELECT status FROM skills WHERE skill_id=?",
                (row["skill_id"],)).fetchone()[0]
            if status != "verified" and not self.headers.get("X-Review-Draft"):
                if status == "retired":
                    return self.send_text(
                        410, f"该skill已retired: {skill} "
                             "(带X-Review-Draft头进入审阅模式)")
                return self.send_text(
                    403, f"draft skill附件不外发: {skill} "
                         "(带X-Review-Draft头进入审阅模式)")
            vi = con.execute(
                "SELECT * FROM vault_index WHERE skill_id=? AND relpath=?",
                (row["skill_id"], relpath)).fetchone()
            if not vi:
                return self.send_text(404, f"附件不在馆: {relpath}")
            # 路径安全: relpath已在索引内(入库时过审), 直接拼
            fpath = os.path.join(VAULT_DIR, row["skill_id"], relpath)
            if not os.path.isfile(fpath):
                # P5(照照四审): 只报relpath——一个把不泄露存在性做到字节级的
                # 系统, 错误页里广播服务器绝对路径是哲学不一致
                return self.send_text(410, f"索引在但文件丢失: {relpath}")
            data = open(fpath, "rb").read()
            if vi["binary"]:
                import base64
                return self.send_text(200, base64.b64encode(data).decode())
            return self.send_text(200, data.decode("utf-8", errors="replace"))
        finally:
            con.close()

    def get_skill(self, skill_id):
        con = db()
        try:
            row = con.execute(
                "SELECT * FROM skills WHERE skill_id = ? OR name = ?",
                (skill_id, skill_id)).fetchone()
            if not row:
                # 名字别名也认(查询侧折叠, 与tag别名同一模式)
                esc = skill_id.replace('"', '\\"')
                row = con.execute(
                    "SELECT s.* FROM skills s JOIN skill_fields f "
                    "ON f.skill_id = s.skill_id AND f.field = 'aliases' "
                    "WHERE f.value LIKE ?",
                    (f'%"{esc}"%',)).fetchone()
            if not row:
                return self.send_text(404, "not found")
            # 三层门(v0.5): 不可见→装404, 与真404字节一致——不泄露存在性。
            # 检查在 retired/draft 之前: 不该看的人连"这是draft/retired"都不知道。
            if not skill_visible(con, row["skill_id"], self.operator):
                return self.send_text(404, "not found")
            if row["status"] == "retired" and \
                    not self.headers.get("X-Review-Draft"):
                return self.send_text(
                    410, f"该skill已retired: {skill_id} (带X-Review-Draft头可查阅)")
            reds = con.execute(
                "SELECT COUNT(*) FROM scan_reports WHERE skill_id=? "
                "AND severity='red'", (row["skill_id"],)).fetchone()[0]
            if row["status"] == "draft" and \
                    not self.headers.get("X-Review-Draft"):
                return self.send_text(
                    403, f"该skill是draft(未过审): {skill_id} — "
                         "默认读面不放行。带X-Review-Draft头进入审阅模式"
                         "(red报告挂横幅)。经图'待审区'段只报数量。")
            banner = ""
            if row["status"] == "draft" and reds:
                banner = (f"> ⚠️ 入关报告带{reds}条red — 审阅时过目, "
                          "转正须ack_red签名。扫描详情: POST /skill查询。\n\n")
            flds = {}
            for f in rows(con.execute(
                    "SELECT field, value FROM skill_fields WHERE skill_id = ?",
                    (row["skill_id"],)).fetchall()):
                flds[f["field"]] = (json.loads(f["value"])
                                    if f["field"] in ("tags", "aliases")
                                    else f["value"])
            head = (f"# {row['name']}\n"
                    f"layer: {row['layer']}  status: {row['status']}\n"
                    f"tags: {flds.get('tags', [])}\n"
                    f"trigger: {flds.get('trigger', '')}\n"
                    f"boundary: {flds.get('boundary', '')}\n"
                    f"why: {flds.get('why', '')}\n"
                    f"baseline_hash: {row['baseline_hash']}\n\n")
            tail = ("\n\n---\n用完顺手记一笔: POST /event "
                    '{"kind":"skill.telemetry.expand","operator":"<你>","skill_id":"'
                    f'{row["name"]}' + '"} — 好用/没用, 你判断.)')
            return self.send_text(200, banner + head + row["body"] + tail)
        finally:
            con.close()

    def do_POST(self):
        if self.path == "/event":
            return self.post_event()
        if self.path == "/skill":
            return self.post_skill()
        if self.path == "/tool":
            return self.post_tool()
        return self.send_text(404, "endpoints: POST /event, POST /skill, POST /tool")

    def post_tool(self):
        """工具层分区 (v0.3) 写口。两种动作:
        1. tool.register (登记制, CLI/API 走这个): 自报能力+门牌, 巡山核验。
           与 skill.submit 同律: 提交即 draft, 巡山使裁决转正。
        2. tool.telemetry (entry 级只记不动): plugin hook 落笔, 零 AI 参与。
        MCP 不走登记——config.yaml 是事实源, sync 是被动镜像 (tools/sync_mcp.py)。
        """
        try:
            body = self.read_json()
        except ValueError as e:
            return self.send_text(400, f"bad json: {e}")
        action = body.get("action")
        if action == "tool.telemetry":
            entry_id = body.get("entry_id")
            if not entry_id:
                return self.send_text(400, "缺 entry_id")
            con = db()
            try:
                with _lock:
                    row = con.execute(
                        "SELECT entry_id FROM tool_entries WHERE entry_id=?",
                        (entry_id,)).fetchone()
                    if not row:
                        return self.send_text(404, f"entry不存在: {entry_id}")
                    con.execute(
                        "INSERT INTO tool_telemetry(ts, entry_id, caller, ok, payload) "
                        "VALUES(?,?,?,?,?)",
                        (now(), entry_id, body.get("caller") or "unknown",
                         1 if body.get("ok", True) else 0,
                         json.dumps({k: v for k, v in body.items()
                                     if k not in ("action", "entry_id")},
                                    ensure_ascii=False)))
                    con.commit()
                return self.send_text(200, "已落账。只记不动。")
            finally:
                con.close()
        if action == "tool.register":
            cap = body.get("capability")
            kind = body.get("kind")
            ref = body.get("ref")
            if not cap or kind not in ("mcp", "cli", "api") or not ref:
                return self.send_text(400, "缺 capability / kind∈{mcp,cli,api} / ref")
            con = db()
            try:
                with _lock:
                    con.execute(
                        "INSERT OR IGNORE INTO tools(capability, note, created_at, updated_at) "
                        "VALUES(?,?,?,?)",
                        (cap, body.get("note") or "", now(), now()))
                    if con.execute(
                            "SELECT COUNT(*) FROM tool_entries "
                            "WHERE capability=? AND kind=? AND ref=?",
                            (cap, kind, ref)).fetchone()[0]:
                        return self.send_text(409, f"entry已存在: {cap}/{kind}/{ref}")
                    eid = "te:" + str(uuid.uuid4())
                    con.execute(
                        "INSERT INTO tool_entries(entry_id, capability, kind, ref, status, "
                        "note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (eid, cap, kind, ref, "active",
                         body.get("entry_note") or "", now(), now()))
                    con.commit()
                return self.send_json(200, {
                    "capability": cap, "kind": kind, "ref": ref,
                    "entry_id": eid, "status": "active"})
            finally:
                con.close()
        return self.send_text(400, "action须为 tool.register | tool.telemetry")

    def get_tools_map(self):
        """工具层读面: capability 视图 (entry 级遥测聚合) + 暗区 (零调用 entry)。
        照照判据落地: disable 决策在 entry 级——死 server 不被忙的 CLI 孪生救活。
        """
        con = db()
        try:
            out = ["工具经图 (capability 视图, 遥测 entry 级聚合):", ""]
            caps = rows(con.execute(
                "SELECT t.capability, t.note FROM tools t "
                "ORDER BY t.capability").fetchall())
            if not caps:
                return self.send_text(200, "工具经图: (空——还没有登记的工具)")
            dark = []
            for c in caps:
                entries = rows(con.execute(
                    "SELECT e.entry_id, e.kind, e.ref, e.status FROM tool_entries e "
                    "WHERE e.capability=? ORDER BY e.kind, e.ref",
                    (c["capability"],)).fetchall())
                lines = []
                for e in entries:
                    tel = con.execute(
                        "SELECT COUNT(*), MAX(ts), SUM(ok) FROM tool_telemetry "
                        "WHERE entry_id=?", (e["entry_id"],)).fetchone()
                    n, last, ok_n = tel[0], tel[1], (tel[2] or 0)
                    stat = (f"{e['kind']:>3} {e['ref']} [{e['status']}]"
                            f"  calls={n} ok={ok_n} last={last or '从未'}")
                    lines.append("  " + stat)
                    if n == 0:
                        dark.append(f"{c['capability']} :: {e['kind']} {e['ref']}")
                cross = {e["kind"] for e in entries}
                out.append(f"◆ {c['capability']}  ({'/'.join(sorted(cross))})"
                           + (f"  — {c['note']}" if c["note"] else ""))
                out.extend(lines)
                out.append("")
            out.append("工具暗区 (零调用 entry):")
            out.extend("  " + d for d in dark if d)
            if len(dark) == 0:
                out.append("  (空)")
            return self.send_text(200, "\n".join(out))
        finally:
            con.close()

    def post_event(self):
        try:
            body = self.read_json()
        except ValueError as e:
            return self.send_text(400, f"bad json: {e}")
        kind = body.get("kind")
        if kind not in EVENT_KINDS:
            return self.send_text(400, f"kind须为: {sorted(EVENT_KINDS)}")
        operator = body.get("operator") or "unknown"
        con = db()
        try:
            with _lock:
                # 遥测: 只记不动。治理: 动作+落账一体。
                if kind in TELEMETRY_KINDS:
                    con.execute(
                        "INSERT INTO events(ts, operator, kind, payload) "
                        "VALUES(?,?,?,?)",
                        (now(), operator, kind,
                         json.dumps(body, ensure_ascii=False)))
                    con.commit()
                    return self.send_text(200, "已落账。只记不动: 账本不触发任何变更。")

                resp = self.governance_flow(con, kind, operator, body)
                return resp
        except sqlite3.Error as e:
            con.rollback()
            return self.send_text(500, f"ledger write failed: {e}")
        finally:
            con.close()

    def governance_flow(self, con, kind, operator, body):
        """治理动作全流程: govern执行→budget检查→落账→响应。
        post_event与do_DELETE共用 (Y5: DELETE /skill/<id> = skill.pool.withdraw)"""
        resp = self.govern(con, kind, operator, body)
        if resp[0] < 300:  # 动作成功才落账
            denied = budget_check(con, operator)
            if denied:
                con.rollback()
                return self.send_json(429, denied)
            con.execute(
                "INSERT INTO events(ts, operator, kind, payload) "
                "VALUES(?,?,?,?)",
                (now(), operator, kind,
                 json.dumps(body, ensure_ascii=False)))
            con.commit()
        return self.send_json(resp[0], resp[1])

    def do_DELETE(self):
        m = re.match(r"^/skill/([^/]+)$", self.path)
        if not m:
            return self.send_text(404, "DELETE /skill/<name|id> (draft撤回)")
        target = urllib.parse.unquote(m.group(1))
        try:
            body = {"kind": "skill.pool.withdraw",
                    "operator": self.headers.get("X-Operator") or "unknown",
                    "skill_id": target}
        except Exception:
            return self.send_text(400, "bad request")
        con = db()
        try:
            with _lock:
                return self.governance_flow(con, "skill.pool.withdraw",
                                            body["operator"], body)
        except sqlite3.Error as e:
            con.rollback()
            return self.send_text(500, f"ledger write failed: {e}")
        finally:
            con.close()

    def govern(self, con, kind, operator, body):
        """巡山使写脸: 执行治理动作。返回 (http_code, payload)。"""
        sid = body.get("skill_id")

        def get_skill_row():
            return con.execute(
                "SELECT skill_id, layer, status, baseline_hash FROM skills "
                "WHERE skill_id = ? OR name = ?", (sid, sid or "")).fetchone()

        if kind == "skill.pool.withdraw":
            row = get_skill_row()
            if not row:
                return 404, {"error": f"skill不存在: {sid}"}
            if row["status"] != "draft":
                return 409, {"error": "withdraw只清draft(探针/误提交); "
                                      "verified馆藏走review:discarded→retired"}
            # 子表先清, 父表后清 — FK ON下父先删会constraint failed
            # (ad-hoc验证抓的: smoke没测DELETE, 套件绿≠端点对)
            for child in ("skill_fields", "scan_reports", "vault_index"):
                con.execute(f"DELETE FROM {child} WHERE skill_id=?",
                            (row["skill_id"],))
            con.execute("DELETE FROM skills WHERE skill_id=?",
                        (row["skill_id"],))
            # P3c(照照四审): vault/<sid>/ 附件目录跟着撤 — 只清索引行会留
            # 不可达但占盘的孤儿目录 (withdraw的语义就是物理撤回)
            vdir = os.path.join(VAULT_DIR, row["skill_id"])
            if os.path.isdir(vdir):
                shutil.rmtree(vdir, ignore_errors=True)
            return 200, {"withdrawn": row["skill_id"], "note": "draft已物理撤回"}

        if kind == "skill.pool.review":
            row = get_skill_row()
            if not row:
                return 404, {"error": f"skill不存在: {sid}"}
            decision = body.get("decision")
            if decision not in ("promoted", "discarded"):
                return 400, {"error": "decision须为 promoted|discarded"}
            # 红标draft转正需过目——巡山使签名即过目, 但红标在案时payload须带ack
            reds = con.execute(
                "SELECT COUNT(*) FROM scan_reports WHERE skill_id=? "
                "AND severity='red'", (row["skill_id"],)).fetchone()[0]
            if reds and not body.get("ack_red"):
                return 409, {"error": "该skill带red入关报告, 转正须带 ack_red: true(过目签名)"}
            new_status = "verified" if decision == "promoted" else "retired"
            con.execute("UPDATE skills SET status=?, updated_at=? WHERE skill_id=?",
                        (new_status, now(), row["skill_id"]))
            return 200, {"skill_id": row["skill_id"], "status": new_status,
                         "red_reports": reds}

        if kind == "skill.roster.update":
            row = get_skill_row()
            if not row:
                return 404, {"error": f"skill不存在: {sid}"}
            layer = body.get("layer")
            if layer not in ("core", "pinned", "index", "archive"):
                return 400, {"error": "layer须为 core|pinned|index|archive"}
            # R3侧门(照照四审): core/pinned是注入面, 非verified书不许挪入——
            # 否则draft带注入话术roster.update一下就进开场注入(与core/pinned
            # 查询只亮verified同一道门的两半; retired挪入返回200但被查询滤掉
            # = 'UPDATE成功'与'UPDATE有意义'是两回事, 一并堵死)
            if layer in ("core", "pinned") and row["status"] != "verified":
                return 409, {
                    "error": f"layer={layer}是注入面, 只收verified书 "
                             f"(当前status={row['status']}); "
                             "draft先走skill.pool.review转正",
                    "hint": "core/pinned只亮verified(与读面同一道门)"}
            con.execute("UPDATE skills SET layer=?, updated_at=? WHERE skill_id=?",
                        (layer, now(), row["skill_id"]))
            return 200, {"skill_id": row["skill_id"], "layer": layer}

        if kind == "skill.description.rewrite":
            row = get_skill_row()
            if not row:
                return 404, {"error": f"skill不存在: {sid}"}
            if body.get("baseline_hash") != row["baseline_hash"]:
                return 409, {"error": "baseline_hash不符(stale)",
                             "current_baseline": row["baseline_hash"],
                             "hint": "基线已被别人改过, 重读再改"}
            new_trigger = body.get("trigger")
            if not new_trigger:
                return 400, {"error": "缺trigger"}
            # 凭证轮换: 改写成功后 baseline_hash 换新 (sha(新trigger||旧hash)),
            # B 拿 A 改之前的旧凭证不能再开门 — 防静默覆盖 (照照审阅首缺陷)
            old_hash = row["baseline_hash"]
            new_hash = sha(new_trigger + old_hash)  # sha()内部encode, 勿传bytes
            con.execute(
                "INSERT OR REPLACE INTO skill_fields(skill_id, field, value) "
                "VALUES(?,?,?)", (row["skill_id"], "trigger", new_trigger))
            con.execute(
                "UPDATE skills SET baseline_hash=?, updated_at=? WHERE skill_id=?",
                (new_hash, now(), row["skill_id"]))
            return 200, {"skill_id": row["skill_id"],
                         "trigger_head": new_trigger[:80],
                         "baseline_hash": new_hash}

        if kind == "skill.tag.alias.add":
            tag, aliases = body.get("tag"), body.get("aliases")
            if not tag or not isinstance(aliases, list) or not aliases:
                return 400, {"error": "缺tag/aliases(非空list)"}
            row = con.execute(
                "SELECT aliases FROM tags WHERE tag=?", (tag,)).fetchone()
            have = json.loads(row["aliases"]) if row and row["aliases"] else []
            merged = list(dict.fromkeys(have + [a for a in aliases
                                                if a and a not in have]))
            # tag不存在则先种山(治理面登记语义: 登记即入库, 无需先有书)
            con.execute(
                "INSERT OR IGNORE INTO tags(tag) VALUES(?)", (tag,))
            con.execute(
                "UPDATE tags SET aliases=? WHERE tag=?",
                (json.dumps(merged, ensure_ascii=False), tag))
            return 200, {"tag": tag, "aliases": merged,
                         "added": [a for a in aliases if a not in have]}

        if kind == "skill.tag.merge":
            old_tag, new_tag = body.get("old_tag"), body.get("new_tag")
            if not old_tag or not new_tag:
                return 400, {"error": "缺old_tag/new_tag"}
            moved = 0
            merged_aliases = []
            for r in rows(con.execute(
                    "SELECT skill_id, value FROM skill_fields "
                    "WHERE field='tags'").fetchall()):
                tags = json.loads(r["value"])
                if old_tag in tags:
                    tags = [t for t in tags if t != old_tag] + [new_tag]
                    con.execute(
                        "UPDATE skill_fields SET value=? "
                        "WHERE skill_id=? AND field='tags'",
                        (json.dumps(tags, ensure_ascii=False), r["skill_id"]))
                    moved += 1
            # aliases 迁移: 旧正字的别名併入新正字, 不隨山刪掉 (照照觀察項1)
            old_row = con.execute(
                "SELECT aliases FROM tags WHERE tag=?", (old_tag,)).fetchone()
            if old_row and old_row["aliases"]:
                merged_aliases = json.loads(old_row["aliases"])
            new_row = con.execute(
                "SELECT aliases FROM tags WHERE tag=?", (new_tag,)).fetchone()
            if new_row and new_row["aliases"]:
                have = json.loads(new_row["aliases"])
                merged_aliases = list(dict.fromkeys(
                    have + [a for a in merged_aliases if a not in have]))
            # 先种山再迁别名: merge目标常是不存在的新tag(改名场景) —
            # tags无该行则UPDATE零行生效, 别名整行蒸发 (照照复验残留缺陷)
            con.execute("INSERT OR IGNORE INTO tags(tag) VALUES(?)", (new_tag,))
            con.execute(
                "UPDATE tags SET aliases=? WHERE tag=?",
                (json.dumps(merged_aliases, ensure_ascii=False)
                 if merged_aliases else None, new_tag))
            con.execute("DELETE FROM tags WHERE tag=?", (old_tag,))
            return 200, {"old_tag": old_tag, "new_tag": new_tag,
                         "skills_moved": moved,
                         "aliases_migrated": merged_aliases}

        if kind == "skill.prereq.update":
            edge, action, reason = (body.get("edge"), body.get("action"),
                                    body.get("reason"))
            if not edge or action not in ("add", "remove") or not reason:
                return 400, {"error": "缺edge/action(add|remove)/reason"}
            requires, _, target = edge.partition("=>")
            if not target:
                return 400, {"error": "edge格式: 'B C D E => A'"}
            requires, target = requires.strip(), target.strip()
            if action == "add":
                con.execute(
                    "INSERT OR REPLACE INTO prereqs(target, requires, reason) "
                    "VALUES(?,?,?)", (target, requires, reason))
            else:
                con.execute("DELETE FROM prereqs WHERE target=? AND requires=?",
                            (target, requires))
            return 200, {"edge": edge, "action": action}

        return 500, {"error": f"unhandled kind: {kind}"}

    def post_skill(self):
        try:
            body = self.read_json()
        except ValueError as e:
            return self.send_text(400, f"bad json: {e}")
        name = body.get("name")
        if not name or not str(name).strip():
            return self.send_text(400, "缺name")
        name = str(name).strip()  # 同名挡前後空格繞過: 入庫前 strip 一刀 (照照觀察項4)
        tags_list = body.get("tags") or []
        skill_body = body.get("body") or ""
        con = db()
        try:
            with _lock:
                existing = con.execute(
                    "SELECT skill_id FROM skills WHERE name = ?",
                    (name,)).fetchone()
                if existing:
                    return self.send_text(
                        409, f"同名skill已在馆: {existing['skill_id']} — "
                             "改写走 skill.description.rewrite 事件 + 巡山使")
                sid = "sk:" + uuid.uuid4().hex[:12]
                con.execute(
                    "INSERT INTO skills(skill_id, name, layer, status, author, "
                    "source, imported_at, body, baseline_hash, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, name, "index", "draft",
                     body.get("author") or "unknown",
                     body.get("source"), body.get("imported_at"),
                     skill_body, sha(skill_body), now(), now()))
                # tag查重: 精确折叠+模糊提示 (写入侧, 只提示不拦)
                folded_tags, tag_hints = tag_dup_check(con, tags_list)
                for field, val in (("tags", folded_tags),
                                   ("trigger", body.get("trigger") or ""),
                                   ("boundary", body.get("boundary") or ""),
                                   ("why", body.get("why") or ""),
                                   ("aliases", body.get("aliases") or [])):
                    con.execute(
                        "INSERT OR REPLACE INTO skill_fields"
                        "(skill_id, field, value) VALUES(?,?,?)",
                        (sid, field, json.dumps(val, ensure_ascii=False)))
                # 山系自动生长: 挂不进现有山的tag在submit时自动种成平铺山根
                # (契约tag卫生规则1: 新skill优先挂现有tag; 门槛是门槛, 长山是长山)
                for t in folded_tags:
                    con.execute(
                        "INSERT OR IGNORE INTO tags(tag) VALUES(?)", (t,))
                # 扫描坛建藏书阁门口: draft起步, 报告挂上, 写入畅通
                findings = scan_skill(con, name, skill_body, exclude_id=sid)
                for f in findings:
                    con.execute(
                        "INSERT INTO scan_reports"
                        "(skill_id, face, severity, findings, ts) "
                        "VALUES(?,?,?,?,?)",
                        (sid, f["face"], f["severity"],
                         json.dumps(f, ensure_ascii=False), now()))
                con.execute(
                    "INSERT INTO events(ts, operator, kind, payload) "
                    "VALUES(?,?,?,?)",
                    (now(), body.get("operator") or body.get("author") or "unknown",
                     "skill.pool.submit",
                     json.dumps({"skill_id": sid, "name": name,
                                 "tags": tags_list,
                                 "scan_findings": len(findings)},
                                ensure_ascii=False)))
                if findings:  # 契约v0.2: 入关报告落账 skill.checkin.scan
                    con.execute(
                        "INSERT INTO events(ts, operator, kind, payload) "
                        "VALUES(?,?,?,?)",
                        (now(), "grimoire-scan", "skill.checkin.scan",
                         json.dumps({"skill_id": sid,
                                     "findings_summary": findings},
                                    ensure_ascii=False)))
                con.commit()
                resp = {"skill_id": sid, "status": "draft",
                        "layer": "index",
                        "scan": {"count": len(findings)}}
                if tag_hints:
                    resp["tag_hints"] = tag_hints
                if findings:
                    resp["scan"]["findings"] = findings
                    resp["note"] = ("报告已挂, 写入畅通(只记不拦)。"
                                    "带red报告的draft到不了verified, "
                                    "除非巡山使过目。")
                return self.send_json(200, resp)
        except sqlite3.Error as e:
            con.rollback()
            return self.send_text(500, f"db error: {e}")
        finally:
            con.close()

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            raise ValueError("empty or oversized body")
        return json.loads(self.rfile.read(n))

    def send_text(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send_text(code, json.dumps(obj, ensure_ascii=False, indent=2))


def init_db():
    con = sqlite3.connect(DB)
    con.executescript(open(SCHEMA).read())
    # 工具层分区 (v0.3): tools/tool_entries/tool_telemetry。幂等, 与主 schema 同律。
    _tools_schema = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "schema_tools.sql")
    con.executescript(open(_tools_schema).read())
    # 附件库 (v0.4): vault_index。附件落盘vault/，库只存索引。
    _vault_schema = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "schema_vault.sql")
    con.executescript(open(_vault_schema).read())
    # 三层门 (v0.5): 可见性分档+名单。visibility 列 ALTER 加(先查后加, 幂等);
    # rosters 表 executescript。名单内容=部署态数据, 留空起步, 库主拍板填。
    _vis_schema = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "schema_visibility.sql")
    con.executescript(open(_vis_schema).read())
    cols = [r[1] for r in con.execute("PRAGMA table_info(skills)").fetchall()]
    if "visibility" not in cols:
        con.execute("ALTER TABLE skills ADD COLUMN visibility TEXT "
                    "NOT NULL DEFAULT 'public' "
                    "CHECK(visibility IN ('public','family','private'))")
        print("visibility column added (default public, existing skills unaffected)")
    con.commit()
    con.close()
    print("initialized:", DB)


def main():
    import sys
    init_db()  # 幂等 (CREATE TABLE IF NOT EXISTS): 空库自动建表, 烟测/新部署零仪式
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8730
    srv = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
    print(f"grimoire serving on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
