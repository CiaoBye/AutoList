import { useEffect, useState } from "preact/hooks";
import { api } from "../api";
import { Poster } from "../components/Poster";
import { formatEta, formatRate, formatSize, formatTime } from "../format";
import { SyncButton } from "../components/SyncButton";
import { useLoad } from "../hooks";
import { href, navigate, type Route } from "../router";
import type { DownloadItem, DownloadsPage } from "../types";

type State = DownloadItem["state"];
type Filter = "all" | "active" | "queued" | "stalled" | "paused" | "error" | "seeding" | "completed";

const STATE_LABELS: Record<State, { label: string; className: string }> = {
  downloading: { label: "下载中", className: "is-ok" },
  checking: { label: "校验中", className: "" },
  stalled: { label: "停滞", className: "is-warn" },
  error: { label: "出错", className: "is-bad" },
  queued: { label: "排队中", className: "" },
  paused: { label: "已暂停", className: "is-warn" },
  seeding: { label: "做种中", className: "is-done" },
  completed: { label: "已完成", className: "is-done" },
};

const FILTERS: { id: Filter; label: string; states: State[] }[] = [
  { id: "all", label: "全部", states: [] },
  { id: "active", label: "下载中", states: ["downloading", "checking"] },
  { id: "queued", label: "排队", states: ["queued"] },
  { id: "stalled", label: "停滞", states: ["stalled"] },
  { id: "paused", label: "暂停", states: ["paused"] },
  { id: "error", label: "出错", states: ["error"] },
  { id: "seeding", label: "做种", states: ["seeding"] },
  { id: "completed", label: "已完成", states: ["completed"] },
];

type Kind = "movie" | "tv";
const KINDS: { id: Kind; label: string }[] = [
  { id: "movie", label: "影片" },
  { id: "tv", label: "剧集" },
];
// 电影与剧集分开列：剧集不在片单里，也不参与入馆；认不出类型的先放在“影片”里。
const kindOf = (item: DownloadItem): Kind => (item.media_type === "tv" ? "tv" : "movie");

const PAGE_SIZES = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

/** 不在片单的种子没有影片编号，用 hash 换一个稳定的海报占位色。 */
const posterSeed = (hash: string): number => parseInt(hash.slice(0, 6), 16) || 0;

const matches = (item: DownloadItem, filter: Filter, kind: Kind, query: string): boolean => {
  if (kindOf(item) !== kind) return false;
  const states = FILTERS.find((entry) => entry.id === filter)?.states || [];
  if (states.length && !states.includes(item.state)) return false;
  if (!query) return true;
  const text = `${item.film_title || ""} ${item.name} ${item.site || ""}`.toLowerCase();
  return text.includes(query.toLowerCase());
};

/** 一个种子：有对应影片时显示海报与片名（点开影片详情），否则只显示种子名。 */
function DownloadRow({ item }: { item: DownloadItem }) {
  const state = STATE_LABELS[item.state];
  const moving = item.state === "downloading";
  const facts = [
    `${item.percent}%`,
    item.size ? `${formatSize(item.downloaded)} / ${formatSize(item.size)}` : null,
    moving ? `↓ ${formatRate(item.rate_down)}` : null,
    item.rate_up ? `↑ ${formatRate(item.rate_up)}` : null,
    moving ? formatEta(item.eta_seconds) : null,
    item.state === "seeding" || item.state === "completed" ? null : `${item.seeders} 个做种者`,
    item.ratio != null ? `分享率 ${item.ratio}` : null,
  ].filter(Boolean);
  return (
    <li class={`download-row ${state.className}`}>
      {item.film_id ? (
        <a class="download-poster" href={href(`/films/${item.film_id}`)} aria-label={`打开《${item.film_title}》详情`}>
          <Poster id={item.film_id} title={item.film_title || item.name} url={item.poster_url} />
        </a>
      ) : item.film_title ? (
        <span class="download-poster">
          <Poster id={posterSeed(item.hash)} title={item.film_title} url={item.poster_url} />
        </span>
      ) : (
        <span class="download-poster download-poster-empty" aria-hidden="true">
          <svg viewBox="0 0 22 22" width="22" height="22"><path d="M11 3v11 M6 9l5 5 5-5 M4 19h14" fill="none" stroke="currentColor" stroke-width="1.6" /></svg>
        </span>
      )}
      <div class="download-main">
        <div class="download-title">
          {item.film_id ? (
            <a href={href(`/films/${item.film_id}`)}>
              <strong>{item.film_title}</strong>
              {item.film_year ? <span class="muted"> · {item.film_year}</span> : null}
            </a>
          ) : item.film_title ? (
            <span>
              <strong>{item.film_title}</strong>
              {item.film_year ? <span class="muted"> · {item.film_year}</span> : null}
              <span class="download-tag">{item.media_type === "tv" ? "剧集" : "不在片单"}</span>
            </span>
          ) : (
            <span>
              <strong class="mono download-name">{item.name}</strong>
              {item.media_type === "tv" ? <span class="download-tag">剧集</span> : null}
            </span>
          )}
          <span class={`download-state ${state.className}`}>{state.label}</span>
        </div>
        {item.film_title ? <span class="download-name mono muted">{item.name}</span> : null}
        <span class="transfer-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={item.percent} aria-label="下载进度">
          <span style={{ width: `${Math.min(100, Math.max(0, item.percent))}%` }} />
        </span>
        <span class="download-facts">
          {item.site ? <span class="download-site">{item.site}</span> : null}
          <span class="mono">{facts.join(" · ")}</span>
          {item.added_at ? <span class="muted">添加于 {formatTime(item.added_at)}</span> : null}
        </span>
        {item.error ? <span class="notice notice-bad download-error">Transmission 报错：{item.error}</span> : null}
      </div>
    </li>
  );
}

