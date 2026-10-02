import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../../api";
import { useLoad, useToast } from "../../hooks";
import type { LogEvent } from "../../types";
import { clock, dayLabel, describe, eventLabel, LEVEL_CLASS, LEVEL_TEXT, levelOf } from "./logText";
import { SectionHead, useAction } from "./shared";

const PAGE_SIZE = 100;

const LEVEL_FILTERS = [
  { value: "", label: "全部" },
  { value: "WARNING", label: "警告" },
  { value: "ERROR", label: "错误" },
];

/** 列表中的一行：一条事件，或折叠后的一串连续相同事件（最新的在前）。 */
interface Row {
  key: string;
  day: string;
  events: LogEvent[];
}

const buildRows = (events: LogEvent[], collapse: boolean): Row[] => {
  const rows: Row[] = [];
  events.forEach((event, index) => {
    const day = dayLabel(event.ts);
    const last = rows[rows.length - 1];
    if (collapse && last && last.day === day && last.events[0].event === event.event && levelOf(last.events[0]) === levelOf(event)) {
      last.events.push(event);
    } else {
      rows.push({ key: `${event.ts}-${index}`, day, events: [event] });
    }
  });
  return rows;
};

/** 事件的全部字段（时间、级别、事件名之外），展开后逐项显示。 */
const detailFields = (event: LogEvent): [string, string][] =>
  Object.entries(event)
    .filter(([key, value]) => !["ts", "level", "event"].includes(key) && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [key, typeof value === "object" ? JSON.stringify(value) : String(value)]);

export function Logs() {
  const toast = useToast();
  const [level, setLevel] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [collapse, setCollapse] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [older, setOlder] = useState<LogEvent[]>([]);
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const debounce = useRef<number | undefined>(undefined);
  const filter = `limit=${PAGE_SIZE}&level=${encodeURIComponent(level)}&query=${encodeURIComponent(appliedQuery)}`;
  const first = useLoad<LogEvent[]>((signal) => api<LogEvent[]>(`/api/logs/events?${filter}`, { signal }), [filter]);
  const { busy, run } = useAction();

  useEffect(() => () => window.clearTimeout(debounce.current), []);
  // 首页数据变化（筛选、刷新）时丢掉已加载的更早记录，重新从最新的开始。
  useEffect(() => {
    setOlder([]);
    setExhausted(Boolean(first.data && first.data.length < PAGE_SIZE));
    setOpen(null);
  }, [first.data]);

  const events = [...(first.data || []), ...older];
  const rows = buildRows(events, collapse);

  const loadMore = async () => {
    const oldest = events[events.length - 1];
    if (!oldest) return;
    setLoadingMore(true);
    try {
      const page = await api<LogEvent[]>(`/api/logs/events?${filter}&before=${encodeURIComponent(oldest.ts)}`);
      setOlder((current) => [...current, ...page]);
      if (page.length < PAGE_SIZE) setExhausted(true);
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "读取更早的日志失败");
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div class="settings-form">
      <SectionHead
        title="诊断日志"
        actions={
          <>
            <button class="btn" type="button" onClick={() => void first.reload()}>
              刷新
            </button>
            <button
              class="btn btn-danger"
              type="button"
              disabled={busy}
              onClick={() => {
                if (!window.confirm("清空诊断日志？已轮转的旧日志文件不受影响。")) return;
                void run(() => api("/api/logs/events", { method: "DELETE" }), "日志已清空", () => first.reload());
              }}
            >
              清空
            </button>
          </>
        }
      />

      <div class="log-filters">
        <div class="chips" role="group" aria-label="按级别筛选">
          {LEVEL_FILTERS.map((item) => (
            <button key={item.value || "all"} class="chip" type="button" aria-current={level === item.value ? "true" : undefined} onClick={() => setLevel(item.value)}>
              {item.value ? <span class={`dot dot-${item.value === "ERROR" ? "bad" : "warn"}`} aria-hidden="true" /> : null}
              {item.label}
            </button>
          ))}
        </div>
        <input
          class="log-search"
          type="search"
          value={query}
          placeholder="片名、站点、错误……"
          aria-label="搜索日志"
          onInput={(event) => {
            const value = (event.target as HTMLInputElement).value;
            setQuery(value);
            window.clearTimeout(debounce.current);
            debounce.current = window.setTimeout(() => setAppliedQuery(value.trim()), 300);
          }}
        />
        <label class="log-collapse">
          <input type="checkbox" checked={collapse} onChange={(event) => setCollapse((event.target as HTMLInputElement).checked)} />
          折叠重复事件
        </label>
      </div>

      {first.error && !first.data ? <div class="notice notice-bad" role="alert">日志读取失败：{first.error.message}</div> : null}
      {!first.data && !first.error ? <p class="muted">正在读取日志……</p> : null}
      {first.data && !events.length ? <div class="card empty"><strong>没有匹配的日志</strong></div> : null}
      {events.length ? (
        <div class="card log-table">
          {rows.map((row, index) => {
            const latest = row.events[0];
            const oldest = row.events[row.events.length - 1];
            const level = levelOf(latest);
            const grouped = row.events.length > 1;
            const expanded = open === row.key;
            const summary = grouped
              ? `${clock(oldest.ts).slice(0, 5)} – ${clock(latest.ts).slice(0, 5)} · ${describe(latest) || "详情见展开"}`
              : describe(latest);
            return (
              <div key={row.key} class="log-block">
                {index === 0 || rows[index - 1].day !== row.day ? <div class="log-day">{row.day}</div> : null}
                <button class="log-row" type="button" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : row.key)}>
                  <span class="mono log-clock">{clock(latest.ts)}</span>
                  <span class={`badge ${LEVEL_CLASS[level]}`}>{LEVEL_TEXT[level]}</span>
                  <span class="log-event">
                    <span class="log-event-name">{eventLabel(latest)}</span>
                    {grouped ? <span class="log-repeat">×{row.events.length}</span> : null}
                  </span>
                  <span class="log-summary">{summary}</span>
                  <span class="log-toggle" aria-hidden="true">{expanded ? "▾" : "▸"}</span>
                </button>
                {expanded ? (
                  <div class="log-detail">
                    {grouped ? (
                      row.events.map((event, position) => (
                        <div key={`${event.ts}-${position}`} class="log-detail-row">
                          <span class="mono muted">{clock(event.ts)}</span>
                          <span>{describe(event) || eventLabel(event)}</span>
                        </div>
                      ))
                    ) : (
                      <dl class="log-fields">
                        <div><dt>事件</dt><dd class="mono">{latest.event}</dd></div>
                        {detailFields(latest).map(([key, value]) => (
                          <div key={key}><dt>{key}</dt><dd>{value}</dd></div>
                        ))}
                      </dl>
                    )}
                  </div>
                ) : null}
              </div>
            );
          })}
          <div class="log-foot">
            <span class="muted">
              已显示最近 {events.length} 条{collapse && rows.length < events.length ? `（折叠后 ${rows.length} 行）` : ""}
            </span>
            {exhausted ? (
              <span class="muted">没有更早的日志了</span>
            ) : (
              <button class="btn btn-small" type="button" disabled={loadingMore} onClick={() => void loadMore()}>
                {loadingMore ? "读取中…" : `加载更早的 ${PAGE_SIZE} 条`}
              </button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
