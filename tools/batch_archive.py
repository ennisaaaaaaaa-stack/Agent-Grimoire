#!/usr/bin/env python3
"""巡山使批量归档工具: 近义合并裁决的执行半边。

与 batch_rewrite.py 配对使用 (合并 = rewrite 保留本 + roster 归档被并本,
各占一笔预算)。按名单逐本 POST /event skill.roster.update layer=archive。
用法: python3 batch_archive.py [--dry-run] 书名1 书名2 ...
每本独立报告成功/失败, 单本失败不阻塞其余。幂等: 已在 archive 的重复归档
仍返回 200 (UPDATE 语义), 账本多一笔留痕不算错。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('GRIMOIRE_PORT', '8730')}"


def archive(name):
    body = json.dumps({
        "kind": "skill.roster.update", "operator": "巡山使",
        "skill_id": name, "layer": "archive",
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{BASE}/event", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    ok = fail = 0
    for name in args:
        if dry:
            layer = None
            try:
                with urllib.request.urlopen(f"{BASE}/skill/{name}") as r:
                    for line in r.read().decode().splitlines():
                        if line.startswith("layer:"):
                            layer = line.split(":", 1)[1].split()[0]
                print(f"DRY  {name}: 当前 layer={layer}"
                      + (" (已在archive, 幂等重放)" if layer == "archive" else ""))
            except urllib.error.HTTPError as e:
                print(f"DRY  {name}: HTTP {e.code} 不存在或不可读")
            continue
        try:
            resp = archive(name)
            if "error" in resp:
                print(f"FAIL {name}: {resp['error']}")
                fail += 1
            else:
                print(f"OK   {name}: layer={resp.get('layer')}")
                ok += 1
        except urllib.error.HTTPError as e:
            print(f"FAIL {name}: HTTP {e.code} {e.read().decode()[:120]}")
            fail += 1
    if not dry:
        print(f"\n{ok} ok, {fail} fail")
        sys.exit(0 if fail == 0 else 1)
