import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { CandidateRow } from "../components/CandidateRow";
import { Poster } from "../components/Poster";
import { IssueBadges, StatusBadge } from "../components/StatusBadge";
import { formatSize } from "../format";
import { useLoad, useToast } from "../hooks";
import { readLastPick, rememberLastPick } from "../lastPick";
import { href, navigate, type Route } from "../router";
import type { SelectionItem, PickBucket, PickItem, PickPage, SubmitResult } from "../types";

const BUCKET_LABELS: Record<PickBucket | "all", string> = {
  all: "全部",
  candidates: "待挑选",
  selected: "已选定",
  no_eligible: "无合格资源",
  stalled: "下载停滞",
};
const BUCKETS: (PickBucket | "all")[] = ["all", "candidates", "selected", "stalled", "no_eligible"];

const selectionTitle = (item: SelectionItem): string => item.tmdb_title || item.chinese_title || item.original_title;

function describeResult(result: SubmitResult): string {
  const parts = [`已提交 ${result.submitted} 部`];
  if (result.skipped.length) {
    const reasons = [...new Set(result.skipped.map((item) => item.reason))].join("、");
    parts.push(`跳过 ${result.skipped.length} 部（${reasons}）`);
  }
  if (result.needs_research) parts.push(`${result.needs_research} 个候选已过期，需要重新寻片`);
  if (result.blocked_unknown.length) parts.push(`${result.blocked_unknown.length} 部无法确认 Emby 状态，已暂缓`);
  if (result.removed.length) parts.push(`${result.removed.length} 个种子已被站点删除，已移出清单，请重新寻片`);
  if (result.blocked_site.length) {
    const reasons = [...new Set(result.blocked_site.map((item) => item.reason))].join("、");
    parts.push(`${result.blocked_site.length} 个无法确认种子仍在站点，已暂缓（${reasons}）`);
  }
  return parts.join("；");
}

function PickGroup({ film, busy, onToggle, onSearch }: {
  film: PickItem;
  busy: boolean;
  onToggle: (candidateId: string) => void;
  onSearch: (filmId: number) => void;
}) {
  const expired = film.issues.includes("context_expired");
  return (
    <section class="card pick-group" id={`pick-card-${film.id}`} aria-labelledby={`pick-${film.id}`}>
      <a class="pick-film" href={href(`/films/${film.id}`)}>
        <span class="pick-poster">
          <Poster id={film.id} title={film.title} url={film.poster_url} />
        </span>
        <span class="pick-film-text">
          <span class="mono muted" style={{ fontSize: "12px" }}>#{film.rank_no ?? "—"}</span>
          <strong id={`pick-${film.id}`} class="serif">{film.title}</strong>
          <span class="muted" style={{ fontSize: "13px" }}>
            {film.original_title} · {film.year ?? "—"}
          </span>
          <span style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            <StatusBadge status={film.status} />
            <IssueBadges issues={film.issues} />
          </span>
        </span>
      </a>
      <div class="pick-body">
        {expired ? (
          <div class="notice notice-bad">
            候选已过期
            <button class="btn btn-small" type="button" disabled={busy} onClick={() => onSearch(film.id)}>
              重新寻片
            </button>
          </div>
        ) : null}
        {film.bucket === "no_eligible" ? (
          <div class="notice notice-bad">
            <span style={{ flex: "1 1 240px" }}>
              {film.excluded_count
                ? film.excluded_summary.map((item) => `${item.reason} ${item.count}`).join("、")
                : "没有匹配的搜索结果"}
            </span>
            <button class="btn btn-small" type="button" disabled={busy} onClick={() => onSearch(film.id)}>
              重新寻片
            </button>
            <a class="btn btn-small" href={href("/settings/rules")}>
              查看入馆标准
            </a>
          </div>
        ) : null}
        {film.candidates.map((candidate) => (
          <CandidateRow key={candidate.id} candidate={candidate} busy={busy} onToggle={onToggle} />
        ))}
      </div>
    </section>
  );
}

