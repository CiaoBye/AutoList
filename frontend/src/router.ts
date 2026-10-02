import { useEffect, useState } from "preact/hooks";

/**
 * Hash 路由：#/、#/films、#/films/12?status=missing、#/downloads?state=stalled、#/settings（概览）、#/settings/sites。
 * 直接访问、刷新与浏览器前进后退都由 hash 本身恢复，服务端只需提供 /。
 */
export type RouteName = "home" | "films" | "pick" | "downloads" | "timeline" | "settings" | "not_found";

export type SettingsSection = "overview" | "services" | "sites" | "playlists" | "rules" | "appearance" | "logs" | "sync";

/** 可以直接出现在地址里的设置分区；概览就是 #/settings 本身。 */
export const SETTINGS_SECTIONS: Exclude<SettingsSection, "overview">[] = ["services", "sync", "sites", "playlists", "rules", "appearance", "logs"];

export interface Route {
  name: RouteName;
  filmId: number | null;
  section: SettingsSection | null;
  query: URLSearchParams;
}

export const parseRoute = (hash: string): Route => {
  const raw = hash.replace(/^#/, "") || "/";
  const [pathPart, queryPart = ""] = raw.split("?", 2);
  const segments = pathPart.split("/").filter(Boolean);
  const query = new URLSearchParams(queryPart);
  const [first, second] = segments;
  const base = { filmId: null, section: null, query };
  if (!first) return { name: "home", ...base };
  if (first === "films") {
    const filmId = second && /^\d+$/.test(second) ? Number(second) : null;
    return { ...base, name: second && filmId === null ? "not_found" : "films", filmId };
  }
  if (first === "settings") {
    if (!second) return { ...base, name: "settings", section: "overview" };
    const section = SETTINGS_SECTIONS.find((item) => item === second) ?? null;
    return { ...base, name: section ? "settings" : "not_found", section };
  }
  if ((first === "pick" || first === "downloads" || first === "timeline") && !second) return { ...base, name: first };
  return { ...base, name: "not_found" };
};

export const href = (path: string, query?: Record<string, string | number | null | undefined>): string => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return `#${path}${text ? `?${text}` : ""}`;
};

export const navigate = (target: string, replace = false): void => {
  if (replace) window.history.replaceState(null, "", target);
  else window.history.pushState(null, "", target);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
};

export const useRoute = (): Route => {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return route;
};
