# AutoList

AutoList 是独立运行的片单识别、PT 搜索与下载决策服务。片单、站点、规则、候选、下载列表与历史保存在本地 SQLite；TMDB 负责影片识别，Emby 负责实体入库检查，MoviePilot 负责分类并提交 Transmission 下载。

当前版本：`0.78`。片名、年份与外部编号以 TMDB 识别结果为准；独立站点搜索会组合 IMDb、TMDB 原名、TMDB 中文名与导入原名，提高不同站点的召回率，并在候选入库前校验高信息量片名、年份及合集标记，避免共享通用词的不同影片混入候选。资源搜索默认只处理未入库且不在 Transmission 下载中的影片，可按过滤后的片单队列选择前 N 部；仍保留按序号范围搜索。电影候选采用硬门槛策略：只允许 `x265 + ADE/FRDS/HDS/CHD`，无首选时提供 `x264 + CMCT` 人工保底；DIY、REMUX、WEB 与完整原盘资源会明确排除。制作组识别内置 MoviePilot 兼容词表，支持一次性导入 MP 自定义词表后由 AutoList 独立维护。下载仍固定委托给 MoviePilot 的 DownloadChain，再由其提交 Transmission，保证分类目录与标签一致；下载历史会根据 MoviePilot、Transmission 与 Emby 的可验证结果显示“已提交、下载中、已整理/已入库、待入库、待确认或失败”。

NAS 部署目录统一为 `/mnt/user/appdata/Autolist`，数据库和运行时设置保存在 `/mnt/user/appdata/Autolist/data`，更新镜像不会丢失。

外部片单先在导入弹窗预览再写入。TMDB 使用官方 API；Letterboxd 公开片单使用其官方嵌入页面，避免普通网页的 Cloudflare 校验；IMDb 公开 List 使用当前 GraphQL 列表接口；MDBList 公开片单使用其 JSON 接口。私有片单仍需使用站点导出文件，AutoList 不绕过验证码或登录限制。

## 页面结构

- **工作台**：片单、最近搜索、候选数量、下载列表、服务状态和最近下载的总览。
- **片单**：查看片单和影片明细、TMDB 识别结果及 Emby 实体/.strm 状态；只有真实媒体文件算已入库，`.strm` 归入未完成。明细使用服务端分页，超大片单不会一次性传到浏览器。
- **资源搜索**：查看片单总数、入库数、Transmission 下载中数和待搜索数；按“未下载”队列或序号范围创建搜索任务，筛选候选并加入或移出下载列表。片单补全和新片自动搜索也在此页面配置。
- **下载列表**：检查已选资源、单项移除，并固定经 MoviePilot 分类后提交到 Transmission；容器重启后失效的搜索上下文会明确标记并引导重新搜索。
- **候选规则**：按“硬性排除 → 允许组合 → 站点 → 分辨率 → 做种与优惠”的顺序决策；可查看内置词表、编辑自定义制作组正则并实时试算标题。
- **站点配置**：复刻 MP 风格的站点总览、筛选和详情；站点协议在后端按地址自动识别，支持搜索选择、代理、优先级、连接测试及 Chrome CookieCloud 一键更新。
- **同种聚合**：相同发布在多个站点只显示一行，并根据站点优先级、免费状态和做种数选择主推荐。
- **TMDB 批量识别**：片单页可主动启动识别任务并查看进度，不再只能通过资源搜索间接识别。
- **下载历史**：查看全部提交记录，并根据 MoviePilot、Transmission 与 Emby 的来源显示可追踪生命周期状态；失败原因经过脱敏且提供下一步处理方向。

侧栏使用页面路由，支持刷新恢复当前页面以及浏览器前进、后退。

## 部署

> **安全警告**：AutoList 默认面向可信内网。未设置 `AUTOLIST_ACCESS_TOKEN` 且未加反向代理鉴权时，任何能访问 `8585` 的客户端都能改设置、同步 Cookie 并提交下载。**禁止将端口直接映射到公网**；访客 Wi‑Fi、远程映射或不可信网段必须启用访问令牌或反向代理 Basic Auth / SSO。

