# AutoList

[![CI](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CiaoBye/AutoList/actions/workflows/ci.yml)

AutoList 是独立运行的片单识别、PT 寻片与下载决策服务。片单、站点、规则、候选与下载历史保存在本地 SQLite；TMDB 负责影片识别，Emby 负责确认实体入馆，MoviePilot 负责分类并提交 Transmission 下载。

当前版本：`1.76`。

## 工作方式

- **识别**：片名、年份与外部编号以 TMDB 识别结果为准。优先按编号识别：IMDb 编号由 TMDB 换算（年份相差不超过 1 年即采用）→ 来源自带的 TMDB 编号；Letterboxd 片单在识别时从其影片页读取 IMDb 与 TMDB 编号并缓存（缺任一个才去取）。都没有时才按标题和年份搜索：片名完全一致优先，其次主标题一致或片名大部分重合，同一档有多部时取评分人数最多的一部；本地化搜索找不到时再用英文搜索（英文片单里的国际译名，如 Cure → キュア），仍找不到才交给可选的 AI 纠正片名。识别出的中文名、原名、年份、原语言和 IMDb ID 会保存下来，作为显示、Emby 查询、站点检索与海报挑选的依据。识别不对时可在影片详情里重新识别或手动指定 TMDB。
- **入馆判断**：只有 Emby 中的真实媒体文件算“已入馆”，`.strm` 归入未完成；Emby 无法确认时显示“待核对”，不会当成缺片。
- **寻片**：只处理未入馆且不在 Transmission 下载中的缺片。首页按片单顺序每批 50 部，片单页可按序号范围寻片，影片详情可单片寻片。站点检索组合 IMDb、TMDB 原名、TMDB 中文名与导入原名（片名检索附片单来源年份，缺失时用 TMDB 年份），并在候选入库前校验片名（也认 TMDB 的其他译名，比对时忽略重音符号）、年份（与片单或 TMDB 年份相差不超过 1 年）及合集标记（三部曲、套装、电影合集、年份区间与中文合集 / 三部曲 / 全集；Criterion Collection 与 REPACK 是单片）。同时搜索 3 部影片，每个站点同时最多 2 个请求、全任务最多 10 个；IMDb 检索已搜到这部影片的站点不再用片名检索；站点临时返回 HTTP 5xx 或 429 时分别等 3 秒、6 秒重试。站点报出 Cookie 失效、二次验证、维护或搜索人机验证时，本次任务的后续影片跳过该站点；重试与重新开始只用当前参与搜索的站点。
- **入馆标准**：硬门槛策略，只接受 `x265 + ADE / FRDS / HDS / CHD`，没有首选时提供 `x264 + CMCT` 作为人工保底；DIY、REMUX、WEB 与完整原盘资源明确排除。规则可在设置中编辑并试算标题。
- **挑选与提交**：候选按电影分组，选定后进入待入馆清单；提交前再次确认 Emby 与 Transmission，跳过已提交、下载中或已入馆的相同发布；NexusPHP 站点还会回详情页确认种子仍在，已被站点删除的移出清单并排除，Cookie 失效或站点无法访问时暂缓提交。候选的下载上下文加密存入数据库，服务重启后仍可提交，7 天后过期需重新寻片；站点 Cookie 不随候选保存，提交时使用站点当前的 Cookie。
- **下载**：只经 MoviePilot 提交到 Transmission，由 MoviePilot 负责下载目录、媒体分类、`MOVIEPILOT` 与站点标签以及后续整理。AutoList 不直连 Transmission 添加任务。
- **导入片单**：先在导入弹窗预览再写入。支持 TMDB（官方 API）、Letterboxd 公开片单（官方嵌入页面）、IMDb 公开 List（GraphQL 列表接口）、MDBList 公开片单（JSON 接口），以及 XLSX / CSV / JSON 文件与粘贴内容。私有片单请使用站点导出文件，AutoList 不绕过验证码或登录限制。
- **海报**：配置 Fanart API Key 后按 fanart.tv → Emby → TMDB 的顺序取海报。fanart.tv 优先挑影片原语言的海报（英语片取英文版、日语片取日文版），其次无字版、英文版，同语言取点赞最多的一张，下载 400px 预览图；fanart.tv 没有的影片自动回退。

## 界面

以每一部电影为中心的“电影藏馆”。顶栏右侧的“寻片”与“服务”状态可以直接点开动态与服务设置。所有页面使用 hash 路由，支持直接访问、刷新恢复以及浏览器前进、后退。

- **藏馆**（`#/`）：馆藏进度与分段进度条、主按钮“为 N 部缺片寻片”、需要你决定的事项、进行中任务（可查看详情或取消）、接下来寻片的缺片与最近入馆海报。
- **片单**（`#/films`）：海报墙与列表两种视图，按状态与异常筛选，支持查找、分页、按序号范围寻片与导入片单。影片详情（`#/films/:id`）顶部显示剧照横幅（fanart.tv 无字剧照优先，其次 TMDB 剧照），展示入馆进度、候选、被排除原因与提交记录，可单片寻片、重新识别或手动指定 TMDB。
- **挑选**（`#/pick`）：按电影分组的推荐 / 保底候选，选定后在底部待入馆清单统一提交。同一种子跨站点合并（制作组、分辨率一致且体积相差不超过 1%），显示做种最多的一条，其余站点可展开逐个选定；种子标题在新标签页打开站点详情页。寻片进行中时显示进度并定时刷新，每部片搜完就出现在这里。
- **动态**（`#/timeline`）：按日期分组的时间线，合并寻片、识别、提交与入馆记录；寻片任务可查看各站点进度、取消、重试或重新开始。“提交记录”标签查看下载历史及 MoviePilot、Transmission、Emby 生命周期状态。
- **设置**（`#/settings[/:section]`）：左侧导航分为“连接 / 内容 / 系统”三组，有异常的分区显示红黄计数；手机上设置首页是带状态的分区列表，进入分区后用“‹ 设置”返回。
  - 概览（`#/settings`）：打开时检测一次各服务连接，汇总站点可用数与失败站点、CookieCloud 同步情况、最近 24 小时的警告与错误、访问令牌与主题，每项可跳到对应分区；
  - 服务连接：MoviePilot、TMDB、Fanart.tv、Emby、Transmission 与可选的 AI 辅助识别各占一行，显示检测结果与已保存配置的摘要（密钥只显示是否已设置）；点开编辑，每个服务单独检测、单独保存，保存后自动重新检测，未保存的修改会标出并可放弃，关闭或刷新页面前提醒。MDBList API Key 在 TMDB 里填写。底部是网络代理（出站代理地址，TMDB / Fanart 与 PT 站点是否走代理），同样折叠为一行，点开编辑、单独保存；
  - 站点：顶部一条 Cookie 来源状态（CookieCloud 的拉取方式、最近一次同步、不在 CookieCloud 中的站点，可立即同步，连接设置按需展开）；站点列表是宽表格（解析方式、检测结果与原因、近 30 天搜索成功率、分享率、Cookie 来源与更新时间、参与搜索开关），可按“全部 / 参与搜索 / 失败 / 搜不到 / 已停用”筛选并查找，失败与搜不到的排在前面；点任意一行在右侧抽屉编辑（账户统计、基本信息、认证、搜索与开关，底部固定删除 / 检测 / 保存，未保存的修改会提示，关闭前确认）。站点协议与解析方式按地址自动识别（高清杜比填 API Key 后改走官方接口，不受网页二次验证影响）；连接检测用《The Godfather》做真实搜索，IMDb 搜不到时回退片名，区分“正常 / 缓慢 / 搜不到 / 失败”；Cookie 有变化的站点会自动重新检测；支持从 MoviePilot 同步站点；
  - 入馆标准：按“硬性排除 → 允许组合 → 站点 → 分辨率 → 做种与优惠”决策，可编辑自定义制作组；右侧标题试算按页面上含未保存修改的规则实时计算；
  - 片单管理：改名、排序、来源同步、新片自动补全、识别、按 IMDb 校准（补齐编号并核对已有识别，不一致的改正后重新核对 Emby）、刷新 Emby 与删除；
  - 外观与访问：主题、首页海报随机展示与本机访问令牌；
  - 诊断日志：按天分组的紧凑列表，连续重复的事件折叠为一行（可展开查看每一次与全部字段），按级别与关键词筛选，每次读取 100 条并可“加载更早的 100 条”，可清空。

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

AutoList 与 MoviePilot 共用同一个 CookieCloud 服务：Chrome 插件把 Cookie 推送到 MoviePilot 自带的 CookieCloud（`COOKIECLOUD_ENABLE_LOCAL=True`，地址 `http://<MoviePilot 主机>:3000/cookiecloud`），AutoList 从那里拉取。AutoList 自身不接收插件推送。

1. 在“设置 → 站点 → Cookie 来源 → 连接设置”填写与插件相同的服务器地址、用户 KEY（5–128 位字母、数字、`_` 或 `-`）与端对端加密密码。
2. AutoList 每 10 分钟拉取并解密一次，只更新 Cookie 有变化的站点，并在后台重新检测这些站点；保存 CookieCloud 设置后下一轮定时任务立即拉取。
3. 寻片、站点检测或提交前确认种子时，站点返回登录页（Cookie 已失效）会立即重新拉取一次；拿到新 Cookie 就用它重试这次请求。2 分钟内最多补拉一次，浏览器里的登录也已过期时不会反复拉取。
4. 站点详情中的“刷新 Cookie”与 Cookie 来源面板的“立即同步”会马上拉取一次。

### 备份与恢复

数据库 `playlist-autodown.db` 使用 SQLite WAL，容器运行时不要直接复制数据库文件或删除 `-wal` / `-shm`。升级前用 SQLite 备份接口在线备份：

```bash
docker exec Autolist python -c "import sqlite3; s=sqlite3.connect('/data/playlist-autodown.db'); d=sqlite3.connect('/data/playlist-autodown.db.bak-1.70-20260930'); s.backup(d); d.close()"
```

恢复时先停止容器，把当前数据库改名保留，删除它的 `-wal` / `-shm`，再把备份复制为 `playlist-autodown.db` 并启动容器。数据库只会向前迁移：程序发现数据库版本高于自身支持的版本时会拒绝启动。运行设置 `runtime-settings.json` 与候选上下文密钥 `candidate-context.key` 同在数据目录，需要完整备份时停止容器后打包整个目录。备份含密钥，只放在管理员可读的位置。

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
- 接口类型：后端在 `app/responses.py` 声明返回格式，`.venv/bin/python scripts/export_openapi.py` 导出到 `frontend/openapi.json`（入库）；`npm run dev` / `npm run build` 先用 `openapi-typescript` 生成 `src/api-schema.d.ts`（不入库）再做类型检查。`tests/test_contract.py` 在快照过期或返回值不符合声明时失败。
- 镜像构建在独立的 Node 阶段完成前端构建，运行时镜像不包含 Node。可用 build arg 覆盖：`NODE_IMAGE`、`NPM_REGISTRY`（默认 npmmirror）、`PYTHON_IMAGE`（默认 `python:3.12-slim`）、`PIP_INDEX_URL`（默认清华源）。构建上下文不包含 `.venv`、`.scratch`、`tests`、日志、数据库、备份或 `.env`。
- 功能更新或问题修复必须递增版本并记录到 `CHANGELOG.md`，协作与验收规则见 `AGENTS.md`，领域词汇见 `CONTEXT.md`。

### 代码结构

- `app/main.py`：FastAPI 入口、鉴权中间件与路由注册。
- `app/responses.py`：接口返回格式（前端类型由它生成）。
- `app/api/`：按域划分的 HTTP 路由（system / sites / playlists / search / selection / history / films / home / picks / timeline / images / logs）。
- `app/services/`、`app/domain/`：业务编排与纯领域逻辑；`app/services/films.py` 负责影片状态计算。
- `app/queries/`：按领域划分的数据库读写（影片、片单、待入馆清单、寻片任务、站点、动态），路由不直接写 SQL。
- `app/migrations.py`：数据库迁移。按编号排列的步骤，每步只在数据库版本低于其编号时执行一次；旧表缺少的列在 `ADDED_COLUMNS` 声明，启动时补齐。
- `app/tasks.py`：后台任务框架。寻片、识别、Emby 状态刷新与新片处理共用运行登记、容量限制、取消、重启后标记中断和首页“进行中”列表。
- `app/outbound.py`：出站请求安全（目标地址校验、DNS 固定、逐跳检查重定向、跨域剥离凭据、响应大小上限），所有外部请求都经过 `safe_request`。
- `app/sites/`：PT 站点接入。按站点档案（`profiles.py`：搜索路径、参数、IMDb 搜索方式、列表与链接规则）由通用 NexusPHP 解析器搜索（`nexusphp.py`，按表头识别列、读取折扣与 IMDb 编号、识别登录 / 二次验证 / 维护 / Cloudflare 页面）；高清杜比填了 API Key 时走官方接口（`hddolby.py`）。`tests/fixtures/sites/` 是各站去除账号信息后的真实搜索页，用于回归。
- `app/clients.py`：MoviePilot、TMDB、Fanart.tv、Emby、Transmission、M-Team、RSS / Torznab 等外部服务客户端，以及站点账户统计。
- `frontend/`：“电影藏馆”界面源码；样式在 `frontend/src/styles/`（外壳、通用组件与各页面各一个文件），由 `app.css` 按顺序引入。
- `tests/`：按领域划分的测试（`test_<领域>.py`），共享的隔离基类在 `tests/support.py`。
- `app/static/`：Logo、favicon 与服务图标。

## 安全边界

- **默认无认证**，信任边界是网络隔离。设置环境变量 `AUTOLIST_ACCESS_TOKEN` 后，除 `/`、静态资源与 `/api/health` 外，全部 `/api/*` 需 `Authorization: Bearer <token>` 或 `X-AutoList-Token`。令牌只读环境变量，不写入运行设置；默认强制至少 32 个字符且具备足够字符多样性，可信内网迁移旧令牌时才设置 `AUTOLIST_REQUIRE_STRONG_TOKEN=false`。启用令牌后，站点、RSS、图标等出站地址拒绝内网 IP 与仅解析到内网的域名（可用 `AUTOLIST_ALLOW_PRIVATE_HOSTS` 按域名后缀放行）。海报等图片使用短时签名地址，不在地址中携带令牌。
- 设置页可管理**本机浏览器**的访问令牌（`localStorage`），不会改写服务端的 `AUTOLIST_ACCESS_TOKEN`。
- Swagger / OpenAPI 文档默认关闭（`AUTOLIST_ENABLE_DOCS=true` 开启）；所有响应附带 CSP、X-Frame-Options、nosniff 与 Referrer-Policy 安全头。
- API Key、密码、Cookie 与 Token 只在前端显示“已配置”，既有值不会返回浏览器；输入框留空表示保留，清除需显式操作。
- 页面保存的运行设置写入 `/data/runtime-settings.json`（权限 `0600`），优先于 `.env`；`.env` 只作为首次启动和未保存字段的默认值。修改 `.env` 后如需生效，请在设置页重新保存对应字段或删除 `runtime-settings.json`。
- 站点下载地址和授权字段只保存在加密的候选下载上下文中（AES-GCM，密钥为数据目录下的 `candidate-context.key`，权限 600），数据库与备份中没有明文，也不写入日志；删除密钥文件会让现有候选全部过期。
- 提交下载时站点 Cookie 与含 passkey 的下载地址会随请求交给 MoviePilot，请确保 `MP_BASE_URL` 使用 HTTPS 或仅暴露于受控内网。
- 代理：“TMDB / Fanart 走代理”控制 TMDB、fanart.tv 与片单来源（TMDB / MDBList / Letterboxd / IMDb）的请求，“PT 站点允许走代理”配合站点里的代理开关控制站点请求；代理认证信息不得写入地址。大陆网络访问 TMDB 请使用代理，Compose 不写入 `extra_hosts`。
- CookieCloud 只从设置里的服务器地址拉取，解密后直接更新站点 Cookie，不在数据目录保存密文。AutoList 不读取 Chrome 配置目录、密码库或浏览器存储。
- 并发寻片任务上限 3 个，避免打满下游站点。
- 日志与错误消息按键名启发式脱敏（Cookie、Token、API Key、Authorization 及含 `key` / `token` / `passkey` / `secret` 的查询参数）；自定义响应头名携带的值与非常规写法可能残留，属纵深防御而非完整保证。
