import { useState } from "preact/hooks";
import { api } from "../api";
import { formatEta, formatRate, formatTime } from "../format";
import { useLoad } from "../hooks";
import { useAction } from "./settings/shared";
import type { HistoryCleared, HistoryRecord } from "../types";

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

type Transfer = NonNullable<HistoryRecord["transfer"]>;

const TRANSFER_STATE: Record<Transfer["state"], { label: string; className: string }> = {
  downloading: { label: "下载中", className: "is-ok" },
  queued: { label: "排队中", className: "" },
  checking: { label: "校验中", className: "" },
  stalled: { label: "停滞", className: "is-warn" },
  paused: { label: "已暂停", className: "is-warn" },
  error: { label: "出错", className: "is-bad" },
};

/** 下载中的提交：进度条、速度、剩余时间与连接的做种者；停滞、暂停与出错单独标出。 */
function TransferProgress({ transfer }: { transfer: Transfer }) {
  const state = TRANSFER_STATE[transfer.state];
  const parts = [
    `${transfer.percent}%`,
    transfer.state === "downloading" ? formatRate(transfer.rate_bps) : null,
    transfer.state === "downloading" ? formatEta(transfer.eta_seconds) : null,
    `${transfer.peers} 个做种者`,
  ].filter(Boolean);
  return (
    <span class={`transfer ${state.className}`}>
      <span class="transfer-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={transfer.percent} aria-label="下载进度">
        <span style={{ width: `${Math.min(100, Math.max(0, transfer.percent))}%` }} />
      </span>
      <span class="transfer-text">
        <strong>{state.label}</strong> <span class="mono">{parts.join(" · ")}</span>
      </span>
    </span>
  );
}

/** 提交记录：按 MoviePilot、Transmission 与 Emby 的状态投影出每次提交当前走到哪一步。 */
export function History() {
  // 有正在下载的提交时每 15 秒刷新一次进度。
  const history = useLoad<HistoryRecord[]>(
    (signal) => api<HistoryRecord[]>("/api/history", { signal }),
    [],
    (data) => (data?.some((row) => row.transfer) ? 15000 : null),
    "history",
  );
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
            void run(() => api<HistoryCleared>(`/api/history?status=${encodeURIComponent(status)}`, { method: "DELETE" }), (result) => `已清除 ${result.deleted} 条`, () => history.reload());
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
                    {row.transfer ? <TransferProgress transfer={row.transfer} /> : null}
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
