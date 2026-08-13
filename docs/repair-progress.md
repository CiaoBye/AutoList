# AutoList 修复进度与发布前验收记录

更新时间：2026-08-13
当前版本：`1.16`（发布候选）
当前分支：`main`
当前 HEAD：`ddc3e05 fix: refine themed map interactions and dashboard contrast`

## 当前结论

本轮已经完成代码库、配置、测试、Docker/Compose、Unraid 模板和主要文档的完整审计，并将后端安全性、任务一致性、媒体资源处理和主题化 UI 修复写入当前工作树。当前代码已进入 `1.16` 发布候选，GitHub 与 Unraid 发布验收仍需完成后才视为交付。

当前工作树包含本轮业务、测试、部署和文档修改；尚未提交 GitHub、尚未推送 `main`，也尚未构建或切换 Unraid 镜像。

## 已完成的审计范围

- FastAPI 启动流程、鉴权中间件、路由、Schema 和错误处理。
- SQLite 初始化、索引、任务状态、并发写入和历史数据兼容。
- TMDB、Emby、Transmission、MoviePilot、RSS/Torznab、CookieCloud 适配器。
- 自动化调度、识别任务、媒体库扫描、下载车和候选资源流程。
- 前端 HTML、JavaScript、主题初始化、三套主题 CSS、响应式布局、弹窗和站点地图交互。
- Python/JavaScript 测试、Dockerfile、Compose、CI、Unraid 模板、README 和 CHANGELOG。

## 已写入工作树的修复

### 后端任务与数据一致性

- `app/database.py`
  - 启动时收敛旧版本遗留的重复 active 任务。
  - 为自动化、识别、媒体库扫描任务增加按片单的 active partial unique index。
- `app/services/automation.py`
  - 自动化任务消费增加原子 claim，降低重复执行风险。
  - Transmission 状态未知时将任务标记为 `blocked`，避免空跑或误报成功。
  - 调度器增加 heartbeat、异常、成功和停止状态记录。
- `app/state.py`
  - 增加 scheduler health 状态和 heartbeat 诊断信息。
- `app/api/playlists.py`
  - 识别任务和媒体库扫描使用 `BEGIN IMMEDIATE`，并处理唯一约束冲突。
- `app/api/cart.py`
  - 下载车 toggle 的检查与写入放入数据库事务，降低并发重复加入风险。

### 鉴权、出站请求和资源大小

- `app/security.py`
  - 增加短期 HMAC 媒体签名 URL。
- `app/main.py`
  - 允许签名媒体资源在开启访问令牌时通过 URL 签名访问，解决 `<img>` 无法自动附带自定义 Header 的问题。
- `app/api/sites.py`
  - 站点图标改为签名资源地址。
  - 图标代理增加响应大小限制。
  - 站点重名更新返回 `409`，不再被转换为 `500`。
- `app/api/playlists.py`
  - Emby 海报使用签名资源地址。
- `app/util.py`
  - CookieCloud request body 改为增量读取并限制大小。
  - 出站响应增加统一大小限制入口。
- `app/api/system.py`
  - `/api/health` 暴露 scheduler health。
  - CookieCloud 使用受限 body 读取。
- `app/clients.py`
  - RSS/Torznab 的非数字 `enclosure.length` 单条容错为 `0`，避免坏条目导致整次搜索失败。
  - Emby 海报响应增加大小限制。
  - 共享搜索连接池在应用关闭和测试隔离清理时显式关闭，避免连接泄漏和 Python 3.14 `ResourceWarning`。
- `app/schemas.py`
  - 为 URL、Cookie、API Key、User-Agent 等外部输入增加长度限制。
- `Dockerfile`、`compose.yml`
  - healthcheck 增加调度器状态检查。

### 前端当前修改

- `app/static/index.html`
  - 站点页增加“编辑节点排布”入口。
- `app/static/app.js`
  - 地图文案改为说明缩放、平移、节点进入档案和排布模式。
  - 增加 `siteMapLayoutEdit` 状态，并同步按钮的 `aria-pressed`、标签和地图 CSS 状态。
  - 节点和线性来源列表增加选中状态表达，避免仅依赖颜色区分。
- 地图状态动作已经集中到 `app/static/js/site-map.js`，编辑模式支持节点拖动、点击抑制、pointer capture 释放、位置持久化、恢复和键盘操作；实机发现的 `setSiteMapViewport` 漏导入也已补齐。
- `app/static/style.css`、`app/static/theme.css` 已收口页面横向溢出、地图移动端 `touch-action`、路线错误条、Cinema/Ledger 主题控件尺寸、错误文字语义和焦点环。
- `tests/` 已补齐调度器、CookieCloud body、出站响应、媒体签名、RSS 坏字段、站点重名、任务唯一性、RuntimeSettings、DNS 绑定和重定向等专项用例。
- 剩余事项集中在发布验收：844×390 与 1024×768 视口记录已补做，仍需完成 Unraid 构建切换、生产健康与数据完整性、GitHub main 推送。

## 测试与验证状态

### 已执行

以下是修复前基线和部分本轮代码检查，均已通过：

```text
.venv/bin/python -m unittest discover -s tests -v  # 108 项
python3 -m compileall -q app
node --check app/static/app.js
node --check app/static/js/core.js
node --check app/static/js/theme-init.js
git diff --check
```

### 当前验证结果与剩余验收

- 最新工作树已执行 `.venv/bin/python -m unittest discover -s tests -v`，173 项通过；`compileall`、四个前端 `node --check`、`pip check`、`git diff --check` 通过。
- 隔离服务已启动并由 Codex 内置浏览器实测 1280px 桌面和 390×844 移动端；三主题桌面路由、来源节点档案、地图缩放／平移、移动端滚动与操作模式、横纵切换、节点编辑键盘确认／撤销已验证。实测中发现并修复 `setSiteMapViewport` 漏导入，修复后无新增控制台错误。
- 已补做 844×390 横屏与 1024×768 窄桌面记录：三套主题共 48 个路由检查无横向溢出、路由错误或控制台错误；仍需完成 Unraid 构建、切换、健康、挂载、数据、设置脱敏和只读连接检查。
- TMDB、Emby、Transmission、MoviePilot 的真实连接。
- 真实下载任务；本轮不会以真实下载作为测试手段。

## 发布与部署状态

- GitHub：未提交，未推送 `main`。
- Unraid：未构建，未切换镜像，未删除或覆盖持久化数据。
- 版本：代码与发布文件已统一为 `1.16`；生产镜像尚未切换。
- 部署约束：只保留一个 AutoList 业务容器；保留 Unraid 数据目录和 `.env`，不将其纳入构建上下文。
- 本记录不包含 SSH 密码、Cookie、API Key、访问令牌或其他敏感值。

## 下一步顺序

1. 只读检查 Unraid，备份数据库/WAL、旧模板与容器配置。
2. 构建并切换唯一 `Autolist` 容器到 `autolist:1.16`，验证健康、端口、挂载、设置脱敏和现有数据。
3. 清理旧业务镜像，提交并推送 GitHub `main`，最后回写生产验收结果。

## 回滚方式

当前修改尚未提交，代码回滚前应先保存工作树差异；发布后如需回滚，使用上一个已验证的 Git 提交和业务镜像标签，仅替换 AutoList 镜像，不删除 `/data` 持久化目录，不覆盖 `.env`。
