#!/usr/bin/env python3
"""telemetry_sink.py — post_tool_call 钩子: 账本即遥测 (v0.3)。

挂在 hermes plugin 的 post_tool_call 上, 每次工具调用落一笔 tool_telemetry。
零 AI 参与 (纯逻辑 adapter, ~40 行壳)——"能不走LLM的事不需要LLM处理"。

匹配规则 (entry 级, 照照判据):
- mcp 工具: tool_name 形如 "mcp__<server>__<tool>" → entry ref=<server>
- cli 工具: tool_name == "terminal" 且 args.command 命中已登记 cli entry
  (按 ref 脚本名出现于命令行判断) → 该 entry
- 其余不落账 (未登记的能力不遥测——暗区里放的都是没门牌的)

失败静默 (spoor 钩子同律): 遥测永远不该打断工具调用本身。
"""
import json
import os
import re
import sqlite3
import threading

_lock = threading.Lock()
_MCP_RE = re.compile(r"^mcp__([a-zA-Z0-9_\-]+)__[a-zA-Z0-9_\-]+$")

DB = os.environ.get("GRIMOIRE_DB",
                    os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), "grimoire.db"))


def _match_entry(tool_name, args):
    """tool_name/args → entry_id。未登记返回 None。"""
    con = sqlite3.connect(DB, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        m = _MCP_RE.match(tool_name or "")
        if m:
            row = con.execute(
                "SELECT entry_id FROM tool_entries "
                "WHERE kind='mcp' AND ref=? AND status='active'",
                (m.group(1),)).fetchone()
            if row:
                return row["entry_id"], "mcp", m.group(1)
            return None, None, None
        cmd = (args or {}).get("command", "") if tool_name == "terminal" else ""
        if cmd:
            for r in con.execute(
                    "SELECT entry_id, ref FROM tool_entries "
                    "WHERE kind='cli' AND status='active'").fetchall():
                if r["ref"] and r["ref"] in cmd:
                    return r["entry_id"], "cli", r["ref"]
        return None, None, None
    finally:
        con.close()


def on_post_tool_call(tool_name="", args=None, result=None, caller="",
                      status=None, duration_ms=0, **kwargs):
    """plugin hook 入口。失败静默。"""
    try:
        entry_id, kind, ref = _match_entry(tool_name, args or {})
        if not entry_id:
            return
        ok = 1
        if isinstance(status, str):
            ok = 0 if "error" in status.lower() else 1
        caller = caller or kwargs.get("session_id") or "unknown"
        con = sqlite3.connect(DB, timeout=2)
        try:
            with _lock:
                con.execute(
                    "INSERT INTO tool_telemetry(ts, entry_id, caller, ok, payload) "
                    "VALUES(?,?,?,?,?)",
                    (_now(), entry_id, caller, ok,
                     json.dumps({"tool": tool_name, "kind": kind,
                                 "status": status or "",
                                 "duration_ms": duration_ms or 0},
                                ensure_ascii=False)))
                con.commit()
        finally:
            con.close()
    except Exception:
        pass  # 失败静默: 遥测不该打断工具调用


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── 自测: python3 tools/telemetry_sink.py ──
if __name__ == "__main__":
    print("match mcp:", _match_entry("mcp__portalk-memory__memory_search", {}))
    print("match terminal:", _match_entry(
        "terminal", {"command": "python3 tools/darkzone_report.py"}))
    print("match unknown:", _match_entry("read_file", {}))
