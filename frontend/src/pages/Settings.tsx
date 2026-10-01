import { href, type SettingsSection } from "../router";
import type { Theme } from "../theme";
import { Appearance } from "./settings/Appearance";
import { useSettingsHealth, type SettingsHealth } from "./settings/health";
import { levelOf } from "./settings/logText";
import { Logs } from "./settings/Logs";
import { Overview } from "./settings/Overview";
import { Playlists } from "./settings/Playlists";
import { ProxySettings } from "./settings/Proxy";
import { Rules } from "./settings/Rules";
import { Services } from "./settings/Services";
import { Sites } from "./settings/Sites";

const SECTION_LABELS: Record<SettingsSection, string> = {
  overview: "概览",
  services: "服务连接",
  sites: "站点",
  playlists: "片单管理",
  rules: "入馆标准",
  appearance: "外观与访问",
  logs: "诊断日志",
};

const GROUPS: { label: string | null; items: SettingsSection[] }[] = [
  { label: null, items: ["overview"] },
  { label: "连接", items: ["services"] },
  { label: "内容", items: ["sites", "playlists", "rules"] },
  { label: "系统", items: ["appearance", "logs"] },
];

type Tone = "ok" | "bad" | "warn";

/** 分区在导航上的状态：桌面显示计数标记，手机的设置首页显示一句说明。 */
interface SectionState {
  tone: Tone;
  mark?: string;
  text: string;
}

const sectionState = (section: SettingsSection, health: SettingsHealth): SectionState | null => {
  switch (section) {
    case "services":
      if (!health.services.data) return null;
      return health.failingServices
        ? { tone: "bad", mark: String(health.failingServices), text: `${health.failingServices} 项异常` }
        : { tone: "ok", text: "正常" };
    case "sites":
      if (health.failingSites.length) return { tone: "bad", mark: String(health.failingSites.length), text: `${health.failingSites.length} 个失败` };
      if (health.emptySites.length) return { tone: "warn", mark: String(health.emptySites.length), text: `${health.emptySites.length} 个搜不到` };
      return null;
    case "appearance": {
      const strength = health.settings.data?.access_token_strength;
      if (strength === "missing") return { tone: "warn", mark: "!", text: "未启用访问令牌" };
      if (strength === "weak") return { tone: "bad", mark: "!", text: "访问令牌强度不足" };
      return null;
    }
    case "logs": {
      const errors = health.recentProblems.filter((event) => levelOf(event) === "ERROR").length;
      return errors ? { tone: "bad", mark: String(errors), text: `${errors} 条错误` } : null;
    }
    default:
      return null;
  }
};

export function Settings({ section, onThemeChange }: { section: SettingsSection; onThemeChange: (theme: Theme) => void }) {
  const health = useSettingsHealth();
  let body;
  switch (section) {
    case "overview":
      body = <Overview health={health} />;
      break;
    case "services":
      body = (
        <div class="settings-stack">
          <Services health={health} />
          <ProxySettings health={health} />
        </div>
      );
      break;
    case "sites":
      body = <Sites health={health} />;
      break;
    case "rules":
      body = <Rules />;
      break;
    case "playlists":
      body = <Playlists />;
      break;
    case "appearance":
      body = <Appearance onThemeChange={onThemeChange} />;
      break;
    case "logs":
      body = <Logs />;
      break;
  }
  const isOverview = section === "overview";
  return (
    <main class={`page settings-page${isOverview ? " is-index" : ""}`}>
      <div class="page-head">
        <h1>设置</h1>
      </div>
      <div class="settings-layout">
        <nav class="settings-nav" aria-label="设置分区">
          {GROUPS.map((group) => (
            <div key={group.label || "top"} class="settings-nav-group">
              {group.label ? <div class="settings-nav-heading">{group.label}</div> : null}
              <div class="settings-nav-items">
              {group.items.map((item) => {
                const state = sectionState(item, health);
                return (
                  <a
                    key={item}
                    class="settings-nav-link"
                    href={href(item === "overview" ? "/settings" : `/settings/${item}`)}
                    aria-current={section === item ? "page" : undefined}
                  >
                    {item === "services" && state ? <span class={`dot dot-${state.tone}`} aria-hidden="true" /> : null}
                    <span class="settings-nav-label">{SECTION_LABELS[item]}</span>
                    {state?.mark ? (
                      <span class={`settings-nav-mark mark-${state.tone}`} aria-label={state.text}>
                        {state.mark}
                      </span>
                    ) : null}
                    {state ? <span class={`settings-nav-state text-${state.tone}`}>{state.text}</span> : null}
                    <span class="settings-nav-chevron" aria-hidden="true">›</span>
                  </a>
                );
              })}
              </div>
            </div>
          ))}
        </nav>
        <div class="settings-content" key={section}>
          {isOverview ? null : (
            <a class="settings-back" href={href("/settings")}>
              ‹ 设置
            </a>
          )}
          {body}
        </div>
      </div>
    </main>
  );
}
