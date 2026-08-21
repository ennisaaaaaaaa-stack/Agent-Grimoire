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
import re
import sqlite3
import hashlib
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/home/ubuntu/Agent-Grimoire/grimoire.db"
SCHEMA = "/home/ubuntu/Agent-Grimoire/schema.sql"
LISTEN_HOST = "127.0.0.1"

# 契约 v0.2 域6全量事件（skill.checkin.scan 由提交路径内部落账，不经 POST /event）
TELEMETRY_KINDS = {          # 只记不动：遥测永远不触发变更
    "skill.telemetry.push",
    "skill.telemetry.expand",
}
GOVERNANCE_KINDS = {         # 写脸：策展动作，动作+落账一起（契约"落盘动作走管理工具，全部产生账本事件"）
    "skill.pool.review",     # decision: promoted|discarded
    "skill.roster.update",   # layer: core|index|archive
    "skill.tag.merge",       # old_tag -> new_tag
    "skill.prereq.update",   # 边增删
    "skill.description.rewrite",  # 改写绑 baseline_hash，不符→409 stale
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


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def slugify(name):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", name.strip().lower())
    return s.strip("-")[:60] or "unnamed"


def rows(rows_or_none):
    return rows_or_none or []


# ---------------- 经图 (opening map) ----------------

def render_map():
    con = db()
    try:
        tags = con.execute(
            "SELECT tag, parent, note FROM tags ORDER BY tag").fetchall()
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
        core_rows = con.execute(
            "SELECT s.skill_id, s.name, s.body FROM skills s "
            "WHERE s.layer = 'core' AND s.status != 'retired' "
            "ORDER BY s.name").fetchall()
        if core_rows:
            lines.append("## core层全文常驻")
            for c in rows(core_rows):
                lines.append(f"### {c['name']}")
                lines.append(c["body"])
                lines.append("")

        mapping = con.execute(
            "SELECT s.name, s.layer, f.value AS tags "
            "FROM skills s LEFT JOIN skill_fields f "
            "ON f.skill_id = s.skill_id AND f.field = 'tags' "
            "WHERE s.layer != 'archive' AND s.status != 'retired' "
            "ORDER BY s.name").fetchall()
        if mapping:
            by_tag = {}
            for m in rows(mapping):
                for t in (json.loads(m["tags"]) if m["tags"] else []):
                    by_tag.setdefault(t, []).append(m["name"])
            lines.append("## tag↔skill 映射")
            for t in sorted(by_tag):
                lines.append(f"{t}: {', '.join(by_tag[t])}")
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

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            return self.send_text(200, "ok")
        if self.path == "/map":
            return self.send_text(200, render_map())
        if self.path == "/darkzone":
            return self.get_darkzone()
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
        名单是它们的保底。逐本判断仍是巡山使的活。"""
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
            out = []
            for r in rows(con.execute(
                    "SELECT s.skill_id, s.name, s.layer, s.status "
                    "FROM skills s WHERE s.status != 'retired' "
                    "AND s.layer != 'archive' "
                    "ORDER BY s.name").fetchall()):
                if r["name"] not in names and r["skill_id"] not in names:
                    out.append(f"{r['name']}  [{r['layer']}/{r['status']}]")
            body = "暗区点名(从未被push/expand):\n" + ("\n".join(out)
                    if out else "(空——没有暗区skill)")
            body += "\n\n出口提醒: 名单是机械的, 逐本判断仍是巡山使的活。"
            return self.send_text(200, body)
        finally:
            con.close()

    def get_tag(self, tag):
        con = db()
        try:
            tag = self.canonical_tag(con, tag)  # 查询侧归并: 别名折叠到正字
            out = []
            for r in rows(con.execute(
                    "SELECT s.skill_id, s.name, s.layer, s.status, f.value AS tags "
                    "FROM skills s LEFT JOIN skill_fields f "
                    "ON f.skill_id = s.skill_id AND f.field = 'tags' "
                    "WHERE s.layer != 'archive' AND s.status != 'retired' "
                    "ORDER BY s.name").fetchall()):
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
            if row["status"] == "retired" and \
                    not self.headers.get("X-Include-Retired"):
                return self.send_text(
                    410, f"该skill已retired: {skill_id} (带X-Include-Retired头可查阅)")
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
            return self.send_text(200, head + row["body"] + tail)
        finally:
            con.close()

    def do_POST(self):
        if self.path == "/event":
            return self.post_event()
        if self.path == "/skill":
            return self.post_skill()
        return self.send_text(404, "endpoints: POST /event, POST /skill")

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

                resp = self.govern(con, kind, operator, body)
                if resp[0] < 300:  # 动作成功才落账
                    con.execute(
                        "INSERT INTO events(ts, operator, kind, payload) "
                        "VALUES(?,?,?,?)",
                        (now(), operator, kind,
                         json.dumps(body, ensure_ascii=False)))
                    con.commit()
                return self.send_json(resp[0], resp[1])
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
            if layer not in ("core", "index", "archive"):
                return 400, {"error": "layer须为 core|index|archive"}
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
            con.execute(
                "INSERT OR REPLACE INTO skill_fields(skill_id, field, value) "
                "VALUES(?,?,?)", (row["skill_id"], "trigger", new_trigger))
            return 200, {"skill_id": row["skill_id"],
                         "trigger_head": new_trigger[:80],
                         "baseline_hash": row["baseline_hash"]}

        if kind == "skill.tag.merge":
            old_tag, new_tag = body.get("old_tag"), body.get("new_tag")
            if not old_tag or not new_tag:
                return 400, {"error": "缺old_tag/new_tag"}
            moved = 0
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
            con.execute("DELETE FROM tags WHERE tag=?", (old_tag,))
            return 200, {"old_tag": old_tag, "new_tag": new_tag, "skills_moved": moved}

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
        if not name:
            return self.send_text(400, "name总要有 (其余全可空)")
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
                for field, val in (("tags", tags_list),
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
                for t in tags_list:
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
    con.commit()
    con.close()
    print("initialized:", DB)


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        return init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8730
    srv = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
    print(f"grimoire serving on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
