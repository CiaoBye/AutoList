# 更新日志

本项目的功能更新、问题修复和可交付界面调整均记录在此文件。

## 1.67 - 2026-09-29

接口声明返回格式，前端接口类型改为由 OpenAPI 生成：后端字段改动而前端没跟上时，测试或前端构建会直接失败。界面行为不变。

### 新增

- **接口返回格式（`app/responses.py`）**：前端读取的 44 个接口（影片、藏馆、挑选、动态、待入馆清单与提交、提交记录、寻片任务、站点、CookieCloud、片单、设置与连接检测、入馆标准、日志）用 Pydantic 声明返回格式并写上 `response_model`。返回值按声明校验，未声明的字段不再返回（前端不使用的 `automation_auto_select`、站点的 `created_at` 与 `search_average_ms`）；未返回的可选字段（如待办的 `names`、动态的 `task_id`）现在以 `null` 返回。
- **前端类型生成**：`scripts/export_openapi.py` 把接口声明导出到 `frontend/openapi.json`（入库，去掉随版本变化的版本号）；`npm run dev` / `build` / `typecheck` 先用 `openapi-typescript`（新增开发依赖，7.13.0）生成 `src/api-schema.d.ts`（不入库），`src/types.ts` 只保留对生成类型的别名。各处手写的接口结果类型（导入预览、入馆标准试算、站点检测与同步、提交记录清理等）改用生成的类型，设置页操作的回调按接口结果推断类型，不再强制转换。
- **契约测试（`tests/test_contract.py`）**：`frontend/openapi.json` 与当前声明不一致时失败；前端读取的接口都必须声明返回格式；用覆盖全部影片状态的样本经 HTTP 请求各读取接口，返回值必须通过校验；返回格式里的影片状态与异常取值必须与状态计算一致。

### 调整

- 测试样本 `FilmFixture` 移到 `tests/support.py`，影片测试与契约测试共用；异常处理测试注册的临时路由在测试结束后移除，不再混入其他用例看到的接口声明。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：297 项全部通过。
- 验证链路：临时去掉后端 `Film.poster_url` 后，快照测试失败；重新导出后前端构建在 4 处使用该字段的地方报错；已还原。
- 上线前在飞牛上用线上数据库副本比对新旧版本：115 个读取接口（含 100 部影片详情）全部返回 200，新版返回的每个字段与旧版的值一致；副本、临时镜像与源码目录已删除。
- 前端构建产物的 JS 与 1.66 完全一致（只改类型）；Docker 镜像构建（含安装依赖、生成类型与类型检查）通过。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.66 - 2026-09-29

CookieCloud 只从与 MoviePilot 共用的服务拉取：缩短拉取间隔，站点 Cookie 失效时立即重新拉取；移除 AutoList 自带的插件推送接收端。

### 调整

- **拉取间隔**：定时拉取 CookieCloud 从每小时一次改为每 10 分钟一次；保存 CookieCloud 设置后下一轮定时任务立即拉取。
- **Cookie 失效后重新拉取**：寻片、站点检测、提交前确认种子时，站点返回登录页（Cookie 已失效）会立即拉取一次 CookieCloud，拿到新 Cookie 就用它重试这次请求；寻片任务里后续的请求直接使用新 Cookie。同时发现失效的多个请求只拉一次，2 分钟内最多补拉一次，浏览器里的登录也过期时不会反复拉取。最近一次同步摘要与日志新增“Cookie 失效后重新拉取”。
- **移除插件推送接收端**：删除 `/cookiecloud/update`、`/cookiecloud/get/*` 及 `/update`、`/get/*` 别名，连同其跨域与私网访问头、访问令牌豁免、上传与读取限流、请求体解压与上限处理；拉取的数据不再写入数据目录的 `cookiecloud/`。CookieCloud 现在需要填写服务器地址才算配置完成。
- 设置页 Cookie 来源面板去掉推送地址，改为说明拉取间隔与失效后的重新拉取；连接设置提示填写 MoviePilot 自带的 CookieCloud 地址。
- 内部：`services/cookiecloud_store.py` 改为 `services/cookiecloud.py`，定时拉取、手动同步、单站刷新、从 MoviePilot 同步站点后的 Cookie 更新与失效补拉共用同一个拉取入口，同一时间只有一次拉取。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：293 项全部通过。删除接收端相关的 7 项用例；新增：拉取服务器密文并解密更新站点（不写数据目录）、接收端路由已移除且启用访问令牌后不再豁免、拉取间隔 10 分钟、Cookie 失效后补拉并重试一次、冷却期内不重复拉取、未配置 CookieCloud 时不补拉、寻片请求在补拉后使用新 Cookie。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.65 - 2026-09-29

架构重构第二批：测试按领域重组、路由中的 SQL 收进查询模块、样式按页面拆分。除刷新片单来源的一条提示文字外，界面与接口行为不变。

### 调整

- **测试按领域重组**：原来按审计轮次组织的 `test_regressions`、`test_workflows`、`test_backend_hardening`、`test_round2_safety`、`test_audit_*` 等 10 个文件拆分合并为 `tests/test_<领域>.py`：app、security、settings、playlists、recognition、search、sites、submission、tasks、migrations、films。用例全部保留（295 项，比 1.64 多 1 项新增）。测试直接从业务模块导入，删除 `app/compat.py` 中转层；需要临时数据目录的用例统一继承 `IsolatedAppTestCase`，预置 250 部影片的用例改用 `SeededPlaylistTestCase`，不再各自维护隔离样板。
- **路由不再直接写 SQL**：片单、待入馆清单、寻片任务、站点、影片、海报与动态的读写收进 `app/queries/`（新增 `playlists`、`selection`、`search`、`sites`、`timeline`），站点相关查询从 `queries/films.py` 移到 `queries/sites.py`；删除 `api/films.py` 中未使用的重复寻片汇总函数。“片单是否有进行中的任务”改由 `app/tasks.py` 的 `active_playlist_task` 按四类任务统一判断。
- **样式按页面拆分**：2287 行的 `app.css` 拆为 `frontend/src/styles/` 下的 `base`、`shell`、`common`、`home`、`films`、`film-drawer`、`pick`、`timeline`、`settings`，各页面的窄屏规则写回该页文件；`app.css` 只按固定顺序引入它们。
- 刷新片单来源时，“片单仍有任务运行”的提示改用统一的任务名称：“寻片”“识别”“Emby 状态刷新”“新片处理”（原为“搜索”“识别”“入库检查”“自动化”）。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：295 项全部通过；拆分前后用例名称逐一核对一致；新增刷新片单来源时有新片处理任务运行会被拒绝且不访问来源的用例，并断言寻片任务竞态时的提示文字。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 样式拆分：新旧编译产物的规则逐条比对一致（381 条，无缺失、无多余）；在浏览器中对同一页面分别套用新旧样式，逐个元素（含伪元素）比对全部计算样式：藏馆、片单、两种状态的影片详情、挑选、动态与 7 个设置分区，在 1440、1098、1024、768、375px 下以及 `cinema` 主题中均无差异。

## 1.64 - 2026-09-29

架构重构第二批：数据库迁移改为按编号排列的步骤。界面与接口行为不变。

### 调整

