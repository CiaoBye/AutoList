# AutoList 审计修复进度

> 历史记录：本表对应 1.15 审计批次；当前 1.16 发布候选的进度与验证以 [`docs/repair-progress.md`](../docs/repair-progress.md) 为准。

- 审计基线：`review/audit.md`（59 项）
- 状态枚举：`PENDING` / `IN_PROGRESS` / `RESOLVED` / `FALSE_POSITIVE` / `DUPLICATE` / `BLOCKED`
- 更新顺序：每完成一项（或一组同根因项）即更新；本表为唯一修复进度权威记录。

## 状态总览

| ID | 级别 | 状态 | 修复摘要 | 验证 |
|---|---|---|---|---|
| P1-1 | P1 | RESOLVED | healthcheck 抽取 app/healthcheck.py，两处统一引用，只断言 ok 不依赖 scheduler_ok | 相关测试通过 |
| P1-2 | P1 | RESOLVED | 新增 BackgroundTaskTests：run_recognition/run_library_scan 完整行为（完成/部分/取消/失败）+ 登记表清理 | 相关测试通过 |
| P1-3 | P1 | RESOLVED | refreshCandidates 只在搜索页调度轮询；navigate 离开搜索页 clearTimeout | 相关测试通过 |
| P2-1 | P2 | RESOLVED | run_playlist_automation 捕获 429 → run 回 queued 由调度器消费；测试覆盖 | 相关测试通过 |
| P2-2 | P2 | RESOLVED | 搜索任务 failed/cancelled → run 报 partial + warning 通知；测试覆盖 | 相关测试通过 |
| P2-3 | P2 | RESOLVED | merge_custom_rules 增加 _has_nested_quantifier（配对括号检测嵌套量词），拒绝 (?:A+)+ 形；既有合法规则保持 | 相关测试通过 |
| P2-4 | P2 | RESOLVED | CookieCloud 限流移到 uuid 校验之后；测试覆盖 | 相关测试通过 |
| P2-5 | P2 | RESOLVED | validated_base_url 收敛为 util.validate_outbound_url 封装；system.py 删除重复实现 | 相关测试通过 |
| P2-6 | P2 | RESOLVED | domain/titles.py 提供 normalized_title_key/item_identity_keys，三处身份键统一 | 相关测试通过 |
| P2-7 | P2 | RESOLVED | 删除 scoring_policy 等 8 个死配置键（种子/白名单/Schema） | 相关测试通过 |
| P2-8 | P2 | RESOLVED | hydrate_recent_emby_posters 下沉到 services/library.py（其余编排下沉为后续演进，残余风险登记） | 相关测试通过 |
| P2-9 | P2 | RESOLVED | clients.py 解析器/数值工具拆到 app/parsers.py（含 site_proxy） | 相关测试通过 |
| P2-10 | P2 | RESOLVED | 引入 SCHEMA_VERSION + PRAGMA user_version 读写 | 相关测试通过 |
| P2-11 | P2 | RESOLVED | api/sites.py 不再依赖 api/system.py（validated_base_url 来自 util） | 相关测试通过 |
| P2-12 | P2 | RESOLVED | main.py 测试再导出收敛到 app/compat.py；tests 改从 compat 导入 | 相关测试通过 |
| P2-13 | P2 | RESOLVED | analyze_candidate 支持 policy 参数复用；run_search 单次解析策略 | 相关测试通过 |
| P2-14 | P2 | RESOLVED | candidates 按 created_at 30 天统一清理（含 completed 任务候选） | 相关测试通过 |
| P2-15 | P2 | RESOLVED | cleanup 的 datetime() 比较建函数索引 | 相关测试通过 |
| P2-16 | P2 | RESOLVED | validate_remote_icon_url 改用 asyncio.to_thread | 相关测试通过 |
| P2-17 | P2 | RESOLVED | CI 增加 node --check app/static/js/core.js | 相关测试通过 |
| P2-18 | P2 | RESOLVED | 新增 cancel_task 测试（404/409/200+cancelled 落库） | 相关测试通过 |
| P2-19 | P2 | RESOLVED | 新增路由行为测试：notifications、read-all、score_preview、cancel、connection、downloads | 相关测试通过 |
| P2-20 | P2 | RESOLVED | notifications 写入/读取/read-all 断言并入测试 | 相关测试通过 |
| P2-21 | P2 | RESOLVED | 错误路径测试：自动化 blocked/failed/partial、MoviePilot 抛异常落库失败历史、Transmission unknown | 相关测试通过 |
| P2-22 | P2 | RESOLVED | 删除 regressions 重复用例，round2 HTTP 端到端保留并补 emby_base_url 清除断言 | 相关测试通过 |
| P2-23 | P2 | RESOLVED | 代理用例改为 patch client.request（不依赖真实网络） | 相关测试通过 |
| P2-24 | P2 | RESOLVED | tests/support.py 共享基类统一清理进程级缓存/限流时间戳 | 相关测试通过 |
| P2-25 | P2 | RESOLVED | 新增 FrontendContractTests（ES 模块导入解析校验）+ CI core.js 检查 | 相关测试通过 |
| P3-1 | P3 | RESOLVED | pending 容量按已过滤队列计数 | 相关测试通过 |
| P3-2 | P3 | RESOLVED | 提交成功写入 candidates.submitted_at；历史清空不解除防重复 | 相关测试通过 |
| P3-3 | P3 | RESOLVED | refresh_playlist_source 复用 playlist_sync_lock | 相关测试通过 |
| P3-4 | P3 | RESOLVED | state.py 5 个策略函数解耦 HTTPException，路由层转换 | 相关测试通过 |
| P3-5 | P3 | RESOLVED | 删除两个 library_state 死 wrapper | 相关测试通过 |
| P3-6 | P3 | RESOLVED | MAX_SEARCH_ITEMS/MAX_IMPORT_ROWS 收敛到 schemas.py | 相关测试通过 |
| P3-7 | P3 | RESOLVED | README 补充配置优先级说明 | 相关测试通过 |
| P3-8 | P3 | RESOLVED | list_sources 四类来源统一 tmdb_proxy_enabled 门控 | 相关测试通过 |
| P3-9 | P3 | RESOLVED | 自动化内嵌搜索改用独立 search_task 句柄 | 相关测试通过 |
| P3-11 | P3 | RESOLVED | healthcheck 内联脚本抽取为 app/healthcheck.py | 相关测试通过 |
| P3-12 | P3 | RESOLVED | 503 弱令牌响应只返回统一提示 | 相关测试通过 |
| P3-13 | P3 | RESOLVED | 令牌失败进程内限速（60s/10 次 → 429） | 相关测试通过 |
| P3-14 | P3 | RESOLVED | 请求体上限中间件（非 /cookiecloud/ 5MB → 413） | 相关测试通过 |
| P3-15 | P3 | RESOLVED | CookieCloud key/uuid 最短 12 字符 | 相关测试通过 |
| P3-16 | P3 | RESOLVED | README 声明 MP_BASE_URL 需 HTTPS/受控内网 | 相关测试通过 |
| P3-17 | P3 | RESOLVED | tests/support.py 共享 IsolatedAppTestCase，round2 切换 | 相关测试通过 |
| P3-18 | P3 | RESOLVED | _FakeRequest.stream 注解改为 AsyncIterator[bytes] | 相关测试通过 |
| P3-19 | P3 | RESOLVED | CI 增加 Python 3.14 矩阵 job | 相关测试通过 |
| P3-20 | P3 | RESOLVED | DNS 解析短 TTL 缓存（60s） | 相关测试通过 |
| P3-21 | P3 | RESOLVED | download_history 增加 candidate/item 索引 | 相关测试通过 |
| P3-22 | P3 | RESOLVED | /api/sites 聚合加 30 天时间窗 | 相关测试通过 |
| P3-23 | P3 | RESOLVED | /api/search-tasks 聚合改为单次 IN 查询 | 相关测试通过 |
| P3-24 | P3 | RESOLVED | 搜索/检测类共享模块级 AsyncClient；测试 mock 适配 | 相关测试通过 |
| P3-25 | P3 | RESOLVED | Transmission 下载快照 10s TTL 缓存 | 相关测试通过 |
| P3-26 | P3 | RESOLVED | site_icon_cache 增加总字节上限（8MB） | 相关测试通过 |
| P3-27 | P3 | RESOLVED | 候选容器移除容器级 aria-live | 相关测试通过 |
| P3-28 | P3 | RESOLVED | /api/connection 30s TTL 缓存 | 相关测试通过 |
| P3-29 | P3 | RESOLVED | run_search 候选写入合并为单事务 executemany | 相关测试通过 |
| P4-1 | P4 | RESOLVED | tr_username 不回传明文（tr_username_configured 布尔 + 前端占位符） | 相关测试通过 |
| P4-2 | P4 | RESOLVED | README 说明脱敏键名启发式边界 | 相关测试通过 |
| P3-10 | P3 | RESOLVED | 地图计算层（视野/节点位置/缩放/状态）抽到 js/site-map.js（12 个导出），app.js 2679→2556 行并通过 ES 模块导入 | 153 项测试通过 |

