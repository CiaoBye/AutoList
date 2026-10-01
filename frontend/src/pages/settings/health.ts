import { useMemo } from "preact/hooks";
import { api } from "../../api";
import { useLoad, type Loadable } from "../../hooks";
import type { CookieCloudStatus, LogEvent, ProviderStatus, RuntimeSettings, Site } from "../../types";

/** 进入设置时检测一次的服务；AI 辅助识别没有检测接口，按是否配置显示。 */
export const CHECKED_SERVICES = [
  { key: "moviepilot", label: "MoviePilot", role: "提交下载" },
  { key: "tmdb", label: "TMDB", role: "识别影片" },
  { key: "emby", label: "Emby", role: "确认入馆" },
  { key: "transmission", label: "Transmission", role: "下载进度" },
  { key: "fanart", label: "Fanart.tv", role: "海报优先来源" },
] as const;

const DAY_MS = 24 * 60 * 60 * 1000;

export interface SettingsHealth {
  services: Loadable<Record<string, ProviderStatus>>;
  checkedAt: Date | null;
  sites: Loadable<Site[]>;
  cookiecloud: Loadable<CookieCloudStatus>;
  settings: Loadable<RuntimeSettings>;
  problems: Loadable<LogEvent[]>;
  /** 最近 24 小时的警告与错误，最新的在前。 */
  recentProblems: LogEvent[];
  failingServices: number;
  failingSites: Site[];
  emptySites: Site[];
  reloadAll: () => void;
}

export const isRecent = (event: LogEvent): boolean => {
  const time = new Date(event.ts).getTime();
  return !Number.isNaN(time) && Date.now() - time < DAY_MS;
};

/** 设置页各分区共用的健康状况：导航上的异常计数与概览页都读这里，只在进入设置时请求一次。 */
export function useSettingsHealth(): SettingsHealth {
  const services = useLoad<Record<string, ProviderStatus>>(
    (signal) => api<Record<string, ProviderStatus>>("/api/settings/test", { method: "POST", signal, timeoutMs: 45000 }),
    [],
  );
  const sites = useLoad<Site[]>((signal) => api<Site[]>("/api/sites", { signal }), []);
  const cookiecloud = useLoad<CookieCloudStatus>((signal) => api<CookieCloudStatus>("/api/cookiecloud/status", { signal }), []);
  const settings = useLoad<RuntimeSettings>((signal) => api<RuntimeSettings>("/api/settings", { signal }), []);
  const problems = useLoad<LogEvent[]>((signal) => api<LogEvent[]>("/api/logs/events?limit=50&level=WARNING,ERROR", { signal }), []);

  const checkedAt = useMemo(() => (services.data ? new Date() : null), [services.data]);
  const checked = Object.values(services.data || {});
  const enabledSites = (sites.data || []).filter((site) => site.enabled);
  return {
    services,
    checkedAt,
    sites,
    cookiecloud,
    settings,
    problems,
    recentProblems: (problems.data || []).filter(isRecent),
    failingServices: checked.filter((item) => item.configured !== false && !item.ok).length,
    failingSites: enabledSites.filter((site) => site.last_status === "error"),
    emptySites: enabledSites.filter((site) => site.last_status === "empty"),
    reloadAll: () => {
      void services.reload();
      void sites.reload();
      void cookiecloud.reload();
      void settings.reload();
      void problems.reload();
    },
  };
}

/** 服务检测结果里可读的补充说明（服务器名、版本、下载器）。 */
export const serviceDetail = (key: string, status: ProviderStatus | undefined): string => {
  if (!status) return "检测中……";
  if (status.configured === false) return "未配置";
  if (!status.ok) return String(status.message || "连接失败");
  if (key === "emby") return [status.server_name, status.version].filter(Boolean).map(String).join(" ");
  if (key === "transmission" && status.version) return String(status.version).split(" ")[0];
  if (key === "moviepilot") {
    const downloaders = (status.downloaders as { data?: { name?: string }[] } | undefined)?.data || [];
    const names = downloaders.map((item) => item.name).filter(Boolean);
    if (names.length) return `下载器 ${names.join("、")}`;
  }
  return "连接正常";
};

export const dotClass = (status: ProviderStatus | undefined) =>
  !status ? "dot-idle" : status.ok ? "dot-ok" : status.configured === false ? "dot-off" : "dot-bad";
