import { useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { CandidateRow } from "../components/CandidateRow";
import { Poster } from "../components/Poster";
import { IssueBadges, StatusBadge } from "../components/StatusBadge";
import { formatSize } from "../format";
import { useLoad, useToast } from "../hooks";
import { href, navigate, type Route } from "../router";
import type { CartItem, PickBucket, PickItem, PickPage, SubmitResult } from "../types";

const BUCKET_LABELS: Record<PickBucket | "all", string> = {
  all: "全部",
  candidates: "待挑选",
  selected: "已选定",
  no_eligible: "无合格资源",
};
const BUCKETS: (PickBucket | "all")[] = ["all", "candidates", "selected", "no_eligible"];

const cartTitle = (item: CartItem): string => item.tmdb_title || item.chinese_title || item.original_title;

function describeResult(result: SubmitResult): string {
  const parts = [`已提交 ${result.submitted} 部`];
  if (result.skipped.length) {
    const reasons = [...new Set(result.skipped.map((item) => item.reason))].join("、");
    parts.push(`跳过 ${result.skipped.length} 部（${reasons}）`);
  }
  if (result.needs_research) parts.push(`${result.needs_research} 个候选已过期，需要重新寻片`);
  if (result.blocked_unknown.length) parts.push(`${result.blocked_unknown.length} 部无法确认 Emby 状态，已暂缓`);
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
    <section class="card pick-group" aria-labelledby={`pick-${film.id}`}>
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
            下载信息已过期（服务重启或超过 2 小时），需要重新寻片后才能提交。
            <button class="btn btn-small" type="button" disabled={busy} onClick={() => onSearch(film.id)}>
              重新寻片
            </button>
          </div>
        ) : null}
        {film.bucket === "no_eligible" ? (
          <div class="notice notice-bad">
            <span style={{ flex: "1 1 240px" }}>
              {film.excluded_count
                ? `搜索结果全部被入馆标准排除：${film.excluded_summary.map((item) => `${item.reason} ${item.count}`).join("、")}。`
                : "各站点都没有返回与这部影片匹配的结果。"}
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
  const [showCart, setShowCart] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const picks = useLoad<PickPage>(
    (signal) => {
      const search = new URLSearchParams({ status });
      if (playlist) search.set("playlist_id", playlist);
      return api<PickPage>(`/api/picks?${search.toString()}`, { signal });
    },
    [status, playlist],
  );
  const cart = useLoad<CartItem[]>((signal) => api<CartItem[]>("/api/cart", { signal }), []);

  const reloadAll = async () => {
    await Promise.all([picks.reload(), cart.reload()]);
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

  const toggle = (candidateId: string) =>
    void act(() => api(`/api/cart/items/${encodeURIComponent(candidateId)}`, { method: "POST" }), "已更新待入馆清单");
  const search = (filmId: number) =>
    void act(() => api(`/api/films/${filmId}/search`, { method: "POST" }), "已开始寻片，完成后会回到这里");

  const submit = async () => {
    setBusy(true);
    setResult(null);
    try {
      const outcome = await api<SubmitResult>("/api/cart/download", { method: "POST", timeoutMs: 180000 });
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
  const items = cart.data || [];
  const totalSize = items.reduce((sum, item) => sum + (item.size || 0), 0);
  const expiredInCart = items.filter((item) => !item.context_available).length;

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

      {picks.error && !data ? (
        <div class="notice notice-bad" role="alert">
          挑选台读取失败：{picks.error.message}
          <button class="btn btn-small" type="button" onClick={() => void picks.reload()}>
            重试
          </button>
        </div>
      ) : null}
      {!data && !picks.error ? <p class="muted">正在读取挑选台……</p> : null}

      {data && !data.items.length ? (
        <div class="card empty">
          <strong>{status === "all" ? "挑选台暂无待选资源" : `没有“${BUCKET_LABELS[status]}”的影片`}</strong>
          <span>为缺片寻片后，合格资源会按电影出现在这里。</span>
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
          {showCart ? (
            <ul class="tray-list">
              {items.map((item) => (
                <li key={item.id}>
                  <span class="tray-item-text">
                    <strong>{cartTitle(item)}</strong>
                    <span>
                      {item.site_name || "未知站点"} · <span class="mono">{formatSize(item.size)}</span>
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
              <span>
                {expiredInCart
                  ? `${expiredInCart} 部已过期，提交时会跳过，需要重新寻片`
                  : "提交前会再次确认 Emby 与 Transmission，避免重复下载"}
              </span>
            </span>
            <button class="btn tray-btn" type="button" aria-expanded={showCart} onClick={() => setShowCart((value) => !value)}>
              {showCart ? "收起清单" : "查看清单"}
            </button>
            <button class="btn btn-primary btn-large" type="button" disabled={busy || expiredInCart === items.length} onClick={() => void submit()}>
              {busy ? "处理中…" : "提交入馆"}
            </button>
          </div>
        </aside>
      ) : null}
    </main>
  );
}