## 详细记录

### P1-1 — RESOLVED

healthcheck 抽取 app/healthcheck.py，两处统一引用，只断言 ok 不依赖 scheduler_ok

### P1-2 — RESOLVED

新增 BackgroundTaskTests：run_recognition/run_library_scan 完整行为（完成/部分/取消/失败）+ 登记表清理

### P1-3 — RESOLVED

refreshCandidates 只在搜索页调度轮询；navigate 离开搜索页 clearTimeout

### P2-1 — RESOLVED

run_playlist_automation 捕获 429 → run 回 queued 由调度器消费；测试覆盖

### P2-2 — RESOLVED

搜索任务 failed/cancelled → run 报 partial + warning 通知；测试覆盖

### P2-3 — RESOLVED

merge_custom_rules 增加 _has_nested_quantifier（配对括号检测嵌套量词），拒绝 (?:A+)+ 形；既有合法规则保持

### P2-4 — RESOLVED

CookieCloud 限流移到 uuid 校验之后；测试覆盖

### P2-5 — RESOLVED

validated_base_url 收敛为 util.validate_outbound_url 封装；system.py 删除重复实现

### P2-6 — RESOLVED

domain/titles.py 提供 normalized_title_key/item_identity_keys，三处身份键统一

### P2-7 — RESOLVED

