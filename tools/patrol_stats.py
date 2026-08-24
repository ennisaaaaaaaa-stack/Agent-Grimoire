#!/usr/bin/env python3
"""巡山使巡逻统计: 一条命令出报告所需的四个数字。
用法: python3 patrol_stats.py ["<上轮报告UTC时刻, 如 2026-08-21T16:31:00>"]
输出: 暗区数量 / 经图字节数 / 弱trigger数 / (可选)上轮以来新增事件数
弱trigger判据与历次报告一致: <25字 或 无中文且<60字。
"""
import json, os, pathlib, re, sqlite3, sys, urllib.request

DB = os.environ.get("GRIMOIRE_DB", str(pathlib.Path(__file__).resolve().parent.parent / "grimoire.db"))
MAP = "http://127.0.0.1:8730/map"

def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    seen = set()
    for r in con.execute("SELECT payload FROM events WHERE kind IN "
                         "('skill.telemetry.push','skill.telemetry.expand')"):
        p = json.loads(r["payload"])
        v = p.get("skill_id") or p.get("name")
        if v:
            seen.add(v)
    dark = weak = 0
    for r in con.execute("""SELECT s.name, s.skill_id, f.value t FROM skills s
        LEFT JOIN skill_fields f ON f.skill_id=s.skill_id AND f.field='trigger'
        WHERE s.layer!='archive' AND s.status!='retired'"""):
        if r["name"] not in seen and r["skill_id"] not in seen:
            dark += 1
        t = r["t"] or ""
        if t.startswith('"'):
            t = json.loads(t)
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
        if len(t) < 25 or (not has_cjk and len(t) < 60):
            weak += 1
    with urllib.request.urlopen(MAP) as resp:
        map_bytes = len(resp.read())
    print(f"暗区数量: {dark}")
    print(f"经图字节数: {map_bytes}")
    print(f"弱trigger数: {weak}")
    print(f"事件总数: {con.execute('SELECT COUNT(*) FROM events').fetchone()[0]}")
    if len(sys.argv) > 1:
        n = con.execute("SELECT COUNT(*) FROM events WHERE ts > ?",
                        (sys.argv[1],)).fetchone()[0]
        print(f"上轮({sys.argv[1]} UTC)以来新增事件: {n}")
    con.close()

if __name__ == "__main__":
    main()
