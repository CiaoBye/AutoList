# AutoList 代码审计基线报告

> 本文件为 2026-08-10 全仓代码审计的**最终基线**。内容来自五轴并行审查（correctness / architecture / security / tests / performance）并经独立 verifier 逐项对照真实代码交叉验证（`review/` 下各轴原始报告与验证记录见 `/tmp/autolist-review/`）。审计期间未修改任何代码。

## 一、审计元信息

| 项目 | 值 |
| --- | --- |
| 项目名称 | AutoList（电影片单运营工作台，FastAPI + SQLite + 原生 JS） |
| 当前分支 | `main` |
| 当前 HEAD | `ddc3e05`（`fix: refine themed map interactions and dashboard contrast`） |
| 工作区状态 | 27 项未提交变更（24 个修改文件 + 未跟踪 `docs/repair-progress.md`、`tests/test_round2_safety.py`、`review/`），未提交 GitHub、未推送 |
| 审计范围 | 整个仓库当前工作树：后端 29 个 Python 模块（~7K 行）、前端（app.js 2679 行 / style.css / theme.css / core.js / theme-init.js / index.html）、14 张表、tests/（~2.8K 行）、Dockerfile、compose.yml、.github/workflows/ci.yml、unraid/ 模板、README/CHANGELOG/CONTEXT 等文档 |
| 审计方法 | 5 个独立 reviewer 并行五轴审查 → 独立 verifier 逐项验证（文件:行号 grep 定位、ReDoS 计时实测、脱敏行为实测、跨轴查重） |
| 验证基线 | 139 项 unittest 通过（Python 3.12 与 3.14）、`compileall`、`node --check`、`git diff --check` |

## 二、编号说明

- 编号格式 `P{级别}-{序号}`：P0（阻断）/ P1（高）/ P2（中高）/ P3（中低）/ P4（security 轴自有低危等级，低于 P3）。
- 分级与内容以 verifier 交叉验证后的最终结论为准（共 59 项）；每项标注原始验证条目号（`verified #N`）与来源审查轴，便于回溯。
- 所有【修正】项均已按 verifier 核实的行号/描述更新；【删除】的 1 项（security 轴误报：图标私网校验"不一致"）不进入本报告。

---

## 三、P0 Critical — 无

本审计未发现 P0 阻断级问题。

---

## 四、P1 High（3 项）

### P1-1 健康检查断言过严，单个片单来源抖动可致容器长期 unhealthy

- 来源：correctness 轴（verified #1）
- 文件：`compose.yml:26`、`Dockerfile:34`、`app/services/automation.py:376-408`、`app/state.py:100-103`
- 问题：健康检查断言 `scheduler_ok is True`；任一调度 tick 出错（含单个片单同步失败）即 degraded，整个容器被标记 unhealthy。心跳只在 tick 开始打点，多片单各 30s 超时累计超 180s 还会误报 stale。
- 证据：compose.yml:26 与 Dockerfile:34 内联脚本均含 `assert ... payload.get("scheduler_ok") is True`；automation.py:383-408 对每个到期片单 `except Exception` 后 `tick_errors.append`，非空即 `state.mark_scheduler_error`（:407-409）；state.py:100-102 `scheduler_last_error` → `status="degraded", ok=False`；心跳打点在 tick 开始（automation.py:379）。
- 影响：可选片单来源抖动/404 会把容器持续拉成 unhealthy；Unraid/监控场景可能触发重启与误告警。
- 修复建议：健康检查只断言 `ok is True`，`scheduler_ok` 降级为诊断字段；或单片单失败只记日志不计入 tick 错误。
- 置信度：高（行为已逐行确认；"超过 180s 误报 stale"依赖多片单同时超时的数据量，为合理推断）
- verifier 结论：【保留】

### P1-2 `run_recognition` 与 `run_library_scan` 两种后台任务零行为测试

- 来源：tests 轴（verified #33）
- 文件：`app/services/automation.py:45`、`app/services/library.py:48`、`tests/`（全目录 grep 无命中）
- 问题：`run_recognition` 与 `run_library_scan` 两种后台任务没有任何行为测试；唯一相关用例（test_regressions.py:1240）只验证 429 容量拒绝，不执行识别逻辑。
- 证据：两函数分别在 automation.py:45、library.py:48；tests/ 全目录 grep `run_recognition`/`run_library_scan` 无命中。
- 影响：识别与 Emby 状态刷新全生命周期、recognition_tasks/library_scan_tasks 行推进无回归保护。
- 修复建议：mock `recognize_movie`/`library_details` 真实执行两个任务函数，断言终态与计数。
- 置信度：高
- verifier 结论：【保留】

### P1-3 搜索轮询无页面守卫，隐藏页面持续高频拉取并全量重建候选 DOM

- 来源：performance 轴（verified #46）
- 文件：`app/static/app.js:772-810、676-728、812、2671`
- 问题：`refreshCandidates` 无 `currentPage === "search"` 判断，隐藏页面持续每 1.4s 拉取 4 个接口并全量重建候选 DOM。
- 证据：refreshCandidates（:772-810）无页面守卫；:807 `if (schedule && ["queued","running"].includes(task.status)) taskTimer = setTimeout(..., 1400)`；recoverLatestTask（:812）在 :2671 任意落地页调用；renderCandidates（:676-728）签名变化即 `container.innerHTML = filtered.map(...).join("")` 全量重建（上限 5000 行，search.py:25 MAX_CANDIDATE_RESPONSE=5000）。
- 影响：数小时搜索期间隐藏页持续约 2.9 req/s、每轮数 MB JSON + 数千 DOM 重建；多开页面成倍放大。
- 修复建议：离开搜索页 clearTimeout 暂停轮询；候选列表分页/虚拟滚动。
- 置信度：高
- verifier 结论：【保留】

---

## 五、P2 Medium-High（25 项）

### P2-1 自动化内嵌搜索在容量满时被标记 failed 而非排队

- 来源：correctness 轴（verified #2）
- 文件：`app/services/automation.py:146、196-199`；`app/services/search.py:159-165`；`app/state.py:200-204`
- 问题：自动化内嵌搜索在容量满（≥3 活动搜索任务）时 `begin_search_task_slot` 抛 429，被外层 `except Exception` 捕获 → run 标记 failed，而非排队。
- 证据：automation.py:146 `begin_search_task_slot(conn)`（内部 search.py:159-165 做 `BEGIN IMMEDIATE` + `enforce_search_task_capacity`，满则 `raise HTTPException(429)`）；run_playlist_automation 无 `except HTTPException` 分支，:196-199 `except Exception` → `update_automation_run(run_id, status="failed", ...)`。对照 `start_playlist_automation`（:312-353）容量满时保留 queued。
- 影响：自动化 run 变 failed 且不自动重试，该批影片被静默跳过。
- 修复建议：捕获 429 将 run 置 queued 依赖调度器消费，或退避重试。
- 置信度：高
- verifier 结论：【保留】

### P2-2 内嵌搜索任务 failed/cancelled 时自动化 run 仍报 completed 并发送完成通知