- **迁移编号化（`app/migrations.py`）**：数据库迁移整理为 `MIGRATIONS` 中按编号排列的步骤，每步只在数据库版本（`PRAGMA user_version`）低于其编号时执行一次；程序支持的数据库版本即最大编号（现为 19）。旧版本建的表缺少的列改为声明表 `ADDED_COLUMNS`，与建索引一样每次启动都补齐，不占编号。
- **历史整理只执行一次**：原先每次启动都会重跑的整理（中断同一片单重复的进行中任务、提交记录回填影片编号与资源指纹、停用旧的自动加入清单与经 MoviePilot 搜索设置、为旧失败任务补日志、清理孤立行、脱敏历史错误文本）改为第 14–19 步。线上数据库升级时会一次性执行这 6 步，之后启动不再重复扫描。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：294 项全部通过；新增 `tests/test_migrations.py`（编号唯一且递增、只执行比数据库版本新的步骤、重复启动不重跑、新库包含全部补齐列），旧数据类测试改为显式设置对应的旧版本号。
- 在容器内用线上数据库副本试运行：版本 13 → 19，各表行数、索引与数据不变，`integrity_check` 为 ok，副本已删除。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.63 - 2026-09-28

架构重构第一批收尾：统一后台任务框架、拆分影片路由、出站安全代码独立。界面与接口行为不变。

### 调整

- **统一后台任务框架（`app/tasks.py`）**：寻片、识别、Emby 状态刷新、新片处理四类任务共用运行登记、容量限制、取消、服务重启后标记中断与首页“进行中”列表，任务结束后自动注销；取代原来四个登记字典、两套容量函数与散落在各处的清理代码。容量提示统一为“已有 N 个××任务在运行，请稍后再试”（寻片任务原先写作“搜索任务”）。
- **拆分影片路由**：原 841 行的 `api/films.py` 按页面拆为 `films`（列表、详情、单片寻片、识别修正）、`home`、`picks`、`timeline` 与 `images`（海报与剧照代理）；影片、片单与站点展示用的查询收进 `app/queries/films.py`，候选的分组展示移到 `services/candidates.py`。
- **出站安全代码独立**：目标地址校验、DNS 固定、重定向逐跳检查、跨域剥离凭据与响应大小上限从 `util.py` 移到 `app/outbound.py`（`util.py` 从 730 行减到约 150 行）。

### 修复

- CookieCloud 面板把定时拉取缓存的数据误显示为“接收插件推送”；现在保存的数据带来源标记，只有插件真正推送过才显示。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：290 项全部通过；新增 `tests/test_tasks.py`（任务结束自动注销、四类任务容量与取消一致、重启时中断未完成任务但保留排队的新片处理）与 CookieCloud 来源显示测试。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.62 - 2026-09-28

“下载车 / cart”在代码里统一改名为待入馆清单（selection）；站点超时放宽到 30 秒。

### 调整

- **统一命名**：接口 `/api/cart` → `/api/selection`、`/api/cart/items/{id}` → `/api/selection/items/{id}`、`/api/cart/download` → `/api/selection/submit`；模块 `api/cart.py` → `api/selection.py`，提交记录路由拆到 `api/history.py`；候选字段 `in_cart` → `in_selection`，片单自动化字段 `auto_cart` → `auto_select`；提示文案里的“下载列表”改为“待入馆清单”。
- **数据库迁移（schema v13）**：表 `cart_items` 改名为 `selection_items`、`playlists.automation_auto_cart` 改名为 `automation_auto_select`，在建表之前完成，已选定的资源原样保留。
- **站点超时**：从 MoviePilot 新同步的站点至少 30 秒（MoviePilot 默认 15 秒，学校、聆音一次搜索就要 23–25 秒）；再次同步不再覆盖本地设置的超时。现有 17 个站点已按要求调为 30 秒。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：286 项全部通过；新增旧命名数据库升级后保留已选定资源、MoviePilot 同步保留本地超时且新站点至少 30 秒。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 线上：朋友重新登录后经 CookieCloud 同步恢复可搜索；聆音、学校在 30 秒超时下检测通过（约 25 秒、23 秒）。

## 1.61 - 2026-09-28

候选下载上下文加密存库；提交前回站点确认种子仍在。

### 调整

- **候选上下文加密存库**：寻片得到的下载上下文（含带 passkey 的下载地址）改存数据库新表 `candidate_contexts`（schema v12），用 AES-GCM 加密，密钥单独保存在数据目录的 `candidate-context.key`（权限 600）；服务重启不再让待入馆清单全部过期，有效期由 2 小时延长到 7 天。数据库与备份中没有下载地址明文；密钥被替换时旧上下文按已过期处理并清理。
- **站点 Cookie 不随候选保存**：提交时按候选所属站点读取当前 Cookie 与 UA 交给 MoviePilot，CookieCloud 在候选保存期间更新的 Cookie 会直接生效。
- **提交前确认种子仍在站点**：NexusPHP 站点在提交前回详情页确认。种子已被删除（404 或“没有该ID的种子”等提示）时移出待入馆清单、标记为排除并提示重新寻片；Cookie 失效、二次验证或站点无法访问时暂缓提交，保留在清单中并说明原因。官方 API、M-Team、RSS 与 Torznab 站点不做这项确认。

### 修复

- 提交成功后删除候选上下文不再等待同一次提交事务的写锁。
- 密钥文件在飞牛等不采用创建权限的文件系统上显式设为 600，权限被放宽时读取会重新收紧。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：285 项全部通过；新增上下文加密（数据库中无下载地址明文、不含站点 Cookie、换密钥后视为过期、密钥文件权限 600）、提交时使用站点当前 Cookie、种子被删除时移出并排除、站点无法确认时暂缓，以及详情页删除提示、登录页与跨域详情页的判断。
- 线上 14 个可搜索的 NexusPHP 站点逐一实测：现有种子全部确认存在；不存在的种子编号 13 个站点识别为已删除，铂金家对不存在的编号只提示“你没有该权限”，按无法判断处理、照常提交。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.60 - 2026-09-28

重写 PT 站点接入（参考 MoviePilot 的站点档案与通用爬虫设计），覆盖现有 17 个站点。

### 调整

