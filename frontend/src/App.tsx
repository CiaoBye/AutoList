import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, AUTH_EVENT, getToken, setToken } from "./api";
import { ConfirmHost } from "./components/ConfirmDialog";
import { ToastContext, useLoad, useStaleData } from "./hooks";
import { Films } from "./pages/Films";
import { Home } from "./pages/Home";
import { Pick } from "./pages/Pick";
import { Timeline } from "./pages/Timeline";
import { Settings } from "./pages/Settings";
import { href, useRoute, type RouteName } from "./router";
import { ThemeMenu } from "./components/ThemeMenu";
import { applyTheme, readTheme, type Theme } from "./theme";
import type { ConnectionStatus, SearchTask } from "./types";
import { Downloads } from "./pages/Downloads";
import { Status } from "./pages/Status";

const NAV: { name: RouteName; label: string; path: string; icon: string }[] = [
  { name: "home", label: "藏馆", path: "/", icon: "M3 10l8-6 8 6v9H3z M9 19v-5h4v5" },
  { name: "films", label: "书目", path: "/films", icon: "M4 5h14M4 11h14M4 17h14" },
  { name: "pick", label: "遴选", path: "/pick", icon: "M5 11l4 4 8-9" },
  { name: "downloads", label: "在途", path: "/downloads", icon: "M11 3v11 M6 9l5 5 5-5 M4 19h14" },
  { name: "status", label: "站况", path: "/status", icon: "M3 17l4-5 3 3 4-7 4 5 M3 19h16" },
  { name: "timeline", label: "馆志", path: "/timeline", icon: "M11 6v5l3 2 M11 20a9 9 0 1 0 0-18 9 9 0 0 0 0 18z" },
  { name: "settings", label: "馆务", path: "/settings", icon: "M11 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M11 2v3M11 17v3M2 11h3M17 11h3" },
];

const PAGE_TITLES: Record<RouteName, string> = {
  home: "藏馆",
  films: "书目",
  pick: "遴选",
  downloads: "在途",
  status: "站况",
  timeline: "馆志",
  settings: "馆务",
  not_found: "页面不存在",
};

function SearchIndicator() {
  // 顶栏只读取轻量的任务列表：有进行中的寻片时每 5 秒刷新，否则 30 秒。
  const tasks = useLoad<SearchTask[]>(
    (signal) => api<SearchTask[]>("/api/search-tasks?limit=10", { signal }),
    [],
    (data) => (data?.some((task) => task.status === "running" || task.status === "queued") ? 5000 : 30000),
  );
  const active = (tasks.data || []).filter((task) => task.status === "running" || task.status === "queued");
  if (!active.length) {
    return (
      <a class="nav-status hide-mobile" href={href("/timeline")} title="查看馆志">
        <span class="dot" aria-hidden="true" />
        寻片空闲
      </a>
    );
  }
  const total = active.reduce((sum, task) => sum + (task.total || 0), 0);
  const completed = active.reduce((sum, task) => sum + (task.completed || 0), 0);
  return (
    <a class="nav-status is-busy" href={href("/timeline", { task: active[0].id })} role="status" title="查看寻片进度">
      <span class="dot" aria-hidden="true" />
      寻片中 <span class="num">{completed} / {total}</span>
    </a>
  );
}

function ServiceStatus() {
  const status = useLoad<ConnectionStatus>(
    (signal) => api<ConnectionStatus>("/api/connection", { signal, timeoutMs: 20000 }),
    [],
    () => 60000,
  );
  const providers = Object.entries(status.data?.providers || {});
  const online = providers.filter(([, value]) => value.ok).length;
  const failing = providers.filter(([, value]) => value.configured !== false && !value.ok).length;
  const allOnline = providers.length > 0 && online === providers.length;
  const label = !status.data ? "服务检测中" : allOnline ? "服务正常" : failing ? `${failing} 项服务异常` : `${online} / ${providers.length} 服务在线`;
  const detail = providers
    .map(([name, value]) => `${name} ${value.ok ? "正常" : value.configured === false ? "未配置" : "异常"}`)
    .join("，");
  return (
    <a
      class={`nav-status ${!status.data ? "" : failing ? "is-bad" : allOnline ? "is-ok" : ""}`}
      href={href("/settings/services")}
      title={detail || undefined}
      aria-label={`服务状态：${label}${detail ? `，${detail}` : ""}，打开往来`}
    >
      <span class="dot" aria-hidden="true" />
      {label}
    </a>
  );
}