删除 scoring_policy 等 8 个死配置键（种子/白名单/Schema）

### P2-8 — RESOLVED

hydrate_recent_emby_posters 下沉到 services/library.py（其余编排下沉为后续演进，残余风险登记）

### P2-9 — RESOLVED

clients.py 解析器/数值工具拆到 app/parsers.py（含 site_proxy）

### P2-10 — RESOLVED

引入 SCHEMA_VERSION + PRAGMA user_version 读写

### P2-11 — RESOLVED

api/sites.py 不再依赖 api/system.py（validated_base_url 来自 util）

### P2-12 — RESOLVED

main.py 测试再导出收敛到 app/compat.py；tests 改从 compat 导入

### P2-13 — RESOLVED

analyze_candidate 支持 policy 参数复用；run_search 单次解析策略

### P2-14 — RESOLVED

candidates 按 created_at 30 天统一清理（含 completed 任务候选）

### P2-15 — RESOLVED

cleanup 的 datetime() 比较建函数索引

### P2-16 — RESOLVED

validate_remote_icon_url 改用 asyncio.to_thread

### P2-17 — RESOLVED

CI 增加 node --check app/static/js/core.js

### P2-18 — RESOLVED

新增 cancel_task 测试（404/409/200+cancelled 落库）

### P2-19 — RESOLVED

