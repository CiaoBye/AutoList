import { useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { ImportDialog } from "../components/ImportDialog";
import { Poster } from "../components/Poster";
import { HomeSkeleton } from "../components/Skeleton";
import { percent } from "../format";
import { useFitCount, useLoad, useToast } from "../hooks";
import { href, navigate } from "../router";
import { STATUS_BAR_COLOR, STATUS_LABELS } from "../status";
import type { ActiveTask, Film, FilmStatus, HomeData, SearchTaskStarted, Todo } from "../types";

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

/** “寻片”已由进度卡的主按钮承担，右侧只列需要人工判断的事项。 */
type DecisionTodo = Todo & { key: Exclude<Todo["key"], "search_missing"> };

interface TodoView {
  title: string;
  detail: string;
  color: string;
  action: { label: string; href?: string; run?: () => Promise<void> };
}

export function startBatchSearch(playlistId: number): Promise<{ total: number }> {
  return api<SearchTaskStarted>("/api/search-tasks", {
    method: "POST",
    body: { playlist_id: playlistId, scope: "pending", count: BATCH_SIZE },
  });
}

export function Home({ playlistParam }: { playlistParam: string | null }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const home = useLoad<HomeData>(
    (signal) => api<HomeData>(`/api/home${playlistParam ? `?playlist_id=${encodeURIComponent(playlistParam)}` : ""}`, { signal }),
    [playlistParam],
    (data) => (data?.tasks.length ? 4000 : null),
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
      <main class="page" aria-busy="true">
        <p class="visually-hidden">正在读取馆藏……</p>
        <HomeSkeleton />
      </main>
    );
  }
  if (!data.playlists.length || data.playlist_id === null) {
    return (
      <main class="page">
        <div class="card empty">
          <strong>还没有片单</strong>
          <span>导入一份片单后，这里会显示馆藏进度和下一步要做的事。</span>
          <button class="btn btn-primary" type="button" onClick={() => setImporting(true)}>
            导入片单
          </button>
        </div>
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
          detail: "选定资源后加入待入馆清单",
          color: "var(--st-candidates-fg)",
          action: { label: "去挑选", href: href("/pick", { status: "candidates", playlist: playlistId }) },
        };
      case "submit":
        return {
          title: `${todo.count} 部已选定，尚未提交`,
          detail: "确认后经 MoviePilot 提交下载",
          color: "var(--st-selected-fg)",
          action: { label: "去提交", href: href("/pick", { status: "selected", playlist: playlistId }) },
        };
      case "unrecognized":
        return {
          title: `${todo.count} 部影片没能对上 TMDB`,
          detail: "识别后才能寻片",
          color: "var(--st-unrecognized-fg)",
          action: { label: "查看", href: films("unrecognized") },
        };
      case "unchecked":
        return todo.emby_configured
          ? {
              title: `${todo.count} 部还没确认是否已在 Emby`,
              detail: "刷新 Emby 状态后才能判断是否缺片",
              color: "var(--muted)",
              action: { label: "刷新 Emby 状态", run: post(`/api/playlists/${playlistId}/library-scan`, "已开始刷新 Emby 状态") },
            }
          : {
              title: `${todo.count} 部无法确认是否已入库`,
              detail: "尚未配置 Emby，配置后才能区分缺片与已入馆",
              color: "var(--muted)",
              action: { label: "去配置", href: href("/settings/services") },
            };
      case "no_eligible":
        return {
          title: `${todo.count} 部没有合格资源`,
          detail: "搜索结果都被入馆标准排除",
          color: "var(--issue-fg)",
          action: { label: "查看", href: href("/pick", { status: "no_eligible", playlist: playlistId }) },
        };
      case "submit_failed":
        return {
          title: `${todo.count} 部提交失败`,
          detail: "查看原因后重新选择或提交",
          color: "var(--issue-fg)",
          action: { label: "查看", href: films("issue:submit_failed") },
        };
      case "context_expired":
        return {
          title: `${todo.count} 部候选已过期`,
          detail: "超过 7 天，需要重新寻片才能下载",
          color: "var(--issue-fg)",
          action: { label: "查看", href: films("issue:context_expired") },
        };
      case "empty_sites":
        return {
          title: `${(todo.names || []).join("、")}${todo.count > (todo.names?.length || 0) ? " 等" : ""} 搜不到结果`,
          detail: "登录正常但检测搜索没有解析到结果，可停用这些站点的搜索",
          color: "var(--st-candidates-fg)",
          action: { label: "查看站点", href: href("/settings/sites") },
        };
      case "failing_sites":
        return {
          title: `${(todo.names || []).join("、")}${todo.count > (todo.names?.length || 0) ? " 等" : ""} 连接失败`,
          detail: "搜索时会失败，请检查 Cookie、网络或代理",
          color: "var(--issue-fg)",
          action: { label: "查看站点", href: href("/settings/sites") },
        };
    }
  };

  const segments = BAR_ORDER.filter((status) => counts[status] > 0);
  const decisions = data.todos.filter((todo): todo is DecisionTodo => todo.key !== "search_missing");
  const compactTodos = decisions.length <= 1;

  return (
    <main class="page">
      <h1 class="visually-hidden">藏馆</h1>
      <div class={`home-grid${compactTodos ? " is-compact" : ""}`}>
        <section class="card progress-card" aria-label="馆藏进度">
          <div class="toolbar" style={{ marginBottom: 0 }}>
            {data.playlists.length > 1 ? (
              <label class="field">
                <span>片单</span>
                <select
                  value={String(playlistId)}
                  onChange={(event) => {
                    window.location.hash = href("/", { playlist: (event.target as HTMLSelectElement).value });
                  }}
                >
                  {data.playlists.map((item) => (
                    <option key={item.id} value={String(item.id)}>
                      {item.name} · {item.item_count} 部
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <span class="muted">
                {playlist?.name}
              </span>
            )}
          </div>
          <div class="progress-figure">
            <span class="big num">{counts.in_library}</span>
            <span class="of num">/ {counts.all}</span>
            <span class="phrase">{remaining > 0 ? `部已入馆，还差 ${remaining} 部` : "部已全部入馆"}</span>
          </div>
          <div
            class="stack-bar"
            role="img"
            aria-label={segments.map((status) => `${STATUS_LABELS[status]} ${counts[status]} 部`).join("，")}
          >
            {segments.map((status) => (
              <span key={status} style={{ width: `${percent(counts[status], counts.all)}%`, background: STATUS_BAR_COLOR[status] }} />
            ))}
          </div>
          <ul class="legend">
            {segments.map((status) => (
              <li key={status}>
                <span class="swatch" style={{ background: STATUS_BAR_COLOR[status] }} />
                {STATUS_LABELS[status]} <span class="num">{counts[status]}</span>
              </li>
            ))}
          </ul>
          <div class="actions">
            {missingTodo ? (
              <button class="btn btn-primary btn-large" type="button" disabled={busy || searching} onClick={() => void runBatch()}>
                {searching ? "寻片进行中" : busy ? "正在创建寻片任务…" : `为 ${Math.min(missingTodo.count, BATCH_SIZE)} 部缺片寻片`}
              </button>
            ) : null}
            <a class="btn btn-large" href={href("/films", { playlist: playlistId })}>
              查看片单
            </a>
          </div>
          {missingTodo ? (
            <span class="progress-hint muted">
              {missingTodo.count > BATCH_SIZE
                ? `共 ${missingTodo.count} 部待寻片，按片单顺序每批 ${BATCH_SIZE} 部；完成后结果进入挑选。`
                : "按片单顺序寻片，完成后结果进入挑选。"}
            </span>
          ) : null}
        </section>

        <section class={`card todo-card${compactTodos ? " is-compact" : ""}`} aria-labelledby="todo-title">
          <h2 id="todo-title" class="section-title">
            需要你决定
          </h2>
          {decisions.length ? (
            <ul class="todo-list">
              {decisions.map((todo) => {
                const view = describe(todo);
                return (
                  <li key={todo.key} class="todo">
                    <span class="dot" style={{ color: view.color }} aria-hidden="true" />
                    <span class="todo-text">
                      <strong>{view.title}</strong>
                      <span>{view.detail}</span>
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
          ) : (
            <p class="todo-empty muted">没有需要你判断的事{missingTodo ? "，直接寻片即可" : "，馆藏正在按计划进行"}。</p>
          )}
        </section>
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

      {data.up_next.length
        ? (
            <Shelf
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
              title="最近入馆"
              id="shelf-title"
              films={data.recent}
              playlistId={playlistId}
              link={{ label: `查看全部 ${counts.in_library} 部`, href: href("/films", { playlist: playlistId, status: "in_library" }) }}
            />
          )
        : null}
      {importDialog}
    </main>
  );
}

/** 海报最小宽度与间距，需与 styles/home.css 中 .shelf-row 的列宽、间距一致。 */
const SHELF_CARD_MIN = 150;
const SHELF_GAP = 16;

interface ShelfProps {
  title: string;
  id: string;
  films: Film[];
  playlistId: number;
  link: { label: string; href: string };
  ranked?: boolean;
}

/** 首页海报架：宽屏按可用宽度只显示一整行，窄屏横向滑动显示全部。 */
function Shelf({ title, id, films, playlistId, link, ranked = false }: ShelfProps) {
  const { ref, count } = useFitCount(SHELF_CARD_MIN, SHELF_GAP);
  const shown = count === null ? films : films.slice(0, count);
  return (
    <section class="shelf" aria-labelledby={id}>
      <div class="shelf-head">
        <h2 id={id} class="section-title">
          {title}
        </h2>
        <a href={link.href}>{link.label}</a>
      </div>
      <div ref={ref} class="shelf-row" style={count === null ? undefined : { gridTemplateColumns: `repeat(${count}, minmax(0, 1fr))` }}>
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
            <span class="film-card-title">{film.title}</span>
            <span class="film-card-sub">
              {film.year ?? "—"} · {film.original_title}
            </span>
          </a>
        ))}
      </div>
    </section>
  );
}
