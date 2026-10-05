# 山海路由层 control case 测试集

反例 query 对，钉能力地图指纹的松紧。正例测「该想起的能不能想起」，反例测「不该想起的会不会被误想起」。

方法论来自 KiroCrew eval harness（zhaozhao侧调研）：每个 skill 配「不该触发」的反例，**改指纹/改地图前后各跑一遍测试集，反例失守=触发器松了，收紧再改**。

- 任务来源：设计师 10/5 拍板，zhaozhao拟任务书（spoor 档案 `shanhai-routing-control-cases`），hui施工，zhaozhao验收。
- 前作：`~/projects/shanhai-route-bench/`（2026-09-05 路由命中率测试床，15题导航性上界）——那轮测的是「找得到吗」，本轮测的是「会不会乱抓」。同一被试协议，判分加严。

## 目录

| 文件 | 干什么 |
|---|---|
| `cases.json` | 三元组测试集：(query, 应路由到的 skill, 不应路由到的 skill)，14 题覆盖 3 簇 + 2 阴性对照 |
| `run.py` | runner：fingerprint（零API）/ baseline（真调LLM）/ diff（两轮比对） |
| `baseline_fingerprints.json` | 当前盖章基线：监控集每本书的 trigger+tags 指纹 |
| `results/` | 每轮 baseline 的完整落盘（含当轮冻结指纹，结果永远可追到「跑在什么地图上」） |

## 簇的圈定（宁少而真）

**F·forensics 三胞胎**（devops 山）：activity / delivery / memory-retrieval。正文全是「查账/取证/归因」家族词汇，判别轴只剩取证对象（活动时间线 / 推送投递链 / 记忆库）。地形陷阱：memory-retrieval 的 trigger 含「快照DB」，与活动取证共享 DB 词汇。

**D·拆解六胞胎**（creative+data 山共享「小红书/拆/调研」词汇场，全库最大簇）：varia-content / xhs-content-production / content-platform-research / platform-account-research / category-competitive-research / community-user-analysis。判别轴：拆的对象（品牌大盘 / 创作者账号 / 平台机制 / 自家社群数据）× 产出（内部文档 / 自家社媒内容 / 策略）。已知重叠：content-platform-research 与 platform-account-research 的 trigger 在「拆大号」字面重叠（已报巡山使待收口），D3 按 can-reject 口径判 SOFT 不算 hard-fail。

**B·调试五胞胎**：systematic-debugging / vibe-diagnose / dev-disciplines / 归还术。共享 bug/修/调试词汇，判别轴是时间轴（修之前诊断 / 修完之后存法 / 全周期纪律）。vibe-diagnose 现挂 .archive 但仍 verified——暗区正文可翻到，是「退役书当活书」的探针。

另配 **N·阴性对照** 2 题（正解 NONE）：全库都不该接的活，专抓「把文字活当内容生产」「把一次性提醒当 cron 设计」的过松触发。

## 跑法

```bash
cd ~/Agent-Grimoire

# ① 指纹核对（零API零LLM，人人可跑）——改任何 trigger/tag/地图前后必跑
python3 tools/control_cases/run.py fingerprint
# 与基线一致 → exit 0；漂移 → exit 2 并列出每处漂移

# ② 基线真跑（LLM 被试，默认 huanapi/claude-opus-4-6）
python3 tools/control_cases/run.py baseline
# 被试=干净上下文，只看经图+一条query，答 ROUTE: 书名|置信|理由
# 跑在隔离实例上（临时DB+18745口+种live当前指纹），不碰 live

# ③ 两轮比对（改指纹前后各跑一次 ②，然后）
python3 tools/control_cases/run.py diff results/A.json results/B.json

# 指纹有意变更且新 baseline 质量过了 → 重新盖章
python3 tools/control_cases/run.py fingerprint --rebase
```

**「不改不测」流程**：任何动指纹/动地图/动 tag 的 commit，前后各跑一次 fingerprint（必跑，零成本）+ baseline（应跑），commit message 带两轮结果文件名。指纹无故漂移=要查；有意漂移=先看 baseline 质量再盖章。