新增路由行为测试：notifications、read-all、score_preview、cancel、connection、downloads

### P2-20 — RESOLVED

notifications 写入/读取/read-all 断言并入测试

### P2-21 — RESOLVED

错误路径测试：自动化 blocked/failed/partial、MoviePilot 抛异常落库失败历史、Transmission unknown

### P2-22 — RESOLVED

删除 regressions 重复用例，round2 HTTP 端到端保留并补 emby_base_url 清除断言

### P2-23 — RESOLVED

代理用例改为 patch client.request（不依赖真实网络）

### P2-24 — RESOLVED

tests/support.py 共享基类统一清理进程级缓存/限流时间戳

### P2-25 — RESOLVED

新增 FrontendContractTests（ES 模块导入解析校验）+ CI core.js 检查

### P3-1 — RESOLVED

pending 容量按已过滤队列计数

### P3-2 — RESOLVED

提交成功写入 candidates.submitted_at；历史清空不解除防重复

### P3-3 — RESOLVED

refresh_playlist_source 复用 playlist_sync_lock

### P3-4 — RESOLVED

state.py 5 个策略函数解耦 HTTPException，路由层转换

### P3-5 — RESOLVED

删除两个 library_state 死 wrapper

### P3-6 — RESOLVED

MAX_SEARCH_ITEMS/MAX_IMPORT_ROWS 收敛到 schemas.py

### P3-7 — RESOLVED

README 补充配置优先级说明

### P3-8 — RESOLVED

list_sources 四类来源统一 tmdb_proxy_enabled 门控

### P3-9 — RESOLVED

自动化内嵌搜索改用独立 search_task 句柄

### P3-11 — RESOLVED

healthcheck 内联脚本抽取为 app/healthcheck.py

### P3-12 — RESOLVED

503 弱令牌响应只返回统一提示

### P3-13 — RESOLVED

令牌失败进程内限速（60s/10 次 → 429）

### P3-14 — RESOLVED

请求体上限中间件（非 /cookiecloud/ 5MB → 413）

### P3-15 — RESOLVED

CookieCloud key/uuid 最短 12 字符

### P3-16 — RESOLVED

README 声明 MP_BASE_URL 需 HTTPS/受控内网

### P3-17 — RESOLVED

tests/support.py 共享 IsolatedAppTestCase，round2 切换

### P3-18 — RESOLVED

_FakeRequest.stream 注解改为 AsyncIterator[bytes]

### P3-19 — RESOLVED

CI 增加 Python 3.14 矩阵 job

### P3-20 — RESOLVED

DNS 解析短 TTL 缓存（60s）

### P3-21 — RESOLVED

download_history 增加 candidate/item 索引

### P3-22 — RESOLVED

/api/sites 聚合加 30 天时间窗

### P3-23 — RESOLVED

/api/search-tasks 聚合改为单次 IN 查询

### P3-24 — RESOLVED

搜索/检测类共享模块级 AsyncClient；测试 mock 适配

### P3-25 — RESOLVED

Transmission 下载快照 10s TTL 缓存

### P3-26 — RESOLVED

site_icon_cache 增加总字节上限（8MB）

### P3-27 — RESOLVED

候选容器移除容器级 aria-live

### P3-28 — RESOLVED

/api/connection 30s TTL 缓存

### P3-29 — RESOLVED

run_search 候选写入合并为单事务 executemany

### P4-1 — RESOLVED

tr_username 不回传明文（tr_username_configured 布尔 + 前端占位符）

### P4-2 — RESOLVED

README 说明脱敏键名启发式边界

### P3-10 — RESOLVED

地图计算层拆分：`js/site-map.js` 承载视野/节点位置/缩放/持久化等 12 个导出（含状态），
app.js 通过 ES 模块导入；app.js 从 2679 行降至 2556 行。事件绑定与页面渲染仍驻留 app.js，
交互层进一步拆分登记为残余演进项。