/** 下载：Transmission 里全部种子的实时状态（只读），不用切到 Transmission 查看。 */
export function Downloads({ route }: { route: Route }) {
  const filter = (FILTERS.find((entry) => entry.id === route.query.get("state"))?.id || "all") as Filter;
  const kind: Kind = route.query.get("kind") === "tv" ? "tv" : "movie";
  const query = route.query.get("q") || "";
  const sizeParam = Number(route.query.get("size"));
  const pageSize = PAGE_SIZES.includes(sizeParam) ? sizeParam : DEFAULT_PAGE_SIZE;
  const pageParam = Math.max(1, Number(route.query.get("page")) || 1);
  const [search, setSearch] = useState(query);
  useEffect(() => setSearch(query), [query]);

  // 有种子在下载、校验或排队时每 5 秒刷新，否则 30 秒。
  const page = useLoad<DownloadsPage>(
    (signal) => api<DownloadsPage>("/api/downloads", { signal, timeoutMs: 20000 }),
    [],
    (data) => (data?.items.some((item) => ["downloading", "checking", "queued", "stalled"].includes(item.state)) ? 5000 : 30000),
    "downloads",
  );
  const data = page.data;
  const items = data?.items || [];
  const filtered = items.filter((item) => matches(item, filter, kind, query));
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(pageParam, pages);
  const visible = filtered.slice((current - 1) * pageSize, current * pageSize);
  // 筛选、查找、每页数量与页码都写在地址里，刷新与前进后退可以恢复；改筛选时回到第一页。
  const link = (changes: { state?: Filter; kind?: Kind; q?: string; page?: number; size?: number }) => {
    const nextState = changes.state ?? filter;
    const nextKind = changes.kind ?? kind;
    const nextSize = changes.size ?? pageSize;
    return href("/downloads", {
      state: nextState === "all" ? null : nextState,
      kind: nextKind === "movie" ? null : nextKind,
      q: (changes.q ?? query).trim() || null,
      size: nextSize === DEFAULT_PAGE_SIZE ? null : nextSize,
      page: changes.page && changes.page > 1 ? changes.page : null,
    });
  };
  const counts = data?.summary?.counts || {};
  const kindItems = items.filter((item) => kindOf(item) === kind);
  const kindCount = (id: Kind) => items.filter((item) => kindOf(item) === id).length;
  const countFor = (entry: (typeof FILTERS)[number]) =>
    entry.states.length ? kindItems.filter((item) => entry.states.includes(item.state)).length : kindItems.length;
  const attention = (counts.stalled || 0) + (counts.error || 0) + (counts.paused || 0);

  const setQuery = (value: string) => {
    setSearch(value);
    navigate(link({ q: value }), true);
  };

  return (
    <main class="page">
      <div class="page-head">
        <h1>下载</h1>
        <span class="grow" />
        <SyncButton onDone={() => page.reload()} />
        {data?.checked_at ? (
          <span class="download-head-meta">
            更新于 {formatTime(data.checked_at)}
            {data.web_url ? (
              <>
                <span aria-hidden="true">·</span>
                <a href={data.web_url} target="_blank" rel="noopener noreferrer" title="暂停、删除等操作在 Transmission 里做">
                  打开 Transmission
                  <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><path d="M4 2h6v6 M10 2 3 9" fill="none" stroke="currentColor" stroke-width="1.4" /></svg>
                </a>
              </>
            ) : null}
          </span>
        ) : null}
      </div>

      {page.error && !data ? (
        <div class="notice notice-bad" role="alert">
          下载状态读取失败：{page.error.message}
          <button class="btn btn-small" type="button" onClick={() => void page.reload()}>
            重试
          </button>
        </div>
      ) : null}
      {!data && !page.error ? <p class="muted">正在读取 Transmission……</p> : null}

      {data && !data.configured ? (
        <div class="card empty">
          <strong>还没有配置 Transmission</strong>
          <a class="btn btn-primary" href={href("/settings/services")}>
            去服务连接
          </a>
        </div>
      ) : null}
      {data?.error && data.configured ? (
        <div class="notice notice-bad" role="alert">
          {data.error}
          <button class="btn btn-small" type="button" onClick={() => void page.reload()}>
            重试
          </button>
        </div>
      ) : null}

      {data?.summary ? (
        <section class="card download-summary" aria-label="下载概况">
          <span><strong class="num">↓ {formatRate(data.summary.download_bps)}</strong><small>下载速度</small></span>
          <span><strong class="num">↑ {formatRate(data.summary.upload_bps)}</strong><small>上传速度</small></span>
          <span><strong class="num">{(counts.downloading || 0) + (counts.checking || 0)}</strong><small>下载中</small></span>
          <span><strong class="num">{counts.queued || 0}</strong><small>排队</small></span>
          <span class={attention ? "is-warn" : undefined}><strong class="num">{attention}</strong><small>停滞 / 暂停 / 出错</small></span>
          <span><strong class="num">{counts.seeding || 0}</strong><small>做种</small></span>
          <span><strong class="num">{data.summary.free_bytes == null ? "—" : formatSize(data.summary.free_bytes)}</strong><small>剩余空间</small></span>
        </section>
      ) : null}

      {data?.summary ? (
        <div class="download-toolbar">
          <div class="chips" role="group" aria-label="按下载状态筛选">
            {FILTERS.map((entry) => (
              <a
                key={entry.id}
                class={`chip${(entry.id === "stalled" || entry.id === "error") && countFor(entry) ? " chip-issue" : ""}`}
                aria-current={filter === entry.id ? "true" : undefined}
                href={link({ state: entry.id, page: 1 })}
              >
                {entry.label} <span class="count">{countFor(entry)}</span>
              </a>
            ))}
          </div>
          <div class="download-toolbar-end">
          <div class="segmented" role="group" aria-label="影片与剧集">
            {KINDS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                aria-pressed={kind === entry.id}
                onClick={() => navigate(link({ kind: entry.id, state: "all", page: 1 }), true)}
              >
                {entry.label} <span class="count">{kindCount(entry.id)}</span>
              </button>
            ))}
          </div>
          <label class="download-search">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="m10.5 10.5 3 3" stroke="currentColor" stroke-width="1.5" /></svg>
            <input
              type="search"
              value={search}
              placeholder="查找片名、种子或站点"
              aria-label="查找下载"
              onInput={(event) => setQuery((event.target as HTMLInputElement).value)}
            />
          </label>
          </div>
        </div>
      ) : null}

      {data?.summary && !filtered.length ? (
        <div class="card empty">
          <strong>{items.length ? (kindItems.length ? "没有符合条件的下载" : kind === "tv" ? "没有剧集下载" : "没有影片下载") : "Transmission 里还没有种子"}</strong>
        </div>
      ) : null}

      {visible.length ? (
        <ul class="card download-list">
          {visible.map((item) => (
            <DownloadRow key={item.hash || item.name} item={item} />
          ))}
        </ul>
      ) : null}

      {filtered.length > PAGE_SIZES[0] ? (
        <nav class="pager download-pager" aria-label="分页">
          <span class="muted">
            第 {current} / {pages} 页 · 共 {filtered.length} 个
          </span>
          <label class="download-size">
            每页
            <select value={String(pageSize)} onChange={(event) => navigate(link({ size: Number((event.target as HTMLSelectElement).value), page: 1 }))}>
              {PAGE_SIZES.map((size) => (
                <option key={size} value={String(size)}>
                  {size}
                </option>
              ))}
            </select>
          </label>
          <span class="actions">
            <a class={`btn${current <= 1 ? " is-disabled" : ""}`} aria-disabled={current <= 1} href={link({ page: Math.max(1, current - 1) })}>
              上一页
            </a>
            <a class={`btn${current >= pages ? " is-disabled" : ""}`} aria-disabled={current >= pages} href={link({ page: Math.min(pages, current + 1) })}>
              下一页
            </a>
          </span>
        </nav>
      ) : null}
    </main>
  );
}
