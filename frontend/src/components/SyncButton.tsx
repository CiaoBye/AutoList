import { useState } from "preact/hooks";
import { api, ApiError } from "../api";
import { useToast } from "../hooks";
import type { SyncResult } from "../types";

const SOURCE_NAMES: Record<string, string> = { transmission: "Transmission", moviepilot: "MoviePilot", emby: "Emby" };

/** 立即把 Transmission、MoviePilot、Emby 的最新情况同步到片单影片，完成后让页面重新读取。 */
export function SyncButton({ onDone }: { onDone: () => void | Promise<unknown> }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const result = await api<SyncResult>("/api/sync/refresh", { method: "POST", timeoutMs: 90000 });
      const broken = Object.entries(result.sources).filter(([, source]) => source.configured && !source.ok);
      if (broken.length) {
        toast.show(`${broken.map(([name]) => SOURCE_NAMES[name] ?? name).join("、")} 暂时无法读取，其余已同步`);
      } else {
        toast.show(`已同步：${result.downloading} 部下载中${result.arrived ? `，${result.arrived} 部新入馆` : ""}${result.removed ? `，清理 ${result.removed} 个停滞的旧任务` : ""}`);
      }
      await onDone();
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "同步失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <button class="btn" type="button" disabled={busy} title="立即向 Transmission、MoviePilot、Emby 读取最新状态" onClick={() => void run()}>
      {busy ? "同步中…" : "立即同步"}
    </button>
  );
}
