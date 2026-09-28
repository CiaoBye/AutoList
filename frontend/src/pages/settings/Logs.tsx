import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../api";
import { useLoad } from "../../hooks";
import type { LogEvent } from "../../types";
import { SectionHead, useAction } from "./shared";

const EVENT_LABELS: Record<string, string> = {
  movie_search_summary: "电影搜索摘要",
  search_task_finished: "搜索任务结束",
  site_search_failed: "站点搜索失败",
  download_submit: "下载提交",
  cookiecloud_sync: "CookieCloud 同步",
  cookiecloud_uploaded: "CookieCloud 推送",
  scheduler_cookiecloud_synced: "定时 Cookie 同步",
  scheduler_cookiecloud_sync_failed: "定时 Cookie 同步失败",
  site_cookie_refreshed: "单站 Cookie 刷新",
  moviepilot_sites_synced: "MoviePilot 站点同步",
  settings_saved: "保存系统设置",
  sites_tested: "站点连通检测",
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

const LEVEL_CLASS: Record<string, string> = { ERROR: "st-issue", WARNING: "st-candidates", INFO: "st-missing" };

const describe = (event: LogEvent): string => {
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

const formatStamp = (value: string): string => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

export function Logs() {
  const [level, setLevel] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const debounce = useRef<number | undefined>(undefined);
  const events = useLoad<LogEvent[]>(
    (signal) => api<LogEvent[]>(`/api/logs/events?limit=300&level=${encodeURIComponent(level)}&query=${encodeURIComponent(appliedQuery)}`, { signal }),
    [level, appliedQuery],
  );
  const { busy, run } = useAction();

  useEffect(() => () => window.clearTimeout(debounce.current), []);

  return (
    <div class="settings-form">
      <SectionHead
        title="诊断日志"
        actions={
          <>
            <button class="btn" type="button" onClick={() => void events.reload()}>
              刷新
            </button>
            <button
              class="btn btn-danger"
              type="button"
              disabled={busy}
              onClick={() => {
                if (!window.confirm("清空诊断日志？已轮转的旧日志文件不受影响。")) return;
                void run(() => api("/api/logs/events", { method: "DELETE" }), "日志已清空", () => events.reload());
              }}
            >
              清空
            </button>
          </>
        }
      >
        服务端的结构化事件日志（已脱敏），用于排查问题。日常进度请看“动态”。
      </SectionHead>
      <div class="toolbar">
        <label class="field">
          <span>级别</span>
          <select value={level} onChange={(event) => setLevel((event.target as HTMLSelectElement).value)}>
            <option value="">全部</option>
            <option value="INFO">信息</option>
            <option value="WARNING">警告</option>
            <option value="ERROR">错误</option>
          </select>
        </label>
        <label class="field field-grow">
          <span>关键词</span>
          <input
            type="search"
            value={query}
            placeholder="片名、站点、错误……"
            onInput={(event) => {
              const value = (event.target as HTMLInputElement).value;
              setQuery(value);
              window.clearTimeout(debounce.current);
              debounce.current = window.setTimeout(() => setAppliedQuery(value.trim()), 300);
            }}
          />
        </label>
      </div>
      {events.error && !events.data ? <div class="notice notice-bad" role="alert">日志读取失败：{events.error.message}</div> : null}
      {events.data && !events.data.length ? <div class="card empty"><strong>没有匹配的日志</strong></div> : null}
      {events.data && events.data.length ? (
        <ol class="card log-list">
          {events.data.map((event, index) => {
            const levelName = String(event.level || "INFO").toUpperCase();
            return (
              <li key={`${event.ts}-${index}`} class="log-item">
                <span class="mono muted log-time">{formatStamp(event.ts)}</span>
                <span class={`badge ${LEVEL_CLASS[levelName] || "st-missing"}`}>{levelName === "ERROR" ? "错误" : levelName === "WARNING" ? "警告" : "信息"}</span>
                <span class="log-text">
                  <strong>{EVENT_LABELS[event.event] || event.event}</strong>
                  {describe(event) ? <span>{describe(event)}</span> : null}
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}
