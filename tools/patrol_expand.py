#!/usr/bin/env python3
"""巡山使批量巡查阅读遥测: 逐本判断过的书各落一笔 expand。

巡逻协议要求点名不豁免——每本轮读过、判断过的书, 账本里要有巡查痕迹。
用法: python3 patrol_expand.py [--verdict 判断语] 书名1 书名2 ...   # 名字含中文亦可
每本独立报告, 单本失败(404等)不阻塞其余。其他 - 开头参数不认识即忽略。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8730"


def expand(name, verdict="巡查阅读"):
    body = json.dumps({
        "kind": "skill.telemetry.expand", "operator": "巡山使",
        "skill_id": name, "verdict": verdict,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{BASE}/event", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status


if __name__ == "__main__":
    args = sys.argv[1:]
    verdict = "巡查阅读"
    names = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--verdict":
            if i + 1 < len(args):
                verdict = args[i + 1]
                i += 2
                continue
            sys.exit("--verdict 缺值")
        if a.startswith("-"):
            i += 1          # 不认识的 flag: 跳过自身(不吃下一个词——昨夜脏遥测之坑)
            continue
        names.append(a)
        i += 1
    if not names:
        sys.exit("用法: patrol_expand.py [--verdict 判断语] 书名1 书名2 ...")
    ok = fail = 0
    for name in names:
        try:
            expand(name, verdict)
            print(f"OK   {name}")
            ok += 1
        except urllib.error.HTTPError as e:
            print(f"FAIL {name}: HTTP {e.code} {e.read().decode()[:80]}")
            fail += 1
    print(f"\n{ok} ok, {fail} fail")
    sys.exit(0 if fail == 0 else 1)
