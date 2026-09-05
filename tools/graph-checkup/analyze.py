#!/usr/bin/env python3
"""skill library graph checkup
four checks: louvain clusters vs manual tags | god nodes | bridge nodes | cross-community surprising edges
graph: shared-tag weak edges (0.5/tag, structural) + mention strong edges (min(w,5), semantic)
"""
import json, os, itertools, collections
import networkx as nx
from community import community_louvain

HERE = os.path.dirname(__file__)
tagmap = {n["id"]: n["tags"] for n in json.load(open(f"{HERE}/mention-edges.json"))["nodes"]}
raw = json.load(open(f"{HERE}/mention-edges.json"))

G = nx.Graph()
for sid, tags in tagmap.items():
    G.add_node(sid, tags=tags)

for (a, b) in itertools.combinations(tagmap, 2):
    shared = set(tagmap[a]) & set(tagmap[b])
    if shared:
        G.add_edge(a, b, weight=0.5 * len(shared), kind="cotag", shared=sorted(shared))

mention_w = {}
for e in raw["edges"]:
    key = tuple(sorted((e["source"], e["target"])))
    mention_w[key] = mention_w.get(key, 0) + min(e["weight"], 5)
for (a, b), w in mention_w.items():
    if G.has_edge(a, b):
        G[a][b]["weight"] += w
        G[a][b]["kind"] = "both"
    else:
        G.add_edge(a, b, weight=w, kind="mention")

print(f"graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, connected={nx.is_connected(G)}")

# ── 1. louvain ───────────────────────────────────────────
part = community_louvain.best_partition(G, weight="weight", resolution=1.0, random_state=42)
nx.set_node_attributes(G, part, "community")
comms = collections.defaultdict(list)
for n, c in part.items():
    comms[c].append(n)
comms = dict(sorted(comms.items(), key=lambda kv: -len(kv[1])))

def tag_of(n):
    t = [x for x in tagmap[n] if not x.startswith(".archive")]
    return t[0] if t else (tagmap[n][0] if tagmap[n] else "?")

# 每个社区的tag构成（找混团=手工分类漏的天然团）
analysis = {}
for cid, members in comms.items():
    tc = collections.Counter(tag_of(m) for m in members)
    dominant, dom_n = tc.most_common(1)[0]
    purity = dom_n / len(members)
    # 跨tag成员（混进来的外来者）
    outsiders = [(m, tag_of(m)) for m in members if tag_of(m) != dominant]
    analysis[cid] = {"size": len(members), "dominant": dominant, "purity": round(purity, 2),
                     "tags": dict(tc), "outsiders": outsiders}

# 手工tag被louvain拆分的情况
tag_members = collections.defaultdict(list)
for n in G:
    for t in tagmap[n]:
        if not t.startswith(".archive"):
            tag_members[t].append(n)
splits = []
for t, ms in sorted(tag_members.items(), key=lambda kv: -len(kv[1])):
    cs = collections.Counter(part[m] for m in ms)
    if len(cs) > 1 and len(ms) >= 6:
        splits.append((t, len(ms), dict(cs)))

# ── 2. god nodes（提及图上的枢纽：被多少书引用）───────────
M = nx.Graph()
M.add_nodes_from(G)
for e in raw["edges"]:
    key = tuple(sorted((e["source"], e["target"])))
    if key in mention_w:
        M.add_edge(key[0], key[1], weight=mention_w[key])
in_deg = collections.Counter()
for e in raw["edges"]:
    in_deg[e["target"]] += min(e["weight"], 5)
god = [(n, round(w, 1), round(nx.degree(M, n) / 2 + 0.0001, 0), len(tagmap[n])) for n, w in in_deg.most_common(15)]

# ── 3. 桥节点（组合图介数中心性, 只看横跨≥2社区的）────────
bt = nx.betweenness_centrality(G, weight="weight", normalized=True)
bridges = []
for n, b in bt.items():
    if b <= 0:
        continue
    nb = set(G.neighbors(n))
    nbr_comms = {part[x] for x in nb} | {part[n]}
    if len(nbr_comms) >= 3:
        bridges.append((n, round(b, 4), sorted(nb - {n})[:6]))
bridges.sort(key=lambda x: -x[1])

# ── 4. surprising边（跨社区的提及边, 无共享tag优先）────────
surprises = []
for (a, b), w in mention_w.items():
    ca, cb = part[a], part[b]
    if ca == cb:
        continue
    shared = set(tagmap[a]) & set(tagmap[b])
    ctx = ""
    for e in raw["edges"]:
        k = tuple(sorted((e["source"], e["target"])))
        if k == (a, b) and e.get("context"):
            ctx = e["context"][0][:80]
            break
    surprises.append({"edge": (a, b), "w": w, "shared_tags": sorted(shared), "ctx": ctx})
surprises.sort(key=lambda x: (-len(x["shared_tags"]) * 0 + x["w"]))  # 权重序, shared_tag少=更意外
surprise_score = lambda s: s["w"] * (1 if not s["shared_tags"] else 0.5)
surprises.sort(key=lambda s: -surprise_score(s))

# ── 输出 ─────────────────────────────────────────────────
out_nodes = []
for n in G:
    out_nodes.append({"id": n, "tags": tagmap[n], "community": part[n],
                      "mention_inflow": in_deg.get(n, 0),
                      "betweenness": round(bt[n], 5)})
out_edges = []
for a, b, d in G.edges(data=True):
    out_edges.append({"source": a, "target": b, "weight": round(d["weight"], 2), "kind": d["kind"]})
json.dump({"nodes": out_nodes, "edges": out_edges,
           "meta": {"method": "louvain(r=1.0, seed=42); cotag=0.5/tag + mention capped 5",
                    "n": G.number_of_nodes(), "m": G.number_of_edges(),
                    "n_communities": len(comms)}},
          open(f"{HERE}/graph.json", "w"), ensure_ascii=False, indent=1)

print(f"\ncommunities: {len(comms)}")
for cid, a in list(analysis.items())[:14]:
    print(f"  C{cid} n={a['size']:2d} dom={a['dominant']} purity={a['purity']} outsiders={[(m,t) for m,t in a['outsiders']][:5]}")
print(f"\ntag splits: {[(t, n, c) for t, n, c in splits]}")
print(f"\ngod nodes (inflow, deg): {[(g[0], g[1]) for g in god[:10]]}")
print(f"\nbridges: {[(b[0], b[1]) for b in bridges[:10]]}")
print(f"\nsurprises top: ")
for s in surprises[:12]:
    print(f"  {s['edge']} w={s['w']} shared={s['shared_tags']} | {s['ctx'][:60]}")
