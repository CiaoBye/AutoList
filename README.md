# AutoList

[![CI](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml)

AutoList 是独立运行的片单识别、PT 寻片与下载决策服务。片单、站点、规则、候选与下载历史保存在本地 SQLite；TMDB 负责影片识别，Emby 负责确认实体入馆，MoviePilot 负责分类并提交 Transmission 下载。

当前版本：`1.59`。

## 工作方式

- **识别**：片名、年份与外部编号以 TMDB 识别结果为准。优先按编号识别：IMDb 编号由 TMDB 换算（年份相差不超过 1 年即采用）→ 来源自带的 TMDB 编号；Letterboxd 片单在识别时从其影片页读取 IMDb 与 TMDB 编号并缓存（缺任一个才去取）。都没有时才按标题和年份搜索：片名完全一致优先，其次主标题一致或片名大部分重合，同一档有多部时取评分人数最多的一部；本地化搜索找不到时再用英文搜索（英文片单里的国际译名，如 Cure → キュア），仍找不到才交给可选的 AI 纠正片名。识别出的中文名、原名、年份、原语言和 IMDb ID 会保存下来，作为显示、Emby 查询、站点检索与海报挑选的依据。识别不对时可在影片详情里重新识别或手动指定 TMDB。
- **入馆判断**：只有 Emby 中的真实媒体文件算“已入馆”，`.strm` 归入未完成；Emby 无法确认时显示“待核对”，不会当成缺片。
- **寻片**：只处理未入馆且不在 Transmission 下载中的缺片。首页按片单顺序每批 50 部，片单页可按序号范围寻片，影片详情可单片寻片。站点检索组合 IMDb、TMDB 原名、TMDB 中文名与导入原名，并在候选入库前校验片名、年份及合集标记。
- **入馆标准**：硬门槛策略，只接受 `x265 + ADE / FRDS / HDS / CHD`，没有首选时提供 `x264 + CMCT` 作为人工保底；DIY、REMUX、WEB 与完整原盘资源明确排除。规则可在设置中编辑并试算标题。
- **挑选与提交**：候选按电影分组，选定后进入待入馆清单；提交前再次确认 Emby 与 Transmission，跳过已提交、下载中或已入馆的相同发布。候选的下载上下文只保存在内存中，服务重启或超过 2 小时后需重新寻片。
- **下载**：只经 MoviePilot 提交到 Transmission，由 MoviePilot 负责下载目录、媒体分类、`MOVIEPILOT` 与站点标签以及后续整理。AutoList 不直连 Transmission 添加任务。
- **导入片单**：先在导入弹窗预览再写入。支持 TMDB（官方 API）、Letterboxd 公开片单（官方嵌入页面）、IMDb 公开 List（GraphQL 列表接口）、MDBList 公开片单（JSON 接口），以及 XLSX / CSV / JSON 文件与粘贴内容。私有片单请使用站点导出文件，AutoList 不绕过验证码或登录限制。
- **海报**：配置 Fanart API Key 后按 fanart.tv → Emby → TMDB 的顺序取海报。fanart.tv 优先挑影片原语言的海报（英语片取英文版、日语片取日文版），其次无字版、英文版，同语言取点赞最多的一张，下载 400px 预览图；fanart.tv 没有的影片自动回退。

## 界面

以每一部电影为中心的“电影藏馆”。顶栏右侧的“寻片”与“服务”状态可以直接点开动态与服务设置。所有页面使用 hash 路由，支持直接访问、刷新恢复以及浏览器前进、后退。

- **藏馆**（`#/`）：馆藏进度与分段进度条、主按钮“为 N 部缺片寻片”、需要你决定的事项、进行中任务（可查看详情或取消）、接下来寻片的缺片与最近入馆海报。
- **片单**（`#/films`）：海报墙与列表两种视图，按状态与异常筛选，支持查找、分页、按序号范围寻片与导入片单。影片详情（`#/films/:id`）顶部显示剧照横幅（fanart.tv 无字剧照优先，其次 TMDB 剧照），展示入馆进度、候选、被排除原因与提交记录，可单片寻片、重新识别或手动指定 TMDB。
- **挑选**（`#/pick`）：按电影分组的推荐 / 保底候选，选定后在底部待入馆清单统一提交。
- **动态**（`#/timeline`）：按日期分组的时间线，合并寻片、识别、提交与入馆记录；寻片任务可查看各站点进度、取消、重试或重新开始。“提交记录”标签查看下载历史及 MoviePilot、Transmission、Emby 生命周期状态。
- **设置**（`#/settings/:section`）：
  - 服务连接：MoviePilot、TMDB、Fanart.tv、Emby、Transmission（可逐项检测），以及可选的 MDBList 与 AI 辅助识别；
  - 代理：出站代理及 TMDB / Fanart、PT 站点分流；
  - 站点：顶部为 Cookie 来源（CookieCloud 的同步方式、最近一次同步结果、不在 CookieCloud 中的站点，以及连接设置）；站点协议按地址自动识别，支持搜索开关、代理、优先级、连接检测（用《The Godfather》做真实搜索，IMDb 搜不到时回退片名，区分“正常 / 缓慢 / 搜不到 / 失败”）、Cookie 刷新（显示 Cookie 来源与更新时间）、账户统计与从 MoviePilot 同步；Cookie 有变化的站点会自动重新检测；
  - 入馆标准：按“硬性排除 → 允许组合 → 站点 → 分辨率 → 做种与优惠”决策，可编辑自定义制作组并实时试算标题；
  - 片单管理：改名、排序、来源同步、新片自动补全、识别、按 IMDb 校准（补齐编号并核对已有识别，不一致的改正后重新核对 Emby）、刷新 Emby 与删除；
  - 外观与访问：主题、首页海报随机展示与本机访问令牌；
  - 诊断日志：按级别与关键词筛选、清空。

影片状态由服务端统一计算：待识别、待核对、缺片、寻片中、有候选、已选定、下载中、已入馆，另有“无合格资源”“提交失败”“候选已过期”三种异常标记。

主题只改变色彩与对比度，选择保存在当前浏览器：

- **馆藏档案 Archive**：默认亮色主题。
- **午夜放映 Cinema**：暗色主题，适合低亮度环境。

## 部署

> **安全警告**：AutoList 默认面向可信内网。未设置 `AUTOLIST_ACCESS_TOKEN` 且未加反向代理鉴权时，任何能访问 `8585` 的客户端都能改设置、同步 Cookie 并提交下载。**禁止将端口直接映射到公网**；访客 Wi‑Fi、远程映射或不可信网段必须启用访问令牌或反向代理 Basic Auth / SSO。

### Docker Compose

将 `compose.yml` 放入专用目录后执行：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

打开 `http://<Docker 主机>:8585`，在“设置 → 服务连接”填写 TMDB、Emby、Transmission 和 MoviePilot 并检测连接。默认拉取 Docker Hub 的 `ayuanaa/autolist:latest`（`linux/amd64`）。

- 数据持久化到命名卷 `autolist_data`，挂载为容器内 `/data`；容器默认以 UID `10001` 运行。
- `.env` 不是必需文件。需要时可在同目录 `.env` 设置 `APP_PORT=8585` 与 `AUTOLIST_ACCESS_TOKEN=<长随机令牌>`；其余连接信息也可用环境变量预填，见 `.env.example`。
- 升级：`docker compose pull && docker compose up -d`，升级前先按下文备份数据库。

### 飞牛（fnOS）局域网部署

`scripts/deploy-fnos.sh` 通过 SSH（主机别名 `fnos`）把源码增量同步到 NAS，在 NAS 上直接构建镜像并重建容器，最后检查 `/api/health` 与版本号。飞牛的镜像加速源拉取 `node:22-*` 会返回 401，脚本默认使用 NAS 本地已有的 `node:22.16.0-alpine` 构建前端，可用 `AUTOLIST_NODE_IMAGE` 覆盖。

```bash
bash scripts/deploy-fnos.sh
```

### 发布镜像

推送 `main` 或手动运行 GitHub Actions 的 **CI**：Python 3.12 / 3.14 测试、前端构建、Compose 校验、镜像构建及容器健康检查全部通过后，才发布到 Docker Hub `ayuanaa/autolist`，同时推送版本标签（取自 `app/config.py` 的 `APP_VERSION`）与 `latest`。发布需要 GitHub Actions Secret `DOCKERHUB_TOKEN`（用户名 `ayuanaa`）；缺少令牌时跳过发布并输出警告。

### Chrome CookieCloud

1. 在“设置 → 站点 → Cookie 来源 → 连接设置”填写 CookieCloud 用户 KEY（5–128 位字母、数字、`_` 或 `-`）与端对端加密密码。
2. 任选一种同步方式：
   - **拉取**：填写与 Chrome 插件相同的 CookieCloud 服务器地址，AutoList 每小时自动拉取并解密；
   - **推送**：服务器地址留空，Chrome CookieCloud 扩展的服务器地址填写 `http://<Docker 主机>:8585/cookiecloud`（设置页可一键复制），KEY 和密码保持一致，并执行一次上传同步。
3. 站点详情中的“刷新 Cookie”会按同一来源重新读取 CookieCloud 数据。

### 备份与恢复

数据库 `playlist-autodown.db` 使用 SQLite WAL，容器运行时不要直接复制数据库文件或删除 `-wal` / `-shm`。升级前用 SQLite 备份接口在线备份：

```bash
docker exec Autolist python -c "import sqlite3; s=sqlite3.connect('/data/playlist-autodown.db'); d=sqlite3.connect('/data/playlist-autodown.db.bak-1.59-20260928'); s.backup(d); d.close()"
```

恢复时先停止容器，把当前数据库改名保留，删除它的 `-wal` / `-shm`，再把备份复制为 `playlist-autodown.db` 并启动容器。数据库只会向前迁移：程序发现数据库版本高于自身支持的版本时会拒绝启动。运行设置 `runtime-settings.json` 与 `cookiecloud/` 同在数据目录，需要完整备份时停止容器后打包整个目录。备份含密钥，只放在管理员可读的位置。

### 健康检查

镜像和 Compose 都配置了轻量 healthcheck（`python -m app.healthcheck`）：请求本机 `/api/health`，以只读方式执行 SQLite `quick_check(1)` 和核心表检查，并确认调度器没有停止或长期失去心跳。它不检测 TMDB、PT、Emby、Transmission 或 MoviePilot，下游临时故障不会触发容器重启。

```bash
docker inspect --format '{{.State.Health.Status}}' Autolist
```

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd frontend && npm ci && npm run build
```

- 本地运行：`DATA_DIR=<数据目录> .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8599`；`frontend` 下 `npm run dev` 会把 `/api` 代理到该端口。未构建前端时访问 `/` 返回 503 并提示构建命令。
- 验证：`.venv/bin/python -m unittest discover -s tests`、`python3 -m compileall -q app`、`frontend` 下 `npm run build`。
- 镜像构建在独立的 Node 阶段完成前端构建，运行时镜像不包含 Node。可用 build arg 覆盖：`NODE_IMAGE`、`NPM_REGISTRY`（默认 npmmirror）、`PYTHON_IMAGE`（默认 `python:3.12-slim`）、`PIP_INDEX_URL`（默认清华源）。构建上下文不包含 `.venv`、`.scratch`、`tests`、日志、数据库、备份或 `.env`。
- 功能更新或问题修复必须递增版本并记录到 `CHANGELOG.md`，协作与验收规则见 `AGENTS.md`，领域词汇见 `CONTEXT.md`。

### 代码结构

- `app/main.py`：FastAPI 入口、鉴权中间件与路由注册。
- `app/api/`：按域划分的 HTTP 路由（system / sites / playlists / search / cart / films / logs）。
- `app/services/`、`app/domain/`：业务编排与纯领域逻辑；`app/services/films.py` 负责影片状态计算。
- `app/clients.py`：MoviePilot、TMDB、Fanart.tv、Emby、Transmission 等外部服务客户端。
- `frontend/`：“电影藏馆”界面源码。
- `app/static/`：Logo、favicon 与服务图标。

## 安全边界

- **默认无认证**，信任边界是网络隔离。设置环境变量 `AUTOLIST_ACCESS_TOKEN` 后，除 `/`、静态资源、`/api/health` 与 `/cookiecloud/*` 外，全部 `/api/*` 需 `Authorization: Bearer <token>` 或 `X-AutoList-Token`。令牌只读环境变量，不写入运行设置；默认强制至少 32 个字符且具备足够字符多样性，可信内网迁移旧令牌时才设置 `AUTOLIST_REQUIRE_STRONG_TOKEN=false`。启用令牌后，站点、RSS、图标等出站地址拒绝内网 IP 与仅解析到内网的域名（可用 `AUTOLIST_ALLOW_PRIVATE_HOSTS` 按域名后缀放行）。海报等图片使用短时签名地址，不在地址中携带令牌。
- 设置页可管理**本机浏览器**的访问令牌（`localStorage`），不会改写服务端的 `AUTOLIST_ACCESS_TOKEN`。
- Swagger / OpenAPI 文档默认关闭（`AUTOLIST_ENABLE_DOCS=true` 开启）；所有响应附带 CSP、X-Frame-Options、nosniff 与 Referrer-Policy 安全头。
- API Key、密码、Cookie 与 Token 只在前端显示“已配置”，既有值不会返回浏览器；输入框留空表示保留，清除需显式操作。
- 页面保存的运行设置写入 `/data/runtime-settings.json`（权限 `0600`），优先于 `.env`；`.env` 只作为首次启动和未保存字段的默认值。修改 `.env` 后如需生效，请在设置页重新保存对应字段或删除 `runtime-settings.json`。
- 站点下载地址和授权字段只保留在当前进程的短暂下载上下文中，不写入数据库或日志。
- 提交下载时站点 Cookie 与含 passkey 的下载地址会随请求交给 MoviePilot，请确保 `MP_BASE_URL` 使用 HTTPS 或仅暴露于受控内网。
- 代理：“TMDB / Fanart 走代理”控制 TMDB、fanart.tv 与片单来源（TMDB / MDBList / Letterboxd / IMDb）的请求，“PT 站点允许走代理”配合站点里的代理开关控制站点请求；代理认证信息不得写入地址。大陆网络访问 TMDB 请使用代理，Compose 不写入 `extra_hosts`。
- AutoList 内置兼容 CookieCloud 的加密数据接收端，密文保存在 `/data/cookiecloud`（权限 `0600`）。上传与读取的 UUID 必须与设置中的 CookieCloud 用户 KEY 一致；未配置 KEY 时拒绝写入，同 KEY 上传有每分钟次数上限。AutoList 不读取 Chrome 配置目录、密码库或浏览器存储。
- 并发寻片任务上限 3 个，避免打满下游站点。
- 日志与错误消息按键名启发式脱敏（Cookie、Token、API Key、Authorization 及含 `key` / `token` / `passkey` / `secret` 的查询参数）；自定义响应头名携带的值与非常规写法可能残留，属纵深防御而非完整保证。
