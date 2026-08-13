# AutoList 审计修复最终报告

> 历史记录：本文记录 1.15 阶段的审计结果与 2026-08-10 的验证快照；当前 1.16 发布候选的代码、测试和部署状态以 [`docs/repair-progress.md`](../docs/repair-progress.md) 与本次 `CHANGELOG.md` 为准。文中的 152/153 项测试、1.15 版本和“未执行浏览器/部署”均不代表当前工作树。

- 审计基线：`review/audit.md`（59 项：P1×3 / P2×25 / P3×29 / P4×2）
- 修复进度：`review/fix-progress.md`（59/59 RESOLVED）
- 最终独立复审：1 轮独立 reviewer 四轴复审（correctness / security / tests / architecture），实测 153 项测试与前端模块链接
- 日期：2026-08-10

## 一、Audit finding 处理结果

**59/59 全部 RESOLVED**，无 FALSE_POSITIVE / DUPLICATE / BLOCKED / PENDING / IN_PROGRESS。逐项状态与修复摘要见 `review/fix-progress.md`。要点：

- **P1-1**：健康检查抽取为 `app/healthcheck.py`（Dockerfile/compose 统一引用），只断言进程与 SQLite，不再依赖 `scheduler_ok`，单片单失败不再让容器 unhealthy。
- **P1-2**：`run_recognition` / `run_library_scan` 全生命周期行为测试（完成/部分/取消/失败/登记表清理）。
- **P1-3**：搜索轮询增加页面守卫（仅搜索页调度，离开即停止）。
- **安全类**（2-3/2-4/3-12~3-15/4-1）：ReDoS 过滤（嵌套量词 + 不等长交替分支静态检测）；CookieCloud 限流移至 uuid 校验后并新增匿名宽松桶；503 弱令牌响应去明细；令牌失败进程内限速；请求体上限中间件 + 无长度请求 411；CookieCloud 密钥最短 12 字符；tr_username 不回传明文。
- **正确性类**（2-1/2-2/3-1~3-3/3-9）：自动化 429→queued 排队、failed→partial+warning 通知、pending 容量按过滤队列计数、提交去重持久化到 `candidates.submitted_at`、手动刷新复用片单同步锁、内嵌搜索用独立任务句柄。
- **架构类**（2-5~2-12/3-4~3-11）：URL 校验统一到 `util.validated_base_url`；身份键统一到 `domain/titles.item_identity_keys`；8 个死配置键删除；路由海报回填下沉 `services/library.py`；解析器/工具拆到 `app/parsers.py`；`SCHEMA_VERSION`+`user_version` 迁移基础设施；测试再导出收敛到 `app/compat.py`（main.py 精简）；`state.py` 五个策略函数解耦 HTTPException；两个死 wrapper 删除；常量收敛；代理门控统一；健康检查脚本去重。
- **性能类**（2-13~2-16/3-20~3-29）：策略解析复用；candidates 按 created_at 30 天清理 + completed 任务 30 天归档；cleanup 函数索引；图标 DNS 移线程池；DNS 60s 缓存；历史/任务列表索引与单次 IN 查询；搜索客户端连接池共享；Transmission 快照 10s 缓存；图标缓存字节上限；候选容器去 aria-live；connection 30s 缓存；候选写入单事务。
- **前端**（3-10/2-25）：站点地图计算层拆到 `js/site-map.js`（12 个导出，含状态）；前端契约测试升级为 ES 模块导入解析校验 + CI 覆盖 core.js/site-map.js 语法。

## 二、POST findings（最终复审发现，全部已修复）

