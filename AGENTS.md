# AutoList 协作规范

## 沟通与范围

- 默认使用简体中文沟通。
- 修改前先明确目标、影响范围、文件清单与验收方式。
- 采用最小必要改动，不做无关重构，不随意增加依赖。
- 不在源码、日志、文档或测试输出中写入 API Key、密码、Cookie、Token 等敏感信息。

## 版本与更新日志

- 功能更新、问题修复或可交付界面调整，每次递增 `0.01`，例如 `0.31` → `0.32` → `0.33`。
- 后端 `FastAPI` 版本、`compose.yml` 镜像标签和 `README.md` 当前版本必须保持一致。
- 每次版本更新必须同步写入 `CHANGELOG.md`，说明新增、调整、修复和验证结果。

## 架构与界面

- 工作台只承担全局总览，不堆放完整业务功能。
- 片单、资源搜索、下载车、候选规则和下载历史使用独立页面路由。
- 页面路由必须支持直接访问、刷新恢复以及浏览器前进和后退。
- 桌面端与移动端都要检查导航、弹窗、空状态和横向溢出。
- 密钥只允许在服务端处理，前端接口仅返回是否已配置。

## 验证与部署

- 修改 Python 后运行 `python3 -m compileall -q app`。
- 修改 JavaScript 后运行 `node --check app/static/app.js`。
- 至少验证健康检查、设置脱敏、TMDB/Emby/Transmission 连接和现有数据完整性。
- Transmission 直连任务必须包含 `MOVIEPILOT` 与站点名标签，供 MoviePilot 接管整理并追加“已整理”。
- 界面更新必须用浏览器检查全部页面，并确认控制台无错误。
- Docker 部署只运行一个 AutoList 容器；完成后只保留当前 AutoList 业务镜像。
- 数据目录和 `.env` 不进入构建上下文，不覆盖远端持久化数据。

## 工程技能

### 问题跟踪方式

需求规格与实现票据保存在本地 Markdown 目录 `.scratch/<feature-slug>/`。详见 `docs/agents/issue-tracker.md`。

### 领域文档

本仓库采用单上下文结构，使用根目录 `CONTEXT.md` 与 `docs/adr/`。详见 `docs/agents/domain.md`。