export function Pick({ route }: { route: Route }) {
  const toast = useToast();
  const status = (route.query.get("status") as PickBucket | null) || "all";
  const playlist = route.query.get("playlist");
  const [busy, setBusy] = useState(false);
  const [showSelection, setShowSelection] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const picks = useLoad<PickPage>(
    (signal) => {
      const search = new URLSearchParams({ status });
      if (playlist) search.set("playlist_id", playlist);
      return api<PickPage>(`/api/picks?${search.toString()}`, { signal });
    },
    [status, playlist],
    // 寻片进行中时定时刷新：每部片搜完就会出现在这里，不必等整批结束。
    (data) => (data?.searching ? 8000 : null),
    `picks:${status}:${playlist}`,
  );
  const selection = useLoad<SelectionItem[]>((signal) => api<SelectionItem[]>("/api/selection", { signal }), [], undefined, "selection");

  // 从别的页面回到挑选台：数据到了就滚到最近一次选定的影片（优先它已选定的那一条），只滚一次。
  const scrolled = useRef(false);
  useEffect(() => {
    if (scrolled.current || !picks.data) return;
    scrolled.current = true;
    const filmId = readLastPick();
    if (!filmId) return;
    const card = document.getElementById(`pick-card-${filmId}`);
    if (!card) return;
    window.requestAnimationFrame(() => (card.querySelector<HTMLElement>(".candidate.is-selected") ?? card).scrollIntoView({ block: "center" }));
  }, [picks.data]);

  const reloadAll = async () => {
    await Promise.all([picks.reload(), selection.reload()]);
  };

  const act = async (run: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await run();
      toast.show(success);
      await reloadAll();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  // 选定时记下是哪部影片：切到别的页面再回来，直接滚到它那里。
  const toggle = (candidateId: string) => {
    const film = picks.data?.items.find((item) =>
      item.candidates.some((candidate) => candidate.id === candidateId || candidate.site_options.some((option) => option.id === candidateId)),
    );
    const selected = film?.candidates.some((candidate) => candidate.site_options.some((option) => option.id === candidateId && option.in_selection));
    if (film && !selected) rememberLastPick(film.id);
    void act(() => api(`/api/selection/items/${encodeURIComponent(candidateId)}`, { method: "POST" }), "已更新待入馆清单");
  };
  const search = (filmId: number) =>
    void act(() => api(`/api/films/${filmId}/search`, { method: "POST" }), "已开始寻片，完成后会回到这里");

  const submit = async () => {
    setBusy(true);
    setResult(null);
    try {
      const outcome = await api<SubmitResult>("/api/selection/submit", { method: "POST", timeoutMs: 180000 });
      const message = describeResult(outcome);
      setResult(message);
      toast.show(message);
      await reloadAll();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "提交失败";
      setResult(message);
      toast.show(message);
      await reloadAll();
    } finally {
      setBusy(false);
    }
  };

  const data = picks.data;
  const items = selection.data || [];
  const totalSize = items.reduce((sum, item) => sum + (item.size || 0), 0);
  const expiredInSelection = items.filter((item) => !item.context_available).length;
  // 影片已入馆的选择：提交时会被跳过，不算待提交；清单里照常列出，由你决定是否移出。
  const inLibrary = items.filter((item) => item.library_state === "in_library").length;
  // Transmission 里已经在下载的（含提交超时、其实已添加的）：提交时同样会跳过。
  const alreadyDownloading = items.filter((item) => item.library_state !== "in_library" && item.downloading).length;
  const skipped = inLibrary + alreadyDownloading;
  const submittable = items.filter((item) => item.context_available && item.library_state !== "in_library" && !item.downloading).length;

  return (
    <main class={`page${items.length ? " has-tray" : ""}`}>
      <div class="page-head">
        <h1>挑选</h1>
        <span class="grow" />
        {data && data.playlists.length > 1 ? (
          <label class="field">
            <span>片单</span>
            <select
              value={playlist || ""}
              onChange={(event) => navigate(href("/pick", { status: status === "all" ? null : status, playlist: (event.target as HTMLSelectElement).value || null }))}
            >
              <option value="">全部片单</option>
              {data.playlists.map((item) => (
                <option key={item.id} value={String(item.id)}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      {data ? (
        <div class="chips" role="group" aria-label="按挑选状态筛选">
          {BUCKETS.map((bucket) => (
            <a
              key={bucket}
              class={`chip${bucket === "no_eligible" ? " chip-issue" : ""}`}
              aria-current={status === bucket ? "true" : undefined}
              href={href("/pick", { status: bucket === "all" ? null : bucket, playlist })}
            >
              {BUCKET_LABELS[bucket]} <span class="count">{data.counts[bucket]}</span>
            </a>
          ))}
        </div>
      ) : null}

      {data?.searching ? (
        <div class="notice pick-searching" role="status">
          <span>
            寻片进行中 <span class="num">{data.searching.completed} / {data.searching.total}</span>，搜完的影片会陆续出现在这里
          </span>
          <a class="btn btn-small" href={href("/timeline", { task: data.searching.task_id })}>
            查看进度
          </a>
        </div>
      ) : null}

      {picks.error && !data ? (
        <div class="notice notice-bad" role="alert">
          挑选台读取失败：{picks.error.message}
          <button class="btn btn-small" type="button" onClick={() => void picks.reload()}>
            重试
          </button>
        </div>
      ) : null}
      {!data && !picks.error ? <p class="muted">正在读取挑选台……</p> : null}

      {data && !data.items.length && !data.searching ? (
        <div class="card empty">
          <strong>{status === "all" ? "挑选台暂无待选资源" : `没有“${BUCKET_LABELS[status]}”的影片`}</strong>
          <a class="btn btn-primary" href={href("/")}>
            回到藏馆寻片
          </a>
        </div>
      ) : null}

      <div class="pick-list">
        {data?.items.map((film) => (
          <PickGroup key={film.id} film={film} busy={busy} onToggle={toggle} onSearch={search} />
        ))}
      </div>

      {result ? (
        <div class="notice" role="status" style={{ marginTop: "20px" }}>
          提交结果：{result}
          <a class="btn btn-small" href={href("/timeline")}>
            查看动态
          </a>
        </div>
      ) : null}

      {items.length ? (
        <aside class="tray" aria-label="待入馆清单">
          <div class="tray-panel">
          {showSelection ? (
            <ul class="tray-list">
              {items.map((item) => (
                <li key={item.id}>
                  <span class="tray-item-text">
                    <strong>{selectionTitle(item)}</strong>
                    <span>
                      {item.site_name || "未知站点"} · <span class="mono">{formatSize(item.size)}</span>
                      {item.library_state === "in_library" ? " · 影片已入馆，提交时会跳过" : item.downloading ? " · Transmission 里已在下载，提交时会跳过" : ""}
                      {item.context_available ? "" : " · 已过期，需要重新寻片"}
                    </span>
                  </span>
                  <button class="btn btn-small" type="button" disabled={busy} onClick={() => toggle(item.id)}>
                    移出
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <div class="tray-bar">
            <span class="tray-summary">
              <strong>
                待入馆清单 · {items.length} 部 · <span class="mono">{formatSize(totalSize)}</span>
              </strong>
              {expiredInSelection || skipped ? (
                <span>
                  {[
                    inLibrary ? `${inLibrary} 部已入馆` : "",
                    alreadyDownloading ? `${alreadyDownloading} 部已在下载` : "",
                    expiredInSelection ? `${expiredInSelection} 部已过期` : "",
                  ].filter(Boolean).join(" · ")}
                  {skipped ? "（已入馆、已在下载的提交时会跳过）" : ""}
                </span>
              ) : null}
            </span>
            <button class="btn" type="button" aria-expanded={showSelection} onClick={() => setShowSelection((value) => !value)}>
              {showSelection ? "收起清单" : "查看清单"}
            </button>
            <button class="btn btn-primary btn-large" type="button" disabled={busy || submittable === 0} onClick={() => void submit()}>
              {busy ? "处理中…" : "提交入馆"}
            </button>
          </div>
          </div>
        </aside>
      ) : null}
    </main>
  );
}