- 来源：correctness 轴（verified #4）
- 文件：`app/services/automation.py:171-173、188-192`
- 问题：内嵌搜索任务 failed/cancelled 时，自动化 run 仍标记 completed 并发送"新增影片处理完成"通知。
- 证据：:172-173 `if final_task and final_task["status"] in ("completed","partial"): searched = ...`，failed 时 searched 保持 0；随后 :188-192 无条件下发 `status="completed"` 与 `add_notification("新增影片处理完成", ...)`。run_search 的快照校验失败确实会把任务置 failed（services/search.py:296-308）。
- 影响：用户看到"处理完成 / 搜索 0 / 推荐 0"的成功通知，无法与"正常完成但无候选"区分。
- 修复建议：搜索任务 failed/cancelled 时 run 置 partial/failed 并带 error_message，或消息中明示。
- 置信度：高（行为确定；是否算 bug 取决于产品意图）
- verifier 结论：【保留】

### P2-3 自定义制作组规则正则 ReDoS，可阻塞事件循环（已实测复现）

- 来源：security 轴（verified #23，【修正】）
- 文件：`app/candidate_policy.py:11、177-198、215-230`
- 问题：自定义制作组规则正则存在 ReDoS。`NESTED_QUANTIFIER`（:11）过滤可被绕过——规则 `(?:(?:a)+)+` 与 `(a|aa)*` 均通过 `merge_custom_rules`；随后 `match_release_group`（:215-230）对 PT 站点标题执行 `re.search`。
- 证据（verifier 独立复现）：`(?:(?:a)+)+` 对 29 字符标题（`'a'*28+'b'`）`re.search` 实测耗时 **7.2 秒**（无超时，阻塞事件循环）。`(a|aa)*` 实测 0.046s（零宽匹配提前成功），未复现灾难计时——该子结论已修正，仅保留"可绕过过滤"事实。
- 影响：操作者（或被诱导）配置恶意规则后，每次搜索/评分预览对命中标题卡死进程数秒；多标题可持续 DoS。规则经 `PUT /api/config` 写入、需认证。
- 修复建议：拒绝规则内任何 `*`/`+`/`{`（或 `(?:...)+` 形重复）；更稳妥是匹配放线程加超时；校验阶段做计时冒烟。
- 置信度：高（`(?:(?:a)+)+` 主结论已复现）；`(a|aa)*` 的灾难计时未复现、已修正
- verifier 结论：【修正】（`(a|aa)*` 灾难计时未复现）

### P2-4 CookieCloud 限流在 uuid 校验前计数，未认证垃圾请求可耗尽共享预算

- 来源：security 轴（verified #24）
- 文件：`app/api/system.py:149-162`、`app/state.py:142-151`、`app/main.py:151`
- 问题：未认证 CookieCloud 上传在读取/校验请求体之前限流计数，垃圾请求消耗 10 次/分钟共享预算。
- 证据：system.py:150 `enforce_cookiecloud_rate_limit()` 先执行，:162 `require_configured_cookiecloud_uuid` 在其后；state.py:142-151 无论后续校验成败都 `append(now)`；main.py:151 `/cookiecloud/` 前缀鉴权豁免。
- 影响：攻击者每 60s 发 10 个垃圾请求即可持续 429 阻断合法 Chrome 扩展同步（影响面限定 Cookie 同步）。
- 修复建议：uuid 校验通过后再计数，或失败请求用独立宽松桶。
- 置信度：高
- verifier 结论：【保留】

### P2-5 出站 URL 校验两套并行实现，敏感键清单重复

- 来源：architecture 轴（verified #8，【修正】）
- 文件：`app/api/system.py:80-127`、`app/util.py:157-208`
- 问题：出站 URL 校验存在两套并行实现（`validated_base_url` vs `validate_outbound_url`），敏感键清单重复定义。
- 证据：两者各自实现 scheme/凭据/私网/DNS/敏感参数校验，信任模型一致（`not access_token_required()` vs `not os.getenv("AUTOLIST_ACCESS_TOKEN")`，system.py:100 vs util.py:186-188）。完全相同的正则有 **2 份**（util.py:123 `SENSITIVE_URL_QUERY_KEY` 与 system.py:120-122 内联正则逐字节一致）；config.py:13-19 与 security.py:7-9 为同关键词的不同变体。
- 影响：多份维护、易漏改分叉。
- 修复建议：`validated_base_url` 改为对 `validate_outbound_url` 的薄封装；敏感键清单收敛一处。
- 置信度：高（重复事实）；"4 处逐字节重复"已修正为"2 处逐字节 + 2 处变体"
- verifier 结论：【修正】

### P2-6 影片身份键三处实现，归一化规则不一致

- 来源：architecture 轴（verified #9）
- 文件：`app/api/playlists.py:262-270`、`app/services/automation.py:235-246`、`app/services/imports.py:111-123`
- 问题：身份键（imdb/tmdb/title+year）三处实现，归一化规则不一致。
- 证据：playlists.py:268 `[^a-z0-9\u4e00-\u9fff]+` + `casefold()`，year 拼入键串；automation.py:238 `\W+` + `lower()`，year 为独立元组元素；imports.py:117 `[^a-z0-9\u4e00-\u9fff]+` + `lower()`。`\W+` 保留重音/`·` 等词字符，`casefold` 与 `lower` 在 ß/İ 上结果不同。
- 影响：同一片单走"网址刷新"与"定时增量同步"两条路径时同一影片可能生成不同 key，重复插入或漏合并；`domain/titles.py` 已有唯一实现可复用。
- 修复建议：在 `domain/titles.py` 提供 `item_identity_keys()` 统一三处。
- 置信度：高（重复与差异确认；对含非 ASCII 词字符标题的影响为推测）
- verifier 结论：【保留】

### P2-7 `scoring_policy` 等 8 个死配置键仍暴露在种子/写接口/Schema

- 来源：architecture 轴（verified #10）
- 文件：`app/database.py:360-366、442-443`、`app/schemas.py:54-62、70`、`app/api/system.py:285、291-296`
- 问题：`scoring_policy` 与 7 个旧键（preferred_resolutions/minimum_resolution/preferred_codecs/priority_groups/secondary_groups/fallback_groups/allow_unknown_groups）仍在种子、写接口与 Schema 中，但任何评分代码不读。
- 证据：database.py:360-366 种子写入、:442-443 save_config 白名单；schemas.py:54-62 ConfigPayload、:70 ScorePreviewPayload.scoring_policy；system.py:285 接受、:291-296 score_preview 只读 candidate_policy。全仓 grep `scoring_policy` 除上述位置外无消费方；candidate_policy.py 全程只读 candidate_policy。
- 影响：API 契约混乱，旧键在 DB 永久残留。
- 修复建议：删除旧键种子/白名单/字段，前后端收敛到 candidate_policy。
- 置信度：高
- verifier 结论：【保留】

### P2-8 业务逻辑散落在路由层（刷新合并/下载提交/候选分组/海报回填）