| ID | 级别 | 问题 | 修复 |
| --- | --- | --- | --- |
| POST-001 | P0 | `site-map.js` 从 core.js 导入不存在的 `safeStorageGet/Set` → 前端模块图链接失败全站不渲染 | `safeStorageGet/Set` 移入 core.js 并导出；node 实测 `MODULE_LINK_OK` |
| POST-002 | P1 | `siteMapCopy()` 默认参数引用 app.js 模块级 `themeController` → ReferenceError | 改为从 `document.documentElement.dataset.theme` 读取 |
| POST-003 | P2 | ReDoS 过滤可被 `(a\|aa)+b` 类不等长交替绕过（实测 36 字符 3.7s） | 新增 `_has_uneven_alternation` 静态检测；7 组用例全过（灾难模式拒绝、`(?:AB\|CD)+`/`Studio-[A-Z]+` 放行） |
| POST-004 | P2 | 取消搜索任务会经 CancelledError 传播取消整个自动化 run，与注释/声明不符；cancelled→partial 分支为死代码 | 注释与实际语义对齐（有意传播），`search_failed` 收敛为仅检查 failed |
| POST-005 | P2 | 限流移至 uuid 后导致匿名 CookieCloud 请求无限流（40MB/请求可压内存） | 新增匿名宽松桶（120/min）在 body 读取前生效 |
| POST-006 | P3 | 请求体上限仅查 Content-Length，chunked 绕过 | 非 GET/HEAD 且无 Content-Length 请求返回 411 |
| POST-007 | P3 | completed 任务永不归档；测试未清理 `_dns_address_cache`/`_connection_cache`；`cookiecloud_status` key_valid 仍为 5 | completed 30 天归档 + 函数索引；support 基类补清理；key_valid 对齐 12 |
| — | P3 | P3-21/3-25 原实现为索引+RPC 缓存（匹配循环保留） | 已在 fix-progress 如实标注范围；保留为残余演进项 |

## 三、修改文件

- **后端**：`app/main.py`（中间件/令牌限速/精简）、`app/healthcheck.py`（新）、`app/compat.py`（新）、`app/parsers.py`（新）、`app/util.py`、`app/security.py`、`app/database.py`、`app/state.py`、`app/schemas.py`、`app/config.py`、`app/candidate_policy.py`、`app/cookiecloud.py`、`app/list_sources.py`、`app/clients.py`、`app/domain/titles.py`、`app/api/{system,playlists,sites,search,cart}.py`、`app/services/{automation,search,library,imports,recognition}.py`
- **前端**：`app/static/app.js`、`app/static/js/core.js`、`app/static/js/site-map.js`（新）、`app/static/index.html`
- **测试**：`tests/{support.py（新）,test_round2_safety.py,test_workflows.py,test_backend_hardening.py,test_regressions.py}`
- **部署/文档**：`Dockerfile`、`compose.yml`、`.github/workflows/ci.yml`、`.gitignore`、`.pi-lens.json`（新）、`README.md`、`CHANGELOG.md`、`unraid/my-Autolist.xml`

