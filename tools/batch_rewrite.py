#!/usr/bin/env python3
"""巡山使批量 rewrite 工具: 按 (name, new_trigger) 清单逐本 POST /event
skill.description.rewrite (绑 baseline_hash, stale 即拒)。
用法: python3 batch_rewrite.py manifest.json   # [{"name":..., "trigger":...}, ...]
每本独立报告成功/失败, 单本失败不阻塞清单其余。
"""
import json, sys, urllib.error, urllib.request

BASE = "http://127.0.0.1:8730"

def get_baseline(name):
    with urllib.request.urlopen(f"{BASE}/skill/{name}") as r:
        for line in r.read().decode().splitlines():
            if line.startswith("baseline_hash:"):
                return line.split(":", 1)[1].strip()
    return None

def rewrite(name, trigger):
    bh = get_baseline(name)
    if not bh:
        return {"name": name, "error": "no baseline_hash"}
    body = json.dumps({
        "kind": "skill.description.rewrite", "operator": "巡山使",
        "skill_id": name, "trigger": trigger, "baseline_hash": bh,
    }).encode()
    req = urllib.request.Request(f"{BASE}/event", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())

if __name__ == "__main__":
    manifest = json.load(open(sys.argv[1]))
    ok = fail = 0
    for item in manifest:
        try:
            resp = rewrite(item["name"], item["trigger"])
            if "error" in resp:
                print(f"FAIL {item['name']}: {resp['error']}")
                fail += 1
            else:
                print(f"OK   {item['name']}: {resp.get('trigger_head','')[:50]}")
                ok += 1
        except urllib.error.HTTPError as e:
            print(f"FAIL {item['name']}: HTTP {e.code} {e.read().decode()[:120]}")
            fail += 1
    print(f"\n{ok} ok, {fail} fail")
    sys.exit(0 if fail == 0 else 1)
