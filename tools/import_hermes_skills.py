#!/usr/bin/env python3
"""把Hermes技能库搬进山海——狗粮第一步。
类目=山系(22座山), SKILL.md frontmatter=条目字段, 正文=body。
导入后批量转正(在产技能, 日常会话已在使用=验证过), 全走正脸接口, 账本留痕。
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8730"
SKILLS_DIR = Path(os.environ.get("HERMES_SKILLS_DIR", str(Path.home() / ".hermes" / "skills")))


def post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, r.read().decode()


def parse_frontmatter(text):
    """返回 (meta dict, body str)。frontmatter不齐也不炸。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    meta, body = {}, text
    if m:
        body = text[m.end():]
        fm = m.group(1)
        for line in fm.splitlines():
            mm = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
            if mm:
                key, val = mm.group(1), mm.group(2).strip().strip('"').strip("'")
                meta[key] = val
    return meta, body


def main():
    files = sorted(SKILLS_DIR.glob("*/*/SKILL.md")) + \
        sorted(SKILLS_DIR.glob("*/SKILL.md"))
    print(f"found {len(files)} SKILL.md")
    ok = fail = 0
    flagged = []
    t0 = time.time()
    for f in files:
        rel = f.relative_to(SKILLS_DIR)
        category = rel.parts[0]              # 山系 = 类目
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        name = meta.get("name") or rel.parts[-2]
        desc = meta.get("description") or ""
        author = meta.get("author") or "hermes-skill"
        s, resp = post("/skill", {
            "name": name,
            "author": author,
            "source": "hermes-import",
            "tags": [category],
            "trigger": desc[:200],
            "body": body,
            "operator": "hui",
        })
        if s == 200:
            ok += 1
            n = resp.get("scan", {}).get("count", 0)
            if n:
                sev = [x.get("severity") for x in resp.get("scan", {}).get("findings", [])]
                flagged.append((name, category, sev))
        else:
            fail += 1
            print(f"FAIL {name}: {s} {str(resp)[:120]}")
    print(f"import: {ok} ok, {fail} fail, {time.time()-t0:.1f}s")

    # 批量转正: 在产技能, 本环境日常会话使用中=已验证。账本逐条留痕。
    t0 = time.time()
    promoted = 0
    for f in files:
        meta, _ = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        name = meta.get("name") or f.parent.name
        s, _ = post("/event", {
            "kind": "skill.pool.review", "operator": "hui",
            "skill_id": name, "decision": "promoted",
            "note": "hermes库批量入馆: 在产技能, 日常会话使用中"})
        promoted += (s == 200)
    print(f"promoted: {promoted}, {time.time()-t0:.1f}s")

    # 扫描把哪些真技能标了色(狗糧数据: 报告不拦截, 巡山使后审)
    print(f"\nscan flagged {len(flagged)} skills:")
    for name, cat, sev in flagged:
        print(f"  [{cat}] {name}: {sev}")

    s, map_text = get("/map")
    print(f"\n/map: {len(map_text)} bytes, status {s}")
    mountains = [l for l in map_text.splitlines()
                 if l and not l.startswith(("#", " ", "查询", "这是"))]
    print(f"map lines: {len(map_text.splitlines())}")
    print("\n--- 经图前25行 ---")
    print("\n".join(map_text.splitlines()[:25]))


if __name__ == "__main__":
    main()
