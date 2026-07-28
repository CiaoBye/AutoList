# PlaylistAutoDown Docker 迁移方案

## 目标

将 PlaylistAutoDown 从 MoviePilot V2 插件迁移为独立 Docker 服务。

- Docker 服务负责片单、规则、搜索任务、候选、下载车、历史和独立页面。
- MoviePilot 继续负责站点搜索、媒体识别、分类下载、下载器调度、整理和通知。
- Emby 直接参与真实媒体路径查询，保留 `.strm` 不视为真实入库的判断。

## 已确认的 MoviePilot 能力

基于 MoviePilot 官方 `v2` 分支源码与 API 文档，以下能力已有公开 REST API：

| 能力 | MoviePilot API | Docker 用途 |
| --- | --- | --- |
| 精确搜索 | `GET /api/v1/search/media/{mediaid}` | 按 TMDB、豆瓣或 Bangumi ID 搜索资源 |
| 渐进搜索 | `GET /api/v1/search/media/{mediaid}/stream` | 通过 SSE 驱动实时搜索进度 |
| 标题搜索 | `GET /api/v1/search/title` | 无媒体 ID 时的标题兜底 |
| 完整下载 | `POST /api/v1/download/` | 传入 `media_in` 和 `torrent_in`，由 MP 决定分类目录 |
| 识别后下载 | `POST /api/v1/download/add` | 仅适用于无法提供完整媒体信息的兜底场景 |
| 下载器列表 | `GET /api/v1/download/clients` | Docker 配置页展示可选下载器 |
| 下载目录 | `GET /api/v1/download/paths` | 只读展示和显式路径选择 |
| 下载进度 | `GET /api/v1/download/` | 同步 MP 当前下载任务状态 |
| 媒体库存在性 | `GET /api/v1/mediaserver/exists` | 获取 MP 索引条目 ID 作为防重初筛 |

`POST /api/v1/download/` 在 MoviePilot 内部会构造媒体和种子上下文，再调用 `DownloadChain.download_single`。因此 Docker 不直接连接 Transmission，仍可保留 MP 的分类目录、下载器选择与通知链路。

## 现有插件功能迁移评估

| 当前插件能力 | 迁移方式 | 结论 |
| --- | --- | --- |
| JSON / xlsx 导入 | 迁移解析器与字段兼容逻辑 | 可直接迁移 |
| 搜索范围 | 保存到 Docker 数据库 | 可直接迁移 |
| 制作组、格式、分辨率排序 | 迁移为独立规则引擎 | 可直接迁移 |
| 跨站重复资源合并 | 迁移候选归一化与镜像逻辑 | 可直接迁移 |
| 下载车 | Docker 数据库中的任务选择记录 | 可直接迁移 |
| 单个/批量下载 | 改调 MP `POST /api/v1/download/` | 可迁移，需实机字段验证 |
| MP 分类下载 | 由 MP 下载 API 保留 | 可迁移，必须实测电影与剧集 |
| 搜索进度 | 消费 MP SSE 并持久化 Docker 任务状态 | 迁移后更完善 |
| Emby 防重 | MP 初筛 + Emby 路径复核 | 可迁移，新增 Emby 适配层 |

## STRM 防重策略

MoviePilot 的公开媒体库存在性接口只返回存在状态和媒体服务器条目 ID，不返回真实文件路径。因此 Docker 不应仅依据该接口跳过下载。

流程如下：

```text
Docker -> MP /mediaserver/exists
       -> 未找到：允许自动加入下载车
       -> 找到条目 ID：调用 Emby Items/{id}
          -> Path 后缀为 .strm：标记“已索引”，允许下载但默认不自动选中
          -> Path 为真实视频文件：标记“已入库”，允许展示资源但默认不自动选中
          -> 无法读取路径：标记“状态未知”，不阻止用户手动下载
```

Docker 将单独保存 Emby 地址和受限 API Key。该 Key 仅用于读取媒体条目路径，不用于下载、删除或写入媒体库。

## 推荐架构

```text
浏览器
  -> PlaylistAutoDown Docker
     -> FastAPI: REST API、SSE、任务调度
     -> 同容器静态页面：导入、搜索、候选、下载车、历史页面
     -> SQLite: 片单、规则、搜索任务、候选快照、下载历史
     -> MoviePilot API Client
     -> Emby API Client
  -> MoviePilot
     -> 站点 Cookie、索引搜索、媒体识别、分类下载、通知、整理
  -> Transmission
```