- 来源：architecture 轴（verified #11）
- 文件：`app/api/playlists.py:214-329`、`app/api/cart.py:102-257`、`app/api/search.py:186-258`、`app/api/playlists.py:49-76`
- 问题：业务逻辑散落在路由层。
- 证据：playlists.py:280-315 路由内直接做 rank 平移/身份匹配/就地更新/删 stale；cart.py:166-244 幂等+Emby 复查+MoviePilot 提交编排；search.py:186-258 candidates 分组折叠；playlists.py:49-76 hydrate_recent_emby_posters 路由内写 DB。
- 影响：业务规则两处分散、路由文件膨胀、难以单测。
- 修复建议：下沉到 `services/playlists.py`、`services/cart.py`，路由只留参数校验与响应组装。
- 置信度：高
- verifier 结论：【保留】

### P2-9 clients.py（844 行）混合解析器、工具与 9 个客户端类

- 来源：architecture 轴（verified #12）
- 文件：`app/clients.py:18、63、96-104、115、120-844`
- 问题：clients.py 混合 HTML 解析器、通用工具、代理判定与 9 个客户端类。
- 证据：NexusTableParser:18、AccountTableParser:63、human_size_bytes/numeric_value:96-104、site_proxy:115、MoviePilot/TMDB/AIRecognition/Emby/Transmission/Torznab/RSS/MTeam/NexusPHP 九个类 :120-844。
- 影响：文件体积大、职责不内聚，通用解析器无法独立复用。
- 修复建议：解析器与通用工具迁 util/domain，按 PT 适配器与外部服务拆分。
- 置信度：高
- verifier 结论：【保留】

### P2-10 `initialize()` 混合建表/迁移/数据收敛，无版本化迁移

- 来源：architecture 轴（verified #13）
- 文件：`app/database.py:215-449`
- 问题：`initialize()` 混合建表/索引/迁移/数据收敛，无版本化迁移。
- 证据：:215 起单函数完成 SCHEMA、二级索引、重复 active 任务收敛、7 张表逐列 `ALTER TABLE`（:321-380）、历史回填、默认值种子、旧 adapter 迁移、孤儿清理、敏感文本清洗、chmod；无 schema_version。
- 影响：演进成本随版本线性上升，无法验证"从任意旧版本升级"。
- 修复建议：按 `PRAGMA user_version` 分派迁移表，initialize 三阶段化。
- 置信度：高
- verifier 结论：【保留】

### P2-11 `api/sites.py` 直接依赖兄弟路由模块 `api/system.py`

- 来源：architecture 轴（verified #14）
- 文件：`app/api/sites.py:24`、`app/api/system.py:80`
- 问题：`api/sites.py` 直接 `from .system import validated_base_url`，兄弟路由模块互相依赖。
- 证据：sites.py:24；该工具又经 main.py:203 再导出给测试，形成 api→api + main→api 双重耦合。
- 影响：system.py 改动波及 sites；复用校验只能 import 路由模块。
- 修复建议：`validated_base_url` 移到 util.py（基于 validate_outbound_url 封装）。
- 置信度：高
- verifier 结论：【保留】

### P2-12 main.py 为测试再导出约 35 个符号，入口模块成依赖图中心

- 来源：architecture 轴（verified #7）
- 文件：`app/main.py:34-46、187-207`
- 问题：main.py 为测试再导出约 35 个符号，入口模块成为依赖图中心。
- 证据：:44-46 注释"Domain / service re-exports for unit tests"；:187 起底部 `from .api.cart import ...`（E402/F401 抑制）；tests/test_regressions.py:23 `from app import main`。
- 影响：任何模块改动波及入口文件；测试无法从真实模块导入。
- 修复建议：测试改从 `app.api.*`/`app.services.*`/`app.util` 直接导入，逐步删除 re-export。
- 置信度：高
- verifier 结论：【保留】

### P2-13 搜索热点路径重复解析候选策略（每 torrent ≥2 次 json.loads）

- 来源：performance 轴（verified #47）
- 文件：`app/services/search.py:412、414-416、470`、`app/services/recognition.py:15-21`
- 问题：run_search 热点路径重复解析候选策略。
- 证据：recognition.py:15-21 `analyze_candidate` 每次 `json.loads(config["candidate_policy"])`；search.py:412 sort 的 key lambda 每项一次、:414-416 已算出 policy 但 :425-428 选择循环与 :470 入选循环未复用。
- 影响：2000 部×多站点时策略解析数十万到上百万次。
- 修复建议：run_search 内复用已解析的 policy 字典，或给 analyze_candidate 加 policy 参数。
- 置信度：高
- verifier 结论：【保留】

### P2-14 completed 搜索任务的候选永不清理，candidates 表无界增长

- 来源：performance 轴（verified #48）
- 文件：`app/database.py:459-471`
- 问题：completed 搜索任务的候选永不清理，candidates 表无界增长。
- 证据：cleanup_old_data 只归档 `status IN ('failed','partial','interrupted','cancelled')`（:459-464），completed 永不到达归档→删除分支（:466-471）。
- 影响：数月积累数十万行，磁盘与查询变慢。
- 修复建议：completed 任务也引入归档/清理窗口，或按 candidates.created_at 独立清理。
- 置信度：高
- verifier 结论：【保留】

### P2-15 cleanup_old_data 的 DELETE 全表扫描且每 60s 执行

- 来源：performance 轴（verified #49）
- 文件：`app/database.py:472-482`、`app/services/automation.py:383`
- 问题：cleanup_old_data 的 DELETE 全表扫描，且每 60s 执行一次。
- 证据：:472-482 四条 DELETE/UPDATE 均 `datetime(col) < datetime('now',...)` 包装在 TEXT 时间列上，使既有索引（含 idx_search_tasks_status_updated、idx_notifications_read_created）失效；automation.py:383 每个调度 tick 调用。
- 影响：数据量增大后每次清理耗时上升，长事务可能逼近 busy_timeout=5000 挤占搜索写锁。
- 修复建议：为时间列建索引或用 ISO 字符串直接比较；按批删除缩短写锁持有。
- 置信度：高（全表扫描行为确定）；写锁竞争为推测
- verifier 结论：【保留】

### P2-16 async 端点内同步 DNS 解析阻塞事件循环（图标代理）

- 来源：performance 轴（verified #50）
- 文件：`app/util.py:478-492`、`app/api/sites.py:142`
- 问题：async 端点内做同步 DNS 解析，阻塞事件循环。
- 证据：util.py:478-492 `validate_remote_icon_url` 是 async 但内部调同步 `validate_outbound_url`（→ `_resolve_outbound_addresses` :149-155 的 `socket.getaddrinfo`），未走 `asyncio.to_thread`（对比 util.py:214-223 已有 async 封装）；sites.py:142 在 async site_icon 中调用。token 模式下同主机图标 `allow_private=False` 触发解析。
- 影响：站点页并行加载多个图标时每个图标 DNS（解析器无响应可达数秒）冻结整个事件循环。
- 修复建议：改用 `validate_outbound_url_async` 或 `asyncio.to_thread`。
- 置信度：高（路径确定）；触发依赖 token 模式+同主机图标
- verifier 结论：【保留】

### P2-17 CI 不检查 `core.js` 语法（app.js 的 ES 模块依赖）

