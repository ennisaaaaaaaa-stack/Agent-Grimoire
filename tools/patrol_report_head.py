#!/usr/bin/env python3
"""patrol_report_head: 巡山报告的机械头部生成器。

把 Patrol-003 报告里巡山使手工拼的四样东西接管:
  1. 四数字 (patrol_stats 已有, 这里直接调用它的输出)
  2. 暗区分组点名 (darkzone_report 逻辑内联: 豁免批/判断批分开)
  3. 超限山头 (tags>15, 协议阈值)
  4. 本周新增事件数 (patrol_stats since 参数)

输出粘进报告当头部, 巡山使只写判断部分 (逐本裁定、rewrite理由、协议例外)。
用法: python3 patrol_report_head.py [上轮报告UTC时刻]
只读零写入。
"""
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = str(ROOT / "grimoire.db")
HERE = __file__.rsplit("/", 1)[0]


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else None
    # produced-by 印章 (v0.3, 照照建议): 报告头自报产出工具。
    # 协议退化(退回手工拼名单)变成可机检——报告头没有工具段 = drift 信号。
    print("produced-by: patrol_report_head.py"
          + (f" (since={since})" if since else ""))
    print()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # -- 1. 四数字: 直接复用 patrol_stats
    cmd = [sys.executable, f"{HERE}/patrol_stats.py"] + ([since] if since else [])
    stats_out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    print(stats_out)
    print()

    # -- 2. 暗区分组
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
        "SELECT skill_id, name, source FROM skills "
        "WHERE status != 'retired' AND layer != 'archive' ORDER BY name"
    ).fetchall():
        if r["name"] in names or r["skill_id"] in names:
            continue
        (exempt if r["source"] == "hermes-import" else judge).append(r["name"])

    print(f"暗区点名: 总 {len(exempt)+len(judge)} = 迁移豁免批 {len(exempt)}"
          f" (点名即可) + 非迁移 {len(judge)} (逐本判断)")
    if judge:
        print("非迁移暗区名单(逐本判断):")
        print("\n".join(f"  - {n}" for n in judge))
    print()

    # -- 3. 超限山头
    c = Counter()
    for r in con.execute(
        "SELECT f.value FROM skill_fields f JOIN skills s ON f.skill_id=s.skill_id "
        "AND f.field='tags' WHERE s.status!='retired'"
    ).fetchall():
        for t in json.loads(r["value"]):
            if not t.startswith("."):
                c[t] += 1
    over = {t: n for t, n in c.items() if n > 15}
    print(f"超限山头 (阈值15): {over if over else '无'}")


if __name__ == "__main__":
    main()
