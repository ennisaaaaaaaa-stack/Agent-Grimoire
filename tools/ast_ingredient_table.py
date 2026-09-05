#!/usr/bin/env python3
"""ast_ingredient_table.py — AST 成分表原型（graphify 收割计划 · 任务A）

输入一个代码目录，输出每个 .py 文件的「成分表」JSON：
  1. imports      — import 了什么（stdlib / third_party / local 分开，带行号）
  2. env_vars     — 引用的环境变量名（os.environ['X'] / os.getenv('X') / environ.get(...)）
                    ★ 安全铁律（学 mcp_ingest.py）：只取名字，永不取值。
  3. network      — URL 字面量 + requests/httpx/aiohttp/urllib 调用目标
  4. paths        — 触碰的文件路径（open() / Path() / 写死的路径字符串）

引擎：tree-sitter（零 API 零 token）。
certainty 三档（学 graphify extraction-spec 纪律）：
  EXTRACTED  — 语法上确凿（import 语句 / open() 的静态参数 / URL 字面量）
  INFERRED   — 启发式推断（像路径的字符串 / importlib 动态导入）
  AMBIGUOUS  — 存在触碰但目标是动态的（变量拼的 env 名 / url 参数）

组织方式：学 graphify/extract.py 的 dispatch table —— 每类节点一个 handler，
walk 一次树，handler 各领各的。每文件带 sha256（为将来双层缓存留的指纹）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import tree_sitter
import tree_sitter_python as tsp

TOOL_NAME = "ast_ingredient_table"
TOOL_VERSION = "0.1.0"

PY_LANGUAGE = tree_sitter.Language(tsp.language())

# ---------------------------------------------------------------- 常量表 --

# 网络客户端模块 → 视为网络调用的方法名（或函数名）
NETWORK_MODULES = {
    "requests": {"get", "post", "put", "patch", "delete", "head", "options", "request"},
    "httpx": {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"},
    "aiohttp": {"get", "post", "put", "patch", "delete", "head", "options", "request"},
    "urllib.request": {"urlopen", "Request"},
    "urllib": {"urlopen"},  # from urllib import urlopen 少见但存在
}
# environ 方法 → 访问性质
ENV_METHOD_ACCESS = {
    "get": "read", "getenv": "read", "pop": "read",
    "setdefault": "read+write", "putenv": "write",
}

URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://[^\s'\"<>`\\]+")
# 路径味启发式：绝对/家目录/相对锚点开头，或 Windows 盘符，或「带扩展名的相对路径」
PATH_ANCHOR_RE = re.compile(r"^(?:/|\./|\.\./|~/|[A-Za-z]:[\\/])")
PATH_EXT_RE = re.compile(r"^[^:\s]+\.[A-Za-z0-9]{1,8}$")

# ---------------------------------------------------------------- 工具函数 --

def node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def child_by_field(node, name: str):
    return node.child_by_field_name(name)


def all_strings(node):
    """递归产出 (string_node, parent_statement_is_docstring) 。"""
    stack = [(node, False)]
    while stack:
        cur, in_expr_stmt = stack.pop()
        for ch in cur.children:
            if ch.type == "string":
                yield ch, in_expr_stmt or cur.type == "expression_statement"
            elif ch.type == "interpolation":
                continue  # f-string 插值内部单独处理
            else:
                stack.append((ch, in_expr_stmt or cur.type == "expression_statement"))


def string_parts(string_node, src: bytes):
    """把 string 节点拆成静态片段。返回 (parts, is_fstring)。"""
    parts, is_f = [], False
    stack = [string_node]
    while stack:
        cur = stack.pop()
        if cur.type == "interpolation":
            is_f = True
            continue
        if cur.type == "string_content":
            parts.append(node_text(cur, src))
        for ch in cur.children:
            stack.append(ch)
    return parts, is_f


def static_string_value(string_node, src: bytes):
    """静态字符串的内容；f-string / 拼接变量 → None（动态）。"""
    node = string_node
    while node is not None and node.type in ("string", "concatenated_string"):
        if node.type == "string":
            parts, is_f = string_parts(node, src)
            return None if is_f else "".join(parts)
        node = None  # concatenated_string 混 interpolation 时保守处理
    return None


# ---------------------------------------------------------------- 主分析器 --

class IngredientTable:
    """单文件成分表。dispatch table：节点类型 → handler。"""

    def __init__(self, root: Path, local_names: set[str], stdlib_names: set[str]):
        self.root = root
        self.local_names = local_names
        self.stdlib_names = stdlib_names
        # alias → 完整点分名（含 from X import y [as z] 的裸名）
        self.aliases: dict[str, str] = {}
        self.result = {
            "imports": {"stdlib": [], "third_party": [], "local": []},
            "env_vars": [],
            "network": {"url_literals": [], "call_targets": []},
            "paths": [],
            "parse_errors": [],
        }

    # ---------- 阶段0：import 收集 + alias 表 ----------

    def _collect_import(self, node, src: bytes):
        line = node.start_point[0] + 1
        if node.type == "future_import_statement":
            # 无 module_name 字段：__future__ 直接当 stdlib 记，不进 alias 表
            names = [node_text(ch, src) for ch in node.children
                     if ch.type == "dotted_name"]
            for nm in names:
                self.result["imports"]["stdlib"].append({
                    "name": f"__future__.{nm}", "line": line,
                    "certainty": "EXTRACTED"})
            return
        if node.type == "import_statement":
            for ch in node.children:
                if ch.type == "dotted_name":
                    self._add_import(node_text(ch, src), line)
                elif ch.type == "aliased_import":
                    full = node_text(child_by_field(ch, "name"), src)
                    alias = node_text(child_by_field(ch, "alias"), src)
                    self._add_import(full, line, alias)
        else:  # import_from_statement
            mod = child_by_field(node, "module_name")
            if mod is None:
                # from . import x / from .x import y 的相对导入 → local
                names = []
                for ch in node.children:
                    if ch.type in ("dotted_name", "aliased_import"):
                        t = ch.type == "aliased_import" and node_text(
                            child_by_field(ch, "name"), src) or node_text(ch, src)
                        names.append(t)
                rel = "." * sum(1 for ch in node.children
                                if ch.type == "." and ch.parent is node) or "."
                self.result["imports"]["local"].append({
                    "name": f"{rel}{'+'.join(names) or '*'}", "line": line,
                    "certainty": "EXTRACTED"})
                return
            module = node_text(mod, src)
            for ch in node.children:
                if ch.type == "dotted_name" and ch.parent is node:
                    self._from_import(module, node_text(ch, src), None, line)
                elif ch.type == "aliased_import":
                    nm = node_text(child_by_field(ch, "name"), src)
                    al = node_text(child_by_field(ch, "alias"), src)
                    self._from_import(module, nm, al, line)
                elif ch.type == "wildcard_import":
                    self._add_import(module + ".*", line)

    def _from_import(self, module: str, name: str, alias: str | None, line: int):
        # from os import environ       → aliases[environ] = os.environ
        # from graphify.ids import x   → 顶层 graphify 判 local/third
        self.aliases[alias or name] = f"{module}.{name}"
        top = module.split(".")[0]
        kind = self._classify(top)
        full = module if name == "*" else f"{module}.{name}"
        # 记的是模块级 import（from X import y 记 X.y 太碎，记 X + 明细）
        entry = {"name": full, "line": line, "certainty": "EXTRACTED"}
        if alias:
            entry["alias"] = alias
        self.result["imports"][kind].append(entry)

    def _add_import(self, full: str, line: int, alias: str | None = None):
        kind = self._classify(full.split(".")[0])
        if alias:
            self.aliases[alias] = full
        entry = {"name": full, "line": line, "certainty": "EXTRACTED"}
        if alias:
            entry["alias"] = alias
        self.result["imports"][kind].append(entry)

    def _classify(self, top: str) -> str:
        if not top:  # from .x import y —— 相对导入永远 local
            return "local"
        if top in ("__future__",) or top in self.stdlib_names:
            return "stdlib"
        if top in self.local_names or top == self.root.name:
            return "local"
        return "third_party"

    # ---------- 表达式点分名解析（alias 还原） ----------

    def resolve_dotted(self, node, src: bytes) -> str | None:
        """把 attribute 链 / 标识符解析成 'os.environ.get' 风格的点分名。"""
        if node.type == "identifier":
            name = node_text(node, src)
            return self.aliases.get(name, name)
        if node.type == "attribute":
            base = self.resolve_dotted(child_by_field(node, "object"), src)
            if base is None:
                return None
            return f"{base}.{node_text(child_by_field(node, 'attribute'), src)}"
        return None

    # ---------- 各类 handler（dispatch table 的"表"） ----------

    def handle_subscript(self, node, src: bytes):
        """os.environ['X'] —— 下标访问。"""
        base = self.resolve_dotted(child_by_field(node, "value"), src)
        if base != "os.environ":
            return
        sub = child_by_field(node, "subscript")
        name, dynamic = None, True
        if sub is not None and sub.type == "string":
            val = static_string_value(sub, src)
            if val is not None:
                name, dynamic = val, False
        parent = node.parent
        access = "write" if (parent and parent.type == "assignment"
                             and child_by_field(parent, "left") is node) else "read"
        self.result["env_vars"].append({
            "name": name, "access": access, "line": node.start_point[0] + 1,
            "certainty": "AMBIGUOUS" if dynamic else "EXTRACTED",
            "how": "os.environ[...]"})

    def handle_call(self, node, src: bytes):
        fn = child_by_field(node, "function")
        args = child_by_field(node, "arguments")
        callee = self.resolve_dotted(fn, src) or (node_text(fn, src) if fn else None)
        line = node.start_point[0] + 1
        arg_nodes = [c for c in args.children] if args else []

        def static_arg(idx: int, kw: str = ""):
            """取第 idx 个位置参数或 kw 关键字的静态字符串值。"""
            cands = []
            pos = 0
            for a in arg_nodes:
                if a.type == "keyword_argument":
                    if kw and node_text(child_by_field(a, "name"), src) == kw:
                        cands.append(child_by_field(a, "value"))
                elif a.type in ("string", "concatenated_string", "identifier",
                                "attribute", "call", "binary_operator"):
                    if pos == idx:
                        cands.append(a)
                    pos += 1
            for c in cands:
                if c is not None and c.type in ("string", "concatenated_string"):
                    v = static_string_value(c, src)
                    if v is not None:
                        return v, True
            return (node_text(cands[0], src) if cands and cands[0] is not None else None), False

        # --- env：os.environ.get('X') / os.getenv('X') ---
        if callee in ("os.environ.get", "os.getenv", "os.getenv",
                      "os.environ.setdefault", "os.environ.pop", "os.putenv"):
            method = callee.rsplit(".", 1)[-1]
            val, static = static_arg(0)
            self.result["env_vars"].append({
                "name": val if static else None,
                "access": ENV_METHOD_ACCESS.get(method, "read"),
                "line": line,
                "certainty": "EXTRACTED" if static else "AMBIGUOUS",
                "how": callee})

        # --- 网络：requests.get(url) / urlopen(url) ---
        if callee:
            top2 = ".".join(callee.split(".")[:2])
            top1 = callee.split(".")[0]
            method = callee.rsplit(".", 1)[-1]
            for mod, methods in NETWORK_MODULES.items():
                if (mod == top2 or mod == top1) and method in methods:
                    val, static = static_arg(0, kw="url")
                    self.result["network"]["call_targets"].append({
                        "callee": node_text(fn, src), "arg": val,
                        "line": line,
                        "certainty": "EXTRACTED" if static else "AMBIGUOUS"})
                    break

        # --- 路径：open() / Path() / .open() ---
        if callee in ("open", "pathlib.Path", "Path"):
            val, static = static_arg(0)
            entry = {
                "value": val, "via": callee, "line": line,
                "certainty": "EXTRACTED" if static else "AMBIGUOUS"}
            if callee == "open":
                mode, _ = static_arg(1, kw="mode")
                if mode:
                    entry["mode"] = mode
            self.result["paths"].append(entry)
        elif fn is not None and fn.type == "attribute" and \
                node_text(child_by_field(fn, "attribute"), src) == "open":
            # Path.open(mode) —— 路径在接收者上，arg0 是 mode
            receiver = self.resolve_dotted(child_by_field(fn, "object"), src)
            mode, _ = static_arg(0, kw="mode")
            self.result["paths"].append({
                "value": receiver, "via": "Path.open", "line": line,
                "certainty": "AMBIGUOUS",  # 接收者是表达式，静态值未知
                "mode": mode})
        elif callee in ("Path.home", "pathlib.Path.home", "Path.cwd", "pathlib.Path.cwd"):
            self.result["paths"].append({
                "value": None, "via": callee + "()", "line": line,
                "certainty": "EXTRACTED"})

        # --- 动态导入：importlib.import_module('x') ---
        if callee in ("importlib.import_module", "import_module"):
            val, static = static_arg(0)
            if val:
                kind = self._classify(val.split(".")[0])
                self.result["imports"][kind].append({
                    "name": val, "line": line,
                    "certainty": "INFERRED", "how": "importlib"})

    def handle_attribute(self, node, src: bytes):
        """裸 os.environ 引用（迭代/传参等，非下标非 .get）→ 记一条动态触碰。"""
        resolved = self.resolve_dotted(node, src)
        if resolved != "os.environ":
            return
        p = node.parent
        # 已被 subscript/call handler 覆盖的上下文跳过
        if p is None:
            return
        if p.type == "subscript" and child_by_field(p, "value") is node:
            return
        if p.type == "attribute":  # 是 os.environ.get 的基座
            return
        if p.type == "call" and child_by_field(p, "function") is node:
            return  # os.environ(...) 不会出现
        self.result["env_vars"].append({
            "name": None, "access": "read", "line": node.start_point[0] + 1,
            "certainty": "AMBIGUOUS", "how": "os.environ (dynamic/iteration)"})

    def handle_string_scan(self, node, src: bytes, is_docstring: bool):
        """所有字符串字面量：扫 URL + 路径味启发。"""
        line = node.start_point[0] + 1
        parts, is_f = string_parts(node, src)
        joined = "".join(parts)
        for m in URL_RE.finditer(joined):
            self.result["network"]["url_literals"].append({
                "url": m.group(0).rstrip(".,);"), "line": line,
                "fstring": is_f, "docstring": is_docstring,
                "certainty": "EXTRACTED"})
        if "://" in joined:
            return  # URL 串不再当路径
        # 路径启发（只对独立表达式里的字符串做，降噪）
        if is_docstring:
            return
        cand = joined.strip()
        if not cand or "\n" in cand:
            return
        if PATH_ANCHOR_RE.match(cand) or ("/" in cand and PATH_EXT_RE.match(cand)) \
                or PATH_EXT_RE.match(cand) and cand.count("/") == 0 and "." in cand[:-1]:
            self.result["paths"].append({
                "value": cand, "via": "literal", "line": line,
                "certainty": "INFERRED"})

    # ---------- 主入口 ----------

    def analyze(self, src: bytes):
        parser = tree_sitter.Parser(PY_LANGUAGE)
        tree = parser.parse(src)
        has_error = tree.root_node.has_error

        # 阶段0：先扫 import 建 alias 表（两遍，学 extract.py 的先注册后走查）
        stack = [tree.root_node]
        while stack:
            cur = stack.pop()
            if cur.type in ("import_statement", "import_from_statement",
                            "future_import_statement"):
                self._collect_import(cur, src)
            for ch in cur.children:
                stack.append(ch)

        # 阶段1：dispatch walk
        stack = [tree.root_node]
        while stack:
            cur = stack.pop()
            t = cur.type
            if t == "subscript":
                self.handle_subscript(cur, src)
            elif t == "call":
                self.handle_call(cur, src)
            elif t == "attribute":
                self.handle_attribute(cur, src)
            elif t == "string":
                parent = cur.parent
                self.handle_string_scan(
                    cur, src, parent is not None and parent.type == "expression_statement")
                continue  # string 内部无需再走
            for ch in cur.children:
                stack.append(ch)

        if has_error:
            self.result["parse_errors"].append(
                "tree-sitter reported syntax error (partial tree)")
        # 排序去重
        for k in ("stdlib", "third_party", "local"):
            self.result["imports"][k].sort(key=lambda e: (e["line"], e["name"]))
        return self.result


# ---------------------------------------------------------------- 目录层 --

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
             ".tox", ".mypy_cache", ".ruff_cache", "dist", "build", "target"}


def discover_local_names(root: Path) -> set[str]:
    """顶层 .py 词干 + 含 __init__.py 的子目录 → local 包名。"""
    names = set()
    try:
        for p in root.iterdir():
            if p.is_file() and p.suffix == ".py":
                names.add(p.stem)
            elif p.is_dir() and (p / "__init__.py").exists():
                names.add(p.name)
    except OSError:
        pass
    return names


def analyze_file(path: Path, root: Path, local_names: set[str],
                 stdlib_names: set[str]) -> dict:
    raw = path.read_bytes()
    table = IngredientTable(root, local_names, stdlib_names)
    res = table.analyze(raw)
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "loc": raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1),
        **res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="AST 成分表：imports/env/网络/路径")
    ap.add_argument("root", type=Path, help="要扫描的代码目录")
    ap.add_argument("-o", "--out", type=Path, default=Path("ingredient_table.json"))
    ap.add_argument("--files", nargs="*", help="只分析这些文件（相对 root）")
    ap.add_argument("--sample", type=int, default=0,
                    help="在全部文件里随机抽 N 个（seed 固定=7，可复现）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    all_py = sorted(p for p in root.rglob("*.py")
                    if not (SKIP_DIRS & set(p.relative_to(root).parts[:-1])))
    if args.files:
        wanted = set(args.files)
        targets = [p for p in all_py if str(p.relative_to(root)) in wanted]
        missing = wanted - {str(p.relative_to(root)) for p in targets}
        if missing:
            print(f"not found: {missing}", file=sys.stderr)
            return 2
    elif args.sample:
        rng = random.Random(args.seed)
        targets = sorted(rng.sample(all_py, min(args.sample, len(all_py))))
    else:
        targets = all_py

    local_names = discover_local_names(root)
    stdlib_names = set(sys.stdlib_module_names)

    files = [analyze_file(p, root, local_names, stdlib_names) for p in targets]

    summary = {
        "files_analyzed": len(files),
        "total_imports_third_party": sum(
            len(f["imports"]["third_party"]) for f in files),
        "total_env_vars": sum(len(f["env_vars"]) for f in files),
        "total_url_literals": sum(
            len(f["network"]["url_literals"]) for f in files),
        "total_network_calls": sum(
            len(f["network"]["call_targets"]) for f in files),
        "total_path_touches": sum(len(f["paths"]) for f in files),
    }
    out = {
        "tool": TOOL_NAME, "version": TOOL_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": f"tree-sitter {tree_sitter.__version__} / "
                  f"tree-sitter-python {getattr(tsp, '__version__', '?')}",
        "root": str(root),
        "stdlib_source": f"sys.stdlib_module_names (py {sys.version.split()[0]})",
        "summary": summary,
        "files": files,
    }
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
