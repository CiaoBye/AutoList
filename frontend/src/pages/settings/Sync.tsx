import { useState } from "preact/hooks";
import { api, ApiError } from "../../api";
import { SyncButton } from "../../components/SyncButton";
import { formatTime } from "../../format";
import { useLoad, useToast } from "../../hooks";
import type { SyncStatus, SyncWebhooks } from "../../types";
import { CardHead, SectionHead } from "./shared";

type Source = "transmission" | "moviepilot" | "emby";

const BASE_KEY = "autolist.sync-base";
const DEFAULT_BASE = "http://Autolist:8080";

const SOURCES: { id: Source; name: string; where: string }[] = [
  {
    id: "transmission",
    name: "Transmission",
    where: "添加、完成脚本",
  },
  {
    id: "emby",
    name: "Emby",
    where: "Webhooks · application/json · 已添加新媒体",
  },
  {
    id: "moviepilot",
    name: "MoviePilot",
    where: "Webhook 插件 · 可选",
  },
];

const KIND_LABELS: Record<string, string> = {
  added: "添加下载", done: "下载完成", removed: "删除下载", organized: "整理完成", organize_failed: "整理失败", library_new: "新入库",
};

const readBase = (): string => {
  try {
    return window.localStorage.getItem(BASE_KEY) || DEFAULT_BASE;
  } catch {
    return DEFAULT_BASE;
  }
};

const mask = (url: string) => url.replace(/(token=)[^&]+/, "$1••••••••");

const script = (event: "added" | "done", url: string) =>
  `#!/bin/sh\n# 通知 AutoList：Transmission 里有种子${event === "added" ? "添加" : "下载完成"}，让它立即同步状态（失败不影响 Transmission）。\n` +
  `curl -s -m 5 -X POST --data-urlencode "event=${event}" --data-urlencode "hash=$TR_TORRENT_HASH" "${url}" >/dev/null 2>&1 &\nexit 0\n`;

/** 设置 → 联动：三个系统与 AutoList 之间的同步状态、事件地址与 Transmission 脚本。 */
export function SyncSettings() {
  const toast = useToast();
  const status = useLoad<SyncStatus>((signal) => api<SyncStatus>("/api/sync/status", { signal }), [], () => 15000, "sync-status");
  const hooks = useLoad<SyncWebhooks>((signal) => api<SyncWebhooks>("/api/sync/webhooks", { signal, timeoutMs: 15000 }), [], undefined, "sync-webhooks");
  const [base, setBase] = useState(readBase);
  const [resetting, setResetting] = useState(false);

  const updateBase = (value: string) => {
    setBase(value);
    try {
      window.localStorage.setItem(BASE_KEY, value.trim() || DEFAULT_BASE);
    } catch {
      // 不能保存时只在本次打开期间有效。
    }
  };
  const fullUrl = (source: Source) => {
    const path = hooks.data?.paths[source];
    return path ? `${(base.trim() || DEFAULT_BASE).replace(/\/+$/, "")}${path}` : "";
  };
  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.show(`已复制${label}`);
    } catch {
      toast.show("浏览器不允许复制，请手动选取");
    }
  };
  const reset = async () => {
    if (!window.confirm("重置事件密钥？Transmission 脚本、Emby 与 MoviePilot 里填的旧地址会立即失效，需要重新填写。")) return;
    setResetting(true);
    try {
      await api<SyncWebhooks>("/api/sync/token/reset", { method: "POST" });
      toast.show("密钥已重置，请更新三处的地址");
      await hooks.reload();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "重置失败");
    } finally {
      setResetting(false);
    }
  };

  const last = status.data?.last;
  const hookState = hooks.data?.transmission_hooks;
  return (
    <div class="settings-form">
      <SectionHead title="联动" actions={<SyncButton onDone={() => status.reload()} />} />

      <section class="card setting-card" aria-labelledby="sync-state">
        <CardHead id="sync-state" title="同步状态" note={last ? `最近一次 ${formatTime(last.ran_at)}，用时 ${(last.duration_ms / 1000).toFixed(1)} 秒` : "还没有同步过"} />
        {SOURCES.map((source) => {
          const info = last?.sources[source.id];
          const events = status.data?.events[source.id];
          return (
            <div key={source.id} class="setting-row sync-row">
              <span class="setting-row-text">
                <strong>
                  <span class={`dot ${info ? (info.ok ? "dot-ok" : info.configured ? "dot-bad" : "dot-warn") : "dot-warn"}`} aria-hidden="true" /> {source.name}
                </strong>
                <small>
                  {info ? (info.ok ? "读取正常" : info.message || "无法读取") : "尚未读取"}
                  {events?.last_at
                    ? ` · 最近事件：${KIND_LABELS[events.last_kind || ""] || events.last_kind} ${formatTime(events.last_at)}（近 30 天共 ${events.count} 个）`
                    : " · 还没有收到过事件"}
                </small>
              </span>
            </div>
          );
        })}
      </section>

      <section class="card setting-card" aria-labelledby="sync-hooks">
        <CardHead id="sync-hooks" title="事件地址" />
        <label class="setting-row sync-base">
          <span class="setting-row-text">
            <strong>AutoList 的地址</strong>
          </span>
          <span class="setting-row-control"><input type="url" value={base} onInput={(event) => updateBase((event.target as HTMLInputElement).value)} /></span>
        </label>
        {hooks.error && !hooks.data ? <p class="notice notice-bad setting-card-body">读取失败：{hooks.error.message}</p> : null}
        {hooks.data
          ? SOURCES.map((source) => {
            const url = fullUrl(source.id);
            return (
              <div key={source.id} class="setting-row sync-source">
                <span class="setting-row-text">
                  <strong>{source.name}</strong>
                  <small>{source.where}</small>
                  <code class="sync-url">{mask(url)}</code>
                  {source.id === "transmission" ? (
                    <small>
                      {hookState
                        ? `钩子状态：添加${hookState.added ? "已启用" : "未启用"}，完成${hookState.done ? "已启用" : "未启用"}`
                        : "读不到 Transmission 的钩子状态"}
                    </small>
                  ) : null}
                </span>
                <span class="sync-actions">
                  <button class="btn btn-small" type="button" onClick={() => void copy(url, `${source.name} 地址`)}>
                    复制地址
                  </button>
                  {source.id === "transmission" ? (
                    <>
                      <button class="btn btn-small" type="button" onClick={() => void copy(script("added", url), "添加脚本")}>
                        复制添加脚本
                      </button>
                      <button class="btn btn-small" type="button" onClick={() => void copy(script("done", url), "完成脚本")}>
                        复制完成脚本
                      </button>
                    </>
                  ) : null}
                </span>
              </div>
            );
          })
          : null}
        <div class="setting-row setting-row-actions">
          <span class="grow" />
          <button class="btn btn-small btn-danger" type="button" disabled={resetting} onClick={() => void reset()}>
            {resetting ? "重置中…" : "重置密钥"}
          </button>
        </div>
      </section>
    </div>
  );
}
