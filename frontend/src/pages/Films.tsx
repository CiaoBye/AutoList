import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { ImportDialog } from "../components/ImportDialog";
import { Poster } from "../components/Poster";
import { PosterSkeletons } from "../components/Skeleton";
import { IssueBadges, StatusBadge } from "../components/StatusBadge";
import { useLoad, useToast } from "../hooks";
import { href, navigate, type Route } from "../router";
import { ISSUE_LABELS, STATUS_LABELS } from "../status";
import type { FilmIssue, FilmPage, FilmStatus } from "../types";
import { FilmDrawer } from "./FilmDrawer";
import { startBatchSearch } from "./Home";

const PAGE_SIZE = 120;
const ALWAYS_SHOWN: FilmStatus[] = ["missing", "in_library"];
const FILTER_ORDER: FilmStatus[] = [
  "missing",
  "candidates",
  "selected",
  "searching",
  "downloading",
  "in_library",
  "unrecognized",
  "unchecked",
];
const ISSUES: FilmIssue[] = ["no_eligible", "submit_failed", "context_expired"];

type View = "grid" | "list";

export function Films({ route }: { route: Route }) {
  const toast = useToast();
  const playlist = route.query.get("playlist");
  const status = route.query.get("status") || "all";
  const query = route.query.get("q") || "";
  const page = Math.max(1, Number(route.query.get("page") || 1));
  const view: View = route.query.get("view") === "list" ? "list" : "grid";
  const [draft, setDraft] = useState(query);
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [range, setRange] = useState({ start: "", end: "" });
  const debounce = useRef<number | undefined>(undefined);

  const params = { playlist, status: status === "all" ? null : status, q: query || null, view: view === "grid" ? null : view };
  const listHref = (overrides: Record<string, string | number | null>) => href("/films", { ...params, page: page > 1 ? page : null, ...overrides });

  const films = useLoad<FilmPage>(
    (signal) => {
      const search = new URLSearchParams({ status, page: String(page), page_size: String(PAGE_SIZE) });
      if (playlist) search.set("playlist_id", playlist);
      if (query) search.set("q", query);
      return api<FilmPage>(`/api/films?${search.toString()}`, { signal });
    },
    [playlist, status, query, page],
    (data) => (data && data.counts.searching > 0 ? 5000 : null),
  );

  useEffect(() => setDraft(query), [query]);

  const onSearchInput = (value: string) => {
    setDraft(value);
    window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => {
      navigate(href("/films", { ...params, q: value.trim() || null, page: null }), true);
    }, 300);
  };

  const data = films.data;
  const playlistId = playlist ? Number(playlist) : data?.playlists[0]?.id ?? null;
  const counts = data?.counts;

  const runBatch = async () => {
    if (!playlistId) return;
    setBusy(true);
    try {
      const result = await startBatchSearch(playlistId);
      toast.show(`已开始为 ${result.total} 部缺片寻片`);
      await films.reload();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "寻片任务创建失败");
    } finally {
      setBusy(false);
    }
  };

  const runRange = async (event: Event) => {
    event.preventDefault();
    const start = Number(range.start);
    const end = Number(range.end || range.start);
    if (!playlistId || !start || !end) return;
    if (end < start) {
      toast.show("结束序号不能小于起始序号");
      return;
    }
    setBusy(true);
    try {
      const result = await api<{ total: number }>("/api/search-tasks", {
        method: "POST",
        body: { playlist_id: playlistId, scope: "range", range_start: start, range_end: end },
      });
      toast.show(`已开始为序号 ${start}–${end} 寻片（${result.total} 部）`);
      await films.reload();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "寻片任务创建失败");
    } finally {
      setBusy(false);
    }
  };

  const openFilm = (id: number) => navigate(href(`/films/${id}`, { ...params, page: page > 1 ? page : null }));
  const closeFilm = () => navigate(listHref({}));

  return (
    <main class="page">
      <div class="page-head">
        <h1>片单</h1>
        {data && data.playlists.length > 1 ? (
          <select
            class="page-head-select"
            aria-label="片单"
            value={playlist || ""}
            onChange={(event) => navigate(href("/films", { ...params, playlist: (event.target as HTMLSelectElement).value || null, page: null }))}
          >
            <option value="">全部片单</option>
            {data.playlists.map((item) => (
              <option key={item.id} value={String(item.id)}>
                {item.name} · {item.item_count} 部
              </option>
            ))}
          </select>
        ) : data?.playlists[0] ? (
          <span class="page-head-sub">
            {data.playlists[0].name} · {data.playlists[0].item_count} 部
          </span>
        ) : null}
        <span class="grow" />
        <button class="btn" type="button" onClick={() => setImporting(true)}>
          导入片单
        </button>
        <button
          class="btn btn-primary"
          type="button"
          disabled={busy || !playlistId || !counts?.missing || (counts?.searching ?? 0) > 0}
          onClick={() => void runBatch()}
        >
          {(counts?.searching ?? 0) > 0 ? "寻片进行中" : "为缺片寻片"}
        </button>
      </div>

      <div class="toolbar films-toolbar">
        {counts ? (
          <div class="chips" role="group" aria-label="按状态筛选">
            <a class="chip" aria-current={status === "all" ? "true" : undefined} href={href("/films", { ...params, status: null, page: null })}>
              全部 <span class="count">{counts.all}</span>
            </a>
            {FILTER_ORDER.filter((item) => ALWAYS_SHOWN.includes(item) || counts[item] > 0).map((item) => (
              <a key={item} class="chip" aria-current={status === item ? "true" : undefined} href={href("/films", { ...params, status: item, page: null })}>
                {STATUS_LABELS[item]} <span class="count">{counts[item]}</span>
              </a>
            ))}
            {ISSUES.filter((issue) => counts[`issue:${issue}`] > 0).map((issue) => (
              <a
                key={issue}
                class="chip chip-issue"
                aria-current={status === `issue:${issue}` ? "true" : undefined}
                href={href("/films", { ...params, status: `issue:${issue}`, page: null })}
              >
                {ISSUE_LABELS[issue]} <span class="count">{counts[`issue:${issue}`]}</span>
              </a>
            ))}
          </div>
        ) : (
          <span class="grow" />
        )}
        <input
          class="films-search"
          type="search"
          aria-label="查找影片"
          value={draft}
          placeholder="查找片名、年份或 IMDb"
          onInput={(event) => onSearchInput((event.target as HTMLInputElement).value)}
        />
        <details class="range-search">
          <summary class="btn">按序号寻片</summary>
          <form class="range-form card" onSubmit={(event) => void runRange(event)}>
            <span class="muted" style={{ fontSize: "13px" }}>按片单序号范围寻片，包含已入馆的影片会被自动跳过。</span>
            <div class="toolbar" style={{ marginBottom: 0 }}>
              <label class="field">
                <span>从</span>
                <input type="number" min={1} value={range.start} onInput={(event) => setRange({ ...range, start: (event.target as HTMLInputElement).value })} style={{ width: "96px" }} />
              </label>
              <label class="field">
                <span>到</span>
                <input type="number" min={1} value={range.end} placeholder={range.start} onInput={(event) => setRange({ ...range, end: (event.target as HTMLInputElement).value })} style={{ width: "96px" }} />
              </label>
              <button class="btn btn-primary" type="submit" disabled={busy || !playlistId || !range.start}>
                寻片
              </button>
            </div>
          </form>
        </details>
        <div class="segmented" role="group" aria-label="显示方式">
          <button type="button" aria-pressed={view === "grid"} onClick={() => navigate(listHref({ view: null }), true)}>
            海报
          </button>
          <button type="button" aria-pressed={view === "list"} onClick={() => navigate(listHref({ view: "list" }), true)}>
            列表
          </button>
        </div>
      </div>

      {films.error && !data ? (
        <div class="notice notice-bad" role="alert">
          片单读取失败：{films.error.message}
          <button class="btn btn-small" type="button" onClick={() => void films.reload()}>
            重试
          </button>
        </div>
      ) : null}

      {!data && !films.error ? (
        <>
          <p class="visually-hidden">正在读取片单……</p>
          <PosterSkeletons count={24} />
        </>
      ) : null}

      {data && !data.items.length ? (
        <div class="card empty">
          <strong>{query ? "没有匹配的影片" : data.playlists.length ? "这个筛选下没有影片" : "还没有片单"}</strong>
          <span>{query ? "换一个关键词，或清空查找条件。" : "切换上方的状态筛选查看其他影片。"}</span>
        </div>
      ) : null}

      {data && data.items.length && view === "grid" ? (
        <div class="film-wall">
          {data.items.map((film) => (
            <button
              key={film.id}
              type="button"
              class="film-card"
              aria-current={route.filmId === film.id ? "true" : undefined}
              aria-label={`${film.title}，${film.status_label}${film.issue_labels.length ? `，${film.issue_labels.join("，")}` : ""}`}
              onClick={() => openFilm(film.id)}
            >
              <Poster
                id={film.id}
                title={film.title}
                rank={film.rank_no}
                url={film.poster_url}
                status={film.status}
                issues={film.issues}
              />
              <span class="film-card-title">{film.title}</span>
              <span class="film-card-sub">
                {film.year ?? "—"} · {film.original_title}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {data && data.items.length && view === "list" ? (
        <table class="film-table">
          <thead>
            <tr>
              <th scope="col">序号</th>
              <th scope="col">影片</th>
              <th scope="col">年份</th>
              <th scope="col" class="col-optional">IMDb</th>
              <th scope="col">状态</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((film) => (
              <tr key={film.id} onClick={() => openFilm(film.id)}>
                <td class="mono">{film.rank_no ?? "—"}</td>
                <td class="title-cell">
                  <a
                    href={href(`/films/${film.id}`, { ...params, page: page > 1 ? page : null })}
                    onClick={(event) => event.stopPropagation()}
                    style={{ color: "inherit", textDecoration: "none" }}
                  >
                    <strong>{film.title}</strong>
                  </a>
                  <span>{film.original_title}</span>
                </td>
                <td class="num">{film.year ?? "—"}</td>
                <td class="mono col-optional">{film.imdb_id ?? "—"}</td>
                <td>
                  <span style={{ display: "inline-flex", gap: "6px", flexWrap: "wrap" }}>
                    <StatusBadge status={film.status} />
                    <IssueBadges issues={film.issues} />
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {data && data.pages > 1 ? (
        <nav class="pager" aria-label="分页">
          <span class="muted">
            第 {data.page} / {data.pages} 页 · 共 {data.total} 部
          </span>
          <span class="actions">
            <a class="btn" aria-disabled={data.page <= 1} href={data.page > 1 ? listHref({ page: data.page - 1 }) : undefined}>
              上一页
            </a>
            <a class="btn" aria-disabled={data.page >= data.pages} href={data.page < data.pages ? listHref({ page: data.page + 1 }) : undefined}>
              下一页
            </a>
          </span>
        </nav>
      ) : null}

      {importing ? (
        <ImportDialog
          onClose={() => setImporting(false)}
          onImported={(result) => {
            toast.show(result.recognition_task_id ? `已导入 ${result.count} 部，开始识别 TMDB` : `已导入 ${result.count} 部${result.recognition_note ? `：${result.recognition_note}` : ""}`);
            navigate(href("/films", { playlist: result.id }));
          }}
        />
      ) : null}

      {route.filmId !== null ? <FilmDrawer id={route.filmId} onClose={closeFilm} onChanged={() => void films.reload()} /> : null}
    </main>
  );
}