- 来源：tests 轴（verified #34）
- 文件：`.github/workflows/ci.yml:42-46`、`app/static/app.js:1-6`
- 问题：`core.js` 是 app.js 的 ES 模块依赖，但 CI 不检查其语法、测试不引用。
- 证据：app.js:1-6 `import { $, $$, escapeHtml, safeExternalUrl } from "./js/core.js"`；ci.yml:42-46 只 `node --check app.js / theme-init.js / docs/prototypes/autolist-directions.js`（`node --check` 不解析 import 目标）。
- 影响：core.js 语法错误时 CI 全绿但生产端 module 加载直接不渲染。
- 修复建议：ci.yml 增加 `node --check app/static/js/core.js`。
- 置信度：高
- verifier 结论：【保留】

### P2-18 取消搜索任务路径无测试

- 来源：tests 轴（verified #35）
- 文件：`app/api/search.py:100`、`app/services/search.py:514-516`
- 问题：取消搜索任务路径无测试。
- 证据：search.py:100 `cancel_task`；run_search 的 `except asyncio.CancelledError: update_task(task_id, status="cancelled"); raise`（实际 :514-516）；tests/ grep `cancel_task` 无命中。
- 影响：取消兜底落库、running_tasks 清理、404/409 语义均无 CI 保护。
- 修复建议：queued/running 任务 → cancel_task → 断言 cancelled 且登记表不含该 id；已结束任务返回 409。
- 置信度：高
- verifier 结论：【保留】

### P2-19 约 19 条 API 路由完全无测试

- 来源：tests 轴（verified #36，【修正】）
- 文件：`app/api/system.py:144-145、189、198、215、234、271、291、301`、`app/api/sites.py:173-174、238-239、256-257`、`app/api/playlists.py:330-331、356-357、387-388、397-398、454、483、491、543`、`app/api/search.py:100`
- 问题：一批 API 路由完全无测试。
- 证据（verifier 逐一路由装饰器核对，行号已修正）：/cookiecloud 两条（system.py:144-145）、score-preview（:291，前端 app.js:2094 实际调用）、connection（:215）、downloads（:234）、health-history（sites.py:173-174）、delete_site（:238-239）、test_all_sites（:256-257）、GET /api/playlists（playlists.py:330-331）、automation-runs（:356-357）、notifications（:387-388）、read-all（:397-398）、library-scan（:454）、library-scan-tasks/{id}（:483）、PUT /api/playlists/{id}（:491）、recognition-tasks/{id}（:543）、cancel_task（search.py:100）等；tests/ 逐一 grep 上述路径无命中（task_attempts 除外：test_workflows.py:216）。
- 影响：设置页/站点页/通知/下载诊断出口回归无 CI 信号。
- 修复建议：按"返回形状+错误码+脱敏"三层为每条路由补最小 HTTP 级用例，优先 score_preview、notifications、connection、downloads。
- 置信度：高（行号修正后全部核对）
- verifier 结论：【修正】

### P2-20 notifications 表零覆盖（含 add_notification 与两条路由）

- 来源：tests 轴（verified #37，【修正】）
- 文件：`app/database.py:168`、`app/services/automation.py:92-97`、`app/api/playlists.py:387-401`
- 问题：notifications 表零覆盖。
- 证据：建表在 database.py:168；add_notification 在 automation.py:92-97；路由 playlists.py:387-388、397-398。tests/ grep `add_notification`/`notifications` 无命中；test_nonempty_automation（test_regressions.py:578-612）执行了自动化但从不断言 notifications 行。
- 影响：通知写入（含脱敏）、未读计数、read-all 翻转未验证。
- 修复建议：在 test_nonempty_automation 中补 SELECT notifications 断言；另测 read_all 计数。
- 置信度：高
- verifier 结论：【修正】（行号 120-128→168）

### P2-21 核心服务错误路径覆盖不全（blocked/failed/429/提交失败等）

- 来源：tests 轴（verified #38，【修正】）
- 文件：`app/services/automation.py:106-111、193-199、269-277`、`app/api/cart.py:236-238`、`app/state.py:142-151`、`app/services/history.py:246-276`、`app/api/sites.py:122-158`
- 问题：核心服务错误路径覆盖不全。
- 证据（行号已修正）：automation.py:110-111（blocked 分支）、:196-199（failed）、:193-195（cancelled）、:269-277（增量同步 fetch 失败 → last_sync_status='failed'）；cart.py:236-238（moviepilot.download 异常 → success=0，现有全部 download mock 均为 return_value/AsyncMock，grep `side_effect` 无 MoviePilot 抛异常用例）；state.py:142-151（CookieCloud 429 无测试）；history.py:246-276（organized/pending_library/downloading 分组删除，仅 failed 被 test_regressions.py:958 覆盖）；sites.py:122-158（图标抓取分支）。
- 影响：blocked/failed/cancelled/429/提交失败落库等"用户可感知失败态"无 CI 保护。
- 修复建议：为每项补针对性用例（blocked、download 抛异常、连续 11 次 /cookiecloud/update、历史分组删除）。
- 置信度：高（行号修正后逐一核对；"未测"由 grep 证实）
- verifier 结论：【修正】

### P2-22 RuntimeSettings 的 null/空串语义被两份测试重复断言

- 来源：tests 轴（verified #39）
- 文件：`tests/test_round2_safety.py:411-430`、`tests/test_regressions.py:1249-1278`
- 问题：round2 与 regressions 重复测试 RuntimeSettings 的 null/空串语义。
- 证据：round2:411-430 走 HTTP（`client.put("/api/settings", json={"mp_api_key": None/""})`），regressions:1249-1278 直接调 `put_runtime_settings`，断言同一核心语义（null 保留、空串清除）。
- 影响：同一语义两份断言，易只改其一导致行为描述分叉。
- 修复建议：保留 round2 的 HTTP 端到端用例，删除或改写 regressions 的直接调用用例。
- 置信度：高（重复事实）；删除建议为建议性
- verifier 结论：【保留】

### P2-23 代理用例依赖 httpx 私有 `_mounts` 且依赖真实网络失败

- 来源：tests 轴（verified #40，报告标注【推测】）
- 文件：`tests/test_round2_safety.py:513-529`
- 问题：代理用例依赖 httpx 私有 `_mounts` 且依赖真实网络失败。
- 证据：:518 `client._mounts = {"all://": httpx.Proxy("http://proxy.example:8080")}`，AsyncClient 未挂 MockTransport，因此 `safe_request` 会真实向 proxy.example:8080 发起连接；`assertRaises(Exception)` 由"代理不可达"满足，有效断言仅 `bind.assert_not_awaited()`。`_mounts` 为 httpx 私有结构。
- 影响：环境中存在同名可达代理时行为不定；httpx 升级可能失效。
- 修复建议：改用 MockTransport 或仅断言 `_bind_outbound_host` 未调用并去掉 assertRaises。
- 置信度：中（与报告一致）
- verifier 结论：【保留】

### P2-24 进程级缓存/限流时间戳在多数测试类不清理

