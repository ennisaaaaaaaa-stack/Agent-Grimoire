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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/home/ubuntu/Agent-Grimoire/grimoire.db"
SCHEMA = "/home/ubuntu/Agent-Grimoire/schema.sql"
LISTEN_HOST = "127.0.0.1"

EVENT_KINDS = {
    "skill.submit",
    "skill.push",
    "skill.expand",
    "skill.description.rewrite",
}

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

        mapping = con.execute(
            "SELECT s.skill_id, s.name, s.tier, s.status, f.value AS tags "
            "FROM skills s LEFT JOIN skill_fields f "
            "ON f.skill_id = s.skill_id AND f.field = 'tags' "
            "WHERE s.tier != 'archive' AND s.status != 'retired' "
            "ORDER BY s.name").fetchall()
        if mapping:
            lines.append("## tag↔skill 映射")
            for m in rows(mapping):
                taglist = json.loads(m["tags"]) if m["tags"] else []
                lines.append(
                    f"{m['skill_id']}  [{m['tier']}/{m['status']}]  {','.join(taglist)}")
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
        m = re.match(r"^/tag/([^/]+)$", self.path)
        if m:
            return self.get_tag(urllib.parse.unquote(m.group(1)))
        m = re.match(r"^/skill/([^/]+)$", self.path)
        if m:
            return self.get_skill(urllib.parse.unquote(m.group(1)))
        return self.send_text(404, "not found")

    def get_tag(self, tag):
        con = db()
        try:
            out = []
            for r in rows(con.execute(
                    "SELECT s.skill_id, s.tier, s.status, f.value AS tags "
                    "FROM skills s LEFT JOIN skill_fields f "
                    "ON f.skill_id = s.skill_id AND f.field = 'tags' "
                    "WHERE s.tier != 'archive' AND s.status != 'retired' "
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
                    out.append(f"{r['skill_id']}  [{r['tier']}/{r['status']}]  {desc}")
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
                "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)).fetchone()
            if not row:
                return self.send_text(404, "not found")
            if row["status"] == "retired" and \
                    not self.headers.get("X-Include-Retired"):
                return self.send_text(
                    410, f"该skill已retired: {skill_id} (带X-Include-Retired头可查阅)")
            flds = {}
            for f in rows(con.execute(
                    "SELECT field, value FROM skill_fields WHERE skill_id = ?",
                    (skill_id,)).fetchall()):
                flds[f["field"]] = (json.loads(f["value"])
                                    if f["field"] in ("tags", "aliases")
                                    else f["value"])
            head = (f"# {row['name']}\n"
                    f"tier: {row['tier']}  status: {row['status']}\n"
                    f"tags: {flds.get('tags', [])}\n"
                    f"trigger: {flds.get('trigger', '')}\n"
                    f"boundary: {flds.get('boundary', '')}\n"
                    f"why: {flds.get('why', '')}\n\n")
            tail = ("\n\n---\n用完顺手记一笔: POST /event "
                    '{"kind":"skill.expand","operator":"<你>","skill_id":"'
                    f'{skill_id}' + '"} — 好用/没用, 你判断.)')
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
                con.execute(
                    "INSERT INTO events(ts, operator, kind, payload) "
                    "VALUES(?,?,?,?)",
                    (now(), operator, kind,
                     json.dumps(body, ensure_ascii=False)))
                con.commit()
            return self.send_text(200, "已落账。只记不动: 账本不触发任何变更。")
        except sqlite3.Error as e:
            con.rollback()
            return self.send_text(500, f"ledger write failed: {e}")
        finally:
            con.close()

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
                sid = slugify(name)
                con.execute(
                    "INSERT INTO skills(skill_id, name, tier, status, author, "
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
                     "skill.submit",
                     json.dumps({"skill_id": sid, "name": name,
                                 "tags": tags_list,
                                 "scan_findings": len(findings)},
                                ensure_ascii=False)))
                con.commit()
                resp = {"skill_id": sid, "status": "draft",
                        "tier": "index",
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
