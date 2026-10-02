import { useState } from "preact/hooks";
import { api } from "../../api";
import { ImportDialog } from "../../components/ImportDialog";
import { formatTime } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { href, navigate } from "../../router";
import type { AutomationStarted, PlaylistRow, PlaylistSynced } from "../../types";
import { SectionHead, useAction } from "./shared";

const INTERVALS = [6, 12, 24, 48, 72, 168];

function PlaylistCard({ playlist, index, total, busy, run, reload, onMove }: {
  playlist: PlaylistRow;
  index: number;
  total: number;
  busy: boolean;
  run: ReturnType<typeof useAction>["run"];
  reload: () => Promise<void>;
  onMove: (direction: -1 | 1) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(playlist.name);
  const [batch, setBatch] = useState(String(playlist.automation_batch_size ?? 50));
  const post = <T,>(path: string, success: string | ((result: T) => string)) =>
    void run(() => api<T>(path, { method: "POST", timeoutMs: 120000 }), success, reload);

  const setSync = (enabled: boolean, hours: number) =>
    void run(() => api(`/api/playlists/${playlist.id}/sync-settings`, { method: "PUT", body: { enabled, interval_hours: hours } }), "同步设置已保存", reload);
  const setAutomation = (enabled: boolean, size: number) =>
    void run(
      () => api(`/api/playlists/${playlist.id}/automation`, { method: "PUT", body: { enabled, auto_select: false, batch_size: Math.min(200, Math.max(1, size || 50)) } }),
      "自动补全设置已保存",
      reload,
    );
  const titleId = `playlist-${playlist.id}`;

  return (
    <section class="card setting-card" aria-labelledby={titleId}>
      <header class="setting-card-head playlist-head">
        {renaming ? (
          <form
            class="playlist-rename"
            onSubmit={(event) => {
              event.preventDefault();
              if (!name.trim()) return;
              void run(() => api(`/api/playlists/${playlist.id}`, { method: "PUT", body: { name: name.trim() } }), "片单已改名", async () => {
                setRenaming(false);
                await reload();
              });
            }}
          >
            <input id={titleId} aria-label="片单名称" value={name} maxLength={120} onInput={(event) => setName((event.target as HTMLInputElement).value)} />
            <button class="btn btn-small btn-primary" type="submit" disabled={busy || !name.trim()}>
              保存
            </button>
            <button class="btn btn-small" type="button" onClick={() => { setRenaming(false); setName(playlist.name); }}>
              取消
            </button>
          </form>
        ) : (
          <>
            <span class="playlist-head-title">
              <h3 id={titleId}>
                <a class="serif" href={href("/films", { playlist: playlist.id })}>{playlist.name}</a>
              </h3>
              <span class="muted">
                {playlist.item_count} 部 · {playlist.recognized_count ?? 0} 部已识别 · {playlist.source_type || "文件"}
                {playlist.source_url ? ` · 同步于 ${formatTime(playlist.last_synced_at)}` : ` · 导入于 ${formatTime(playlist.created_at)}`}
              </span>
            </span>
            <span class="setting-card-actions">
              {index === 0 ? <span class="badge st-searching">首页默认</span> : null}
              <button class="btn btn-small" type="button" onClick={() => setRenaming(true)}>
                改名
              </button>
              <button class="btn btn-small" type="button" disabled={busy || index === 0} aria-label={`上移 ${playlist.name}`} onClick={() => onMove(-1)}>
                上移
              </button>
              <button class="btn btn-small" type="button" disabled={busy || index === total - 1} aria-label={`下移 ${playlist.name}`} onClick={() => onMove(1)}>
                下移
              </button>
            </span>
          </>
        )}
      </header>

      <div class="setting-row setting-row-field">
        <span class="setting-row-text">
          <strong>识别与入库</strong>
        </span>
        <span class="setting-row-control setting-row-buttons">
          <button class="btn btn-small" type="button" disabled={busy} onClick={() => post(`/api/playlists/${playlist.id}/recognize`, "已开始识别片单")}>
            识别 TMDB
          </button>
          <button
            class="btn btn-small"
            type="button"
            disabled={busy}
            title="补齐每部影片的 IMDb 编号，按 IMDb 换算 TMDB 核对已有识别，不一致的改正后重新核对 Emby"
            onClick={() => {
              if (!window.confirm("按 IMDb 编号校准整份片单：补齐编号并核对已有识别，不一致的会改正并重新核对 Emby。确定开始？")) return;
              void post(`/api/playlists/${playlist.id}/recognize?mode=verify`, "已开始按 IMDb 校准");
            }}
          >
            按 IMDb 校准
          </button>
          <button class="btn btn-small" type="button" disabled={busy} onClick={() => post(`/api/playlists/${playlist.id}/library-scan`, "已开始刷新 Emby 状态")}>
            刷新 Emby 状态
          </button>
        </span>
      </div>

      {playlist.source_url ? (
        <>
          <div class="setting-row setting-row-field">
            <label class="setting-row-text" for={`${titleId}-sync`}>
              <strong>定时同步新片</strong>
              <small>
                {playlist.sync_enabled && playlist.next_sync_at ? `下次同步 ${formatTime(playlist.next_sync_at)}` : ""}
                {playlist.last_sync_message ? (
                  <span class={playlist.last_sync_status === "failed" ? "text-issue" : undefined}> · 最近：{playlist.last_sync_message}</span>
                ) : null}
              </small>
            </label>
            <span class="setting-row-control setting-row-control-narrow">
              <select
                aria-label="同步间隔"
                value={String(playlist.sync_interval_hours ?? 24)}
                disabled={busy}
                onChange={(event) => setSync(Boolean(playlist.sync_enabled), Number((event.target as HTMLSelectElement).value))}
              >
                {INTERVALS.map((hours) => (
                  <option key={hours} value={String(hours)}>
                    每 {hours >= 24 ? `${hours / 24} 天` : `${hours} 小时`}
                  </option>
                ))}
              </select>
              <input
                id={`${titleId}-sync`}
                class="switch"
                type="checkbox"
                role="switch"
                checked={Boolean(playlist.sync_enabled)}
                disabled={busy}
                onChange={(event) => setSync((event.target as HTMLInputElement).checked, playlist.sync_interval_hours ?? 24)}
              />
            </span>
          </div>
          <div class="setting-row setting-row-field">
            <span class="setting-row-text">
              <strong>手动同步</strong>
            </span>
            <span class="setting-row-control setting-row-buttons">
              <button class="btn btn-small" type="button" disabled={busy} onClick={() => post<PlaylistSynced>(`/api/playlists/${playlist.id}/sync-now`, (result) => result.message || "已同步")}>
                立即同步新片
              </button>
              <button
                class="btn btn-small"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm("完整刷新会按来源重新生成整份片单的顺序与条目（已识别信息会尽量保留）。确定继续？")) return;
                  post(`/api/playlists/${playlist.id}/refresh-source`, "已按来源完整刷新");
                }}
              >
                完整刷新
              </button>
            </span>
          </div>
        </>
      ) : (
        <div class="setting-row">
          <span class="setting-row-text">
            <strong>来源同步</strong>
          </span>
        </div>
      )}

      <div class="setting-row setting-row-field">
        <label class="setting-row-text" for={`${titleId}-automation`}>
          <strong>新片自动补全</strong>
        </label>
        <span class="setting-row-control setting-row-control-narrow">
          <input
            type="number"
            min={1}
            max={200}
            aria-label="每批数量（1–200）"
            title="每批数量（1–200）"
            value={batch}
            onInput={(event) => setBatch((event.target as HTMLInputElement).value)}
            onBlur={() => Number(batch) !== playlist.automation_batch_size && setAutomation(Boolean(playlist.automation_enabled), Number(batch))}
          />
          <span class="setting-row-unit">部/批</span>
          <input
            id={`${titleId}-automation`}
            class="switch"
            type="checkbox"
            role="switch"
            checked={Boolean(playlist.automation_enabled)}
            disabled={busy}
            onChange={(event) => setAutomation((event.target as HTMLInputElement).checked, Number(batch))}
          />
        </span>
      </div>

      <div class="setting-row setting-row-actions">
        <button class="btn btn-small" type="button" disabled={busy} onClick={() => post<AutomationStarted>(`/api/playlists/${playlist.id}/automation/run`, (result) => result.message || "已开始后台补全")}>
          立即后台补全
        </button>
        <span class="grow" />
        <button
          class="btn btn-small btn-danger"
          type="button"
          disabled={busy}
          onClick={() => {
            if (!window.confirm(`确定删除片单“${playlist.name}”？片单条目、候选与搜索记录会一并删除，下载历史会保留。`)) return;
            void run(() => api(`/api/playlists/${playlist.id}`, { method: "DELETE" }), "片单已删除", reload);
          }}
        >
          删除片单
        </button>
      </div>
    </section>
  );
}

