# AutoList 中长期开发与验证路线图

> 来源：2026-08-21 全仓审计（v1.18 → v1.19 修复批次之后）。本文档是中长期改进项的**唯一跟踪入口**：每项包含目标、任务分解、验收标准与验证方法。完成一项就在「状态」列标记并回填验证证据到 CHANGELOG。
>
> 短期批次（1.19）已完成：设置弹窗错误路由修复、服务状态按钮无关重绘移除、`ALLOWED_PRIVATE_HOSTS` 死变量删除、片单明细/可搜索队列请求取消、`[aria-busy]` 视觉加载态、style.css 约 140 行死代码清理、route-error 边框 token 化、弹窗遮罩统一 `--theme-backdrop`。

## 总原则

- 每次改动遵循 AGENTS.md：最小必要改动；改 Python 后 `python3 -m compileall -q app`；改 JS 后 `node --check`；版本递增 `0.01` 并同步 `config.py / compose.yml / unraid/my-Autolist.xml / .github/workflows/ci.yml / README.md / index.html（缓存参数+侧栏）/ tests/test_regressions.py:117-118（缓存参数断言）` 与 `CHANGELOG.md`。
- 界面改动必须完成浏览器矩阵：1280px、1024×768、390×844（竖屏）、844×390（横屏）× 三主题 × 八路由，控制台无错误。
- 不引入新框架、不加构建步骤，除非该项明确说明并获得一致同意。

---

## 中期（M 系列）

### M1 前端请求取消全面覆盖

- **状态**：部分完成（1.19 已覆盖 `loadPlaylistItems` / `loadSearchableQueue`）
- **目标**：所有"响应写入共享状态/渲染"的异步加载都具备取消或序号校验，杜绝旧响应覆盖新数据。
- **任务**：
  1. 盘点剩余加载函数：`loadOverview`、`loadPlaylists`、`refreshCart`、`refreshHistory`、`loadLogs`、站点列表渲染等；
  2. 对写入共享缓存或共享 DOM 的函数套用 1.19 模式：模块级 `AbortController` + 「新请求 abort 旧请求」+ 「仅最新 controller 可落地」（参考 app.js 中 `playlistItemsAbort` 实现）；
  3. dashboard 与 playlists 共享的 `#metric-items` 等元素写入前校验 `currentPage`。
- **验收**：快速连续翻页/切换筛选/在 dashboard↔playlists 间快速切换，最终 UI 数据始终与最后一次操作一致；DevTools Network 可见旧请求处于 canceled 状态。
- **验证**：`node --check app/static/app.js`；174 项测试通过；浏览器矩阵手动竞态测试；控制台无未捕获 AbortError。

### M2 前端公共抽象去重

- **目标**：收敛四组重复代码，降低 app.js 修改成本。
- **任务**：
  1. `createPollTask({fetch, interval, maxFailures, onError, onUpdate})`：统一候选轮询 / Emby 扫描轮询 / TMDB 识别轮询的「失败计数 + 3s 重试 + 10 次熔断 + 重试按钮 + aria-busy」骨架；
  2. `bindRovingTabs(container)`：统一三处 tablist 方向键导航；
  3. `trapFocus(container)`：合并两处焦点陷阱实现；
  4. `withButtonLoading(button, text, fn)`：替换约 30 处 `setButtonLoading + try/catch/finally + showToast` 样板；
  5. 合并 `playlistLibraryState` 缓存与 `candidateState` 的同构逻辑。
- **验收**：行为零变化（重构前后页面交互逐项对照）；app.js 净行数下降。
- **验证**：每完成一个抽象跑全量测试；浏览器矩阵回归轮询重试、tab 键盘导航、焦点恢复三条链路；CHANGELOG 记录净行数变化。

### M3 app.js 模块化拆分

