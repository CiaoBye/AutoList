# AutoList · 电影藏馆

[![CI](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**把一份电影片单，变成一座补得齐的私人影库。**

AutoList 以“片单”为中心：你给它一份想收藏的电影清单（TMDB、Letterboxd、IMDb、MDBList 或表格文件），它会找出 Emby 里还缺哪些，替你到自己的 PT 站点里逐部寻片，按你定的规则挑出合格的资源，经 MoviePilot 提交下载，并持续跟踪到 Emby 入库为止。

当前版本：`2.36`

![藏馆首页：片单进度与最近入馆](docs/screenshots/home.jpg)

## 它解决什么问题

收藏一份“经典电影 100 部”这类片单，手工做起来是这样的：对着清单一部部去 Emby 里确认有没有，缺的去好几个站点分别搜，在一堆画质、编码、制作组各不相同的资源里挑，下载后还要盯着有没有真的入库，中途换了资源又要清理旧任务。

AutoList 把这条线串起来：

- **不用逐部对账**：片名、年份、IMDb / TMDB 编号统一识别，Emby 里已有的自动标为已入馆，只处理真正缺的。
- **不用逐站搜索**：一次在你添加的全部站点里检索，同一资源跨站点合并，只留做种最多的一条。
- **不用凭感觉挑**：用可编辑的入馆标准（分辨率、编码、制作组、体积等）自动筛选，不合格的资源带着原因被排除，拿不准的留给你人工决定。
- **不会重复下载**：提交前再次确认 Emby 与 Transmission；已入馆、已在下载的相同发布会被跳过。
- **看得见全程**：从缺片到寻片、挑选、下载、整理、入库，每部影片只有一个由服务端统一计算的状态，下载停滞、整理失败、站点失效等需要你处理的事会集中提示。

## 功能

- **片单导入**：TMDB 列表、Letterboxd、IMDb 公开列表、MDBList，以及 XLSX / CSV / JSON 文件或粘贴内容；导入前可预览，来源更新后可增量同步。
- **识别**：优先按来源自带的编号识别，其次按片名与年份匹配 TMDB；识别不对可在影片详情里重新识别或手动指定，也可接入可选的 AI 辅助纠正片名。
- **入馆判断**：只有 Emby 里的真实媒体文件算“已入馆”；`.strm` 或 Emby 暂时无法确认的影片不会被当成缺片。
- **寻片**：支持 NexusPHP 类站点（通过站点档案适配不同页面结构）、站点官方 API、Torznab 与 RSS；按 IMDb 编号与多种片名检索，校验片名、年份与合集，对每个站点限速，遇到 Cookie 失效、二次验证或维护会明确提示并跳过。
- **站点 Cookie**：可与 MoviePilot 共用 CookieCloud，Cookie 失效时自动重新拉取并重试。
- **挑选与提交**：候选按电影分组，选定后统一提交；下载只经 MoviePilot 提交，由它负责分类、整理与后续入库。
- **下载与联动**：读取 Transmission 的实时状态，也接收 MoviePilot、Transmission、Emby 主动推送的事件；换了资源后自动清理被取代的停滞旧任务。
- **界面**：藏馆、片单、挑选、下载、动态、设置六个页面，亮色与暗色两套主题，桌面与手机都可用；第一次打开会有分步引导。
- **安全默认**：密钥只在服务端处理，前端只显示“已配置”；可选访问令牌；所有外部请求经统一的出站校验。

| 片单与状态筛选 | 影片详情 |
| --- | --- |
| ![片单海报墙](docs/screenshots/films.jpg) | ![影片详情](docs/screenshots/detail.jpg) |

## 工作方式

```
片单 ──识别──▶ TMDB
  │
  ├─ 已在 Emby ─▶ 已入馆
  │
  └─ 缺片 ──寻片──▶ 你的站点 ──按入馆标准筛选──▶ 挑选 ──提交──▶ MoviePilot ──▶ Transmission
                                                                            │
                                         Emby 入库 ◀── MoviePilot 整理 ◀────┘
```

AutoList 本身不下载、不整理文件，也不内置任何站点：站点由你自己添加，下载与整理交给 MoviePilot 和 Transmission，入库状态以 Emby 为准。

## 快速开始

需要一台能运行 Docker 的机器，以及：

| 服务 | 作用 | 是否必需 |
| --- | --- | --- |
| [TMDB](https://www.themoviedb.org/) API Key | 识别影片、取海报 | 必需 |
| [MoviePilot](https://github.com/jxxghp/MoviePilot) | 提交下载、分类与整理 | 必需 |
| [Emby](https://emby.media/) | 判断影片是否已入库 | 建议 |
| Transmission | 读取下载状态 | 建议 |
| 你自己的 PT 站点账号 | 寻片的来源 | 必需 |

```bash
docker compose pull
docker compose up -d
```

打开 `http://<Docker 主机>:8585`，按首页的引导依次连接 TMDB 与 MoviePilot、添加站点、导入片单即可。数据保存在命名卷 `autolist_data`（容器内 `/data`）。可选配置见 `.env.example`，所有连接信息也都能在“设置”页里填写。

> **安全提示**：AutoList 默认面向可信内网。未设置 `AUTOLIST_ACCESS_TOKEN`、也没有反向代理鉴权时，任何能访问端口的人都能修改设置并提交下载。**不要把端口直接映射到公网**；需要远程访问时请设置访问令牌（至少 32 位）或放在带鉴权的反向代理后面。

### 升级与备份

```bash
docker compose pull && docker compose up -d
```

数据库使用 SQLite WAL，升级前请用备份接口在线备份，不要直接复制文件：

```bash
docker exec Autolist python -c "import sqlite3; s=sqlite3.connect('/data/playlist-autodown.db'); d=sqlite3.connect('/data/playlist-autodown.db.bak'); s.backup(d); d.close()"
```

恢复时先停止容器，把当前数据库改名保留，删除它的 `-wal` / `-shm`，再把备份复制为 `playlist-autodown.db`。完整备份还应包含数据目录里的 `runtime-settings.json` 与 `candidate-context.key`。

### CookieCloud

AutoList 可与 MoviePilot 共用 CookieCloud：浏览器插件把 Cookie 推送到 MoviePilot 自带的 CookieCloud，AutoList 从同一个服务拉取。在“设置 → 站点 → Cookie 来源”填写与插件相同的服务器地址、用户 KEY 和端对端加密密码即可。AutoList 每 10 分钟拉取一次，只更新有变化的站点；寻片遇到 Cookie 失效时会立即补拉一次。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd frontend && npm ci && npm run build        # 前端构建到 app/static/ui
DATA_DIR=./data .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8599
```

- 后端 FastAPI + SQLite，前端 Preact + Vite + TypeScript。
- 验证：`python3 -m compileall -q app`、`.venv/bin/python -m unittest discover -s tests`、`frontend` 下 `npm run build`。
- 接口类型由后端声明生成：改动返回格式后运行 `.venv/bin/python scripts/export_openapi.py` 更新 `frontend/openapi.json`。
- 协作规范见 [AGENTS.md](AGENTS.md)，领域术语见 [CONTEXT.md](CONTEXT.md)，版本记录见 [CHANGELOG.md](CHANGELOG.md)。
- `scripts/deploy-fnos.sh` 是作者自用的局域网部署脚本（通过 SSH 构建并重建容器，部署前自动备份数据库），主机、路径与地址都可用环境变量覆盖。

## 说明

- 本项目仅用于管理你自己合法获得的内容与账号。请遵守各站点的规则，不要用来绕过验证码、登录或访问频率限制——AutoList 对每个站点限速，遇到验证会停下来提示你。
- 本项目使用 TMDB API，但未获得 TMDB 的认可或认证。影片信息与海报来自 TMDB（可选 fanart.tv）。
- 项目与 MoviePilot、Emby、Transmission、TMDB 无隶属关系。

## 许可证

[MIT](LICENSE)
