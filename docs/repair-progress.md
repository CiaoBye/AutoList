# AutoList 修复进度与发布验收记录

更新时间：2026-08-13
当前版本：`1.16`（已发布）
当前分支：`main`
代码发布提交：`9495fb3`（后续提交仅补充发布记录与固定图标引用）

## 当前结论

本轮已经完成代码库、配置、测试、Docker/Compose、Unraid 模板和主要文档的完整审计，并将后端安全性、任务一致性、媒体资源处理和主题化 UI 修复写入 `1.16`。GitHub `main` 已推送，Unraid 唯一生产容器已切换并通过生产验收。

发布提交与生产镜像均已完成；部署前持久化数据、`.env`、旧模板和容器配置已备份，旧 `autolist:1.14` 容器与镜像已在验收通过后删除。

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
- 发布验收已完成：844×390 与 1024×768 视口记录、Unraid 构建切换、生产健康、挂载、数据完整性、设置脱敏、Logo 资源、只读连接和 GitHub `main` 均已完成。

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
- 已补做 844×390 横屏与 1024×768 窄桌面记录：三套主题共 48 个路由检查无横向溢出、路由错误或控制台错误；生产页面 8 个路由、三套主题和控制台也已验收通过。
- 生产只读连接检测：TMDB、Emby、Transmission、MoviePilot 均返回 `ok=true`；没有执行真实下载或外部写入。
- 真实下载任务；本轮不会以真实下载作为测试手段。

## 发布与部署状态

- GitHub：提交 `9495fb3` 已推送 `main`；后续图标引用和发布记录提交会继续推送到同一分支。
- Unraid：唯一 `Autolist` 容器运行 `autolist:1.16` 且为 `healthy`；旧 `autolist:1.14` 容器和镜像已删除。
- 版本：代码、静态资源、Compose、Dockerfile、Unraid 模板和生产镜像统一为 `1.16`。
- 部署前备份：`/mnt/cache/appdata/Autolist/backups/1.16-predeploy-20260813T143224Z`。
- 部署约束：只保留一个 AutoList 业务容器；保留 Unraid 数据目录和 `.env`，不将其纳入构建上下文。
- 本记录不包含 SSH 密码、Cookie、API Key、访问令牌或其他敏感值。

## 后续维护

1. 继续保留当前备份与版本化镜像策略；任何新改动仍须先跑回归、再推送 `main` 和更新唯一 Unraid 容器。
2. 真实下载仍不作为自动化验收动作；若要验证下载链路，应由操作者明确选择测试影片并在 MoviePilot/Transmission 侧观察整理结果。

## 回滚方式

如需回滚，使用备份目录中的旧数据库/WAL、`.env` 和模板，并从 GitHub 的已验证提交重新构建旧镜像；仅替换 AutoList 镜像，不删除 `/mnt/user/appdata/Autolist/data`，不覆盖当前 `.env`。
