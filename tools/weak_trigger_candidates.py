#!/usr/bin/env python3
"""weak_trigger_candidates: 弱trigger候选名单(机械半边)。

判据 v1 与历次报告一致: <25字 或 无中文且<60字。
本脚本只出候选名单——判断"短而完整(xurl型)还是短而空"仍归巡山使,
Patrol-003 已证机械判据分不开这两者(伪精度销案), 但名单本身是机械的。
用法: python3 weak_trigger_candidates.py   # 只读
"""
import json
import sqlite3

DB = "grimoire.db"


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    weak = []
    for r in con.execute(
        "SELECT s.name, f.value trig FROM skills s "
        "JOIN skill_fields f ON f.skill_id=s.skill_id AND f.field='trigger' "
        "WHERE s.status!='retired' AND s.layer!='archive' ORDER BY s.name"
    ).fetchall():
        t = (r["trig"] or "").strip('"')
        if len(t) < 25 or (not any("\u4e00" <= c <= "\u9fff" for c in t) and len(t) < 60):
            weak.append((r["name"], t))
    print(f"弱trigger候选: {len(weak)} 本 (判断短而完整还是短而空 → 巡山使)")
    for n, t in weak:
        print(f"  {n}: {t[:60]!r}")


if __name__ == "__main__":
    main()
