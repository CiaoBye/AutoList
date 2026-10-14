import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { Poster } from "../components/Poster";
import { DrawerSkeleton } from "../components/Skeleton";
import { IssueBadges, StatusBadge } from "../components/StatusBadge";
import { formatSize, formatTime, rankDigits } from "../format";
import { useLoad, useToast } from "../hooks";
import { href } from "../router";
import { rememberLastPick } from "../lastPick";
import { TRANSFER_LABELS } from "../status";
import { CandidateRow } from "../components/CandidateRow";
import type { FilmDetail, TmdbMatch } from "../types";
import { DrawerLayer } from "../components/DrawerLayer";
import { CandidateTools } from "../components/CandidateTools";
import { ExcludedPanel } from "../components/ExcludedPanel";
import { confirmDialog, releaseConfirm } from "../components/ConfirmDialog";

interface Step {
  title: string;
  detail: string;
  state: "done" | "now" | "todo";
}


function buildSteps(film: FilmDetail): Step[] {
  const recognized = film.tmdb_id !== null;
  const checked = ["in_library", "not_found", "strm"].includes(film.library_state || "");
  const inLibrary = film.status === "in_library";
  const searched = film.search !== null || ["candidates", "selected", "downloading", "in_library"].includes(film.status);
  const state = (done: boolean, now: boolean): Step["state"] => (done ? "done" : now ? "now" : "todo");
  return [
    {
      title: recognized ? "已识别" : "识别 TMDB",
      detail: "",
      state: state(recognized, !recognized),
    },
    {
      title: checked ? (inLibrary ? "Emby 中已有实体文件" : "Emby 中没有实体文件") : "确认 Emby 入库状态",
      detail: checked ? `最近检查：${formatTime(film.library_checked_at)}` : "",
      state: state(checked, recognized && !checked),
    },
    {
      title: film.status === "searching" ? "寻片中" : "寻片",
      detail: film.search
        ? `最近一次：${formatTime(film.search.finished_at)} · ${film.search.sites} 个站点 · ${film.search.results} 条结果`
        : "",
      state: state(searched && film.status !== "searching", film.status === "missing" || film.status === "searching"),
    },
    {
      title: "挑选资源",
      detail: film.candidates.length ? `${film.candidates.length} 个合格资源` : "",
      state: state(["selected", "downloading", "in_library"].includes(film.status), film.status === "candidates"),
    },
    {
      title: "下载并入馆",
      detail: film.transfer ? TRANSFER_LABELS[film.transfer] : "",
      state: state(inLibrary, film.status === "selected" || film.status === "downloading"),
    },
  ];
}