- 来源：tests 轴（verified #41，【修正】，报告标注【推测】）
- 文件：`tests/test_regressions.py:327-346`、`tests/test_workflows.py:16-21`、`tests/test_round2_safety.py:64-70`、`tests/test_backend_hardening.py:42-46`、`app/state.py:32-34`
- 问题：进程级缓存/限流时间戳在多数测试类不清理。
- 证据：`poster_cache.clear()` 仅在 DatabaseAndApiTests（regressions:328、346）；`site_icon_cache` 在**任何**测试类都未清理（grep 全 tests 无命中，比原描述更严重）；workflows/round2/hardening 的 setUp/tearDown 只清 raw_candidates。state.py:32-34 定义进程级 cache。
- 影响：未来用例复用同一 emby_item_id/tag 或站点 id 会读到残留缓存产生假阳性；新增第二个 /cookiecloud/update 用例可能意外 429。
- 修复建议：统一基类清理 `poster_cache`/`site_icon_cache`/`_cookiecloud_upload_times`/`_cookiecloud_get_times`。
- 置信度：中（潜在风险，当前未触发）；site_icon_cache 细节已修正
- verifier 结论：【修正】

### P2-25 前端契约测试停留在字符串/标记层，浏览器验收不在 CI

- 来源：tests 轴（verified #43）
- 文件：`tests/test_regressions.py:79-193`、`.github/workflows/ci.yml`
- 问题：前端契约测试停留在字符串/标记层，浏览器验收不在 CI。
- 证据：:93-98 `html.count(f'id="{element_id}"')` 断言；ci.yml 无浏览器步骤。docs/repair-progress.md 自述"地图编辑模式尚未闭环"，而 139 个测试全部通过，证明字符串契约无法捕获行为回归。
- 影响：路由刷新恢复、主题对比度/溢出、移动端导航等 AGENTS.md 强制行为只能人工验收。
- 修复建议：短期 CI 增加 Playwright 冒烟；长期将字符串契约降级为存在性断言并补 DOM 行为测试；作为发布前残留风险登记。
- 置信度：高
- verifier 结论：【保留】

---

## 六、P3 Medium-Low（29 项）

### P3-1 pending 搜索 413 误报（容量按区间全部行计数，含已入库）

- 来源：correctness 轴（verified #3）
- 文件：`app/api/search.py:39、45-52`
- 问题：pending 搜索的 413 用 rank 区间内全部 playlist_items（含已入库）行数作 total，大片单且已入库交错分布时误报 413。
- 证据：:39-40 `range_start/range_end` 取队列子集 min/max rank；:45-48 `SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ?`（未过滤 `library_state`）；:52 `if total > MAX_SEARCH_ITEMS: raise HTTPException(413)`，而实际搜索量（item_ids）≤2000 已由 `TaskPayload.count`（schemas.py:32, le=2000）与 `searchable_playlist_items` limit 限制。
- 影响：数千条目且已入库条目较多的片单 pending 搜索被错误 413。
- 修复建议：pending 分支用 `len(item_ids)` 做容量校验，或区间查询同样过滤 `library_state!='in_library'`。
- 置信度：中（逻辑确定；触发依赖大数据量片单）
- verifier 结论：【保留】

### P3-2 清空下载历史会删除重复提交防护的去重依据

- 来源：correctness 轴（verified #5）
- 文件：`app/api/cart.py:166-176`、`app/services/history.py:214-216`
- 问题：清空下载历史会删除 `success=1` 的去重依据，重新提交同一发布可能绕过幂等。
- 证据：cart.py `already_submitted` 查询 `WHERE h.success=1 AND (h.candidate_id=? OR EXISTS(...))`（:166-176）；history.py:215 `if status == "all": return conn.execute("DELETE FROM download_history")`（无 WHERE）。
- 影响：已成功下载+历史清空+Transmission 已移除+Emby 未检出（或未配置）的组合下可重复下载。
- 修复建议：把成功提交去重迁移到不受历史清空影响的表/列（如 candidates.submitted_at 或只追加提交记录表）。
- 置信度：中（去重信息被用户操作删除这一事实确定；触发需多条件组合）
- verifier 结论：【保留】

### P3-3 手动 refresh 不与定时增量同步共享进程内同步锁

- 来源：correctness 轴（verified #6）
- 文件：`app/api/playlists.py:214-329`、`app/services/automation.py:207-215`
- 问题：手动 `refresh_playlist_source` 不使用 `_playlist_sync_locks`，与定时增量同步并发时可能违反 `UNIQUE(playlist_id, rank_no)`。
- 证据：`sync_playlist_incremental` 用进程内锁串行化（automation.py:211-214，注释明确说明"否则手动请求与调度器会同时读到 max(rank_no) 竞争唯一约束"）；`refresh_playlist_source` 只在 fetch 后用 `BEGIN IMMEDIATE` 保护（playlists.py:246-329），不获取该锁。`UNIQUE(playlist_id, rank_no)` 约束在 database.py:42。
- 影响：偶发同步失败（IntegrityError → last_sync_status='failed'，1 小时后重试），非数据损坏。
- 修复建议：refresh 复用 `_playlist_sync_locks[playlist_id]`，统一所有 rank 写入路径。
- 置信度：中（并发时序依赖来源抓取网络耗时窗口；碰撞需片单增长）
- verifier 结论：【保留】

### P3-4 进程内状态模块（state.py）依赖 FastAPI HTTPException

- 来源：architecture 轴（verified #15）
- 文件：`app/state.py:10、166-171、200-204`
- 问题：进程内状态模块依赖 FastAPI `HTTPException`，鉴权/限流/容量检查放 state 而非 security。
- 证据：state.py:10 `from fastapi import HTTPException`；:166-171 `require_configured_cookiecloud_uuid`、:142-151 限流、:200-204 `enforce_search_task_capacity` 均抛 HTTPException。
- 影响：state 无法脱离 Web 框架复用/单测，职责定位模糊。
- 修复建议：鉴权归 security.py，限流/容量返回 bool 或自定义异常，路由层转 HTTPException。
- 置信度：高
- verifier 结论：【保留】

### P3-5 两个 `library_state` 包装函数零调用（死代码）

- 来源：architecture 轴（verified #16）
- 文件：`app/clients.py:305-307`、`app/services/library.py:31-35`
- 问题：两个 `library_state` 包装函数无任何调用点（死代码）。
- 证据：全仓 grep `library_state(` 仅两处定义（clients.py:305、library.py:31），无调用；实际使用 `library_match`/`library_details`。
- 影响：误导维护者、增加 grep 噪音。
- 修复建议：删除两个 wrapper。
- 置信度：高
- verifier 结论：【保留】

### P3-6 `MAX_SEARCH_ITEMS`/`MAX_IMPORT_ROWS` 各在两模块重复定义

- 来源：architecture 轴（verified #17）
- 文件：`app/schemas.py:10 vs app/services/search.py:39`；`app/schemas.py:13 vs app/services/imports.py:22`
- 问题：上限常量双模块重复定义，靠数值一致维持。
- 证据：schemas.py:10 `MAX_SEARCH_ITEMS=2000`、search.py:39 同值；schemas.py:13 `MAX_IMPORT_ROWS=200_000`、imports.py:22 同值。
- 影响：调上限漏改一处即产生不一致校验边界。
- 修复建议：上限常量收敛到 config.py 或 schemas.py，业务层 import。
- 置信度：高
- verifier 结论：【保留】