- **新的 `app/sites/`**：每个站点由一份档案描述（搜索路径、关键词参数、IMDb 搜索方式、列表选择器、详情与下载链接规则），通用 NexusPHP 解析器按档案搜索；页面解析改用 lxml（新增依赖 `lxml`、`cssselect`），取代原先手写的 HTMLParser 行解析与零散的站点特例字典。
- **按表头识别列**：优先用 NexusPHP 通用的排序链接 `sort=N`（观众等改过主题的站点也保留），其次按表头文字与图标；多出“进度”“置顶促销”等列的站点不再错位。表格行被 `<form>` 包住（南洋）、下载按钮是表单（天空）都能处理；未登记的站点找不到列表表格时，退回到种子行最多的表格，并按列顺序推断做种 / 下载 / 完成数。
- **结果字段更完整**：新增下载人数、完成数、副标题；优惠按实际折扣计算（免费 / 50% / 30% / 2X 上传，此前只认“免费”，其余一律当原价）；读取行内 IMDb 编号并补齐前导零。字段同时按 MoviePilot 的 TorrentInfo 命名（`peers`、`grabs`、`uploadvolumefactor`、`imdbid`、`description`）。
- **按 IMDb 编号确认资源**：站点行带 IMDb 编号时以编号为准——与目标影片不一致直接排除（如搜《教父》搜到的《日本的首领》tt0076461），一致则不再比对年份与片名；合集仍排除。
- **听听歌支持按 IMDb 搜索**：关键词写成 `imdb0068646`（与 MoviePilot 相同）。
- **高清杜比官方接口**：填写 API Key 后改走 `api.hddolby.com` 搜索，不受网页二次验证影响，结果直接带 TMDB / IMDb 编号；未填时仍走网页。
- **登录页判断更准**：登录表单里嵌 Cloudflare Turnstile 验证码时仍判定为“Cookie 已失效”，只有 Cloudflare 拦截页才报“人机验证”。
- 站点详情显示解析方式（通用 NexusPHP / 听听歌专用 / 高清杜比官方 API）。
- 搜索请求加上 `search_mode=0`（全部关键词都要匹配）与 `notnewword=1`（不计入站点热搜）。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：279 项全部通过；站点测试移到 `tests/test_sites.py`，用 14 个站点去除账号信息后的真实搜索页回归（每站首条种子的片名、大小、做种、IMDb 与下载方式），另测档案参数、页面拦截识别、兜底解析、高清杜比接口与 IMDb 身份判断。
- 用线上 17 个站点刚抓取的真实页面离线验证：14 个站点全部解析成功，字段完整；朋友、高清杜比、烧包乐园分别识别为登录失效、二次验证、维护。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.59 - 2026-09-28

理清站点 Cookie 与 CookieCloud：放在一起、看得见同步结果、Cookie 变了自动重新检测。

### 调整

- **CookieCloud 移到站点页**：“设置 → 站点”顶部新增“Cookie 来源”面板，显示同步方式（每小时拉取 / 接收插件推送）、最近一次同步结果（更新了哪些站点、多少个已是最新）和不在 CookieCloud 中的站点（这些站点的 Cookie 只能手动更新，过期后不会自动恢复），并提供检测与立即同步；连接设置收在面板里展开。原“网络与 CookieCloud”改名为“代理”，只保留出站代理。
- **记录 Cookie 来源**：站点新增 `cookie_updated_at` / `cookie_source`（schema v11），CookieCloud 同步、手动填写、从 MoviePilot 同步时分别记录；站点详情显示“Cookie 由 CookieCloud 更新于 …”。
- **Cookie 变化后自动重新检测**：同步后只对 Cookie 有变化的站点在后台重新检测，站点状态不再停留在旧 Cookie 的结果。
- **检测回退片名**：用 IMDb 搜不到《The Godfather》时再按片名搜索（学校等站点不支持 IMDb 搜索，寻片时本来也会按片名搜），两次都没有结果才算“搜不到”。
- 站点列表的连接状态悬停可看检测说明。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：272 项全部通过（新增同步来源与摘要、只复测有变化的站点、手动填写 Cookie 记录来源、检测回退片名测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 本地实例（线上数据库只读快照）浏览器检查：1440 / 375px 站点、代理、入馆标准页无横向溢出、控制台无错误。

## 1.58 - 2026-09-27

修复站点搜索：线上 19 个站点实测，按 IMDb 搜索时 16 个站点返回 0 条，部分站点的结果解析不出来。

### 修复

- **按 IMDb 搜索用错了搜索范围**：NexusPHP 的 `search_area=0` 只搜标题，按 IMDb 编号搜索必须用 `search_area=4`。修正后铂金家、观众、葡萄、我堡等按标题搜不到的站点也能按 IMDb 找到资源。
- **天空（HDSky）解析不到结果**：下载按钮是表单（`<form action="download.php?…">`），解析器只认链接；现在同时读取表单地址。
- **听听歌（TTG）搜索无效**：它的搜索参数是 `search_field` 而不是 `search`，此前关键词被忽略、返回的是最新种子；种子链接为 `/t/<id>/` 与 `/dl/<id>/`，做种数写成“306 / 6”。现在按它的格式搜索和解析；它不支持按 IMDb 搜索，IMDb 查询改用片名。
- **个人主页链接被当成种子详情**：`userdetails.php?id=` 也匹配“details.php?id=”，现在要求前面不能是字母。
- **二次验证与维护页当成“连接正常”**：高清杜比的搜索被跳转到二次验证页、烧包乐园跳转到维护公告，此前显示“正常”；现在明确报错说明原因。

### 调整

- **连接检测改为真实搜索**：此前检测用一个不存在的词搜索，只能说明 Cookie 能登录，所有站点都是“解析到 0 条”。现在用《The Godfather》（IMDb tt0068646）搜索：解析到结果为“正常 / 缓慢”；登录正常但解析不到结果的新增“搜不到”状态（站点可能不收录电影，或页面结构暂不兼容），计入站点页“需要处理”筛选，首页“需要你决定”也会提示。
- **入馆标准说明与实际排序一致**：说明改为“先判断能否入馆，再按推荐 / 保底 → 站点优先级 → 分辨率 → 做种人数 → 免费与折扣排序”，并写明 0 人做种、制作组未识别、冒用组名三条固定排除规则，以及不在顺序里的分辨率仍可入馆；制作组词表显示“内置 84 个制作组（86 条识别规则）”，不再出现 86 与 84 两个数字。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：270 项全部通过（新增 IMDb 搜索范围、表单下载与个人主页链接、听听歌参数与做种数、二次验证与维护跳转、真实搜索检测与“搜不到”状态测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 用线上保存的天空与听听歌搜索页离线验证解析：天空《Casablanca》解析出 27 条（此前 0 条），听听歌做种数正确读出。

## 1.57 - 2026-09-27

识别以 IMDb 编号换算 TMDB 为准，并可按 IMDb 校准整份片单。

### 新增

- **按 IMDb 校准**：“设置 → 片单管理”新增“按 IMDb 校准”，对整份片单补齐 IMDb / 来源 TMDB 编号（Letterboxd 片单从影片页读取并缓存），按 IMDb 换算 TMDB 核对已有识别；只有按编号得到不同结果时才改正（重新识别并重新核对 Emby），按片名搜索得到的不同结果不推翻已有识别。结束后自动刷新 Emby 状态，动态里显示“核对 N 部，改正 M 部”，每次改正写入诊断日志。识别任务新增 `mode` / `corrected`（schema v10）。

### 调整

- **识别顺序**：IMDb 编号换算 TMDB → 来源自带的 TMDB 编号 → 按片名搜索。识别结果记录依据（`matched_by`：imdb / tmdb / title）。
- **Letterboxd 编号补取**：此前只在缺少来源 TMDB 编号时才读取影片页，现在缺 IMDb 或 TMDB 任一编号都会读取一次并缓存。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：264 项全部通过（新增 IMDb 优先于来源编号、缺 IMDb 时补取 Letterboxd、校准只改正按编号得到的差异测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.56 - 2026-09-27

### 修复

- **影片详情横幅被压扁**：详情抽屉是可滚动的纵向 flex 容器，内容较长的影片（如带“还没有寻过片”说明的缺片）会把剧照横幅从 220px 压到约 30px，海报盖住抽屉标题。抽屉内元素不再被压缩。

