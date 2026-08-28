#!/usr/bin/env python3
"""vault 同步: skill目录下的附件(references/scripts/templates/...)→山海附件库。

设计依据: 契约v0.2主权边界"sync是被动镜像"; 用户2026-08-27令"附件要存的"。
- BLOB不进库: 文件落 vault/<skill_id>/<relpath>, SQLite只存索引(sha256幂等)
- 跳过: .curator_backups(技能库自身备份)、.git、__pycache__、*.pyc(可再生)
- binary判定: NUL字节嗅探(ttf/png/pack等), GET时base64返回
- 幂等: sha256不变→跳过; 变了→更新索引+覆盖文件(被动镜像语义)
用法: python3 tools/sync_vault.py [--dry-run]
"""
import hashlib
import os
import re
import sqlite3
import sys

BASE = "http://127.0.0.1:8730"
SKILLS_DIR = os.path.expanduser("~/.hermes/skills")
VAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "vault")
DB = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "grimoire.db")

SKIP_DIRS = {".curator_backups", ".git", "__pycache__", "node_modules"}
SKIP_EXT = {".pyc"}
# 附件大小上限: 单文件>8MB跳过并报告(curator快照级的重物不进馆)
MAX_SIZE = 8 * 1024 * 1024


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(2048)
    except Exception:
        return True


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")

    # skill名→id 映射(馆内)
    name2id = {r["name"]: r["skill_id"]
               for r in con.execute("SELECT skill_id, name FROM skills")}

    scanned = stored = skipped = oversized = 0
    report = []
    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        skill_dir = None
        # 找SKILL.md确定归属: 向上找最近含SKILL.md的目录
        cur = root
        while cur != os.path.dirname(SKILLS_DIR):
            if os.path.isfile(os.path.join(cur, "SKILL.md")):
                skill_dir = cur
                break
            cur = os.path.dirname(cur)
        if not skill_dir:
            continue
        meta_name = None
        for line in open(os.path.join(skill_dir, "SKILL.md"),
                         encoding="utf-8", errors="replace").read().splitlines()[:30]:
            m = re.match(r"^name:\s*(.+)$", line.strip())
            if m:
                meta_name = m.group(1).strip().strip('"').strip("'")
                break
        sname = meta_name or os.path.basename(skill_dir)
        sid = name2id.get(sname)
        if not sid:
            report.append(f"SKIP(不在馆): {sname}")
            continue
        for fname in files:
            if fname == "SKILL.md":
                continue
            if os.path.splitext(fname)[1].lower() in SKIP_EXT:
                continue
            fpath = os.path.join(root, fname)
            # R1(外聘审计): 拒绝文件symlink — walk的followlinks=False只挡目录
            # 链接挡不住文件链接; 跟随即可能把~/.ssh/id_rsa等借道发布进vault
            if os.path.islink(fpath):
                report.append(f"SKIP(symlink拒绝入馆): {fname} @ {root}")
                continue
            rel = os.path.relpath(fpath, skill_dir)
            scanned += 1
            size = os.path.getsize(fpath)
            if size > MAX_SIZE:
                oversized += 1
                report.append(
                    f"OVERSIZE: {sname}/{rel} ({size/1024/1024:.1f}MB)")
                continue
            sha = file_sha(fpath)
            existing = con.execute(
                "SELECT sha256 FROM vault_index "
                "WHERE skill_id=? AND relpath=?", (sid, rel)).fetchone()
            if existing and existing["sha256"] == sha:
                skipped += 1
                continue
            binflag = 1 if is_binary(fpath) else 0
            dest = os.path.join(VAULT_DIR, sid, rel)
            if not dry:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # 被动镜像: 直接覆盖(sha已变)
                with open(fpath, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                import time
                fid = hashlib.sha256(
                    f"{sid}:{rel}".encode()).hexdigest()[:16]
                con.execute(
                    "INSERT INTO vault_index(file_id, skill_id, relpath, "
                    "size, sha256, binary, mtime, synced_at) "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(skill_id, relpath) DO UPDATE SET "
                    "size=excluded.size, sha256=excluded.sha256, "
                    "binary=excluded.binary, mtime=excluded.mtime, "
                    "synced_at=excluded.synced_at",
                    (fid, sid, rel, size, sha, binflag,
                     time.strftime("%Y-%m-%dT%H:%M:%S"),
                     time.strftime("%Y-%m-%dT%H:%M:%S")))
                stored += 1
            else:
                report.append(f"WOULD STORE: {sname}/{rel} ({size}B)")

    if not dry:
        con.commit()
    con.close()
    print(f"scanned={scanned} stored={stored} "
          f"unchanged={skipped} oversized={oversized}"
          + (" [DRY]" if dry else ""))
    for line in report:
        print(" ", line)


if __name__ == "__main__":
    main()
