#!/usr/bin/env python3
"""从skill正文提取 skill↔skill 提及边。v2：防英文常用词误报。
规则：
- 连字符id（如 audio-analysis）：词边界全名匹配即有效（足够独特）
- 单词id（如 reading/sketch/notion/pdf）：只认三种精确语境
    1) 反引号/加粗/链接文本内的精确引用
    2) "<id> skill" / "skill <id>" 短语
    3) "skill/<id>" URL
- 排除自引用；权重=有效提及次数
"""
import json, os, re, collections

HERE = os.path.dirname(__file__)
TXT = os.path.join(HERE, "skills-txt")
tagmap = json.load(open(os.path.join(HERE, "tag-map.json")))
ids = sorted(tagmap)

docs = {}
for sid in ids:
    p = os.path.join(TXT, f"{sid}.md")
    docs[sid] = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""

hyphen_ids = [s for s in ids if "-" in s]
word_ids = [s for s in ids if "-" not in s]

edges = collections.Counter()
detail = collections.defaultdict(list)

for src, doc in docs.items():
    marked = set(re.findall(r"`([^`\n]{2,50})`", doc))
    marked |= set(re.findall(r"\*\*([^*\n]{2,50})\*\*", doc))
    marked |= set(re.findall(r"\[([^\]\n]{2,50})\]", doc))
    for dst in hyphen_ids:
        if dst == src:
            continue
        for m in re.finditer(rf"(?<![\w-]){re.escape(dst)}(?![\w-])", doc):
            before = doc[max(0, m.start() - 2):m.start()]
            after = doc[m.end():m.end() + 2]
            if before.endswith("/") or after.startswith("/"):
                edges[(src, dst)] += 0.3  # 路径组件提及: 机械依赖
            else:
                edges[(src, dst)] += 1  # 文字提及: 语义引用
    for dst in word_ids:
        if dst == src:
            continue
        # 语境1: 标记文本精确等于 id
        if dst in marked:
            edges[(src, dst)] += 1
        # 语境2: "<id> skill" / "skill <id>"（不区分大小写的skill字样）
        cnt2 = len(re.findall(rf"(?<![\w-]){re.escape(dst)}(?![\w-])[ ]+skill\b", doc, re.I)) \
             + len(re.findall(rf"\bskill[ ]+{re.escape(dst)}(?![\w-])", doc, re.I))
        if cnt2:
            edges[(src, dst)] += cnt2
        # 语境3: skill/<id>
        cnt3 = len(re.findall(rf"skill/{re.escape(dst)}(?![\w-])", doc))
        if cnt3:
            edges[(src, dst)] += cnt3

for (s, d), w in edges.items():
    pat = rf"(?<![\w-]){re.escape(d)}(?![\w-])"
    m = re.search(pat, docs[s])
    if m:
        ctx = docs[s][max(0, m.start() - 40):m.end() + 40].replace("\n", " ")
        detail[(s, d)].append(ctx)

out = {
    "nodes": [{"id": s, "tags": tagmap[s]} for s in ids],
    "edges": [{"source": s, "target": d, "weight": w, "context": detail[(s, d)][:2]}
              for (s, d), w in sorted(edges.items())],
}
json.dump(out, open(os.path.join(HERE, "mention-edges.json"), "w"), ensure_ascii=False, indent=1)
print(f"nodes={len(ids)} directed_edges={len(edges)} total_weight={sum(edges.values())}")
