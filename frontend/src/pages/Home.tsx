import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { SetupGuide } from "../components/SetupGuide";
import { ImportDialog } from "../components/ImportDialog";
import { Poster } from "../components/Poster";
import { SyncButton } from "../components/SyncButton";
import { HomeSkeleton } from "../components/Skeleton";
import { percent } from "../format";
import { useFitCount, useLoad, useShelfCardWidth, useToast } from "../hooks";
import { href, navigate } from "../router";
import { STATUS_LABELS } from "../status";
import type { ActiveTask, Film, FilmStatus, HomeData, SearchTaskStarted, Todo } from "../types";
import { forgetCurrentPlaylist, readCurrentPlaylist, rememberCurrentPlaylist } from "../currentPlaylist";

const BAR_ORDER: FilmStatus[] = [
  "in_library",
  "downloading",
  "selected",
  "candidates",
  "searching",
  "missing",
  "unchecked",
  "unrecognized",
];

const TASK_LABELS: Record<ActiveTask["kind"], string> = {
  search: "寻片",
  recognition: "TMDB 识别",
  library: "刷新 Emby 状态",
  automation: "片单自动补全",
};

const BATCH_SIZE = 50;
/** 待决定事项默认最多显示几条，其余折叠，避免挤掉下方海报。 */
const TODO_VISIBLE = 3;

/** “寻片”已由进度卡的主按钮承担，右侧只列需要人工判断的事项。 */
type DecisionTodo = Todo & { key: Exclude<Todo["key"], "search_missing"> };

interface TodoView {
  title: string;
  color: string;
  action: { label: string; href?: string; run?: () => Promise<void> };
}

export function startBatchSearch(playlistId: number): Promise<{ total: number }> {
  return api<SearchTaskStarted>("/api/search-tasks", {
    method: "POST",
    body: { playlist_id: playlistId, scope: "pending", count: BATCH_SIZE },
  });
}