### P3-7 配置源四层混合，文件优先于 env 且无文档

- 来源：architecture 轴（verified #18）
- 文件：`app/config.py:79-138、41-47、186-207`
- 问题：配置源四层混合（dataclass 环境默认值、函数内 os.getenv、runtime-settings.json、app_config 表），文件优先于 env 且无文档。
- 证据：config.py:118-138 dataclass 字段 import 时读 env；:41-47 access_token 系列调用时读 env；:186-207 `save_runtime_settings` 将除 data_dir 外全部字段（含 mp_api_key/tr_password 等密钥）写入 runtime-settings.json（0600）。
- 影响：运维改 .env 后旧文件值仍生效；读 env 时机不一致。
- 修复建议：明确单一来源策略并写入 README；apply() 白名单与 save 字段集对齐。
- 置信度：高（行为确认）；"运维困惑"为推断
- verifier 结论：【保留】

### P3-8 `list_sources._proxy()` 无条件走代理，与 tmdb_proxy_enabled 门控不一致

- 来源：architecture 轴（verified #19）
- 文件：`app/list_sources.py:67-69、94`
- 问题：`_proxy()` 无条件走代理，与 `_tmdb` 的 `tmdb_proxy_enabled` 门控不一致。
- 证据：:67-69 `return settings.outbound_proxy_url or None`（_mdblist/_letterboxd/_imdb 使用）；:94 `_tmdb` 用 `settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None`。
- 影响：配了代理后 TMDB 片单抓取可被开关关掉，其余三类来源永远走代理，语义分裂。
- 修复建议：四类来源统一门控或明确文档。
- 置信度：高（行为确认）；意图不明为推测
- verifier 结论：【保留】

### P3-9 自动化把当前协程注册进搜索任务登记表

- 来源：architecture 轴（verified #20）
- 文件：`app/services/automation.py:167`
- 问题：自动化把当前协程句柄注册进搜索任务登记表以伪装"搜索中"。
- 证据：:167 `running_tasks[task_id] = asyncio.current_task()`，随后 `await run_search(task_id)`；run_search 的 finally pop。
- 影响：`/api/search-tasks/{id}/cancel` 会 cancel 整个自动化任务而非仅搜索；容量统计期间槽位被占用。
- 修复建议：用独立 `asyncio.create_task(run_search(task_id))` 句柄并 await，或显式 owner 标记。
- 置信度：高
- verifier 结论：【保留】

### P3-10 前端单文件膨胀（app.js 2679 行）

- 来源：architecture 轴（verified #21）
- 文件：`app/static/app.js`（全文）
- 问题：前端单文件膨胀，混合路由/8 页面渲染/轮询/CRUD/交互式站点地图（约 850 行）/主题/无障碍。
- 证据：app.js 2679 行；站点地图缩放/视野同步 :1077-1092 内联在页面脚本；core.js 仅 250 行帮助函数。
- 影响：难以定位与回归；地图逻辑无法独立测试。
- 修复建议：按页拆分渲染模块并抽出 js/site-map.js。
- 置信度：高
- verifier 结论：【保留】

### P3-11 同一段健康检查内联脚本在 Dockerfile 与 compose 各复制一份

- 来源：architecture 轴（verified #22）
- 文件：`Dockerfile:33-34`、`compose.yml:20-26`
- 问题：同一段健康检查内联脚本在 Dockerfile 与 compose 各复制一份，CI 只校验 compose 合法性。
- 证据：两处内联 `python -c "import json,sqlite3,urllib.request; ..."` 断言 scheduler_ok 与 DB quick_check。
- 影响：收紧健康检查时漏改一处则本地构建与 compose 部署行为分叉。
- 修复建议：提取 `app/healthcheck.py` 两处统一引用。
- 置信度：高
- verifier 结论：【保留】

### P3-12 弱令牌 503 响应与 /api/health 向未认证调用者泄露鉴权配置细节

- 来源：security 轴（verified #25）
- 文件：`app/main.py:153-160`、`app/api/system.py:131-142`
- 问题：弱令牌 503 响应向未认证调用者泄露鉴权配置细节；`/api/health`（豁免路径）返回 access_token_strength/version。
- 证据：main.py:153-160 503 响应体含 `configured`/`validation`；system.py:131-142 health 返回 `access_token_required`、`access_token_strength`、`access_token_strength_enforced`、`version`，且 /api/health 在 AUTH_EXEMPT_PATHS（main.py:151 先豁免）。
- 影响：可探测令牌配置/弱点/版本号，辅助社工与定向攻击；仅信息泄露。
- 修复建议：503/health 只返回统一提示，不下发 configured/validation 明细。
- 置信度：高
- verifier 结论：【保留】

### P3-13 令牌失败尝试无任何速率限制/锁定

- 来源：security 轴（verified #26）
- 文件：`app/main.py:161-163`
- 问题：令牌失败尝试无任何速率限制/锁定（原报告标注【推测】）。
- 证据：main.py:161-163 校验失败仅返回 401；全仓 grep 429 仅出现在 CookieCloud 限流与任务容量处，无令牌失败计数/退避。
- 影响：仅当操作者设 `AUTOLIST_REQUIRE_STRONG_TOKEN=false` 放行短令牌时，远程可无限枚举；默认 32 字符强令牌下不可行。
- 修复建议：失败尝试进程内限速+退避；文档将弱令牌开关标注"公网暴露下禁止"。
- 置信度：中（无限流事实确定；可利用性依赖配置）
- verifier 结论：【保留】

### P3-14 除 CookieCloud 外无全局请求体大小上限；策略 dict 字段无界

- 来源：security 轴（verified #27）
- 文件：`app/schemas.py:62-63`、`app/api/system.py:275-289`
- 问题：除 CookieCloud（40MB）外无全局请求体大小上限；`scoring_policy`/`candidate_policy` 为无界 dict。
- 证据：schemas.py:62-63 两个 dict 字段无长度约束；system.py:276-288 `put_config` 直接 `payload.model_dump` + 存库；`put_runtime_settings`（:249-273）同样无 body 上限。对照 CookieCloud 有 `read_request_body_limited`（util.py:81-105）、ImportPayload.json_data 有 10MB 校验（schemas.py:25-40）。
- 影响：持有令牌者可提交超大 JSON 造成内存峰值与 DB 膨胀（需认证，低危 DoS）。
- 修复建议：中间件统一 body 上限（如 5MB），或 dict 字段 validator 内做长度检查。
- 置信度：高
- verifier 结论：【保留】

### P3-15 CookieCloud 密钥允许 5 字符短键；合法上传无条件覆盖全部匹配站点 Cookie

