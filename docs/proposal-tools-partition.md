# 工具层平行分区提案（v0.3 审阅项 #4）

状态：设计稿，待协作者审阅。审阅重点不在"要不要做"，在"边界划得对不对、分期对不对"。

## 定位

山海目前只收 skill（程序性知识：怎么把事做好）。本提案增加平行分区：工具层（能力注册表）。
两层同库不同表，共用经图 tag 树，map 上以类型槽区分（例：`工具·视觉` / `知识·咖啡`）。

## 分界判据：知识类型，不是协议

CLI / API / MCP 统一入工具层。判据是条目回答什么问题：

- 工具层条目 = 能力本身。字段：调用方式（MCP schema / CLI 命令 / API endpoint）、
  一句话描述、遥测（被谁调用、次数、最近一次）。
  回答："我能不能做 X，怎么调。"
- skill 层条目 = 程序性知识：何时用、怎么串、坑在哪。
  回答："怎么把 X 做好。"

同一能力可在两层各有一条（例：himalaya 二进制 → 工具层；himalaya 工作流 → skill 层），
tag 树负责互链。不视为冗余：是两个分辨率。

## 与 hermes 现状的衔接

hermes 已把 84 个 MCP 工具以一行描述挂进 deferred tool catalog（tool_search 按需加载）——
即"描述层"已存在。山海工具层补的是它缺的：tag/能力树分组 + 用量遥测。
不重建已有机制，只加地图层。

## 分期

- 第一期只收 MCP。事实源干净：config.yaml 列出全部 server，tool schema 自描述。
  同步脚本读配置 upsert 入库，巡山使 cron 顺路跑。
- 第二期收 CLI / API。没有统一事实源（CLI 知识散在 skill 正文），需先定 cross-link
  设计：工具层条目 ↔ 引用它的 skill 条目 互链字段。

## 主权边界

配置主权留在 config.yaml 和各工具自身。山海只做镜像读脸——与 stats 收编同一判据：
读脸吸收进服务，写入不走山海。enable/disable 一个 server 仍改 config，不改库。

## 遥测价值（做这件事的最大收益）

工具暗区检测：注册了但从未被调用的工具，从 session 日志挖，与 skill 暗区同一巡逻逻辑。
MCP server 常年全暗 → disable 候选，工具上限（当前 104）腾出来给挣饭吃的。

## schema 草案

```
tools 表（平行于 skills 表，不混 schema）：
  tool_id      TEXT PRIMARY KEY
  name         TEXT NOT NULL UNIQUE
  kind         TEXT NOT NULL CHECK(kind IN ('mcp','cli','api'))
  entry        TEXT          -- server 名 / 可执行名 / endpoint
  description  TEXT          -- 一行
  schema_json  TEXT          -- MCP 原生 schema；cli/api 为调用示例
  first_seen   TEXT
  last_seen    TEXT
  call_count   INTEGER DEFAULT 0
```

## 开放问题（交审）

1. 遥测采集点：hermes 侧 hook 还是事后从 session 日志批量挖？前者实时但动核心，后者零侵入但滞后。
2. tools 分区要不要进 dup-check 管辖（同名工具重复注册）？
3. map 渲染：工具条目默认折叠还是与 skill 同权展开？