export function Playlists() {
  const toast = useToast();
  const playlists = useLoad<PlaylistRow[]>((signal) => api<PlaylistRow[]>("/api/playlists", { signal }), [], undefined, "playlists");
  const [importing, setImporting] = useState(false);
  const { busy, run } = useAction();
  const list = playlists.data || [];

  const move = (index: number, direction: -1 | 1) => {
    const ids = list.map((item) => item.id);
    const target = index + direction;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    void run(() => api("/api/playlists/reorder", { method: "POST", body: { ids } }), "顺序已保存", () => playlists.reload());
  };

  return (
    <div class="settings-form">
      <SectionHead
        title="片单管理"
        actions={
          <button class="btn btn-primary" type="button" onClick={() => setImporting(true)}>
            导入片单
          </button>
        }
      />
      {playlists.error && !playlists.data ? <div class="notice notice-bad" role="alert">片单读取失败：{playlists.error.message}</div> : null}
      {!playlists.data && !playlists.error ? <p class="muted">正在读取片单……</p> : null}
      {playlists.data && !list.length ? (
        <div class="card empty">
          <strong>还没有片单</strong>
          <span>从网址或文件导入一份片单开始。</span>
        </div>
      ) : null}
      {list.map((playlist, index) => (
        <PlaylistCard
          key={playlist.id}
          playlist={playlist}
          index={index}
          total={list.length}
          busy={busy}
          run={run}
          reload={() => playlists.reload()}
          onMove={(direction) => move(index, direction)}
        />
      ))}
      {importing ? (
        <ImportDialog
          onClose={() => setImporting(false)}
          onImported={(result) => {
            toast.show(result.recognition_task_id ? `已导入 ${result.count} 部，开始识别 TMDB` : `已导入 ${result.count} 部${result.recognition_note ? `：${result.recognition_note}` : ""}`);
            void playlists.reload();
            navigate(href("/films", { playlist: result.id }));
          }}
        />
      ) : null}
    </div>
  );
}