function IdentityFix({ film, busy, onRun }: {
  film: FilmDetail;
  busy: boolean;
  onRun: (run: () => Promise<unknown>, success: string) => Promise<void>;
}) {
  const [query, setQuery] = useState(film.original_title);
  const [matches, setMatches] = useState<TmdbMatch[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = async (event: Event) => {
    event.preventDefault();
    setSearching(true);
    setError(null);
    try {
      setMatches(await api<TmdbMatch[]>(`/api/films/${film.id}/tmdb-matches?q=${encodeURIComponent(query.trim())}`));
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "TMDB 搜索失败");
    } finally {
      setSearching(false);
    }
  };

  return (
    <details class="panel identity" open={film.status === "unrecognized"}>
      <summary>{film.status === "unrecognized" ? "识别这部影片" : "识别不对？"}</summary>
      <div class="identity-body">
        {film.tmdb_id ? (
          <span class="muted" style={{ fontSize: "13px" }}>
            当前对应 TMDB {film.tmdb_id}《{film.title}》
          </span>
        ) : null}
        <span class="actions">
          <button
            class="btn btn-small"
            type="button"
            disabled={busy}
            onClick={() => void onRun(() => api(`/api/films/${film.id}/recognize`, { method: "POST", timeoutMs: 60000 }), "已重新识别")}
          >
            重新识别
          </button>
        </span>
        <form class="toolbar" style={{ marginBottom: 0 }} onSubmit={(event) => void search(event)}>
          <label class="field field-grow">
            <span>在 TMDB 中查找</span>
            <input type="search" value={query} onInput={(event) => setQuery((event.target as HTMLInputElement).value)} />
          </label>
          <button class="btn" type="submit" disabled={searching || !query.trim()}>
            {searching ? "查找中…" : "查找"}
          </button>
        </form>
        {error ? <span class="notice notice-bad">{error}</span> : null}
        {matches && !matches.length ? <span class="muted">没有找到结果</span> : null}
        {matches && matches.length ? (
          <ul class="match-list">
            {matches.map((match) => (
              <li key={match.tmdb_id}>
                <span class="match-text">
                  <strong>
                    {match.title}
                    {match.year ? ` (${match.year})` : ""}
                  </strong>
                  <span>
                    {match.original_title} · TMDB {match.tmdb_id}
                  </span>
                  {match.overview ? <span>{match.overview}</span> : null}
                </span>
                <button
                  class="btn btn-small"
                  type="button"
                  disabled={busy || match.current}
                  onClick={() =>
                    void onRun(
                      () => api(`/api/films/${film.id}/tmdb`, { method: "POST", body: { tmdb_id: match.tmdb_id }, timeoutMs: 60000 }),
                      `已指定为《${match.title}》`,
                    )
                  }
                >
                  {match.current ? "当前" : "指定这一部"}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </details>
  );
}

export function FilmDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const film = useLoad<FilmDetail>(
    (signal) => api<FilmDetail>(`/api/films/${id}`, { signal }),
    [id],
    (data) => (data?.status === "searching" || data?.upgrade_searching ? 4000 : null),
    `film:${id}`,
  );

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !document.querySelector(".confirm-dialog[open]")) onClose();
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [id]);

  const act = async (run: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await run();
      toast.show(success);
      await film.reload();
      onChanged();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const data = film.data;
  const [failedBackdrop, setFailedBackdrop] = useState<string | null>(null);
  const search = () => act(() => api(`/api/films/${id}/search`, { method: "POST" }), "已开始寻片");
  const toggle = (candidateId: string) => {
    if (!data?.candidates.some((candidate) => candidate.site_options.some((option) => option.id === candidateId && option.in_selection))) rememberLastPick(id);
    return act(() => api(`/api/selection/items/${encodeURIComponent(candidateId)}`, { method: "POST" }), "已更新待入馆清单");
  };

  return (
    <DrawerLayer>
      <button class="drawer-backdrop" type="button" aria-label="关闭影片详情" tabIndex={-1} onClick={onClose} />
      <aside class="drawer drawer-film" role="dialog" aria-modal="true" aria-labelledby="film-title">
        <div class="drawer-head">
          <span class="mono muted" style={{ fontSize: "13px" }}>
            {data ? (
              <>
                {data.rank_no ? <span class="rank-no">{rankDigits(data.rank_no)}</span> : "—"} · {data.playlist_name}
              </>
            ) : "影片详情"}
          </span>
          <button ref={closeRef} class="btn icon-btn" type="button" aria-label="关闭详情" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>

        {film.error && !data ? (
          <div class="notice notice-bad" role="alert">
            影片读取失败：{film.error.message}
          </div>
        ) : null}
        {!data && !film.error ? (
          <>
            <p class="visually-hidden">正在读取影片……</p>
            <DrawerSkeleton />
          </>
        ) : null}

        {data ? (
          <>
            {data.backdrop_url && failedBackdrop !== data.backdrop_url ? (
              <div class="film-banner" aria-hidden="true">
                <img src={data.backdrop_url} alt="" onError={() => setFailedBackdrop(data.backdrop_url)} />
              </div>
            ) : null}
            <div class={`film-hero${data.backdrop_url && failedBackdrop !== data.backdrop_url ? " has-banner" : ""}`}>
              <Poster id={data.id} title={data.title} url={data.poster_url} />
              <div class="film-meta">
                <h2 id="film-title">{data.title}</h2>
                <span>
                  {data.original_title} · {data.year ?? "年份未知"}
                </span>
                <span class="mono muted" style={{ fontSize: "13px" }}>
                  {data.tmdb_id ? `TMDB ${data.tmdb_id}` : "未识别"}
                  {data.imdb_id ? ` · ${data.imdb_id}` : ""}
                </span>
                <span style={{ display: "flex", gap: "6px", flexWrap: "wrap", marginTop: "4px" }}>
                  <StatusBadge status={data.status} />
                  <IssueBadges issues={data.issues} />
                </span>
              </div>
            </div>

            {data.status === "in_library" ? (
              <p class="muted">Emby 中已有实体文件 · 最近检查 {formatTime(data.library_checked_at)}</p>
            ) : (
            <ol class="steps" aria-label="入馆进度">
              {buildSteps(data).map((step) => (
                <li key={step.title} class={`step step-${step.state}`}>
                  <span class="step-mark" aria-hidden="true" />
                  <span class="step-text">
                    <strong>
                      {step.title}
                      {step.state === "now" ? <span class="visually-hidden">（当前步骤）</span> : null}
                    </strong>
                    {step.detail ? <span>{step.detail}</span> : null}
                  </span>
                </li>
              ))}
            </ol>
            )}

            {data.library_versions.length > 1 ? (
              <section class="panel library-versions">
                <h3>Emby 里有 {data.library_versions.length} 个版本</h3>
                <ul class="version-list">
                  {data.library_versions.map((version) => (
                    <li key={version.name}>
                      <span class="mono">{version.name}</span>
                      <span class="muted">{[version.resolution, version.size ? formatSize(version.size) : null].filter(Boolean).join(" · ")}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {data.status === "in_library" && data.library_tier && data.upgrade_enabled ? (
              <section class="panel upgrade-panel">
                <h3>升级画质</h3>
                <p class="muted">Emby 里现有最高 {data.library_tier}p</p>
                {data.upgrade_above !== null && data.candidates.length ? (
                  <>
                    {data.candidates.map((candidate) => (
                      <CandidateRow key={candidate.id} candidate={candidate} busy={busy} onToggle={(candidateId) => void toggle(candidateId)} />
                    ))}
                    {data.candidates.some((candidate) => candidate.in_selection) ? (
                      <span class="actions">
                        <a class="btn btn-primary" href="#/pick">去遴选提交</a>
                      </span>
                    ) : null}
                  </>
                ) : data.upgrade_above !== null && !data.upgrade_searching ? (
                  <p class="muted">没有找到高于 {data.upgrade_above}p 的合格资源</p>
                ) : null}
                {data.library_tier < 2160 ? (
                  <span class="actions">
                    <button
                      class="btn"
                      type="button"
                      disabled={busy || data.upgrade_searching}
                      onClick={() => void act(() => api(`/api/films/${id}/upgrade-search`, { method: "POST" }), "已开始升级搜索")}
                    >
                      {data.upgrade_searching ? "正在搜索…" : data.upgrade_above !== null ? "重新找更高画质" : "找更高画质"}
                    </button>
                  </span>
                ) : null}
              </section>
            ) : null}

            {data.status === "unrecognized" ? (
              <section class="panel">
                <h3>还没有对上 TMDB</h3>
                <span class="actions">
                  <button
                    class="btn btn-primary"
                    type="button"
                    disabled={busy}
                    onClick={() => void act(() => api(`/api/playlists/${data.playlist_id}/recognize`, { method: "POST" }), "已开始识别片单")}
                  >
                    识别片单
                  </button>
                </span>
              </section>
            ) : null}

            {data.status === "unchecked" ? (
              <section class="panel">
                <h3>还没确认是否已在 Emby</h3>
                <span class="actions">
                  <button
                    class="btn btn-primary"
                    type="button"
                    disabled={busy}
                    onClick={() => void act(() => api(`/api/playlists/${data.playlist_id}/library-scan`, { method: "POST" }), "已开始刷新 Emby 状态")}
                  >
                    刷新 Emby 状态
                  </button>
                </span>
              </section>
            ) : null}

            {data.status === "missing" ? (
              <section class="panel">
                <h3>{data.issues.includes("no_eligible") ? "等待资源" : "还没有寻过片"}</h3>
                {data.issues.includes("no_eligible") ? (
                  <ExcludedPanel
                    summary={data.excluded_summary}
                    count={data.excluded_count}
                    candidates={data.excluded_candidates}
                    nextSearchAt={data.next_search_at}
                    busy={busy}
                    identity={data.identity_flag}
                    onFixIdentity={async (tmdbId) => {
                      if (!(await confirmDialog({ title: "改用 TMDB 反查到的这部影片？", message: "之后会按它重新识别、寻片。" }))) return;
                      void act(() => api(`/api/films/${id}/tmdb`, { method: "POST", body: { tmdb_id: tmdbId }, timeoutMs: 60000 }), "已改用新的 TMDB 识别");
                    }}
                    onRelease={async (candidate) => {
                      if (!(await confirmDialog(releaseConfirm({ title: data.title, year: data.year }, candidate)))) return;
                      rememberLastPick(id);
                      void act(() => api(`/api/selection/items/${encodeURIComponent(candidate.id)}?force=true`, { method: "POST" }), "已放行并加入待入馆清单");
                    }}
                  />
                ) : null}
                <span class="actions">
                  <button class="btn btn-primary" type="button" disabled={busy} onClick={() => void search()}>
                    {data.search ? "重新寻片" : "寻片"}
                  </button>
                </span>
              </section>
            ) : null}

            {data.status === "downloading" && data.issues.includes("download_stalled") ? (
              <section class="panel">
                <h3>下载停滞，可以换一个资源</h3>
                <span class="muted">超过 24 小时没有做种者</span>
                <span class="actions">
                  <a class="btn btn-primary" href={href("/pick", { status: "stalled", playlist: data.playlist_id })}>
                    去换资源
                  </a>
                  <button class="btn" type="button" disabled={busy} onClick={() => void search()}>
                    重新寻片
                  </button>
                </span>
              </section>
            ) : null}

            {data.status === "searching" ? (
              <section class="panel" aria-live="polite">
                <h3>正在寻片</h3>
              </section>
            ) : null}

            {data.status === "candidates" || data.status === "selected" ? (
              <section class="panel">
                <h3>{data.status === "selected" ? "已选定的资源" : "合格资源"}</h3>
                {data.issues.includes("context_expired") ? (
                  <div class="notice notice-bad">
                    候选已过期
                    <button class="btn btn-small" type="button" disabled={busy} onClick={() => void search()}>
                      重新寻片
                    </button>
                  </div>
                ) : null}
                <CandidateTools
                  canClear={data.status === "selected" && data.candidates.some((candidate) => !candidate.in_selection)}
                  failed={data.issues.includes("submit_failed")}
                  busy={busy}
                  onClear={async () => {
                    if (!(await confirmDialog({ title: "清除这部影片没选定的候选？", message: "它会回到缺片，下一批寻片会重新搜（已选定的保留）。", confirmText: "清除" }))) return;
                    void act(() => api(`/api/films/${id}/clear-candidates`, { method: "POST" }), "已清除没选定的候选");
                  }}
                  onDismiss={() => void act(() => api(`/api/films/${id}/dismiss-failure`, { method: "POST" }), "已忽略提交失败（提交记录保留）")}
                />
                {data.candidates.map((candidate) => (
                  <CandidateRow key={candidate.id} candidate={candidate} busy={busy} onToggle={(candidateId) => void toggle(candidateId)} />
                ))}
                {data.status === "selected" ? (
                  <span class="actions">
                    <a class="btn btn-primary" href="#/pick?status=selected">
                      去遴选提交
                    </a>
                  </span>
                ) : null}
              </section>
            ) : null}

            {data.status === "downloading" ? (
              <section class="panel">
                <h3>{data.history.length ? "已提交下载" : "正在下载"}</h3>
                <span class="muted">{data.transfer ? TRANSFER_LABELS[data.transfer] : ""}</span>
              </section>
            ) : null}

            <IdentityFix key={`${data.id}-${data.tmdb_id}`} film={data} busy={busy} onRun={act} />

            {data.history.length ? (
              <section>
                <h3 style={{ margin: "0 0 4px", fontSize: "15px" }}>提交记录</h3>
                <ul class="history-list">
                  {data.history.map((record) => (
                    <li key={record.id}>
                      <span>
                        <span class={`badge ${record.success ? "st-in_library" : "st-issue"}`}>{record.success ? "已提交" : "失败"}</span>{" "}
                        <span class="mono">{formatTime(record.created_at)}</span> · {record.site_name || "未知站点"}
                      </span>
                      <span class="candidate-name">{record.torrent_name}</span>
                      {record.message && !record.success ? <span class="muted">{record.message}</span> : null}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </>
        ) : null}
      </aside>
    </DrawerLayer>
  );
}
