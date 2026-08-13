# AutoList 修复进度与发布验收记录

更新时间：2026-08-14
当前版本：`1.17`（修复进行中，尚未发布）
当前分支：`main`
代码基线提交：`a47e395`

## 当前结论

上一轮已完成代码库、配置、测试、Docker/Compose、Unraid 模板和主要文档的完整审计，并将后端安全性、任务一致性和资源处理修复发布到 `1.16`。本轮针对首页信息密度、地图视口、异常 SVG 和主题一致性形成 `1.17` 修复集；代码回归与本地浏览器验收已完成，当前仍需提交、GitHub 推送和 Unraid 更新。

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
- 上一轮 `1.16` 的生产验收记录已保留；本轮 `1.17` 仅在隔离临时数据实例执行浏览器验收，不把本地结果冒充生产状态。
- 1.17 本轮：统一左侧导航、移动端导航、页面标题、首页正文和来源地图说明；主题菜单说明改为只描述色彩搭配。
- 1.17 本轮：地图视口写入具体 `translate3d(...) scale(...)`，修正缩放／双指锚点，升级视口存储键并取消首次自动选站；选中站点不再改变 Logo 尺寸。
- 1.17 本轮：为 `inline-icon` 及规则、历史、地图、空状态等裸 SVG 增加显式尺寸和安全描边；主题 CSS 增加统一结构契约，保留主题色彩差异。
- 1.17 本轮补充：来源检索候选列改为可收缩轨道，修复 1280px Ledger 横向溢出；来源地图提示层避开缩放控件，横向／纵向按钮统一读取地图状态；待入馆、操作日志和来源档案室文案收敛到电影藏馆语义。
- 1.17 本轮补充：选中站点及手机端节点保持与普通节点相同的 Logo 尺寸，取消 hover 造成的二次放大。
- 1.17 本轮收口：三套主题在 1280px、1024px、390px 下的主要页面结构完成自动化实机对比；残留的检索表单、候选列表、日志边框和线性来源目录差异已统一为共享结构，仅保留色彩、材质和对比度差异。

## 测试与验证状态

### 已执行

以下是本轮已完成的静态检查与回归：

```text
.venv/bin/python -m unittest discover -s tests -v  # 174 项
python3 -m compileall -q app
node --check app/static/app.js
node --check app/static/js/core.js
node --check app/static/js/site-map.js
node --check app/static/js/theme-init.js
git diff --check
```

### 当前验证结果与剩余验收

- `.venv/bin/python -m unittest discover -s tests -v`：174 项通过。
- `python3 -m compileall -q app`、`node --check app/static/app.js`、`node --check app/static/js/core.js`、`node --check app/static/js/site-map.js`、`node --check app/static/js/theme-init.js`、`git diff --check`：通过。
- 隔离服务由 Codex 内置浏览器实测：1280px 桌面、1024×768 窄桌面、390×844 手机竖屏、844×390 横屏；8 个路由 × 3 个主题均只激活对应页面，无横向溢出、无可见超大 SVG，左侧／移动导航和页面正文保持统一。桌面与窄桌面结构差异为 0，手机三主题结构差异为 0。
- 地图实测：缩放按钮、视口 transform、选中节点、提示层避让、横向／纵向布局切换、编辑排布按钮和手机节点尺寸均通过；未执行真实下载、第三方写入或生产连接测试。

## 发布与部署状态

- GitHub：当前基线为 `a47e395`；1.17 尚未提交和推送，完成最终差异检查后推送 `main`。
- Unraid：当前生产容器仍运行 `autolist:1.16` 且为 `healthy`；1.17 尚未切换。
- 版本：工作树代码、静态资源、Compose、Unraid 模板和 CHANGELOG 已开始统一为 `1.17`，生产镜像尚未更新。
- 部署前备份：`/mnt/cache/appdata/Autolist/backups/1.16-predeploy-20260813T143224Z`。
- 部署约束：只保留一个 AutoList 业务容器；保留 Unraid 数据目录和 `.env`，不将其纳入构建上下文。
- 本记录不包含 SSH 密码、Cookie、API Key、访问令牌或其他敏感值。

## 后续维护

1. 继续保留当前备份与版本化镜像策略；任何新改动仍须先跑回归、再推送 `main` 和更新唯一 Unraid 容器。
2. 真实下载仍不作为自动化验收动作；若要验证下载链路，应由操作者明确选择测试影片并在 MoviePilot/Transmission 侧观察整理结果。

## 回滚方式

如需回滚，使用备份目录中的旧数据库/WAL、`.env` 和模板，并从 GitHub 的已验证提交重新构建旧镜像；仅替换 AutoList 镜像，不删除 `/mnt/user/appdata/Autolist/data`，不覆盖当前 `.env`。