- **依赖**：建议在 M2 之后进行（抽象层就位后拆分更安全）。
- **目标**：3960 行单文件按职责拆分，路由入口保留编排职能。
- **任务**：
  1. 目标结构：`js/pages/dashboard.js|playlists.js|search.js|cart.js|rules.js|sites.js|history.js|logs.js` + `js/polling.js` + `js/dialogs.js` + `js/settings.js`；app.js 保留 hash 路由、`navigate()` 编排与启动初始化；
  2. 模块间通信显式化：以 import/export 替代共享模块级变量，逐页迁移、每页一个提交；
  3. 更新 CI 的 `node --check` 清单与新文件列表；保持 `<script type="module">` 入口不变。
- **验收**：全部路由直接访问/刷新恢复/前进后退不回归；`handleDocumentClick` 拆分后事件委托映射表清晰可查。
- **验证**：每拆一页跑 `node --check` 全部新文件 + 174 测试（含 `test_frontend_module_contract_js_imports_resolve`）；最终浏览器矩阵全量回归。

### M4 移动端与体验补齐

- **任务**：
  1. ≤900px 顶栏恢复「导入片单」可达性：icon-only 按钮（保留 `aria-label`）或并入「更多」菜单，二选一后走版本流程；
  2. 入馆记录表格 680px 卡片化：为 `#history tbody tr` 补 `data-label`，复用 `.playlist-table` 的卡片化模式；
  3. 站点图标 `<img>` 补 `loading="lazy" decoding="async"`；同时给 img src 套 `safeExternalUrl` 协议白名单（当前仅 `<a href>` 使用）；
  4. 删除确认类交互从 `window.confirm/prompt` 迁移到现有 `<dialog>` 模式（保留焦点管理一致性）。
- **验收**：390px 宽度下历史页无需横向滚动即可读完全部字段；移动端任意页面两步内可达导入功能。
- **验证**：844×390 与 390×844 浏览器实测截图对比；`test_theme_ui_keeps_three_presets_and_shared_scene_hooks` 等契约测试通过；触控目标 ≥44px 抽查。

### M5 CSS 单一权威源治理

- **背景修正**：审计曾建议删除 theme.css 的 "1.18 theme contract" 结构段。经核实该段是被 `test_theme_variants_share_layout_and_keep_palette_contracts` 显式锁定的防御性收敛层（前面的主题布局分叉已在 1.16–1.18 删除并有 NotIn 断言保护），**不应直接删除**。真正目标是消除"同属性双权威源"。
- **任务**：
  1. 以 contract 段数值为基准，把桌面端布局真相合并进 style.css 对应规则（它们本就与 style.css 数值一致，属重申而非差异）；
  2. contract 段缩水为纯 token 覆盖后，更新契约测试断言（改为断言 style.css 含对应规则、theme.css 不再含 `grid-template-*` 布局属性）;
  3. 同步清理 style.css 中残余的死变量（如 ledger 未使用的 backdrop 覆盖已在 1.19 处理）。
- **验收**：三主题八路由布局像素级一致（对比截图）；theme.css 中不再出现布局属性。
- **验证**：更新后的契约测试 + 浏览器矩阵三主题对比截图；此项必须独立成版本，禁止与其他改动混批。

### M6 浏览器 E2E 冒烟测试进 CI

- **目标**：让 2995 行这类"契约测试抓不住的功能性 bug"有自动化防线。
- **任务**：
  1. 引入 Playwright（devDependency，不进生产镜像）；用 `uvicorn` 起隔离实例（`DATA_DIR` 指向临时目录，参照 tests/support.py 的环境准备）；
  2. 首批用例（冒烟级）：启动→导入 JSON 片单→创建搜索任务（mock 站点）→候选出现→加入下载车→设置弹窗打开且读取失败时错误落在设置弹窗内（锁定 1.19 修复）；三主题各加载一次 dashboard 断言无 console error；
  3. GitHub Actions 增加 job，失败阻塞合入。
- **验收**：CI 在 PR 上完整运行 <10 分钟；故意回滚 1.19 任一修复时对应用例变红。
- **验证**：本地 `npx playwright test` 全绿后再接入 CI；E2E 与单元测试分层文档写入本文件附录。

---

## 长期（L 系列）

### L1 compat.py 退役