### 验证

- 线上复现：《Cure》详情横幅高度 33px；修复后见下方部署验证。`.venv/bin/python -m unittest discover -s tests` 261 项通过，`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.55 - 2026-09-27

界面细节打磨第 3、4 步，新标志与更有分量的顶栏。

### 新增

- **新标志**：锈红色圆角底（与界面强调色一致）、奶白色实心 “A” 与上下两排胶片齿孔，16px 下仍清楚。`logo.svg`、`favicon.svg` 与 `autolist-icon.png`（512px）同步更新，顶栏左侧显示标志。
- **影片详情剧照横幅**：详情抽屉顶部显示通栏剧照，底部渐隐，海报放大到 140px 压在渐隐处。剧照优先取 fanart.tv 的无字背景图（其次点赞最多），没有时用 TMDB 剧照（w1280）；选择结果保存在 `fanart_backdrop_url` / `tmdb_backdrop_path`（schema v9），重新识别时清空。都没有或加载失败时不显示横幅。
- **读取占位**：首页、片单海报墙与影片详情读取时显示海报形状的占位块，数据到达时不再跳动。

### 调整

- **顶栏**：改为深色色带（亮暗两套主题各有配色），导航文字加大加粗，当前页用强调色下划线，顶部不再显得轻飘。
- **顶栏状态**：“寻片空闲 / 寻片中 / 服务正常 / N 项服务异常”平时是一行浅色文字，进行中或异常时才加色；点击分别打开动态（进行中时直接打开该寻片任务）与服务设置，不再是点击重新检测。
- **数字排版**：计数、统计与首页大数字改用正文字体的等宽数字，等宽字体只用于编号、文件大小、IMDb 编号与日志时间。
- **阅读型页面对齐**：时间线与设置保持 1200px 行宽，但与顶栏、其他页面左边缘对齐，不再单独居中。
- 片单查找框提示文字缩短，1024px 下不再被截断。
- 服务端海报内存缓存上限由 64 张提高到 300 张（总大小上限 32MB 不变），100 部片单的海报墙不再反复向 fanart.tv 取图。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：261 项全部通过（新增剧照选择与缓存、回退 TMDB 与无剧照隐藏、重新识别清空剧照测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 本地实例（线上数据库只读快照）浏览器检查：1440 / 375px，亮暗两套主题下顶栏、首页、片单、影片详情、时间线与设置无横向溢出、控制台无错误。

## 1.54 - 2026-09-27

界面细节打磨第 2 步：版式对齐。

### 调整

- **顶栏与内容对齐**：顶栏左右内边距随内容宽度变化，宽屏下 Logo 与页面内容左边缘对齐（2560px 下均为 376px），不再贴在屏幕最左侧。
- **片单页顶部由三行改为两行**：第一行为标题、片单名称与“导入片单 / 为缺片寻片”；第二行左侧为状态筛选，右侧为查找、按序号寻片与海报 / 列表切换。去掉“片单”“查找”两个小标签（查找框保留无障碍名称），海报墙上移约 60px。多个片单时片单选择器放在标题旁。
- **首页待决定事项较少时收成一条**：“需要你决定”只有一条或没有时，进度卡占满整行，事项显示为进度卡下方的一条提醒，不再在右侧留下大块空白；两条及以上仍为左右两栏。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：258 项全部通过；`python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 本地实例（线上数据库只读快照）浏览器检查：2560 / 1440 / 768 / 375px 首页与片单无横向溢出，375px 下按序号寻片浮层在屏幕内，控制台无错误。

## 1.53 - 2026-09-27

界面细节打磨第 1 步：海报卡片。

### 调整

