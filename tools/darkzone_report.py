#!/usr/bin/env python3
"""darkzone_report: 暗区点名的机械半边全接管。

服务端 /darkzone 给全量平单; 本脚本按 source 分组重排:
  - 迁移豁免批 (source=hermes-import): 点名即可, 巡山协议原文"点名即可，不逐本判断"
  - 非迁移暗区 (source=self/None/其他): 真判断对象, 逐本过
分组依据是 DB 里的 source 字段 (import 事件时落账), 不是日期猜测。
用法: python3 darkzone_report.py   # 只读, 零写入
"""
import sys
import json
import pathlib
import re
import sqlite3
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = str(ROOT / "grimoire.db")
BASE = "http://127.0.0.1:8730"


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # 名单口径与 get_darkzone 完全一致: 从未被 push/expand 且未退役未归档
    names = set()
    for r in con.execute(
        "SELECT payload FROM events WHERE kind IN "
        "('skill.telemetry.push','skill.telemetry.expand')"
    ).fetchall():
        p = json.loads(r["payload"])
        v = p.get("skill_id") or p.get("name")
        if v:
            names.add(v)

    exempt, judge = [], []
    for r in con.execute(
        "SELECT skill_id, name, source, layer, status FROM skills "
        "WHERE status != 'retired' AND layer != 'archive' ORDER BY name"
    ).fetchall():
        if r["name"] in names or r["skill_id"] in names:
            continue
        (exempt if r["source"] == "hermes-import" else judge).append(
            f"{r['name']}  [{r['layer']}/{r['status']}]"
        )

    print(f"暗区总数: {len(exempt) + len(judge)}")
    print(f"\n== 迁移豁免批 (hermes-import, 点名即可): {len(exempt)} 本 ==")
    print("\n".join(exempt) if exempt else "(无)")
    print(f"\n== 非迁移暗区 (巡山使逐本判断): {len(judge)} 本 ==")
    print("\n".join(judge) if judge else "(无——非迁移暗区清零)")


if __name__ == "__main__":
    sys.exit(main())