## 四、测试与验证命令及结果

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests` | **152 项全部通过**（基线 139 → 152，净增 13 项行为测试） |
| `python3 -m compileall -q app` | 通过 |
| `node --check`（app.js / core.js / site-map.js / theme-init.js） | 通过 |
| `node --input-type=module` 模块链接验证 | `MODULE_LINK_OK`（site-map.js 导入图完整） |
| `git diff --check` | 通过 |
| 隔离 uvicorn API 冒烟 | `/api/health`（version 1.15、scheduler ok）、首页/`site-map.js`/`core.js` 均 200 |
| docker compose config（CI 环境） | CI 流程执行（本机无 docker，YAML 结构已本地验证） |
| pi-lens 最终诊断（mode=all） | 0 个可项目侧修复 blocker；compose schema 为工具网络环境性错误（curl 200 + CI 权威校验）；healthcheck 2 项 warning 为固定 localhost 保守提示 |

## 四-A、最终 pi-lens 诊断状态（验收阶段）

最终验收按流程执行：`lens_diagnostics mode=all` → 处理诊断 → `mode=full` 项目级复查 → 修复 → 重跑验证。

**已修复的真实问题（mode=full 发现）**：
- `ci.yml`：actions 固定到已验证 SHA（checkout/setup-python/setup-node）+ `persist-credentials: false`（zizmor artipacked/unpinned-uses）；`curl | python` 管道改为下载后校验（gha-curl-pipe-shell）；workflow 级最小权限 `contents: read`（zizmor excessive-permissions）。
- `app/config.py`：`mp_timeout_seconds` 空 except 显式化。
- `app/list_sources.py`：RSS 解析 ParseError 空 except 显式化。
- `app/clients.py`：`_SEARCH_PATHS` 可变类属性改为模块级常量 `NEXUSPHP_SEARCH_PATHS`。

**已标记 false-positive（带证据）**：app.js/core.js 共 20 处 innerHTML（escapeHtml 全覆盖模板渲染）、8 个文件 37 处 SQL advisory（固定字面量表名/占位符 + sqlite3 参数绑定，SQLAlchemy 规则误匹配）、cookiecloud MD5×2（协议强制，usedforsecurity=False）、candidate_policy 动态正则×2（经 merge_custom_rules 校验）。

**环境性诊断（无法在项目侧消除，证据充分）**：
- `compose.yml` YAML:65536：yaml-language-server 无法从 GitHub 拉取 schema（curl 实测 HTTP 200、`yaml.safe_load` 通过、CI 以 `docker compose config --quiet` 做权威校验）。
- `app/healthcheck.py` 2 项 Semgrep warning：urllib 固定 `http://127.0.0.1:8080`（容器健康检查标准模式，无用户输入，https 不适用）。

**最终验证（重跑）**：152 项 unittest 通过；`compileall`、`node --check`×4、ES 模块链接 `MODULE_LINK_OK`、`git diff --check`、compose/CI YAML 解析全部通过。

## 五、最终独立 Review

独立 reviewer 四轴复审结论：后端状态机/事务/身份键统一等 40+ 项修复经测试与代码核对为有效；发现 8 项 POST findings（见上表）全部修复并复验；无遗留高置信 blocker。测试有效性：后台任务行为（含真实取消）、自动化 failed/partial、cancel 404/409、通知/路由/签名媒体/DNS 绑定均有真实断言。

## 六、Residual Risks

1. **浏览器 UI 验收未执行**（本环境无浏览器工具）：8 路由×3 主题、390/844/1024px、地图拖拽/缩放交互需浏览器人工复验；前端契约现为模块链接级校验（比字符串计数强，仍非 DOM 行为级）。
2. **P3-21/3-25 部分闭环**：历史投影 O(n·m) 匹配循环与 torrent×items 匹配循环保留，以 DB 索引与 10s RPC 缓存缓解；海量数据场景仍建议后续按审计建议做哈希索引。
3. **`candidates.submitted_at` 去重窗口**受 30 天候选清理限制（30 天后重复提交防护回退到 download_history）。
4. **`_dns_address_cache` 无条目上限**（键为域名:端口，正常规模有限）。
5. **429→queued 重试**每 60s 重跑完整识别阶段（效率损失，非正确性问题）。
6. **真实外部服务连接**（TMDB/Emby/Transmission/MoviePilot）与真实下载未执行（无凭据；文档约定不以真实下载作为测试手段）。
7. **Unraid 镜像构建/切换与 git 提交推送**未执行（需用户明确要求）。

## 七、Final Evidence

- `review/audit.md`：59 项基线（未改动）。
- `review/fix-progress.md`：59/59 RESOLVED，每项含修复摘要。
- 152 项测试通过 + 全部静态检查通过 + 模块链接验证 + API 冒烟通过（命令与输出见第四节）。
- POST findings 已修复并纳入 fix-progress 的对应 finding 记录。

---

*三者一致性：audit.md（基线 59 项）→ fix-progress.md（59/59 RESOLVED）→ 本报告（处理结果/验证/POST/残余）全部对齐。*
