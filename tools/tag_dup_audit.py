#!/usr/bin/env python3
"""tag_dup_audit — 全库tag查重审计 (巡山使工具, 只报告不动手).

扫描正字表+别名表, 输出三类发现:
1) 精确重复: 归一化后同形的正字对 (建库事故, 应合并)
2) 近义候选: 编辑距离≤2的正字对 (巡山使复核后决定谁进aliases)
3) 组合tag体检: 槽位里藏着未折叠旧词的组合 (槽内折正字演示)

用法: python3 tools/tag_dup_audit.py [--db PATH]
"""
import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from grimoire import _slot_norm, _levenshtein  # noqa: E402

DB = os.environ.get("GRIMOIRE_DB", str(pathlib.Path(__file__).resolve().parent.parent / "grimoire.db"))


def norm(s):
    return _slot_norm(s).lower()


def main():
    db = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else DB
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tags = {r["tag"]: (json.loads(r["aliases"]) if r["aliases"] else [])
            for r in con.execute("SELECT tag, aliases FROM tags")}
    con.close()

    canon = {}
    for t, al in tags.items():
        canon[norm(t)] = t
        for a in al:
            canon[norm(a)] = t

    # 1) 精确重复 (归一化撞形)
    exact = []
    seen_norm = {}
    for t in tags:
        n = norm(t)
        if n in seen_norm and seen_norm[n] != t:
            exact.append((seen_norm[n], t))
        else:
            seen_norm.setdefault(n, t)

    # 2) 近义候选 (编辑距离≤2, 双方len≥3)
    near = []
    tl = list(tags)
    for i, a in enumerate(tl):
        for b in tl[i + 1:]:
            na, nb = norm(a), norm(b)
            if abs(len(na) - len(nb)) <= 2 and len(na) >= 3 and len(nb) >= 3 \
                    and _levenshtein(na, nb) <= 2:
                near.append((a, b))

    # 3) 组合tag体检: 槽内藏旧词 → 演示折正字
    combo = []
    for t in tags:
        if "·" not in t:
            continue
        slots = [_slot_norm(x) for x in t.split("·") if x.strip()]
        folded = [canon.get(norm(s), s) for s in slots]
        if folded != slots:
            combo.append((t, "·".join(folded)))

    print(f"== tag查重审计 ({len(tags)} 个正字) ==")
    print(f"\n[1] 精确重复 (归一化撞形, 建议合并): {len(exact)}")
    for a, b in exact:
        print(f"  {a}  ≈  {b}")
    print(f"\n[2] 近义候选 (编辑距离≤2, 复核后决定谁进aliases): {len(near)}")
    for a, b in near:
        print(f"  {a}  ~  {b}")
    print(f"\n[3] 组合tag槽内藏旧词 (可折正字): {len(combo)}")
    for t, f in combo:
        print(f"  {t}  →  {f}")
    total = len(exact) + len(near) + len(combo)
    print(f"\n共 {total} 条发现。只报告不动手, 归并动作走巡山使流程。")
    sys.exit(0)


if __name__ == "__main__":
    sys.exit(main())
