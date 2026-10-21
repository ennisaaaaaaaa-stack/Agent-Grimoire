# use / inject 写面（T23）— 取用记账仪

状态：已实施（洄洄 10/20 拍板，堂屋 #1107）。本页把语义钉死，防后人在措辞上漂移。

## 病根

events 表全史零 `skill.use` / `skill.inject` 事件。auto/ 新兵的转正判据挂在 use 层
（经图 auto 段原文："转正判据挂use层"），但记录仪从未装过——判据在数一个永远为零的
指标。修法：**get_skill 命中落 use、render_map 落 inject**，读脸自带记账。

## use 语义（钉死）

**use = 取用代理指标，非用上了。**

- 记的是"GET /skill 命中、200 返回"这一次取用动作。取了 ≠ 用了：
  agent 拉了正文可能在当轮就没用上，甚至没读完。use 是代理指标，
  精确度到此为止，转正判据要的就是这个分辨率——"有没有人取"。
- 真实用效（好用/没用）仍走 `skill.telemetry.expand` 自觉报，
  即取正文尾行那句搭车提醒。use 与 expand 两层不互替：
  use 是被动机械账（不可伪造），expand 是主动判断账（自觉）。

## inject 语义

**inject = 经图被拉取的代理指标。** GET /map 一次记一笔，
payload 带 map_bytes。拉了 ≠ 注入成功 ≠ 被读到——同样是代理指标分辨率。

## 记账规则

| 路径 | kind | 记否 |
|---|---|---|
| GET /skill 命中（过完全部门、200） | `skill.use` | 记 |
| GET /skill 门拒（404 / 装404 / 403 draft门 / 410 retired） | — | **零事件** |
| GET /map | `skill.inject` | 记（operator 取 X-Operator 头，缺省 unknown） |
| GET /stats 内部渲染经图 | — | **不记**（量尺不是注入，否则每次读 /stats 虚增 inject 账） |

- 写面落在**门后**：404/403/410 都在 get_skill 里提前 return，
  走到写面 = 过了全部可见性门、即将 200 的那次取用。门前的取用不算 use。
- **不合并、不幂等**：同一 skill 连取两次记两行。use 计数就是要数次数。
- **内部落账，不可伪造**：use/inject 不进 `EVENT_KINDS`，与
  `skill.pool.submit` / `skill.checkin.scan` 同律——POST /event 提交这两个
  kind 一律 400。use 是 auto/ 转正判据的数据源，开了外部口子等于判据可刷。
- 只记不动：与 events 表契约同律，事件本身不触发任何变更。
- 预算无关：遥测类事件不占 one-mutation budget，use/inject 同。

## 边界情形（照照裁过留档）

- **审阅态取用**（带 X-Review-Draft 头读 draft/retired）走同一 200 路径 → 记 use。
  任务书拍板"成功返回 200 的那次取用才算 use"，此为字面执行。
  若日后定审阅读不应污染 use 账，改 get_skill 写面前加头判断即可，测试同步翻。
- operator 语义沿用读脸：X-Operator 头，缺省 "unknown"，防误看不防冒领（R2 管）。