- **状态标在海报上**：海报墙与首页海报架不再给每张卡片挂“已入馆 / 缺片”文字标签。已入馆、缺片、待核对只在海报右下角显示一个小色点（颜色与首页进度条一致）；寻片中、有候选、已选定、下载中、待识别以及“无合格资源 / 提交失败 / 候选已过期”才显示文字标签。卡片的无障碍名称仍包含完整状态。
- **片名最多两行**：不再截成一行省略号；第二行固定为“年份 · 原名”。
- **缺片海报降低饱和度**：缺片、待核对、待识别的海报降低饱和度，已入馆保持原色，悬停、键盘聚焦或选中时恢复原色。
- **交互**：悬停时海报轻微上浮；海报图片加载完成后淡入（系统开启“减少动态效果”时不做动画）。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：258 项全部通过；`python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 本地实例（线上数据库只读快照）浏览器检查：1440 / 375px 海报墙与首页无横向溢出、控制台无错误。

## 1.52 - 2026-09-27

识别改为优先使用片单来源自带的身份，按片名搜索只作兜底。

### 调整

- **识别顺序**：来源 TMDB 编号 → IMDb 编号 → 按片名搜索（本地化、英文、AI 纠正）。给出 TMDB 编号时直接读取该影片的本地化资料，不再搜索片名；编号在 TMDB 已不存在（404）时才退回搜索。
- **Letterboxd**：Letterboxd 的影片资料本身来自 TMDB，影片页带有 TMDB 与 IMDb 编号。导入时记录影片标识（`source_ref`，如 `letterboxd:cure`），识别时从官方嵌入域名 `embed.letterboxd.com` 的影片页读取编号并保存（每部只取一次；Letterboxd 暂时不可用时退回片名搜索）。letterboxd.com 本身有 Cloudflare 校验，不直接访问。导入与定时同步不逐部请求影片页，速度不受影响。
- **来源身份与识别结果分开保存**：新增 `source_tmdb_id`、`source_ref`（schema v8）。此前来源自带的 TMDB 编号写进识别结果 `tmdb_id`，而识别任务又忽略它按片名重新搜索，可能被覆盖成别的影片；现在来源编号只作为识别依据。来源刷新时如果来源编号与已识别结果不一致，以来源为准并清空识别结果重做。
- **补齐已有片单**：“立即同步”（增量同步）不改动已有影片，只为早期导入、缺少来源身份的影片补上 `source_ref` / `source_tmdb_id`。
- 调用识别的四处（识别任务、自动化、寻片前识别、影片详情“重新识别”）统一改为按片单条目识别（`recognize_item`）。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：258 项全部通过（新增来源编号直接采用、404 回退、Letterboxd 编号读取与保存、Letterboxd 不可用时回退、影片页解析、增量同步补齐来源身份测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 实测 `embed.letterboxd.com/film/cure/` 可直接访问并返回 `data-tmdb-id="36095"` 与 IMDb `tt0123948`；`letterboxd.com` 的列表 RSS 与影片 JSON 返回 Cloudflare 校验页。

## 1.51 - 2026-09-27

### 修复

- **重新挑选后仍显示旧海报**：尚未查询 fanart.tv 的影片使用 `?v=0` 地址，1.49 时这个地址返回的海报被浏览器缓存 7 天；1.50 按原语言重新挑选后地址仍是 `?v=0`，浏览器继续显示旧海报（如《战舰波将金号》仍是中文版）。现在未查询时的版本号改为 `pending`，避开旧缓存；只有地址版本号与当前所选海报一致时才长期缓存，其余只缓存 1 小时。

### 数据

- 用 1.50 的识别规则对线上 100 部影片做只读复核：96 部结果不变，4 部原先误识别，已按用户要求在线上重新识别：《Cure》→ X圣治 (36095)、《Poetry》→ 诗 (47909)、《Still Life》→ 三峡好人 (2346)、《Parasite》→ 寄生虫 (496243)。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：252 项全部通过（海报缓存断言改为区分待定地址与带版本号地址）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.50 - 2026-09-27

修正 TMDB 识别规则，海报改为按原语言挑选，宽屏下提高页面利用率。

### 修复

- **TMDB 误识别**：《Cure》(1997) 被识别成 “Say It, Fight It, Cure It”。原因是本地化（zh-CN）搜索结果只有中文名与原名（X圣治 / キュア），没有英文名可比对，而“同年且片名包含”规则只要片名里有同一个词就算命中。现在：
  - 片名部分重合要求主标题一致（如 “M” 对 “M - Eine Stadt sucht einen Mörder”），或较短片名覆盖较长片名至少 60% 的词；
  - 同一档有多部符合时取评分人数最多的一部，不再取搜索结果里的第一部；
  - 本地化搜索找不到时再用英文搜索，命中后换回本地化的片名、年份与海报字段；
  - 片单带 IMDb ID 时，按编号查到且年份相差不超过 1 年即直接采用，不再因导入片名是另一种语言而放弃。

### 调整

- **海报按原语言挑选**：fanart.tv 优先原语言海报（英语片取英文版、日语片取日文版，粤语片按中文），其次无字版、英文版；不再按 TMDB 显示语言（中文）优先。识别时保存 TMDB 原语言（新增 `tmdb_original_language`，schema v7），之前识别的影片在取海报时向 TMDB 补取一次；升级时清空已选的 fanart 海报，全部按新规则重新挑选。
- **宽屏利用率**：浏览型页面最大内容宽度由 1400px 提高到 1920px（时间线与设置保持 1200px 阅读宽度）；片单海报墙在 2560px 屏幕上由每行 7 张增加到 10 张，每页由 60 部增加到 120 部；首页两个海报架最多返回 16 部，并按可用宽度恰好显示一整行，窄屏仍横向滑动。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：252 项全部通过（新增 Cure 误识别、主标题与词覆盖、评分人数取舍、英文搜索回退、IMDb 直接采用、原语言海报排序与补取测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 本地实例（线上数据库只读快照）浏览器检查：2560 / 1440 / 1024 / 375px 无横向溢出、控制台无错误；2560px 下海报墙每行 10 张、首页海报架各一整行 10 张。

## 1.49 - 2026-09-27

清理旧界面遗留的接口、代码与历史文档，开始新的开发周期。

### 修复

- **Fanart 海报一张都取不到**：fanart.tv 当前返回的海报地址是 `assets.fanart.tv/fanart/<名称>.jpg`，1.48 的地址校验只接受早期的 `/fanart/movies/<编号>/movieposter/` 格式，所有海报都被丢弃，影片被记成“fanart.tv 没有海报”后回退到 Emby / TMDB。现在两种格式都接受；数据库升级到 schema v6，一次性清掉这批误记的空记录，让它们重新查询（1.48 程序无法打开 v6 数据库，回退需先恢复 `bak-1.48` 备份）。
- “重试失败的站点”只重搜失败的影片与站点组合，但影片状态、影片详情与挑选页只看最近一次产生候选的任务，重试后原任务里其他站点的候选会消失。现在最近的任务是重试时，沿父任务链合并候选；“重新开始”仍只看自己的结果。

### 移除

- 新界面不再调用的旧接口：`/api/overview`、`/api/candidates`、`/api/downloads`、`/api/automation-runs`、`/api/notifications` 与 `/api/notifications/read-all`、`/api/recognition-tasks/{id}`、`/api/library-scan-tasks/{id}`、`/api/playlists/{id}/items`、`/api/playlists/{id}/searchable-items`。站内通知仍写入并显示在动态时间线。
- 旧入口兼容：`/next` 重定向与旧 hash 地址（`#sites`、`#cart` 等）自动跳转。
- 历史文档与工具产物：`docs/`（备份流程并入 README，领域约定并入 AGENTS.md 与 CONTEXT.md）、`.pi-lens.json`；未使用的样式与指向旧界面的注释。
- 对应的旧接口测试；候选相关测试改为走影片详情与挑选页使用的同一套聚合。

### 调整

- AGENTS.md：新版本通过验证后默认部署到飞牛；仓库只保留 README、CHANGELOG、AGENTS.md 与 CONTEXT.md 四份文档。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：245 项全部通过（移除 8 项旧接口测试，新增重试候选合并、两种 fanart 地址格式与 v6 一次性重查测试）。
- 在线上容器内用真实密钥确认 fanart.tv 当前返回 `/fanart/<名称>.jpg` 格式，`bigpreview` 同样可用。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。

## 1.48 - 2026-09-27

海报改为优先取自 fanart.tv，首页布局调整。

### 新增

- **Fanart.tv 海报**：“设置 → 服务连接”新增 Fanart.tv 分区，可填写 Project API Key 并检测连接（环境变量 `FANART_API_KEY`）。配置后海报按 fanart.tv → Emby → TMDB 的顺序选取：
  - fanart.tv 按 TMDB 语言（默认中文）、英文、无字版的顺序挑选，同语言取点赞最多的一张，下载 400px 预览图（不存在时回退原图）；
  - 只接受 `assets.fanart.tv` 的电影海报地址，密钥以请求头发送，只在服务端使用；
  - 查询结果记在 `playlist_items.fanart_poster_url`（新增可空列，不提升 schema 版本）：`NULL` 未查过、空串表示 fanart.tv 没有海报，此时回到 Emby / TMDB 海报；网络或密钥错误不会记为“没有海报”，下次仍重试；
  - 重新识别或手动指定 TMDB 后清空记录，海报地址附带版本参数，浏览器不会继续显示旧海报。
- **首页“接下来寻片”**：按片单顺序展示下一批将要寻片的缺片海报（与“为 N 部缺片寻片”按钮范围一致），可直达全部缺片。

### 调整

- 首页“需要你决定”不再重复列出“N 部缺片还没有寻片”（由进度卡主按钮承担），没有待判断事项时给出提示；进度卡与待办卡等高。
- 窄屏（≤1100px）下首页海报架改为单行横向滑动，不再把 8 张海报排成两行大图。
- “检测全部连接”附带检测 Fanart；顶栏“服务正常”统计仍只计 TMDB、Emby、Transmission、MoviePilot。网络设置中的开关更名为“TMDB / Fanart 走代理”。

### 文档