- 来源：security 轴（verified #28）
- 文件：`app/schemas.py:83`、`app/api/system.py:178-182、189-196`、`app/services/sites.py:119-132`
- 问题：CookieCloud 密钥为唯一凭据且允许 5 字符短键；合法上传自动无条件覆盖全部匹配站点 Cookie（原报告标注【推测】）。
- 证据：schemas.py:83 `cookiecloud_key` pattern `[A-Za-z0-9_-]{5,128}`、CookieCloudUploadPayload.uuid `min_length=5`；system.py:178-182 解密成功后立即 `apply_cookie_groups(groups)`；sites.py:126-131 对匹配 host 无条件 `UPDATE pt_sites SET cookie=?`。
- 影响：弱 KEY 可遍历（GET 限 30/min）；KEY 泄露后攻击者可无条件覆盖站点 Cookie 实现投毒。
- 修复建议：最小长度提到 12+；GET 失败尝试指数退避；覆盖前对数量突变告警。
- 置信度：中（上传→覆盖链路确定；可利用性依赖弱 KEY 配置）
- verifier 结论：【保留】

### P3-16 站点 Cookie 与含 passkey 的下载地址随 torrent_in 明文提交 MoviePilot

- 来源：security 轴（verified #29）
- 文件：`app/api/cart.py:209`、`app/clients.py:461、694、319-334`
- 问题：站点 Cookie 与含 passkey 的下载地址随 `torrent_in` 明文提交 MoviePilot。
- 证据：clients.py:461（RSS）、:694（NexusPHP）适配器输出 `site_cookie`；`MoviePilotClient.download`（:319-334）只剔除 volume_factor/publish_time/detail_url，site_cookie/site_ua/enclosure 保留；cart.py:209 提交。
- 影响：凭据离开 AutoList 进程，传输强度取决于 MP_BASE_URL（.env.example 示例为明文 http）；属"设计使然+传输安全建议"。
- 修复建议：README 要求 MP 走 HTTPS/受控内网；site_cookie 仅下载时按需注入。
- 置信度：高（行为明确；风险等级为设计权衡）
- verifier 结论：【保留】

### P3-17 三份测试文件重复实现互不相同的 settings/env 隔离样板

- 来源：tests 轴（verified #42）
- 文件：`tests/test_round2_safety.py:52-80`、`tests/test_backend_hardening.py:29-57`、`tests/test_regressions.py` 各测试类
- 问题：三份测试文件重复实现互不相同的 settings/env 隔离样板。
- 证据：round2 用 `settings.__dict__.clear()+update(snapshot)`（:69-70）；hardening 逐字段恢复；regressions 各测试类手写 try/finally。
- 影响：复制粘贴的隔离逻辑是后续测试最易出错处；`__dict__` 方案与逐字段方案对新增属性行为不同。
- 修复建议：抽共享 `IsolatedAppTestCase` 基类。
- 置信度：高（重复事实）；影响低
- verifier 结论：【保留】

### P3-18 `_FakeRequest.stream` 返回类型注解错误

- 来源：tests 轴（verified #44）
- 文件：`tests/test_round2_safety.py:139-144`
- 问题：`_FakeRequest.stream` 返回类型注解错误。
- 证据：:139-144 async generator 注解为 `-> bytes` 并 `# type: ignore[return]`。
- 影响：仅类型标注，无运行时影响。
- 修复建议：改为 `AsyncIterator[bytes]`。
- 置信度：高
- verifier 结论：【保留】

### P3-19 CI 与本地 Python 版本不一致且依赖未锁定

- 来源：tests 轴（verified #45）
- 文件：`.github/workflows/ci.yml:20`
- 问题：CI 与本地 Python 版本不一致且依赖未锁定。
- 证据：ci.yml:20 `python-version: "3.12"`，本地 .venv 为 3.14；requirements.txt 无 pin。已实测两版本下 139 个测试均通过，当前无回归。
- 影响：本地绿 ≠ CI 绿的未来可能；私有 API 用例（P2-23）在依赖升级时漂移风险最大。
- 修复建议：CI 加 3.14 job 或统一版本；对 httpx/pydantic/fastapi 做最低版本约束。
- 置信度：高（事实）；影响评估为推测
- verifier 结论：【保留】

### P3-20 safe_request 每跳 2 次 DNS 解析且无进程内缓存

- 来源：performance 轴（verified #51）
- 文件：`app/util.py:349-351、360-366`
- 问题：safe_request 每跳 2 次 DNS 解析且无进程内缓存。
- 证据：:349-351 `validate_outbound_url_async`（1 次 getaddrinfo）+ :360-366 `_bind_outbound_host`（再 1 次 `asyncio.to_thread(socket.getaddrinfo)`）。LAN 模式（allow_private）会跳过。
- 影响：token 模式直连下搜索任务数千请求 DNS 开销翻倍。
- 修复建议：对已校验域名做短 TTL 内存缓存，复用解析结果。
- 置信度：高
- verifier 结论：【保留】

### P3-21 下载历史投影 O(历史数×活动 torrent 数)，每次 dashboard 加载执行

- 来源：performance 轴（verified #52）
- 文件：`app/services/history.py:84-124、179-210`
- 问题：下载历史投影 O(n·m)。
- 证据：:84-124 `_active_history_matches` 对每个活动 torrent 对 eligible（至多 5000 行）做 hash/name/identity 三次 list 推导；identity 匹配内每个候选调 `strict_torrent_matches_item` 多正则；:179-210 每次调用取 5000 行+一次 Transmission RPC。
- 影响：5000 历史×数十活动下载=数十万次正则，dashboard 每次加载都触发。
- 修复建议：按 submission_hash/torrent_name 建 Python 索引降为哈希查找。
- 置信度：高
- verifier 结论：【保留】

### P3-22 /api/sites 对全表 search_attempts 做无 LIMIT 聚合

- 来源：performance 轴（verified #53）
- 文件：`app/api/sites.py:64-82`
- 问题：/api/sites 对全表 search_attempts 做无 LIMIT 聚合。
- 证据：:66-76 `LEFT JOIN search_attempts a ON a.site_id=s.id GROUP BY s.id`，覆盖整个 30 天保留窗口无时间窗/分页。
- 影响：重度使用下该接口聚合几十万行，随数据线性变慢。
- 修复建议：加时间窗或物化站点统计。
- 置信度：高
- verifier 结论：【保留】

### P3-23 /api/search-tasks 列表对每个任务执行一条聚合查询（受限 N+1）

- 来源：performance 轴（verified #54）
- 文件：`app/api/search.py:75-97`
- 问题：/api/search-tasks 列表对每个任务执行一条聚合查询。
- 证据：:89-96 Python 循环内每任务 `SELECT ... FROM search_attempts WHERE task_id=?`（≤100 次）。
- 影响：≤100 次小查询/请求，规模有限，可接受但可合并。
- 修复建议：一次 `WHERE task_id IN (...)` GROUP BY。
- 置信度：高
- verifier 结论：【保留】

### P3-24 站点搜索每个请求新建 httpx.AsyncClient，无连接复用

- 来源：performance 轴（verified #55）
- 文件：`app/clients.py:389、428、533、559、671`
- 问题：站点搜索每个请求新建 httpx.AsyncClient，无连接复用。
- 证据：TorznabClient.search:389、RSSClient.search:428、MTeamClient.search:533/559、NexusPHPClient.search:671 均 `async with httpx.AsyncClient(...)` 新建。
- 影响：跨查询/跨站点无连接池复用，DNS/握手收益丢失。
- 修复建议：按 adapter/站点复用模块级 AsyncClient（safe_request 已处理 headers 恢复），或 search_one_site 内共享。
- 置信度：高
- verifier 结论：【保留】

