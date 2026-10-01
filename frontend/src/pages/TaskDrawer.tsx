import { useEffect, useRef } from "preact/hooks";
import { api } from "../api";
import { formatTime } from "../format";
import { useLoad } from "../hooks";
import { useAction } from "./settings/shared";
import type { SearchAttempts, SearchTask, SearchTaskLog, SearchTaskStarted } from "../types";
import { DrawerLayer } from "../components/DrawerLayer";

const STATUS_TEXT: Record<string, string> = {
  queued: "排队中", running: "进行中", completed: "已完成", partial: "部分完成", failed: "失败",
  cancelled: "已取消", interrupted: "服务重启中断", archived: "已归档",
};

interface TaskBundle {
  task: SearchTask;
  attempts: SearchAttempts;
  logs: SearchTaskLog[];
}

/** 寻片任务详情：逐站点结果、任务日志，以及取消 / 重试失败站点 / 重新开始。 */
export function TaskDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: (newId?: number) => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const bundle = useLoad<TaskBundle>(
    async (signal) => {
      const [task, attempts, logs] = await Promise.all([
        api<SearchTask>(`/api/search-tasks/${id}`, { signal }),
        api<SearchAttempts>(`/api/search-tasks/${id}/attempts?limit=300`, { signal }),
        api<SearchTaskLog[]>(`/api/search-tasks/${id}/logs?limit=120`, { signal }),
      ]);
      return { task, attempts, logs };
    },
    [id],
    (data) => (data && ["queued", "running"].includes(data.task.status) ? 4000 : null),
  );
  const { busy, run } = useAction();

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [id]);

  const data = bundle.data;
  const task = data?.task;
  const running = task ? ["queued", "running"].includes(task.status) : false;
  const failed = Number(task?.attempt_summary.failed || 0);

  const followup = (action: "retry" | "restart", success: string) =>
    void run(
      () => api<SearchTaskStarted>(`/api/search-tasks/${id}/${action}`, { method: "POST" }),
      success,
      () => {
        onChanged();
      },
    );

  return (
    <DrawerLayer>
      <button class="drawer-backdrop" type="button" aria-label="关闭任务详情" tabIndex={-1} onClick={onClose} />
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="task-title">
        <div class="drawer-head">
          <h2 id="task-title" class="section-title">寻片任务 #{id}</h2>
          <button ref={closeRef} class="btn icon-btn" type="button" aria-label="关闭" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>
        {bundle.error && !data ? <div class="notice notice-bad" role="alert">任务读取失败：{bundle.error.message}</div> : null}
        {!data && !bundle.error ? <p class="muted">正在读取任务……</p> : null}
        {task && data ? (
          <>
            <section class="panel">
              <div class="stat-row">
                <span><strong>{STATUS_TEXT[task.status] || task.status}</strong><small>状态</small></span>
                <span><strong class="num">{task.completed} / {task.total}</strong><small>影片</small></span>
                <span><strong class="num">{task.matched}</strong><small>找到合格资源</small></span>
                <span><strong class="num">{failed}</strong><small>站点失败次数</small></span>
              </div>
              <span class="muted" style={{ fontSize: "13px" }}>
                创建于 {formatTime(task.created_at)} · 更新于 {formatTime(task.updated_at)}
                {task.error_message ? ` · ${task.error_message}` : ""}
              </span>
              <span class="actions">
                {running ? (
                  <button class="btn btn-small btn-danger" type="button" disabled={busy} onClick={() => void run(() => api(`/api/search-tasks/${id}/cancel`, { method: "POST" }), "任务已取消", async () => { await bundle.reload(); onChanged(); })}>
                    取消任务
                  </button>
                ) : null}
                {!running && failed > 0 ? (
                  <button class="btn btn-small" type="button" disabled={busy} onClick={() => followup("retry", "已开始重试失败的站点")}>
                    重试失败的站点
                  </button>
                ) : null}
                {!running ? (
                  <button class="btn btn-small" type="button" disabled={busy} onClick={() => followup("restart", "已重新开始整个任务")}>
                    重新开始
                  </button>
                ) : null}
              </span>
            </section>

            {data.attempts.sites.length ? (
              <section>
                <h3 style={{ margin: "0 0 8px", fontSize: "15px" }}>各站点结果</h3>
                <table class="film-table">
                  <thead>
                    <tr>
                      <th scope="col">站点</th>
                      <th scope="col">成功 / 失败</th>
                      <th scope="col">平均耗时</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.attempts.sites.map((site) => (
                      <tr key={`${site.site_id}-${site.site_name}`}>
                        <td>{site.site_name}</td>
                        <td class="mono">
                          {site.succeeded} / <span class={site.failed ? "text-issue" : ""}>{site.failed}</span>
                        </td>
                        <td class="mono">{site.average_ms == null ? "—" : `${site.average_ms}ms`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ) : null}

            {data.attempts.items.some((item) => item.status === "failed") ? (
              <section>
                <h3 style={{ margin: "0 0 4px", fontSize: "15px" }}>失败明细</h3>
                <ul class="history-list">
                  {data.attempts.items.filter((item) => item.status === "failed").slice(0, 40).map((item) => (
                    <li key={item.id}>
                      <span>#{item.rank_no} {item.original_title} · {item.site_name}</span>
                      <span class="muted">{item.error_message || "失败"}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {data.logs.length ? (
              <details>
                <summary class="muted">任务日志（{data.logs.length}）</summary>
                <ol class="log-list compact">
                  {data.logs.map((log) => (
                    <li key={log.id} class="log-item">
                      <span class="mono muted log-time">{formatTime(log.created_at)}</span>
                      <span class={`badge ${log.level === "error" ? "st-issue" : log.level === "warning" ? "st-candidates" : "st-missing"}`}>{log.stage}</span>
                      <span class="log-text"><span>{log.message}</span></span>
                    </li>
                  ))}
                </ol>
              </details>
            ) : null}
          </>
        ) : null}
      </aside>
    </DrawerLayer>
  );
}
