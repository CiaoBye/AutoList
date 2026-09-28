import { useState } from "preact/hooks";
import { api } from "../api";
import { formatTime } from "../format";
import { useLoad } from "../hooks";
import { useAction } from "./settings/shared";
import type { HistoryRow } from "../types";

const STATUS_OPTIONS: { id: string; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "submitted", label: "已提交" },
  { id: "downloading", label: "下载中" },
  { id: "pending_confirmation", label: "待确认" },
  { id: "pending_library", label: "待入库" },
  { id: "organized", label: "已入馆" },
  { id: "failed", label: "失败" },
];
const STATUS_CLASS: Record<string, string> = {
  submitted: "st-downloading",
  downloading: "st-searching",
  pending_confirmation: "st-candidates",
  pending_library: "st-candidates",
  organized: "st-in_library",
  failed: "st-issue",
};
const PAGE_SIZE = 30;

/** 提交记录：按 MoviePilot、Transmission 与 Emby 的状态投影出每次提交当前走到哪一步。 */
export function History() {
  const history = useLoad<HistoryRow[]>((signal) => api<HistoryRow[]>("/api/history", { signal }), []);
  const [status, setStatus] = useState("all");
  const [page, setPage] = useState(1);
  const { busy, run } = useAction();

  const rows = (history.data || []).filter((row) => status === "all" || row.lifecycle_status === status);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, pages);
  const visible = rows.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);
  const label = STATUS_OPTIONS.find((item) => item.id === status)?.label || "全部";

  return (
    <div>
      <div class="toolbar">
        <label class="field">
          <span>状态</span>
          <select value={status} onChange={(event) => { setStatus((event.target as HTMLSelectElement).value); setPage(1); }}>
            {STATUS_OPTIONS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
                {item.id === "all" ? ` (${history.data?.length ?? 0})` : ` (${(history.data || []).filter((row) => row.lifecycle_status === item.id).length})`}
              </option>
            ))}
          </select>
        </label>
        <span class="grow" style={{ flex: "1 1 auto" }} />
        <button class="btn" type="button" onClick={() => void history.reload()}>
          刷新状态
        </button>
        <button
          class="btn btn-danger"
          type="button"
          disabled={busy || !rows.length}
          onClick={() => {
            if (!window.confirm(`清除“${label}”分组的 ${rows.length} 条提交记录？已提交过的资源仍会被识别为重复，不会重新下载。`)) return;
            void run(() => api<{ deleted: number }>(`/api/history?status=${encodeURIComponent(status)}`, { method: "DELETE" }), (result) => `已清除 ${(result as { deleted: number }).deleted} 条`, () => history.reload());
          }}
        >
          清除这一组
        </button>
      </div>
      {history.error && !history.data ? <div class="notice notice-bad" role="alert">提交记录读取失败：{history.error.message}</div> : null}
      {!history.data && !history.error ? <p class="muted">正在读取提交记录……</p> : null}
      {history.data && !rows.length ? (
        <div class="card empty">
          <strong>{history.data.length ? "这一组没有记录" : "还没有提交记录"}</strong>
          <span>在挑选台提交入馆后，这里会跟踪每一次提交的下载与入库进度。</span>
        </div>
      ) : null}
      {visible.length ? (
        <div class="table-scroll">
          <table class="film-table">
            <thead>
              <tr>
                <th scope="col">状态</th>
                <th scope="col">影片与资源</th>
                <th scope="col" class="col-optional">站点</th>
                <th scope="col" class="col-optional">提交时间</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => (
                <tr key={row.id}>
                  <td>
                    <span class={`badge ${STATUS_CLASS[row.lifecycle_status] || "st-missing"}`}>{row.status_label}</span>
                  </td>
                  <td class="title-cell">
                    <strong>{row.title}</strong>
                    <span>{row.torrent_name}</span>
                    <span>
                      {row.status_reason}
                      {row.next_action && row.next_action !== "无需操作" ? ` · 下一步：${row.next_action}` : ""}
                    </span>
                  </td>
                  <td class="col-optional">{row.site_name || "—"}</td>
                  <td class="mono col-optional">{formatTime(row.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {pages > 1 ? (
        <nav class="pager" aria-label="分页">
          <span class="muted">第 {current} / {pages} 页 · 共 {rows.length} 条</span>
          <span class="actions">
            <button class="btn" type="button" disabled={current <= 1} onClick={() => setPage(current - 1)}>
              上一页
            </button>
            <button class="btn" type="button" disabled={current >= pages} onClick={() => setPage(current + 1)}>
              下一页
            </button>
          </span>
        </nav>
      ) : null}
    </div>
  );
}