## 技术选型

| 层级 | 选型 | 原因 |
| --- | --- | --- |
| 后端 | Python 3.12 + FastAPI | 可最大化复用现有 Python 筛选逻辑，原生支持异步与 SSE |
| 数据库 | SQLite | 单用户 NAS 部署足够，备份和迁移简单 |
| 前端 | 原生静态页面，由 FastAPI 同容器托管 | 保持单一应用镜像，不增加 Node/Nginx 构建或运行容器 |
| HTTP 客户端 | httpx | 支持异步、超时与 SSE 流消费 |
| 容器 | Docker Compose | 统一配置、数据卷、升级与健康检查 |

不直接挂载 MoviePilot 配置目录，不读取 MP 数据库，不复制站点 Cookie。

## 必须先完成的实机验证

验证均为只读或可控操作，不应在未确认前批量下载：

1. 读取 MP 版本、下载器列表、下载目录列表。
2. 对一部电影与一部剧集调用 MP 搜索 API，检查返回的 `Context`、`media_info` 与 `torrent_info`。
3. 确认搜索结果中的 `torrent_info` 能作为 `POST /api/v1/download/` 的输入；下载测试只使用一个明确选择的候选。
4. 通过 MP 媒体库接口拿到 Emby 条目 ID。
5. 通过 Emby API 查询条目 `Path`，验证 `.strm` 与真实视频文件的判断。
6. 验证电影和剧集分别进入 MP 预期的分类下载目录。

## 分阶段迁移计划

### 阶段 1：API 兼容性验证

- 建立 MoviePilot 与 Emby 的只读探测脚本。
- 固化搜索结果、下载请求与 Emby 条目的 JSON 样本。
- 确认字段映射和分类下载结果。

验收：电影、剧集各完成一次搜索与受控下载，并进入正确目录。

### 阶段 2：Docker 服务骨架

- 新建独立服务目录、Dockerfile、Compose、环境变量模板与健康检查。
- 建立 SQLite 数据库及迁移机制。
- 实现 MoviePilot、Emby 客户端，统一超时、重试与错误处理。

验收：容器可启动，配置可保存，可测试连接 MP 与 Emby。

### 阶段 3：核心功能迁移

- 迁移 JSON / xlsx 导入。
- 迁移制作组、格式、分辨率规则与候选排序。
- 迁移重复资源合并、镜像选择、下载车和历史。
- 使用 MP SSE 实现后台搜索任务、进度、取消和恢复。

验收：导入片单后可后台搜索、选择候选并提交给 MP。

### 阶段 4：独立界面与防重

- 实现片单、搜索任务、候选详情、下载车和历史页面。
- 接入 MP 下载状态查询。
- 接入 Emby 路径复核与 `.strm` 状态展示。

验收：页面不依赖 MoviePilot 插件组件；`.strm`、真实入库与未知状态均可区分。

### 阶段 5：增强能力

- 多片单管理。
- 定时搜索、未命中重试、质量阈值。
- 失败重试与通知汇总。
- 导出搜索报告和下载审计日志。

## 配置边界

Docker 服务仅保存以下外部凭据：

- `MP_BASE_URL`
- `MP_API_KEY`
- `EMBY_BASE_URL`
- `EMBY_API_KEY`

所有凭据写入 `.env`，不提交到仓库、不输出到前端、不写入日志。

## 当前阻塞项

已完成只读实机核对：MoviePilot 为 v2.14.2，下载器为 Transmission，媒体服务器为 Emby，电影和电视剧下载目录均可读取；已验证当前 `PlaylistAutoDown` 的规则、历史、片单 xlsx（500 条）和一次站点候选搜索。

已完成单镜像 Docker 服务骨架：FastAPI、静态页面和 SQLite 由同一容器提供；不启用额外数据库、Nginx 或 Node 容器。旧插件的筛选规则和历史可经服务端 API 幂等导入，原始 xlsx 可导入为独立片单。

仍待用户在实际 Docker 主机执行受控下载验收：电影与剧集各选择一个候选，确认均由 `POST /api/v1/download/` 进入预期的 MoviePilot 分类目录；同时以真实 Emby Key 验证 `.strm` 和实体文件路径复核。
