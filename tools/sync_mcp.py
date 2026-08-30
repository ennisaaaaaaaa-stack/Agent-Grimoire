#!/usr/bin/env python3
"""sync_mcp.py — MCP 侧工具层镜像同步 (v0.3 一期)。

事实源: ~/.hermes/config.yaml 的 mcp_servers 键。
山海只做镜像读脸 (主权在外面, sync 是被动的):
- 新 server 出现 → capability + mcp entry 落库 (status=active)
- server 消失   → entry 标 retired (不删——账本文化, 消失也是事件)
- 已存在       → no-op (幂等)

CLI/API 不走这个脚本: 无被动事实源, 走 POST /tool 登记制。

用法: python3 tools/sync_mcp.py [--db PATH] [--dry-run]
"""
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone


def _norm_ref(server):
    """server名归一化: config.yaml连字符 / Hermes运行时下划线, 折到同一边对账。
    (Bug A照照四审: 两边形状不一致→5/6个server遥测静默漏账)"""
    return (server or "").replace("-", "_")

try:
    import yaml
except ImportError:
    yaml = None

DB = os.environ.get("GRIMOIRE_DB",
                    os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), "grimoire.db"))
CONFIG = os.environ.get("HERMES_CONFIG",
                        os.path.join(os.path.expanduser("~"),
                                     ".hermes", "config.yaml"))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def load_mcp_servers():
    if yaml:
        with open(CONFIG) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        # 无 yaml 库时最小解析: 取 mcp_servers: 块下的两空格缩进行
        servers = {}
        in_block = False
        for line in open(CONFIG):
            if line.startswith("mcp_servers:"):
                in_block = True
                continue
            if in_block:
                if line.strip() and not line.startswith("  "):
                    in_block = False
                    continue
                s = line.strip()
                if s and not s.startswith("#") and ":" in s:
                    name = s.split(":")[0].strip()
                    if name and not name.startswith("-"):
                        servers[name] = True
        cfg = {"mcp_servers": servers}
    return sorted((cfg.get("mcp_servers") or {}).keys())


def http(method, path, body=None):
    port = os.environ.get("GRIMOIRE_PORT", "8730")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode()


def main():
    dry = "--dry-run" in sys.argv
    servers = load_mcp_servers()
    print(f"config.yaml mcp_servers: {servers}")
    # Bug B(照照四审): fresh clone 无 grimoire.db 时直接 no such table 崩——
    # 库还没初始化就明说, 不留半截 traceback
    if not os.path.exists(DB):
        print(f"fresh 库不存在: {DB}\n先跑 python3 grimoire.py init_db 初始化再同步")
        return 1
    # 读现有 mcp entries
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    have = {r["ref"]: r for r in con.execute(
        "SELECT * FROM tool_entries WHERE kind='mcp'").fetchall()}
    con.close()
    to_add = [s for s in servers
              if _norm_ref(s) not in {_norm_ref(r) for r in have}]
    to_retire = [r for ref, r in have.items()
                 if r["status"] == "active"
                 and _norm_ref(ref) not in {_norm_ref(s) for s in servers}]
    if not to_add and not to_retire:
        print("sync: 无变化 (幂等)")
        return
    for s in to_add:
        cap = f"mcp-{s}"
        print(f"+ {s} -> capability={cap} kind=mcp")
        if not dry:
            code, resp = http("POST", "/tool", {
                "action": "tool.register", "capability": cap,
                "kind": "mcp", "ref": s,
                "note": "auto-sync from config.yaml (事实源在外)"})
            print(f"  -> {code} {resp.strip()}")
    for r in to_retire:
        # 被动事实源判 retired 而非 delete——消失也是事件
        print(f"- {r['ref']} -> retired (config.yaml 已无此 server)")
        if not dry:
            con = sqlite3.connect(DB)
            con.execute(
                "UPDATE tool_entries SET status='retired', updated_at=? "
                "WHERE entry_id=?", (now(), r["entry_id"]))
            con.commit()
            con.close()
    if dry:
        print("(dry-run, 未落库)")


if __name__ == "__main__":
    main()