export function Home({ playlistParam: routePlaylist }: { playlistParam: string | null }) {
  const toast = useToast();
  // 不带 playlist 时显示当前片单（首页或片单页最后选的）。
  const playlistParam = routePlaylist ?? readCurrentPlaylist();
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [todosExpanded, setTodosExpanded] = useState(false);
  const home = useLoad<HomeData>(
    (signal) => api<HomeData>(`/api/home${playlistParam ? `?playlist_id=${encodeURIComponent(playlistParam)}` : ""}`, { signal }),
    [playlistParam],
    (data) => (data?.tasks.length ? 4000 : 60000),
    `home:${playlistParam ?? ""}`,
  );

  const importDialog = importing ? (
    <ImportDialog
      onClose={() => setImporting(false)}
      onImported={(result) => {
        toast.show(result.recognition_task_id ? `已导入 ${result.count} 部，开始识别 TMDB` : `已导入 ${result.count} 部${result.recognition_note ? `：${result.recognition_note}` : ""}`);
        navigate(href("/", { playlist: result.id }));
        void home.reload();
      }}
    />
  ) : null;

  const data = home.data;
  useEffect(() => {
    if (!routePlaylist && home.error?.status === 404 && readCurrentPlaylist()) {
      forgetCurrentPlaylist();
      void home.reload();
    }
  }, [home.error]);
  // 切回本页时立即刷新，待办不滞后于在别处所做的操作（如手动下载、站点验证）。
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void home.reload();
    };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, []);
  // 两排海报（接下来寻片、最近入馆）按窗口剩余高度定尺寸；任务、待办变化会改变海报架的位置。
  // 版式固定为两排海报：有“接下来寻片”时各占一排，没有时“最近入馆”独占两排，铺满首屏。
  const hasNext = Boolean(data?.up_next.length);
  const shelfLines = [...(hasNext ? [1] : []), ...(data?.recent.length ? [hasNext ? 1 : 2] : [])];
  const shelves = useShelfCardWidth(shelfLines, [data?.tasks.length, data?.todos.length, todosExpanded, Boolean(data)]);
  if (home.error && !data) {
    return (
      <main class="page">
        <div class="notice notice-bad" role="alert">
          首页数据读取失败：{home.error.message}
          <button class="btn btn-small" type="button" onClick={() => void home.reload()}>
            重试
          </button>
        </div>
      </main>
    );
  }
  if (!data) {
    return (
      <main class="page page-home" aria-busy="true">
        <p class="visually-hidden">正在读取馆藏……</p>
        <HomeSkeleton />
      </main>
    );
  }
  if (!data.playlists.length || data.playlist_id === null) {
    return (
      <main class="page">
        <SetupGuide onImport={() => setImporting(true)} />
        {importDialog}
      </main>
    );
  }

  const playlistId = data.playlist_id;
  const playlist = data.playlists.find((item) => item.id === playlistId);
  const counts = data.counts;
  const missingTodo = data.todos.find((todo) => todo.key === "search_missing");
  const searching = data.tasks.some((task) => task.kind === "search");
  const remaining = counts.all - counts.in_library;

  const runBatch = async () => {
    setBusy(true);
    try {
      const result = await startBatchSearch(playlistId);
      toast.show(`已开始为 ${result.total} 部缺片寻片`);
      await home.reload();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "寻片任务创建失败");
    } finally {
      setBusy(false);
    }
  };

  const post = (path: string, success: string) => async () => {
    try {
      await api(path, { method: "POST" });
      toast.show(success);
      await home.reload();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "操作失败");
    }
  };

  const describe = (todo: DecisionTodo): TodoView => {
    const films = (status: string) => href("/films", { playlist: playlistId, status });
    switch (todo.key) {
      case "pick":
        return {
          title: `${todo.count} 部有候选等待挑选`,
          color: "var(--st-candidates-fg)",
          action: { label: "去挑选", href: href("/pick", { status: "candidates", playlist: playlistId }) },
        };
      case "submit":
        return {
          title: `${todo.count} 部已选定，尚未提交`,
          color: "var(--st-selected-fg)",
          action: { label: "去提交", href: href("/pick", { status: "selected", playlist: playlistId }) },
        };
      case "unrecognized":
        return {
          title: `${todo.count} 部影片没能对上 TMDB`,
          color: "var(--st-unrecognized-fg)",
          action: { label: "查看", href: films("unrecognized") },
        };
      case "unchecked":
        return todo.emby_configured
          ? {
              title: `${todo.count} 部还没确认是否已在 Emby`,
              color: "var(--muted)",
              action: { label: "刷新 Emby 状态", run: post(`/api/playlists/${playlistId}/library-scan`, "已开始刷新 Emby 状态") },
            }
          : {
              title: `${todo.count} 部无法确认是否已入库`,
              color: "var(--muted)",
              action: { label: "去配置", href: href("/settings/services") },
            };
      case "no_eligible":
        return {
          title: `${todo.count} 部没有合格资源`,
          color: "var(--issue-fg)",
          action: { label: "查看", href: href("/pick", { status: "no_eligible", playlist: playlistId }) },
        };
      case "submit_failed":
        return {
          title: `${todo.count} 部提交失败`,
          color: "var(--issue-fg)",
          action: { label: "查看", href: films("issue:submit_failed") },
        };
      case "context_expired":
        return {
          title: `${todo.count} 部候选已过期`,
          color: "var(--issue-fg)",
          action: { label: "查看", href: films("issue:context_expired") },
        };
      case "download_stalled":
        return {
          title: `${todo.count} 部下载停滞`,
          color: "var(--st-candidates-fg)",
          action: { label: "去换资源", href: href("/pick", { status: "stalled", playlist: playlistId }) },
        };
      case "organize_failed":
        return {
          title: `${todo.count} 部整理失败`,
          color: "var(--issue-fg)",
          action: { label: "查看", href: films("issue:organize_failed") },
        };
      case "empty_sites":
        return {
          title: `${(todo.names || []).join("、")}${todo.count > (todo.names?.length || 0) ? " 等" : ""} 搜不到结果`,
          color: "var(--st-candidates-fg)",
          action: { label: "查看站点", href: href("/settings/sites") },
        };
      case "failing_sites":
        return {
          title: `${(todo.names || []).join("、")}${todo.count > (todo.names?.length || 0) ? " 等" : ""} 连接失败`,
          color: "var(--issue-fg)",
          action: { label: "查看站点", href: href("/settings/sites") },
        };
    }
  };

  const segments = BAR_ORDER.filter((status) => counts[status] > 0);
  const others = segments.filter((status) => status !== "in_library");

  const decisions = data.todos.filter((todo): todo is DecisionTodo => todo.key !== "search_missing");
  const shownTodos = todosExpanded ? decisions : decisions.slice(0, TODO_VISIBLE);

  return (
    <main class="page page-home">
      <h1 class="visually-hidden">藏馆</h1>
      <div class={`home-grid${decisions.length ? "" : " is-solo"}`}>
        <section class="card progress-card" aria-label="馆藏进度">
          <div class="progress-head">
            {data.playlists.length > 1 ? (
              <select
                class="progress-playlist"
                aria-label="片单"
                value={String(playlistId)}
                onChange={(event) => {
                  rememberCurrentPlaylist((event.target as HTMLSelectElement).value);
                  window.location.hash = href("/", { playlist: (event.target as HTMLSelectElement).value });
                }}
              >
                {data.playlists.map((item) => (
                  <option key={item.id} value={String(item.id)}>
                    {item.name} · {item.item_count} 部
                  </option>
                ))}
              </select>
            ) : (
              <span class="muted progress-playlist-name">{playlist?.name}</span>
            )}
            <div class="actions">
              <a class="btn" href={href("/films", { playlist: playlistId })}>
                查看片单
              </a>
              <SyncButton onDone={() => home.reload()} />
              {missingTodo ? (
                <button
                  class="btn btn-primary"
                  type="button"
                  disabled={busy || searching}
                  title={missingTodo.count > BATCH_SIZE
                    ? `共 ${missingTodo.count} 部待寻片，按片单顺序每批 ${BATCH_SIZE} 部；完成后结果进入挑选`
                    : "按片单顺序寻片，完成后结果进入挑选"}
                  onClick={() => void runBatch()}
                >
                  {searching ? "寻片进行中" : busy ? "正在创建…" : `开始寻片 · ${Math.min(missingTodo.count, BATCH_SIZE)} 部`}
                </button>
              ) : null}
            </div>
          </div>
          <div class="scoreboard">
            <div class="score score-main" data-status="in_library">
              <span class="score-label">已入馆</span>
              <span class="score-num num">{counts.in_library}</span>
              <span class="score-sub">{remaining > 0 ? `共 ${counts.all} 部 · 还差 ${remaining} 部` : `共 ${counts.all} 部 · 已全部入馆`}</span>
            </div>
            {others.map((status) => (
              <div key={status} class="score" data-status={status}>
                <span class="score-label">{STATUS_LABELS[status]}</span>
                <span class="score-num num">{counts[status]}</span>
                <span class="score-sub num">{percent(counts[status], counts.all)}%</span>
              </div>
            ))}
          </div>
          <div
            class="stack-bar"
            role="img"
            aria-label={segments.map((status) => `${STATUS_LABELS[status]} ${counts[status]} 部`).join("，")}
          >
            {segments.map((status) => (
              <span key={status} data-status={status} style={{ width: `${percent(counts[status], counts.all)}%` }} />
            ))}
          </div>
        </section>

        {decisions.length ? (
          <section class="card todo-card" aria-labelledby="todo-title">
            <div class="todo-head">
              <h2 id="todo-title" class="section-title">
                需要你决定
              </h2>
              {decisions.length > TODO_VISIBLE ? (
                <button class="btn btn-small" type="button" onClick={() => setTodosExpanded(!todosExpanded)}>
                  {todosExpanded ? "收起" : `还有 ${decisions.length - TODO_VISIBLE} 项`}
                </button>
              ) : null}
            </div>
            <ul class="todo-list">
                {shownTodos.map((todo) => {
                  const view = describe(todo);
                  return (
                    <li key={todo.key} class="todo">
                      <span class="dot" style={{ color: view.color }} aria-hidden="true" />
                      <span class="todo-text">
                        <strong>{view.title}</strong>
                      </span>
                      {view.action.run ? (
                        <button class="btn btn-small" type="button" disabled={busy} onClick={() => void view.action.run?.()}>
                          {view.action.label}
                        </button>
                      ) : (
                        <a class="btn btn-small" href={view.action.href}>
                          {view.action.label}
                        </a>
                      )}
                    </li>
                  );
                })}
            </ul>
          </section>
        ) : null}
      </div>

      {data.tasks.length ? (
        <section class="tasks" aria-labelledby="tasks-title">
          <h2 id="tasks-title" class="section-title">
            进行中
          </h2>
          {data.tasks.map((task) => (
            <div key={`${task.kind}-${task.id}`} class="card task">
              <span>
                <strong>{TASK_LABELS[task.kind]}</strong>
                <span class="muted"> · {task.playlist_name}</span>
              </span>
              <span
                class="task-bar"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={task.total || 0}
                aria-valuenow={task.completed || 0}
                aria-label={`${TASK_LABELS[task.kind]}进度`}
              >
                <span style={{ width: `${percent(task.completed, task.total)}%` }} />
              </span>
              <span class="task-tail">
                <span class="num muted">
                  {task.status === "queued" ? "排队中" : `${task.completed} / ${task.total}`}
                </span>
                {task.kind === "search" ? (
                  <>
                    <a class="btn btn-small" href={href("/timeline", { task: task.id })}>
                      详情
                    </a>
                    <button
                      class="btn btn-small"
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        if (!window.confirm("取消这个寻片任务？已找到的候选会保留。")) return;
                        void post(`/api/search-tasks/${task.id}/cancel`, "寻片任务已取消")();
                      }}
                    >
                      取消
                    </button>
                  </>
                ) : null}
              </span>
            </div>
          ))}
        </section>
      ) : null}

      <div ref={shelves.ref} class={`shelves${shelves.ready ? " is-ready" : ""}`}>
      {data.up_next.length
        ? (
            <Shelf
              cardWidth={shelves.width}
              title="接下来寻片"
              id="next-title"
              films={data.up_next}
              playlistId={playlistId}
              ranked
              link={{ label: `全部缺片 ${missingTodo?.count ?? data.up_next.length} 部`, href: href("/films", { playlist: playlistId, status: "missing" }) }}
            />
          )
        : null}
      {data.recent.length
        ? (
            <Shelf
              cardWidth={shelves.width}
              title="最近入馆"
              id="shelf-title"
              films={data.recent}
              lines={hasNext ? 1 : 2}
              playlistId={playlistId}
              link={{ label: `查看全部 ${counts.in_library} 部`, href: href("/films", { playlist: playlistId, status: "in_library" }) }}
            />
          )
        : null}
      </div>
      {importDialog}
    </main>
  );
}