- README 改为只描述当前行为：修正默认镜像标签（`latest`）、补充飞牛部署脚本与开发命令、合并重复的代理说明，历史版本说明移到本文件。
- AGENTS.md 按新界面更新页面结构、版本同步位置（`config.py`、`frontend/package.json`、README）、验证命令与飞牛部署约定。
- CONTEXT.md 与 ADR-0001 改用当前界面的术语（缺片、寻片、挑选、待入馆清单、已入馆等）。
- `docs/backup-restore.md` 从 Unraid / 1.03 改写为飞牛部署的备份、升级与恢复流程。
- 删除针对旧界面的过时文档：`docs/repair-progress.md`、`docs/roadmap-and-verification.md`、`docs/ux-audit.md`、`docs/archive/`、`docs/prototypes/`、旧界面截图 `docs/screenshots/` 与 1.15 审计报告 `review/`；CI 移除对原型脚本的语法检查，设置脱敏检查加入 `fanart_api_key`。
- 本文件 1.43 及更早版本压缩为摘要（这些版本的旧界面细节已不适用）。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：250 项全部通过（新增 6 项 Fanart 海报选择、回退、失败不记忆、重新识别清空与设置脱敏测试，扩展首页“接下来寻片”断言）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`、`git diff --check` 通过。
- 用真实 fanart.tv 验证：无效密钥检测返回“Fanart API Key 无效”；`bigpreview` 海报为 400×570、约 60KB，`poster_image` 下载成功。
- 本地实例（线上数据库只读副本）浏览器检查：1440 / 1024 / 768 / 375px 下首页、片单、影片详情、挑选、动态与设置无横向溢出，控制台无脚本错误（本地未配置 Emby，Emby 海报请求 502 属环境原因）。

## 1.47 - 2026-09-27

界面重构第 4 阶段：设置、导入与任务详情迁入新界面，新界面成为唯一界面，旧界面移除。

### 新增

- **设置**（`#/settings/:section`）分为七个分区：服务连接（MoviePilot / Emby / TMDB / Transmission 检测，MDBList 与 AI 辅助识别）、网络与 CookieCloud（出站代理与分流、拉取或推送同步、复制推送地址、立即同步）、站点（列表筛选、编辑抽屉、连接测试、刷新 Cookie、从 MoviePilot 同步、批量检测）、入馆标准（规则编辑与标题试算）、片单管理（改名、排序、来源同步、新片自动补全、识别、刷新 Emby、删除）、外观与访问（主题、首页海报随机展示、本机访问令牌）、诊断日志。密钥输入框留空表示保留，支持显式清除；每个分区只提交自己改动过的字段。
- **导入片单**：藏馆首页、片单页与片单管理都可打开导入弹窗，支持网址（TMDB、Letterboxd、IMDb、MDBList）、XLSX / CSV / JSON 文件与粘贴内容，先预览再导入。
- **按序号寻片**：片单页可按序号范围创建寻片任务。
- **寻片任务详情**：动态时间线里的寻片任务可打开详情抽屉（`#/timeline?task=`），查看各站点进度与日志，取消、重试或重新开始；首页进行中任务同样可查看详情或取消。
- **提交记录**：动态页新增“提交记录”标签（`#/timeline?view=history`），按生命周期筛选下载历史并可清除当前分组。

### 调整

- `/` 直接提供新界面；`/next` 与 `/next/` 永久重定向（308）到 `/`。旧地址 `#dashboard`、`#playlists`、`#search`、`#cart`、`#rules`、`#history`、`#sites`、`#logs` 自动跳转到对应的新页面。
- 移除旧界面：`app/static/index.html`、`app.js`、`style.css`、`theme.css` 与 `js/core.js`、`js/site-map.js`、`js/theme-init.js`；Logo、favicon 与服务图标保留。CI 移除旧脚本的语法检查，AGENTS.md 的前端验证改为 `frontend` 目录下 `npm run build`。
- 窄屏（≤900px）下“按序号寻片”浮层改为以整条工具栏为锚点并左右对齐，修复 375px 下浮层超出屏幕。
- 首页“最近入馆”在开启“每天随机展示”时按日期随机排序；时间线的寻片事件附带任务编号与状态。
- 与旧界面绑定的测试改为针对新界面：设置保存直接用 Node 执行 `frontend/src/settingsPayload.ts` 的真实组装逻辑再提交到 `/api/settings`；新增可清除字段与服务端一致、旧界面文件已移除、主题只切换 token、`formatSize` TB 显示等契约测试。

### 修复

- 设置保存时 `cookiecloud_url` 为 `null` 会被当成空地址清除已保存的 CookieCloud 服务器地址；现在 `null` 表示保留，清除需显式 `clear_cookiecloud_url`。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：244 项全部通过（移除 13 项只针对旧界面源码的字符串测试，新增 6 项新界面契约与设置保存测试）。
- `python3 -m compileall -q app`、`frontend` 的 `npm run build`（`tsc --noEmit` + `vite build`）、`git diff --check` 通过。
- 本地实例浏览器检查：1440 / 1024 / 768 / 375px 下藏馆、片单（海报 / 列表 / 详情）、挑选、动态（时间线 / 任务详情 / 提交记录）与设置七个分区均无横向溢出与错误提示，控制台无错误；旧 hash 地址与 `/next` 跳转正确，前进后退可用；导入弹窗粘贴 JSON 预览正常。

## 1.46 - 2026-09-27

界面重构第 3 阶段：新界面（`/next`）上线挑选台与动态时间线，并可在影片详情里修正识别。

### 新增

- **挑选台**（`#/pick`，`GET /api/picks`）：按电影分组展示“有候选、已选定、无合格资源”的影片，筛选带数量；每部电影列出推荐 / 保底候选，可选定或取消；候选下载信息过期（服务重启或超过 2 小时）时提示并提供“重新寻片”；无合格资源时汇总被入馆标准排除的原因。
- **待入馆清单**：挑选台底部常驻深色清单栏，显示部数、体积与过期提示，可展开逐条移出；“提交入馆”复用原有提交流程（Emby / Transmission 复核、防重复提交、MoviePilot 分类与标签），并汇总提交、跳过、过期与暂缓结果。手机端清单位于底部标签栏上方。
- **动态时间线**（`#/timeline`，`GET /api/timeline`）：合并提交记录（已提交 / 已入馆 / 失败）、寻片任务、TMDB 识别、Emby 刷新、同步通知与少量设置变更（导入片单、站点增删改、修正识别等），按日期分组，可按“影片 / 系统”筛选、点击影片跳转详情；有进行中的任务时自动刷新。诊断日志仍在旧界面。
- **修正识别**（影片详情“识别不对？”）：`POST /api/films/{id}/recognize` 按导入原名重新识别单部影片；`GET /api/films/{id}/tmdb-matches` 在 TMDB 中查找、`POST /api/films/{id}/tmdb` 手动指定。修正会整体替换 TMDB 身份与海报、作废旧的 Emby 关联并立即重新核对 Emby；未找到可靠匹配时保留原识别。用于修正线上第 40 部《Cure》误识别、第 49 部《Yi Yi》未识别等情况。

### 调整

- 首页“去挑选 / 去提交 / 无合格资源”与影片详情“去提交”改为进入新挑选台；挑选、动态不再是过渡页，设置仍链接旧界面。
- 影片详情的候选行抽取为共享组件，挑选台与详情共用；候选查询改为按影片批量读取最近一次搜索结果。
- 提示条移到页面顶部，避免与底部清单栏重叠；过期候选的按钮改为中性样式。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：250 项全部通过（`tests/test_films.py` 新增 7 项：挑选队列分组与筛选、时间线来源合并 / 白名单 / 脱敏 / 按影片筛选、重新识别替换身份并复核 Emby、无匹配时保留原识别、手动指定清理旧关联、非法与不一致编号、TMDB 候选标记当前项）。
- `python3 -m compileall -q app`、`frontend` 的 `tsc --noEmit` 与 `vite build` 通过。
- 本地实例浏览器检查：1440px 与 375px 下挑选台、动态、影片详情均无横向溢出；手机端清单栏位于标签栏上方；全部过期时“提交入馆”禁用。

