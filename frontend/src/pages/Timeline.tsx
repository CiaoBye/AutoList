import { api } from "../api";
import { useLoad } from "../hooks";
import { href, navigate, type Route } from "../router";
import type { Timeline, TimelineEvent } from "../types";
import { History } from "./History";
import { TaskDrawer } from "./TaskDrawer";

type Kind = "all" | "films" | "system";

const KIND_LABELS: Record<Kind, string> = { all: "全部", films: "影片", system: "系统" };
const LEVEL_COLORS: Record<TimelineEvent["level"], string> = {
  info: "var(--muted)",
  success: "var(--st-in_library-fg)",
  warning: "var(--st-candidates-fg)",
  error: "var(--issue-fg)",
};

const dayKey = (value: string | null): string => {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};

const dayLabel = (key: string): string => {
  if (key === "unknown") return "时间未知";
  const today = dayKey(new Date().toISOString());
  const yesterday = dayKey(new Date(Date.now() - 86400000).toISOString());
  if (key === today) return "今天";
  if (key === yesterday) return "昨天";
  const [, month, day] = key.split("-");
  return `${Number(month)} 月 ${Number(day)} 日`;
};

const timeLabel = (value: string | null): string => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
};

export function Timeline({ route }: { route: Route }) {
  const view = route.query.get("view") === "history" ? "history" : "timeline";
  const taskParam = route.query.get("task");
  const taskId = taskParam && /^\d+$/.test(taskParam) ? Number(taskParam) : null;
  const kind = (["films", "system"].includes(route.query.get("type") || "") ? route.query.get("type") : "all") as Kind;
  const baseQuery = { type: kind === "all" ? null : kind };
  const events = useLoad<{ items: TimelineEvent[] }>(
    (signal) => api<Timeline>(`/api/timeline?type=${kind}`, { signal }),
    [kind],
    // 有进行中的寻片或识别时 10 秒刷新一次，否则 60 秒。
    (data) => (data?.items.some((event) => /进行中|排队中/.test(event.detail)) ? 10000 : 60000),
  );

  const groups: { key: string; items: TimelineEvent[] }[] = [];
  for (const event of events.data?.items || []) {
    const key = dayKey(event.at);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(event);
    else groups.push({ key, items: [event] });
  }

  const tabs = (
    <div class="segmented tabs" role="tablist" aria-label="动态视图">
      <a role="tab" aria-selected={view === "timeline"} class="tab-link" href={href("/timeline")}>
        时间线
      </a>
      <a role="tab" aria-selected={view === "history"} class="tab-link" href={href("/timeline", { view: "history" })}>
        提交记录
      </a>
    </div>
  );

  if (view === "history") {
    return (
      <main class="page page-reading">
        <div class="page-head">
          <h1>动态</h1>
          <span class="grow" />
          {tabs}
        </div>
        <History />
      </main>
    );
  }

  return (
    <main class="page page-reading">
      <div class="page-head">
        <h1>动态</h1>
        <span class="grow" />
        {tabs}
      </div>
      <div class="chips" role="group" aria-label="按类型筛选">
        {(Object.keys(KIND_LABELS) as Kind[]).map((item) => (
          <a key={item} class="chip" aria-current={kind === item ? "true" : undefined} href={href("/timeline", { type: item === "all" ? null : item })}>
            {KIND_LABELS[item]}
          </a>
        ))}
        <a class="chip" href={href("/settings/logs")}>
          诊断日志
        </a>
      </div>

      {events.error && !events.data ? (
        <div class="notice notice-bad" role="alert">
          动态读取失败：{events.error.message}
        </div>
      ) : null}
      {!events.data && !events.error ? <p class="muted">正在读取动态……</p> : null}
      {events.data && !events.data.items.length ? (
        <div class="card empty">
          <strong>还没有动态</strong>
          <span>寻片、提交下载与入馆后，记录会按时间出现在这里。</span>
        </div>
      ) : null}

      {groups.map((group) => (
        <section key={group.key} class="timeline-day" aria-label={dayLabel(group.key)}>
          <h2 class="section-title timeline-date">{dayLabel(group.key)}</h2>
          <ol class="timeline card">
            {group.items.map((event) => (
              <li key={event.id} class="timeline-item">
                <span class="mono muted timeline-time">{timeLabel(event.at)}</span>
                <span class="dot" style={{ color: LEVEL_COLORS[event.level] }} aria-hidden="true" />
                <span class="timeline-text">
                  {event.task_id ? (
                    <a class="timeline-title" href={href("/timeline", { ...baseQuery, task: event.task_id })}>
                      {event.title}
                    </a>
                  ) : event.film_id ? (
                    <a class="timeline-title" href={href(`/films/${event.film_id}`)}>
                      {event.title}
                    </a>
                  ) : (
                    <strong class="timeline-title">{event.title}</strong>
                  )}
                  {event.detail ? <span class="timeline-detail">{event.detail}</span> : null}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ))}

      {taskId !== null ? (
        <TaskDrawer
          id={taskId}
          onClose={() => navigate(href("/timeline", baseQuery))}
          onChanged={() => void events.reload()}
        />
      ) : null}
    </main>
  );
}