/** 页面拿着旧数据、刷新却在失败时提示一下；网络恢复、请求成功后自动消失。 */
function StaleBanner() {
  const stale = useStaleData();
  if (!stale) return null;
  return (
    <div class="stale-banner" role="status">
      数据刷新失败，当前显示的可能不是最新内容，正在自动重试。
    </div>
  );
}

function TokenGate({ onSaved, message }: { onSaved: () => void; message: string }) {
  const [value, setValue] = useState(getToken());
  return (
    <div class="gate">
      <form
        class="card"
        onSubmit={(event) => {
          event.preventDefault();
          setToken(value);
          onSaved();
        }}
      >
        <h1 class="serif" style={{ margin: 0, fontSize: "26px" }}>
          电影藏馆
        </h1>
        <p class="muted" style={{ margin: 0 }}>
          {message}
        </p>
        <label class="field">
          <span>访问令牌</span>
          <input type="password" autocomplete="current-password" value={value} onInput={(event) => setValue((event.target as HTMLInputElement).value)} />
        </label>
        <button class="btn btn-primary" type="submit">
          保存并进入
        </button>
      </form>
    </div>
  );
}

export function App() {
  const route = useRoute();
  const [theme, setTheme] = useState<Theme>(readTheme());
  const [toast, setToast] = useState<string | null>(null);
  const [authMessage, setAuthMessage] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const toastTimer = useRef<number | undefined>(undefined);

  const show = useCallback((message: string) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  useEffect(() => {
    document.title = `${PAGE_TITLES[route.name]} · 电影藏馆`;
  }, [route.name]);

  useEffect(() => {
    const onAuth = (event: Event) => setAuthMessage((event as CustomEvent<string>).detail);
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);

  const changeTheme = (next: Theme) => {
    applyTheme(next, true);
    setTheme(next);
  };

  if (authMessage) {
    return (
      <TokenGate
        message={authMessage}
        onSaved={() => {
          setAuthMessage(null);
          setEpoch((value) => value + 1);
        }}
      />
    );
  }

  let page;
  switch (route.name) {
    case "home":
      page = <Home playlistParam={route.query.get("playlist")} />;
      break;
    case "films":
      page = <Films route={route} />;
      break;
    case "pick":
      page = <Pick route={route} />;
      break;
    case "downloads":
      page = <Downloads route={route} />;
      break;
    case "status":
      page = <Status route={route} />;
      break;
    case "timeline":
      page = <Timeline route={route} />;
      break;
    case "settings":
      page = <Settings section={route.section ?? "services"} onThemeChange={setTheme} />;
      break;
    default:
      page = (
        <main class="page">
          <div class="card empty">
            <strong>页面不存在</strong>
            <a class="btn" href={href("/")}>
              回到藏馆
            </a>
          </div>
        </main>
      );
  }

  return (
    <ToastContext.Provider value={{ show }}>
      <div key={epoch}>
        <button
          class="visually-hidden"
          type="button"
          onClick={() => document.getElementById("main-content")?.focus()}
          onFocus={(event) => (event.currentTarget as HTMLElement).classList.remove("visually-hidden")}
          onBlur={(event) => (event.currentTarget as HTMLElement).classList.add("visually-hidden")}
        >
          跳到正文
        </button>
        <header class="topnav">
          <a class="brand" href={href("/")}>
            <img src="/assets/logo.svg" alt="" width="32" height="32" />
            <strong>AutoList</strong>
            <span>电影藏馆</span>
          </a>
          <nav class="nav-links" aria-label="主导航">
            {NAV.filter((item) => item.name !== "settings").map((item) => (
              <a key={item.name} class="nav-link" href={href(item.path)} aria-current={route.name === item.name ? "page" : undefined}>
                {item.label}
              </a>
            ))}
          </nav>
          <div class="nav-tools">
            <SearchIndicator />
            <span class="hide-mobile">
              <ServiceStatus />
            </span>
            <ThemeMenu theme={theme} onChange={changeTheme} />
            <a class="nav-btn hide-mobile" href={href("/settings")} aria-current={route.name === "settings" ? "page" : undefined}>
              馆务
            </a>
          </div>
        </header>
        <StaleBanner />
        <div id="main-content" tabIndex={-1}>{page}</div>
        <nav class="tabbar" aria-label="主导航">
          {NAV.map((item) => (
            <a key={item.name} class="tab" href={href(item.path)} aria-current={route.name === item.name ? "page" : undefined}>
              <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d={item.icon} />
              </svg>
              <span>{item.label}</span>
            </a>
          ))}
        </nav>
        <ConfirmHost />
        {toast ? (
          <div class="toast" role="status" aria-live="polite">
            {toast}
          </div>
        ) : null}
      </div>
    </ToastContext.Provider>
  );
}