## 判分

| 判 | 含义 |
|---|---|
| PASS | 命中 should_route |
| SOFT | 命中 can-reject 邻居（D3 对 content-platform-research 的现管口径） |
| FAIL-LOOSE | 落进 should_not_route（**触发器松了——本测试集要抓的病**） |
| FAIL-TIGHT | 该想起的没想起（NONE/格式崩） |
| FAIL-WILD | 路由到第三者（含幻觉书名、山级路由如 `/tag/data`） |

被试偶发把书名包进动作句（`GET /skill/xhs-content-production`），runner 的 `normalize_route` 会剥壳——图上书名逐字出现即算路由到它；原始串留档 `route_raw`。

## 首轮基线（2026-10-05，claude-opus-4-6 ×2 轮）

10/14 PASS（两轮一致），4 处失败**就是基线的校准产出**——测试集第一天不需要全绿，需要的是把松紧现状钉在账上：

| 题 | 判 | 发现（两轮复现性） |
|---|---|---|
| D1 | ✗松×2 | **稳定失守**：「写小红书帖子讲coffee-brand磨豆机」两轮都路由到 xhs-content-production。它的 trigger「小红书AI/Agent领域内容生产」里，「小红书」词汇场压过了「AI/Agent」领域限定词——限定词挡不住跨域对象。收紧方向：trigger 首句立领域边界，或 boundary 字段写明「咖啡/品牌内容→varia-content」。 |
| B2 | ✗×2（松/野各一） | **稳定失守**：「修完502把排查思路存下来」两轮都没找到归还术（一轮去 systematic-debugging，一轮去错山）。归还术的 trigger「刚修完一个bug、想把这个修法存下来下次直接用时」是时间锚写法，被试在「存」意图下仍被调试词汇拉走——core 层全文常驻救不了路由判断（被试只看描述行）。 |
| F2 | ✗野×1，过×1 | 边界态：「早报cron跑成功但没收到推送」一半概率被 exploration-cron 抢走。「cron」字面对 exploration-cron 是强吸引子，delivery-forensics 的「没收到」锚不够硬。 |
| N1 | 过×1，✗松×1 | **阴性对照首杀**：「翻译成英文自然吗」有一轮被 humanizer 抓走。humanizer 管「去AI腔」不管翻译润色——它的 trigger「Humanize text」对外延没有收口。这正是阴性对照存在的意义。 |
| D5/D3 | ✗野×1，过×1 | 被试偶发答山名（`/tag/data`）不答书名——被试风格 artifact，normalize 已兜住书名包动作句的形态，纯山名路由按 WILD 记账如实留档。 |

## 设计口径（对着验收看）

1. **fingerprint 直读 live DB（mode=ro）**：基线的语义就是「live 指纹变了要报警」，隔离种副本会掐掉信号源。ro 连接零服务交互零账本污染。
2. **baseline 的被试跑在隔离实例**：种的是 live 当前指纹（`fixtures_live_pull` 冻结进结果文件）——测的就是现管地图，不是副本快照；同时真跑不碰 live 事件账本。
3. **被试统一按 index 层种**：core/pinned 的注入形态差异是另一层变量（归还术在 live 是 core 层全文常驻），本轮钉 trigger 指纹，控制变量法把层归一。层注入的路由效应留给后续（B2 的失守提示 core 层未必帮路由，值得单独验证）。
4. **单轮≠规律**：F2/N1 这类 50/50 失守记「边界态」不记「稳定失守」，收紧决策要复跑确认。
5. **成本**：一轮 14 题约 30k-60k tokens（经图随书数变化），日常回归用 fingerprint（零成本），baseline 用在改指纹的 commit 前后。

## 与经图路由的前瞻对接

设计师 10/5 定案：pianist 壳建好后 skill 注入层走山海经图（不走 skill 描述索引注入）。这套测试集就是未来经图路由的尺子——届时被试协议不变，把「隔离实例」换成 pianist 的真实注入路径即可复跑全量。
