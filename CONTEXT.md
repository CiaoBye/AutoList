# AutoList Context

AutoList 是一个电影片单运营工作台：把有序影片转成可搜索资源，允许操作者选择候选资源，将选中的资源提交给 MoviePilot，并展示后续下载与媒体库结果。

## 术语

- **片单** — 由操作者导入或维护的有序影片集合；其中单个条目称为**影片**。
- **核心闭环** — 操作者从片单开始，经过资源搜索、候选选择、下载列表、提交、下载直到媒体库整理的完整路径。
- **候选资源** — 经过电影资源规则评估的站点结果。只有符合条件的候选资源可以选择；被排除的搜索结果仍可作为解释展示，但不可操作。
- **下载列表** — 提交前由操作者控制的资源选择队列。用户界面避免使用“下载车”。
- **已提交** — AutoList 已成功把选中的资源交给 MoviePilot；这不代表资源已经下载完成或整理入库。
- **下载中** — Transmission 中存在与已提交资源匹配的活动下载任务。
- **已整理 / 已入库** — Emby 确认对应影片已经进入媒体库；按照现有产品语义，`.strm` 条目仍属于**待入库**。
- **待确认** — 已存在提交记录，但暂时无法匹配或确认对应的 Transmission 或 Emby 状态。
- **失败** — 提交或后续状态处理失败，并提供脱敏、用户可读的原因。
- **部分完成** — 搜索任务已处理所选影片，但一个或多个站点尝试失败；失败范围可以重试。
- **搜索上下文过期** — 之前展示的候选资源已经无法提交，因为搜索上下文不可用；操作者必须重新搜索。
- **重试失败站点** — 只重新搜索失败的站点与影片组合。
- **重新搜索整项** — 对当前任务的完整搜索范围重新执行搜索。

_Avoid_ using “完成” as an unqualified status: it may mean search finished, submission succeeded, download finished, or media was organized. Use the source-backed term instead.

_Avoid_ treating a successful submission as “已整理”; MoviePilot and Transmission do not prove that Emby has the movie.

_Avoid_ automatic retry, automatic resource replacement, or automatic download in the closed-loop recovery experience. These actions can create duplicate tasks or download an unintended resource.
