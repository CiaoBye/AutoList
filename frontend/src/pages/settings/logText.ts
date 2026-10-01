import type { LogEvent } from "../../types";

/** 诊断日志事件的中文名称；没有列出的事件显示原始事件名。 */
export const EVENT_LABELS: Record<string, string> = {
  movie_search_summary: "电影搜索摘要",
  search_task_finished: "寻片任务结束",
  site_search_failed: "站点搜索失败",
  download_submit: "提交下载",
  cookiecloud_sync: "CookieCloud 同步",
  scheduler_cookiecloud_synced: "定时拉取 Cookie",
  scheduler_cookiecloud_sync_failed: "定时拉取 Cookie 失败",
  cookiecloud_expired_refreshed: "Cookie 失效后重新拉取",
  cookiecloud_expired_refresh_failed: "Cookie 失效后拉取失败",
  site_cookie_refreshed: "单站 Cookie 刷新",
  moviepilot_sites_synced: "从 MoviePilot 同步站点",
  settings_saved: "保存设置",
  sites_tested: "站点检测",
  site_added: "新增站点",
  site_updated: "更新站点",
  site_deleted: "删除站点",
  playlist_imported: "导入片单",
  playlist_synced: "片单来源同步",
  playlist_sync_failed: "片单同步失败",
  scheduler_playlist_sync_failed: "定时片单同步失败",
  scheduler_tick_failed: "定时调度异常",
  recognition_finished: "TMDB 识别结束",
  library_scan_auto_trigger_failed: "自动刷新 Emby 状态失败",
  film_reidentified: "修正识别",
  unhandled_server_error: "服务端异常",
};

export type LevelName = "INFO" | "WARNING" | "ERROR";

export const levelOf = (event: LogEvent): LevelName => {
  const level = String(event.level || "INFO").toUpperCase();
  return level === "ERROR" || level === "WARNING" ? level : "INFO";
};

export const LEVEL_TEXT: Record<LevelName, string> = { INFO: "信息", WARNING: "警告", ERROR: "错误" };
export const LEVEL_CLASS: Record<LevelName, string> = { INFO: "st-missing", WARNING: "st-candidates", ERROR: "st-issue" };

export const eventLabel = (event: LogEvent): string => EVENT_LABELS[event.event] || event.event;

/** 一行可读的事件说明：影片、站点、说明或原因，以及数量类字段。 */
export const describe = (event: LogEvent): string => {
  const parts: string[] = [];
  const text = (key: string) => (event[key] === undefined || event[key] === null ? "" : String(event[key]));
  if (text("movie")) parts.push(`${text("movie")}${text("rank") ? ` #${text("rank")}` : ""}`);
  if (text("site")) parts.push(`站点 ${text("site")}`);
  if (text("detail")) parts.push(text("detail"));
  else if (text("reason")) parts.push(text("reason"));
  if (text("status")) parts.push(`状态 ${text("status")}`);
  if (text("results")) parts.push(`返回 ${text("results")} 条`);
  if (text("kept")) parts.push(`保留 ${text("kept")} 个`);
  if (text("submitted")) parts.push(`提交 ${text("submitted")}`);
  if (text("skipped")) parts.push(`跳过 ${text("skipped")}`);
  if (text("error")) parts.push(text("error"));
  return parts.join(" · ");
};

const pad = (number: number) => String(number).padStart(2, "0");

/** 本地时间 HH:MM:SS。 */
export const clock = (value: string): string => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

/** 按本地日期分组的标题：今天 / 昨天 / M 月 D 日。 */
export const dayLabel = (value: string): string => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  const start = (day: Date) => new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
  const diff = Math.round((start(new Date()) - start(date)) / 86_400_000);
  const text = `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
  if (diff === 0) return `今天 · ${text}`;
  if (diff === 1) return `昨天 · ${text}`;
  return text;
};

/** 简短时间：今天显示 HH:MM，更早显示“昨天”或日期。 */
export const shortStamp = (value: string): string => {
  const label = dayLabel(value);
  if (label.startsWith("今天")) return clock(value).slice(0, 5);
  return label.startsWith("昨天") ? "昨天" : label;
};