## 1.45 - 2026-09-27

界面重构第 1–2 阶段：新版“电影藏馆”界面以每一部电影为中心，先在 `/next` 与旧界面并行提供，共用同一后端与数据。

### 新增

- **统一的影片状态**（`app/services/films.py`）：服务端按“已入馆 → 下载中 → 已选定 → 寻片中 → 有候选 → 待识别 → 待核对 → 缺片”的优先级为每部电影计算唯一状态，另有“无合格资源”“提交失败”“候选已过期”三种异常标记；批量查询加一次 Transmission 快照，不逐片请求。Emby 未确认的影片显示为“待核对”，不再当作缺片。
- **以影片为中心的接口**：`GET /api/films`（按片单、状态、异常、关键词筛选与分页，返回各状态数量）、`GET /api/films/{id}`（识别、候选、被排除原因汇总、提交记录、最近一次搜索摘要）、`POST /api/films/{id}/search`（单片寻片，沿用搜索并发上限与重复任务检查）、`GET /api/home`（馆藏进度、需要你决定、进行中任务、最近入馆）。
- **TMDB 海报**：识别时保存 TMDB 海报路径（schema v5，`playlist_items.tmdb_poster_path`，只接受 TMDB 图片路径形状）；新增签名媒体接口 `/api/playlist-items/{id}/tmdb-poster`，旧数据首次访问时补取并记住“没有海报”，经现有出站校验与缓存上限代理，前端不接触 TMDB 密钥；未配置 TMDB 时不生成海报地址。
- **新界面（`frontend/`，Preact + Vite + TypeScript）**：
  - 顶部导航（藏馆 / 片单 / 挑选 / 动态 / 设置），手机为底部标签栏；hash 路由支持直接访问、刷新恢复与前进后退。
  - 藏馆首页：馆藏进度与分段进度条、唯一主按钮“为 N 部缺片寻片”（每批 50 部）、“需要你决定”清单、进行中任务、最近入馆海报。
  - 片单：海报墙与列表两种视图，状态与异常筛选（带数量）、查找、分页；影片详情抽屉（`#/films/:id` 可直链）显示入馆进度步骤、候选与选定、被排除原因、提交记录，支持单片寻片、识别片单与刷新 Emby 状态。
  - 设计令牌：“馆藏档案”（亮）与“午夜放映”（暗）两套主题只是同一组变量的不同取值，组件不按主题写选择器；与旧界面共用主题与访问令牌存储。
  - 挑选、动态、设置暂为过渡页，链接到旧界面对应页面。
- 旧界面侧栏新增“试用新界面”入口；`/next` 与首页同样无需令牌即可加载，所有数据接口仍需令牌。

### 调整

- 候选“同种聚合”逻辑抽取为 `app/services/candidates.py`，旧搜索页与新影片详情共用，行为不变。
- Docker 镜像增加 Node 构建阶段生成 `/next` 前端，运行时镜像不含 Node；npm 源默认 `registry.npmmirror.com`，可用 `NPM_REGISTRY` 覆盖。CI 增加前端类型检查与构建，并在容器冒烟测试中检查 `/next`。部署脚本同步源码时排除 `node_modules` 与本地构建产物。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：243 项全部通过（新增 `tests/test_films.py` 18 项：各状态与异常、筛选分页、详情脱敏、单片寻片、首页汇总、海报路径校验与缓存、`/next` 入口与鉴权）。
- `python3 -m compileall -q app`、`node --check`（旧界面脚本）、`frontend` 的 `tsc --noEmit` 与 `vite build` 通过；构建产物无内联脚本，符合现有 CSP。
- 在飞牛上以独立临时目录构建验证镜像（`node:22.16.0-alpine` 构建阶段 + Python 运行时），镜像内 `/next` 产物、新路由与版本号正确；验证后删除临时镜像与目录，线上容器未改动。飞牛的镜像加速源拉取 `node:22-*` 返回 401，部署脚本因此默认使用 NAS 本地的 `node:22.16.0-alpine`（可用 `AUTOLIST_NODE_IMAGE` 覆盖）。
- 本地实例（覆盖全部状态的模拟数据）浏览器检查：1440px 与 375px 下首页、片单（海报/列表）、影片详情、过渡页与 404 均无横向溢出；亮暗主题计算色正确。

## 1.44 - 2026-09-26

### 修复

- **移动端底部导航当前项不可见**：1.43 的主题规则 `html[data-theme] .nav-item.active` 同时作用于浅色底部导航，导致当前项白字白图标。主题导航规则收敛到 `.sidebar`，底部导航显式使用自身配色；三套主题下文字与图标对比度均不低于 5:1。
- **纯 RSS 站点重启后被改为 NexusPHP**：适配器修正迁移改为 `SCHEMA_VERSION 4` 的一次性迁移，按当前规则重算（有 Cookie 走 NexusPHP，只有 RSS 地址保持 RSS），并修复已被误改的站点；CookieCloud 为 RSS 站点写入 Cookie 时同步切换为 NexusPHP。
- **CookieCloud KEY 长度前后不一致**：设置、扩展上传、读取与本地存储统一使用同一格式定义（5–128 位），不再出现设置可保存但扩展推送 422。
- **识别完成后自动刷新 Emby 失败会把识别任务标为失败**：修正 `event_logger()` 调用；自动刷新失败只记录日志，不改写识别结果。
- **未配置 Emby 时影片被标为“待入库”**：识别后的自动刷新在未配置 Emby 时跳过，手动“刷新 Emby 状态”直接提示先配置。
- **TMDB 识别误配**：包含关系按完整单词比较（单字母片名不再命中任意含该字母的标题）；跨语言首项命中要求年份精确且原语言非英语，或它是唯一年份相近的候选。
- **站点访问频率**：`limit_interval`/`limit_count` 按 MoviePilot 语义实现为“周期内最多 N 次”，只设周期时为最小间隔；每站点加锁，并发搜索也按序等待。
- **M-Team 优惠**：识别 `_2X_FREE`、`_2X_PERCENT_50` 等双倍上传优惠，免费加权不再丢失，并在标签中显示 `2X`。
- **MoviePilot 站点同步**：已有站点只同步地址、Cookie、RSS、超时、代理等连接信息，保留本地名称、优先级、启用与参与搜索开关及自定义 UA；新站点导入 API Key；跳过无效地址并在结果中说明；CookieCloud 失败原因返回给界面。
- **CookieCloud**：本地无站点时的自动导入写入同步提示；单站“刷新 Cookie”与全量同步使用同一来源（优先服务器拉取）；远程拉取先校验 KEY 再拼接地址，并原子写入；`/api/cookiecloud/status` 不再返回未脱敏的服务器地址。
- **站点诊断**：连接检测与账户统计的错误转换为中文诊断，不再显示 httpx 原始英文错误与外部文档链接；未配置 Cookie/API Key 的站点不再发起账户统计请求；升级时清空旧的原始错误并重新读取。
- **导入与识别**：未配置 TMDB 时导入不再自动创建必然失败的识别任务并返回原因；识别任务在缺少 TMDB Key 时直接失败，不再逐部重复同一错误。
- 移除无效的 `PRAGMA incremental_vacuum`。