1. `cp .env.example .env`，至少填写 TMDB 与 MoviePilot；Emby、AI 按需配置。Transmission 由 MoviePilot 自身配置并负责实际下载。
2. 不可信网络环境请设置 `AUTOLIST_ACCESS_TOKEN`（强随机串）。浏览器首次调用受保护接口时会提示输入，令牌仅保存在本机 `localStorage`。
3. `docker compose up -d --build`
4. 打开 `http://<Docker 主机>:8585`，在右上角“设置”中填写连接信息，再导入原始 xlsx 或 JSON 片单。
### Chrome CookieCloud

1. 在 AutoList 设置中填写 CookieCloud 用户 KEY 与端对端加密密码。
2. Chrome CookieCloud 扩展的服务器地址填写 `http://<Docker 主机>:8585/cookiecloud`，KEY 和密码保持一致，并执行一次上传同步。
3. 在“站点配置”点击“同步 Chrome Cookie”，AutoList 会按站点域名更新 Cookie；User-Agent 保留站点当前配置。
4. 开启“转发 MoviePilot”后，AutoList 收到的同一份端到端加密数据会转发至 `<MoviePilot 地址>/cookiecloud/update`，Chrome 扩展无需维护两个服务地址。MoviePilot 需启用其本地 CookieCloud 服务。

Compose 仅启动 `autolist` 一个容器。前端静态文件、FastAPI 和 SQLite 都在这个容器中；数据库持久化到 `./data/playlist-autodown.db`。

## 开发约定

功能更新或问题修复必须递增补丁版本，并在 `CHANGELOG.md` 记录本次变化。项目协作与验收规则见 `AGENTS.md`。

## 安全边界

- **默认无认证**，信任边界是网络隔离。设置环境变量 `AUTOLIST_ACCESS_TOKEN` 后，除 `/`、静态资源、`/api/health` 与 `/cookiecloud/*` 外，全部 `/api/*` 需 `Authorization: Bearer <token>` 或 `X-AutoList-Token`。令牌只读环境变量，不进 `runtime-settings.json`。
- 内网模式可直接编辑连接信息；API Key、密码、Cookie 与 Token 只在前端显示“已配置”状态，既有值不会返回浏览器。
- 页面保存的运行设置写入 `/data/runtime-settings.json`，文件权限为 `0600`；`.env` 仍作为首次启动和未保存设置时的默认值。
- 站点下载 URL 和授权字段只保留在当前进程的短暂下载上下文中；不会写入数据库或日志。容器重启后需重新搜索再下载。
- AutoList 将媒体与种子提交给 MoviePilot，并指定其 Transmission 下载器；MP 负责应用自身的下载目录、媒体二级分类、`MOVIEPILOT` 与站点标签和后续整理规则。AutoList 不提供直连下载模式或独立目录映射。
- TMDB 优先使用片单 IMDb ID 查找，未命中再以标题和年份搜索；识别后的 TMDB 中文名、原名、年份和 IMDb ID 会持久化并作为片单显示、Emby 查询和站点检索的权威元数据。站点 API Key 与代理认证不返回浏览器。
- 代理地址按用户的内网显示偏好返回设置页，可分别启用 TMDB 与 PT 站点分流；代理认证信息不得写入地址。
- **TMDB 支持代理**：在设置中填写 `OUTBOUND_PROXY_URL`（或页面「代理地址」）并勾选「TMDB 走代理」后，识别与片单导入的 TMDB API 请求经代理发出。
- AutoList 内置兼容 CookieCloud 的加密数据接收端，密文保存在 `/data/cookiecloud`，权限为 `0600`。**上传与读取的 UUID 必须与设置中的 CookieCloud 用户 KEY 一致**；未配置 KEY 时拒绝写入。同 KEY 上传另有每分钟次数上限。
- 并发搜索任务默认上限 3 个，避免误操作或滥用打满下游站点。
- 浏览器 Cookie 不能由普通网页或 Docker 容器直接读取；Chrome 登录态通过 CookieCloud 扩展主动上传。AutoList 不读取 Chrome 配置目录、密码库或浏览器存储。
- Compose **默认不再**写入 `api.themoviedb.org` 的 `extra_hosts`。大陆网络请用 TMDB 代理；若仍需 hosts，在自有 compose 覆盖或宿主机 DNS 中维护，并自行更新可能漂移的 CDN IP。
- 设置页可管理**本机浏览器**访问令牌（`localStorage`）；服务端 `AUTOLIST_ACCESS_TOKEN` 只读环境变量，不会被页面改写。
