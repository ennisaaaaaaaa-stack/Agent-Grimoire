#!/usr/bin/env python3
"""build tag-map.json (skill id -> [tags]) from the map service's /map markdown.

The map endpoint lists `tag: skill1, skill2, ...` lines; skills live under
multiple tags, so we invert that mapping. Run this first if tag-map.json is
missing — fetch_skills/extract_edges/analyze all depend on it.
"""
import json, os, re, sys, urllib.request

BASE = os.environ.get("SKILL_MAP_BASE", "http://127.0.0.1:8730")
HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "tag-map.json")


def main() -> int:
    url = f"{BASE}/map"
    raw = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")

    tagmap: dict[str, list[str]] = {}
    n_lines = 0
    for line in raw.splitlines():
        # match "tag: skill1, skill2, ..." (tag itself may contain letters/-/_/中文)
        m = re.match(r"^([^:#\s][^:]*):\s*(.+)$", line.strip())
        if not m:
            continue
        tag, rhs = m.group(1).strip(), m.group(2).strip()
        if not tag or tag.startswith("#") or " " in tag:
            continue
        if rhs.startswith("GET") or ";" in rhs:  # usage-syntax header line, not data
            continue
        for sid in [s.strip() for s in rhs.split(",") if s.strip()]:
            if " " in sid or any(ch in sid for ch in "<>()。；"):
                continue
            tagmap.setdefault(sid, [])
            if tag not in tagmap[sid]:
                tagmap[sid].append(tag)
        n_lines += 1

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(tagmap, f, ensure_ascii=False, indent=1)
    print(f"parsed {n_lines} tag lines -> {len(tagmap)} skills -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