- **定时 Cookie 同步每小时把所有站点记为“已更新”**：Cookie 与现有值相同的站点不再写库，也不再重置账户统计缓存（此前会让全部站点的账户统计从 6 小时一次变成每小时重新读取），操作日志只在 Cookie 真正变化时记录；手动同步与单站刷新会提示“已是最新”。

### 界面

- **窗口宽度 ≤1160px 时内容紧贴侧栏**：侧栏已缩为 210px，但页面背景仍画 236px 的深色带，内容左侧看起来没有留白。背景色带改为跟随 `--sidebar-width`（移动端为 0），三套主题一致。
- **1024px 左右页面标题被挤成两行**：标题与英文眉题不再换行，空间不足时改为让顶部操作按钮换行；≤1160px 时服务状态只显示“N/4 服务在线”。
- **升级后浏览器继续使用旧前端模块**：`app.js` 导入的 `./js/core.js` 不带版本号，静态资源也没有缓存策略，浏览器会按启发式规则沿用旧模块（线上 1.44 首次部署后实测仍显示旧的容量格式）；新旧脚本混用时甚至可能因缺少导出而白屏。`/assets/` 静态资源改为 `Cache-Control: no-cache`，每次以 ETag 校验，未变化时返回 304。
- 站点上传/下载量超过 1024 GB 时显示为 TB（如 “47.2 TB”），不再显示 “48294 GB”。
- 设置页恢复显示 AutoList CookieCloud 推送地址与复制按钮，并说明“服务器拉取 / 扩展推送”两种方式。
- 片单分页“每页”不再竖排换行；站点卡片显示易读的协议名称，ARIA 属性移到卡片内的按钮上；站点详情空状态文案去掉已不存在的“地图”。
- 搜索页“搜索未下载影片”更名为“后台批量补全”并补充说明，与“开始搜索”区分。
- 操作日志新增导入片单、识别结束、站点增删改等事件。
- 移除已不再显示的来源地图代码（`app/static/js/site-map.js`、`app.js` 中约 500 行交互代码与对应 CSS）。

### 测试与维护

- 新增 `tests/test_audit_round3.py`（22 项），覆盖上述修复；前端模块契约改为校验所有 ES 模块导入。
- 测试基类在清理临时目录前取消并等待后台任务，消除 “unable to open database file” 噪声。
- 全量升级至 `1.44`，静态资源缓存标记更新为 `?v=1.44.0`。

### 验证

- `.venv/bin/python -m unittest discover -s tests`：225 项全部通过，无后台任务报错输出。
- `python3 -m compileall -q app`、`node --check`（`app.js`、`core.js`、`theme-init.js`）、`git diff --check` 通过。
- 本地隔离实例：v3→v4 迁移修复被误改的 RSS 站点；5 位 KEY 可推送；未配置 Emby 时手动刷新返回 422；未配置 TMDB 时导入跳过自动识别；浏览器检查全部页面在 375px、960px、1024px 与 1440px 下无横向溢出、标题单行、控制台无错误。

## 1.43 及更早版本摘要

1.44 之前的界面（`app/static` 下的旧界面、侧栏导航、主题布局分叉、站点星图等）已在 1.47 整体移除，相关逐版本的界面细节不再适用，这里只保留仍在生效的能力来源。1.25 及更早的完整原文可在 Git 历史中查看。

- **1.26–1.43（2026-09-18 ~ 09-20）**：放行受信任的内网下游服务；TMDB 匹配移除盲目兜底，增加跨语言别名、首映跨年 ±1 年容差与长片名退避；站点检索按 `limit_interval` 限频；访问令牌失败按客户端 IP 限流；Transmission 会话 ID 进程级缓存；日志异常堆栈脱敏与全局异常处理，SQLite 定期增量整理；CookieCloud 支持从远程服务器每小时拉取并解密，推送接收端支持 CORS / 私有网络访问预检与根路径别名；可从 MoviePilot 一键同步站点与 Cookie，修复带 RSS 地址的 NexusPHP 站点被误判为 RSS；修复 `DELETE` 请求因缺少 `Content-Length` 返回 411；导入片单后自动识别 TMDB 并刷新 Emby 状态。
- **1.22–1.25（2026-09-13 ~ 09-17）**：修复设置保存（留空用户名保留原值、TMDB 代理开关持久化）、搜索匹配与历史清理；服务状态区分“未配置”与“连接失败”；CI 发布版本标签与 `latest` 到 Docker Hub；默认 Compose 改为飞牛首次部署方案并取消必需的 `.env`；移除 Unraid 支持，新增 `scripts/deploy-fnos.sh` 局域网构建部署。
- **1.00–1.21（2026-08-02 ~ 08-22）**：访问令牌强度要求（≥32 字符）；所有出站请求逐跳校验重定向、拒绝危险地址与敏感查询参数；修复 TMDB 客户端丢失 `/3/` 前缀与代理配置；站点图标请求支持站点代理、超时与同源 User-Agent / Cookie；前端请求取消与缓存发布策略。其余为旧界面的主题与站点地图迭代。
- **0.74–0.99（2026-07-26 ~ 08-01）**：代码仓库初始提交（0.74）；下载历史按来源区分已提交、下载中、已入馆、待确认与失败；可选 `AUTOLIST_ACCESS_TOKEN`，CookieCloud 上传绑定用户 KEY；`main.py` 拆分为 `app/api`、`app/services`、`app/domain`；Compose 移除写死的 TMDB `extra_hosts`，改用代理；下载提交幂等保护与识别 / 入库 / 自动化任务并发上限；候选按做种数聚合、展示发布时间、识别伪造制作组；NexusPHP 与 M-Team 账户统计；结构化事件日志与轮转；搜索任务支持“重试失败站点”与“重新搜索整项”。
- **0.3.1–0.73（2026-07-11 ~ 07-26）**：片单导入（TMDB List / Collection、Letterboxd、IMDb、MDBList 网址与 XLSX / CSV / JSON 文件）；TMDB 识别并持久化中文名、原名、年份与 IMDb ID；Emby 按 TMDB / IMDb Provider ID 查询，实体文件优先于 `.strm`；下载固定经 MoviePilot `DownloadChain` 提交 Transmission；站点协议按地址自动识别（NexusPHP、M-Team、Torznab、RSS）；每部影片最多四站并发检索，检索词组合 IMDb、TMDB 原名、中文名与导入原名；入馆标准定为 `x265 + ADE / FRDS / HDS / CHD` 与保底 `x264 + CMCT`，硬排除 DIY、REMUX、WEB 与原盘；制作组词表可合并 MoviePilot 内置与自定义正则；片单定时增量同步与自动化；内置 CookieCloud 兼容接收端；Emby 海报代理；SQLite WAL 与定期数据清理。0.71、0.72 早于代码仓库，没有变更记录。
