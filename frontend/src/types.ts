/**
 * 接口数据类型。
 *
 * 全部来自后端的接口声明（`app/responses.py` → `frontend/openapi.json` → `npm run gen:api` 生成的
 * `src/api-schema.d.ts`），这里只起前端使用的别名；不要在这里手写接口字段，后端改了返回格式后
 * 重新导出 openapi.json，字段对不上时 `tsc` 会报错。
 */
import type { components } from "./api-schema";

type Schemas = components["schemas"];

export type Film = Schemas["Film"];
export type FilmStatus = Film["status"];
export type FilmIssue = Film["issues"][number];
export type Playlist = Schemas["PlaylistSummary"];
export type Counts = FilmPage["counts"];
export type FilmPage = Schemas["FilmPage"];
export type SiteOption = Schemas["SiteOption"];
export type Candidate = Schemas["Candidate"];
export type FilmHistoryRecord = Schemas["FilmHistoryRecord"];
export type FilmSearchSummary = Schemas["FilmSearchSummary"];
export type FilmDetail = Schemas["FilmDetail"];
export type TmdbMatch = Schemas["TmdbMatch"];

export type ActiveTask = Schemas["ActiveTask"];
export type Todo = Schemas["Todo"];
export type HomeData = Schemas["HomeData"];
export type SyncResult = Schemas["SyncResult"];
export type SyncStatus = Schemas["SyncStatus"];
export type SyncWebhooks = Schemas["SyncWebhooks"];

export type PickItem = Schemas["PickItem"];
export type PickBucket = PickItem["bucket"];
export type PickPage = Schemas["PickPage"];

export type TimelineEvent = Schemas["TimelineEvent"];
export type Timeline = Schemas["Timeline"];

export type SelectionItem = Schemas["SelectionItem"];
export type SubmitResult = Schemas["SubmitResult"];
export type HistoryRecord = Schemas["HistoryRecord"];
export type DownloadsPage = Schemas["DownloadsPage"];
export type DownloadItem = Schemas["DownloadItem"];
export type HistoryCleared = Schemas["HistoryCleared"];

export type SearchTask = Schemas["SearchTask"];
export type SearchAttempts = Schemas["SearchAttempts"];
export type SearchTaskLog = Schemas["SearchTaskLog"];
export type SearchTaskStarted = Schemas["SearchTaskStarted"];

export type Site = Schemas["Site"];
export type SiteCheck = Schemas["SiteCheck"];
export type SiteChecks = Schemas["SiteChecks"];
export type SiteSaved = Schemas["SiteSaved"];
export type MoviePilotSitesSynced = Schemas["MoviePilotSitesSynced"];
export type CookieCloudSynced = Schemas["CookieCloudSynced"];
export type SiteCookieRefreshed = Schemas["SiteCookieRefreshed"];
export type CookieCloudStatus = Schemas["CookieCloudStatus"];

export type PlaylistRow = Schemas["PlaylistRow"];
export type ImportPreview = Schemas["ImportPreview"];
export type PlaylistImported = Schemas["PlaylistImported"];
export type PlaylistSynced = Schemas["PlaylistSynced"];
export type AutomationStarted = Schemas["AutomationStarted"];

export type ProviderStatus = Schemas["ProviderCheck"];
export type ConnectionStatus = Schemas["ConnectionStatus"];
/** 运行设置；设置表单按字段名读写，因此附带索引签名。 */
export type RuntimeSettings = Schemas["PublicSettings"] & Record<string, string | number | boolean | null | undefined>;
export type CandidateAnalysis = Schemas["CandidateAnalysis"];
export type ReleaseGroupCatalog = Schemas["ReleaseGroupCatalog"];
export type LogEvent = Schemas["LogEvent"];
