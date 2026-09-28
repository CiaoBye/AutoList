# AutoList 协作规范

## 沟通与范围

- 默认使用简体中文沟通。
- 修改前先明确目标、影响范围、文件清单与验收方式。
- 采用最小必要改动，不做无关重构，不随意增加依赖。
- 不在源码、日志、文档或测试输出中写入 API Key、密码、Cookie、Token 等敏感信息。

## 版本与更新日志

- 功能更新、问题修复或可交付界面调整，每次递增 `0.01`，例如 `1.47` → `1.48`。
- 版本号需同步：`app/config.py` 的 `APP_VERSION`、`frontend/package.json` 与 `package-lock.json`（写作 `1.48.0`）、`README.md` 当前版本。`compose.yml` 使用 `latest` 标签，CI 发布时从 `APP_VERSION` 读取版本号，同时推送版本标签与 `latest`。
- 每次版本更新必须同步写入 `CHANGELOG.md`，说明新增、调整、修复和验证结果；README 只描述当前行为，不记录历史版本。

## 架构与界面

- 界面源码在 `frontend/`（Preact + Vite + TypeScript），构建到 `app/static/ui/`，不入库。
- 页面：藏馆 `#/`（只做总览）、片单 `#/films[/:id]`、挑选 `#/pick`、动态 `#/timeline`、设置 `#/settings/:section`，均为 hash 路由，必须支持直接访问、刷新恢复以及浏览器前进和后退。
- 影片状态只在服务端计算（`app/services/films.py`），前端不自行推断。
- 主题只有 `archive` 与 `cinema`，只切换 `frontend/src/tokens.css` 中的变量；`app.css` 不写 `[data-theme` 选择器。
- 桌面端与移动端都要检查导航、弹窗、空状态和横向溢出。
- 密钥只允许在服务端处理，前端接口仅返回是否已配置；密钥输入留空表示保留原值，清除需显式发送 `clear_<字段>: true`。

## 验证与部署

- 修改 Python 后运行 `python3 -m compileall -q app` 与 `.venv/bin/python -m unittest discover -s tests`。
- 修改前端后在 `frontend/` 运行 `npm run build`（含 `tsc --noEmit` 类型检查与 Vite 构建）。
- 完成前运行 `git diff --check`。
- 至少验证健康检查、设置脱敏、TMDB / Emby / Transmission / MoviePilot 连接和现有数据完整性。
- 下载只经 MoviePilot 提交到 Transmission，由其写入 `MOVIEPILOT` 与站点名标签，供 MoviePilot 接管整理；AutoList 不直连 Transmission 添加任务。
- 界面更新必须用浏览器在 1440、1024、768、375px 下检查全部页面，并确认控制台无错误。
- **新版本默认部署**：每次递增版本并通过上述验证后，直接执行 `bash scripts/deploy-fnos.sh` 部署到飞牛，无需再征求同意；验证未通过时不部署。
- 部署前先在容器内用 SQLite 备份接口备份数据库（`/data/playlist-autodown.db.bak-<当前版本>-<日期>`，权限改为 `600`）；部署后确认健康检查、版本号、设置脱敏、各服务连接与数据完整性，并用浏览器检查线上页面。
- Docker 部署只运行一个 AutoList 容器；完成后只保留当前 AutoList 业务镜像（核对悬空镜像的 Cmd 含 `app.main:app` 后再删除）。
- 数据目录和 `.env` 不进入构建上下文，不覆盖远端持久化数据；未经用户同意不修改生产数据。

## 文档

- 仓库只保留 `README.md`（当前行为与部署）、`CHANGELOG.md`（版本记录）、`AGENTS.md`（协作规范）与 `CONTEXT.md`（领域术语），不新增审计报告、路线图、截图或原型等历史文档；过时内容直接删除或改写。
- 规格、票据、测试与界面文案使用 `CONTEXT.md` 中的术语；新概念先补充到 `CONTEXT.md`，不要悄悄创造同义词。
- 需求规格与实现票据保存在本地 `.scratch/<feature-slug>/`（`spec.md` 与 `issues/<NN>-<slug>.md`，顶部保留 `Status:` 行），不入库，完成后删除。