### P3-25 searchable_playlist_items 的 O(downloading×items) 匹配 + 每次调用一次 Transmission RPC

- 来源：performance 轴（verified #56）
- 文件：`app/services/search.py:99-104`
- 问题：download 匹配循环 + 每次调用一次 Transmission RPC。
- 证据：:99-104 `for torrent in downloading: for item in rows: ... torrent_matches_item(item, torrent_title)`；:65-75 每次调用新建 TransmissionClient 并 RPC（timeout 6s）。
- 影响：2000 部×100 活动下载≈20 万次正则，片单每次刷新发生；Transmission 不可达时接口逼近 6s。
- 修复建议：按 normalized_download_name 建索引匹配；RPC 结果短缓存。
- 置信度：高
- verifier 结论：【保留】

### P3-26 site_icon_cache 只有条数上限无字节上限

- 来源：performance 轴（verified #57）
- 文件：`app/state.py:32-34、175-180`
- 问题：site_icon_cache 只有条数上限无字节上限。
- 证据：:175-180 `while len(site_icon_cache) > MAX_SITE_ICONS: popitem`（128 条）；单图标上限 512KB（sites.py:33）→ 理论驻留约 64MB；对比 poster_cache 有 64 条+32MB 双上限（:183-189）。
- 影响：极端场景（128 站点都接近上限）内存偏高，属边界场景。
- 修复建议：与 remember_poster 一致加总字节上限。
- 置信度：高
- verifier 结论：【保留】

### P3-27 候选列表容器 aria-live + 每轮全量 innerHTML 重建

- 来源：performance 轴（verified #58）
- 文件：`app/static/index.html:200`、`app/static/app.js:695-728`
- 问题：候选列表容器 aria-live + 每轮全量 innerHTML 重建。
- 证据：index.html:200 `<div id="candidates" ... aria-live="polite" aria-busy="false">`；app.js:695-706 仅签名守卫，:707+ `container.innerHTML = filtered.map(...).join("")` 整段替换（至多 5000 行）。
- 影响：搜索进行中每 1.4s 数千节点 live region 重建，读屏与低端设备卡顿。
- 修复建议：移除容器级 aria-live，改 role="status" 摘要区；列表增量/分页渲染。
- 置信度：高
- verifier 结论：【保留】

### P3-28 dashboard 每次加载最多 6 次 Emby 回填 + 4 个下游健康检查

- 来源：performance 轴（verified #59）
- 文件：`app/api/playlists.py:49-76`、`app/api/system.py:215-232`
- 问题：dashboard 每次加载触发 Emby 回填与下游健康检查，无 TTL 缓存。
- 证据：playlists.py:46-61 overview 内 `hydrate_recent_emby_posters` 对缺失 emby_item_id 的近期影片逐个 library_details（semaphore=3）；system.py:215-232 /api/connection 每次并行检测 TMDB/Emby/Transmission/MoviePilot（各 timeout 6s）；均无进程内 TTL 缓存。
- 影响：下游故障时进入 dashboard 可能等待约 6s；频繁切页重复触发。
- 修复建议：Emby 回填与连接检测结果加 30-60s TTL 缓存。
- 置信度：高
- verifier 结论：【保留】

### P3-29 run_search 每影片/每候选独立 SQLite 连接与事务（写放大）

- 来源：performance 轴（verified #60）
- 文件：`app/services/search.py:151-156、203-211、484-497、514`
- 问题：run_search 每影片/每候选独立 SQLite 连接与事务。
- 证据：task_log:151-156、record_search_attempt:203-211、每候选 INSERT:484-497、update_task:514 均独立 `with connect()`。
- 影响：2000 部任务约 1-2 万个独立事务，WAL 写放大；多任务并发时叠加清理写锁竞争。
- 修复建议：影片粒度内合并事务/批量 INSERT。
- 置信度：高
- verifier 结论：【保留】

---

## 七、P4（security 轴自有低危等级，2 项）

### P4-1 /api/settings 明文返回 Transmission 用户名 tr_username

- 来源：security 轴（verified #30）
- 文件：`app/config.py:167`
- 问题：`/api/settings` 明文返回 Transmission 用户名 `tr_username`，破坏"密钥不回传"约定。
- 证据：config.py:167 `"tr_username": self.tr_username`（其余密钥类字段均为空串+`*_configured` 布尔）。
- 影响：需认证才可见；用户名低敏感，但破坏一致性约定、可能辅助弱口令尝试。
- 修复建议：改为 `tr_username_configured` 布尔，前端改占位符。
- 置信度：高
- verifier 结论：【保留】

### P4-2 sanitize_sensitive_text 为键名启发式，自定义头/含空格形式值会残留

- 来源：security 轴（verified #32，【修正】）
- 文件：`app/security.py:7-24`
- 问题：`sanitize_sensitive_text` 为键名启发式，非敏感键名携带的值不被脱敏；含空格形式只脱敏到首个空格前。
- 证据（verifier 实际运行验证）：`'headers="X-Session: xyz"'`、`'X-Tracker-Session: abc123'`、`'session=abc123'` 均原样返回；`'cookie=abc def'` → `'cookie=*** def'`（空格截断确认）。修正点：报告示例 `'Cookie: session=abc123' 不会被遮蔽` 不成立——实测返回 `'Cookie: ***'`（SENSITIVE_HEADER_LINE，security.py:12-13 覆盖 Cookie/Set-Cookie/Authorization 前缀行）。
- 影响：个别站点协议自定义头/键的值可能残留于日志与错误消息；属"尽力而为+纵深防御"边界。
- 修复建议：文档明确启发式边界；URL 类错误整体截断到安全格式。
- 置信度：高（行为实测）；示例已修正
- verifier 结论：【修正】

---

## 八、审计统计

| 等级 | 数量 |
| --- | --- |
| P0 Critical | 0 |
| P1 High | 3 |
| P2 Medium-High | 25 |
| P3 Medium-Low | 29 |
| P4（security 轴低危） | 2 |
| **总 finding 数** | **59** |

验证过程留痕：验证前 60 项 → 验证后 59 项（删除误报 1 项：security 轴"token 模式下同站点私网图标被拒与白名单机制不一致"——经核实与站点地址校验走同一 `host_is_allowlisted` 机制，行为一致且为有意设计；合并 0 项——五轴无真正跨轴重复；修正 7 项——ReDoS 示例、脱敏示例、tests 轴 4 处行号、architecture 轴"4 处重复"→"2 处逐字节+2 处变体"）。

关键行为均已实测：ReDoS 规则 `(?:(?:a)+)+` 对 29 字符标题耗时 7.2s（P2-3）；`sanitize_sensitive_text` 对 6 组示例字符串逐项验证（P4-2）；139 项 unittest 在 Python 3.12/3.14 下全部通过。

---

*本报告为当前审计的最终基线。后续任何修复、复审或变更均应以本文件为准更新；未经新一轮审计确认的问题不得添加，已确认问题不得删除。*