- **目标**：删除 156 行测试兼容聚合层，测试直接从真实模块导入。
- **任务**：按符号分组迁移 `tests/` 导入路径 → 全部迁移后删除 `app/compat.py` → 从 `main = types.SimpleNamespace(...)` 相关引用中清理。
- **验收**：`grep -r "app.compat" tests/ app/` 为空；174 测试通过。
- **验证**：独立版本提交；`python3 -m compileall -q app` + 全量测试。

### L2 外部客户端连接池化

- **目标**：TMDBClient / EmbyClient / TransmissionClient / MoviePilotClient 复用连接，降低高片单量下的握手开销。
- **任务**：仿照 `_search_client()` 按 (base_url, timeout) 建模组级 AsyncClient 缓存；lifespan shutdown 统一 `aclose()`（已有 `close_search_clients` 模式可扩展）；注意 Emby api_key 走 query 参数、Transmission 有 session-id 握手语义，需各自验证。
- **验收**：批量识别/扫描任务中 TCP 连接数显著下降（抓包或 httpx event hook 计数）；全部连接检测与真实链路行为不变。
- **验证**：现有客户端相关测试全过 + 新增连接复用计数单测；对真实 TMDB/Emby 手工连通性验证（AGENTS.md 要求项）。

### L3 密钥静态加密（可选增强）

- **现状**：密钥明文落盘 `runtime-settings.json`（0600）与 SQLite `pt_sites` 表；数据目录与备份泄露即全泄。
- **任务**：评估方案——(a) 主密钥来自环境变量 `AUTOLIST_SECRET_KEY`，AES-GCM 加密封装敏感字段；(b) 保持明文但在备份文档中强制标注风险。倾向 (b) 起步 + (a) 作为 opt-in。
- **验收**：启用加密后设置页读写、站点 Cookie 自动刷新、PT 搜索均正常；`runtime-settings.json` 中不再出现明文 key。
- **验证**：新增加解密往返单测；备份恢复演练（docs/backup-restore.md 流程）后功能完好；此项涉及数据格式迁移，必须提供回退开关。

### L4 版本号单一来源注入

- **目标**：消灭 7+ 处手工版本同步点（本次 1.19 已暴露第二处：测试断言）。
- **任务**：`/` 路由返回 index.html 时以 `APP_VERSION` 模板替换 `?v=` 参数与侧栏标识（FileResponse 改 Response 渲染，注意 Cache-Control no-cache 语义保留）；CI 增加一致性脚本校验 compose/unraid/README 标签与 `APP_VERSION` 一致；测试断言改为从 config 读版本号而非硬编码。
- **验收**：升版只改 `APP_VERSION` + CHANGELOG 两处；CI 校验脚本能在不一致时失败。
- **验证**：模拟升版演练（1.19 → 1.20 dry-run）；全量测试 + CI 本地复现。

### L5 同步 SQLite 的执行模型评估

- **现状**：async 端点内直接调用同步 `connect()`。当前查询均有索引且有 LIMIT、单 uvicorn worker、自托管低并发，实际风险低——**先文档化假设，再谈改造**。
- **任务**：README 部署章节声明"单进程假设"；若未来出现慢查询或多 worker 需求，再评估 `asyncio.to_thread` 包装或 WAL 模式下的读写分离，不做预防性重构。
- **验收**：文档合入；性能基线（overview/candidates 接口 p95）记录在案供未来对比。
- **验证**：`curl` 计时抽样三次取均值记录到本文件附录。

---

## 附：每批次通用验证清单

1. `python3 -m compileall -q app`
2. `node --check app/static/app.js app/static/js/core.js app/static/js/theme-init.js app/static/js/site-map.js`（新增 js 文件加入清单与 CI）
3. `.venv/bin/python -m unittest discover -s tests`（174+ 项全绿）
4. CSS 改动：花括号配平检查 + 契约测试
5. 版本一致性 grep：`APP_VERSION` / compose / unraid xml / ci.yml / README / index.html
6. 界面改动：1280/1024/390×844/844×390 × 三主题 × 八路由，控制台无错误
7. 后端改动：健康检查、设置脱敏、TMDB/Emby/Transmission/MoviePilot 连接、现有数据完整性
8. CHANGELOG.md 条目含：新增/调整/修复/验证四节
