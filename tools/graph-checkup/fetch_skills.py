#!/usr/bin/env python3
"""fetch all skill bodies from the skill-library map service → skills-txt/<id>.md (read-only, for graph checkup)"""
import json, os, time, urllib.request

BASE = "http://127.0.0.1:8730"
OUT = os.path.join(os.path.dirname(__file__), "skills-txt")
tagmap = json.load(open(os.path.join(os.path.dirname(__file__), "tag-map.json")))

ok, fail = 0, []
for sid in sorted(tagmap):
    dest = os.path.join(OUT, f"{sid}.md")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        ok += 1
        continue
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(f"{BASE}/skill/{sid}", timeout=15) as r:
                data = r.read().decode("utf-8", "replace")
            open(dest, "w").write(data)
            ok += 1
            break
        except Exception as e:
            if attempt == 2:
                fail.append((sid, str(e)))
            time.sleep(0.3)
print(f"fetched {ok}/{len(tagmap)}, failed: {fail}")
