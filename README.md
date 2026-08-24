# Agent-Grimoire山海

A skill library shared across agent runtimes — one collection, per-agent ledgers. Sessions open with a map, not a manifest: tag tree + hard-prereqs, descriptions pulled by tag, bodies read only when chosen. Foreign skills pass an import scan; a weekly curator grooms the tree.

山海 · 跨 runtime 共享的 agent 技能书——一座馆藏，各记各账。每个 session 只注入一张经图（tag 树＋硬前置表），描述按 tag 现拉，正文选中才翻；外来技能先过入关三面扫，每周巡山使策展；draft→verified→retired，全程账本留痕。

**Inject the map, not the manifest.** 不给 agent 塞整本书，只给一张图。有兽焉，用时召来。

## 核心机制

- **经图（map）不清单（manifest）**：session 开场只注入 tag 树＋硬前置表＋tag↔skill 映射＋查询语法，描述行检索期才拉，正文选中才翻。成本压在 tag 数上，不压在 skill 数上。
- **四层名册**：core（全文常驻）/ pinned（描述行常驻，正文按需——给"每 session 必知但正文太重"的书）/ index（地图可见按需取）/ archive（退役留痕）。
- **状态机**：draft → verified → retired。退休标准是检索信噪比（挡了多少路），不是"多久没用"。retired 不删：正文进归档版本链，tag 树除名，账本留 retire 事件。
- **写入扫描**：所有写入统一过（自写/外来不分家）——脚本面静态分析、文面模式扫、重复面查重。只记不拦，带红标报告的 draft 到不了 verified，除非策展人过目签名（ack_red）。
- **one-mutation budget**：服务端强制"单次巡视至多一个落盘动作"——同一 operator 滚动窗口（默认 24h）内治理事件达到上限（默认 2）后，后续落盘动作返回 429，响应带窗口重置时间和契约提示。库主豁免；遥测不占预算。
- **组合 tag**：`类型·场景·关键词`（上限 5 段）。折槽不折串（折叠只在槽内），槽位定序（canonical 排序），提交侧查重三档（精确命中自动折、组合槽规范化、近义只提示不折叠——选择权留提交者）。

## trigger 怎么写（好坏对照）

trigger 回答"什么时候该想起我"，写事前一刻，不写功能清单。好坏对照比规则管用：

**坏 → 好的四个真实例子**（改写均来自真实巡山 rewrite 记录）：

| 坏 trigger | 为什么坏 | 好 trigger |
|---|---|---|
| `journal管理指南` | 功能名。只有已知道它存在的人才会被命中——那就不需要它了 | `想往journal追加条目时` |
| `AI图片生成工具` | 抽象概念词，跟当下处境共振不了 | `用户要求画图/生成插图/做封面时` |
| `git操作的完整参考` | 写成参考手册的口吻，念不完。一行能读完，念不完=没提炼完 | `push被拒(403/409)时先查credential helper链` |
| `处理用户反馈` | 场景太宽，什么都命中=什么都不命中 | `客诉升级到社媒公开帖前，需要站队判断时` |

**五规**（展开）：

1. **写场景不写功能**——"遇到X时"不是"X管理工具"
2. **用具体名词**——错误码/工具名/文件路径才能跟当下处境共振
3. **写出事前一刻**——trigger 回答"什么时候该想起我"
4. **自包含**——读的未来模型只有当下处境，不许有"上面说的这个问题"
5. **一行能读完**——念不完 = 没提炼完

## 快速开始

```bash
# 服务（SQLite + 本地 HTTP，零依赖）
python3 grimoire.py 8730 &

# session 开场拉经图（这就是全部注入量）
curl -s http://127.0.0.1:8730/map

# 按tag拉描述清单（含首行trigger）
curl -s http://127.0.0.1:8730/tag/维修

# 选中才翻正文（响应尾行搭车usage提醒）
curl -s http://127.0.0.1:8730/skill/归还术-扫描修复流程

# 提交（三面扫描自动跑，draft起步）
curl -X POST http://127.0.0.1:8730/skill -d '{
  "name": "my-skill", "tags": ["维修"], "body": "..."}'

# 遥测（expand=翻了这本书）
curl -X POST http://127.0.0.1:8730/event -d '{
  "kind": "skill.telemetry.expand", "operator": "my-agent",
  "skill_id": "my-skill"}'
```

## 读面 / 写面

| 端点 | 作用 |
|---|---|
| GET /map | 经图：tag 树＋前置表＋映射＋语法（pinned 带 trigger 描述行） |
| GET /tag/<tag> | 山下条目清单（别名查询侧折叠） |
| GET /skill/<id-or-name> | 正文＋元数据（带 baseline_hash，rewrite 的钥匙） |
| GET /darkzone | 暗区点名：从未被 push/expand 的书（archive 层已排除） |
| GET /stats | 四数字：暗区数/经图字节/弱 trigger/事件总数（?since=UTC 增量） |
| POST /skill | 提交（tag 查重+扫描+记账原子） |
| POST /event | 遥测（只记不动）+ 治理（动作+落账一体，受 one-mutation budget 约束） |

治理事件（governance）：`skill.pool.review`（转正/废弃，红标需 ack_red）/ `skill.roster.update`（layer 变更）/ `skill.description.rewrite`（绑 baseline_hash，stale 拒绝）/ `skill.tag.merge`（不可逆，别名先迁移）/ `skill.tag.alias.add` / `skill.prereq.update`。

## 设计立场（为什么这样做）

- **不用 embedding 检索 skill 文本**：tag 树+描述行总量读得完，模型读着挑又准又便宜。命中粒度是整套技能不是知识点；知识点检索归内容层（journal/archive），两层分工。
- **扫描坐藏书阁门口不坐藏书人门口**：威胁看内容不看出处——author 可伪造，按出处豁免=留后门。
- **默认只记不动，不默认 auto**：单体 runtime 的肌肉记忆 auto 合理；共享馆藏的公共变更默认保守，改需要理由。哪天被 fork 成单 runtime 私用，auto 就 defensible（OpenClaw Skill Workshop 对照，骨相差异详见 docs/proposal-tools-partition.md）。
- **组合 tag 的折槽数学**：整条组合永不折成另一条，折叠只在槽内（每槽查正字/别名归并）——自由组合造出的暗区被槽位定序+查重三档挡在门外。

## 测试

```bash
python3 tests/smoke.py        # 主套件 32 查（读面/写面/扫描/查重/merge）
python3 tests/budget_smoke.py # one-mutation budget 专项 11 查
python3 tests/probe_zhao.py   # 探针回归 16 查
```

所有烟测自带隔离（临时库+临时端口+atexit 拆除），不碰 live。

## 相关文档

- docs/proposal-tools-partition.md — 工具层平行分区提案（v0.3 候选）
- tools/patrol-protocol.md — 巡山使巡逻协议
- 管理契约：portalk-contract-skill-lifecycle v0.2（外部文档）

---

作者：洄（hui-morgana）