/** 海报间距，需与 styles/home.css 中 .shelf-row 的间距一致。 */
const SHELF_GAP = 16;

interface ShelfProps {
  title: string;
  id: string;
  films: Film[];
  playlistId: number;
  link: { label: string; href: string };
  ranked?: boolean;
  cardWidth: number;
  /** 宽屏下显示几排海报。 */
  lines?: number;
}

/** 首页海报架：宽屏按可用宽度排满指定的排数，窄屏横向滑动显示全部。 */
function Shelf({ title, id, films, playlistId, link, ranked = false, cardWidth, lines = 1 }: ShelfProps) {
  const { ref, count } = useFitCount(cardWidth, SHELF_GAP);
  const shown = count === null ? films : films.slice(0, count * lines);
  return (
    <section class="shelf" aria-labelledby={id}>
      <div class="shelf-head">
        <h2 id={id} class="section-title">
          {title}
        </h2>
        <a href={link.href}>{link.label}</a>
      </div>
      <div
        ref={ref}
        class="shelf-row"
        style={{ "--shelf-card": `${cardWidth}px`, ...(count === null ? {} : { gridTemplateColumns: `repeat(${count}, ${cardWidth}px)` }) }}
      >
        {shown.map((film) => (
          <a
            key={film.id}
            class="film-card"
            href={href(`/films/${film.id}`, { playlist: playlistId })}
            aria-label={`${film.title}，${film.status_label}${film.issue_labels.length ? `，${film.issue_labels.join("，")}` : ""}`}
          >
            <Poster
              id={film.id}
              title={film.title}
              rank={ranked ? film.rank_no : null}
              url={film.poster_url}
              status={film.status}
              issues={film.issues}
            />
            <span class="film-card-title" title={film.title}>{film.title}</span>
            <span class="film-card-sub">
              {film.year ?? "—"} · {film.original_title}
            </span>
          </a>
        ))}
      </div>
    </section>
  );
}
