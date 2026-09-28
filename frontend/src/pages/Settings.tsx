import { href, SETTINGS_SECTIONS, type SettingsSection } from "../router";
import type { Theme } from "../theme";
import { Appearance } from "./settings/Appearance";
import { Logs } from "./settings/Logs";
import { Network } from "./settings/Network";
import { Playlists } from "./settings/Playlists";
import { Rules } from "./settings/Rules";
import { Services } from "./settings/Services";
import { Sites } from "./settings/Sites";

const SECTION_LABELS: Record<SettingsSection, string> = {
  services: "服务连接",
  network: "代理",
  sites: "站点",
  rules: "入馆标准",
  playlists: "片单管理",
  appearance: "外观与访问",
  logs: "诊断日志",
};

export function Settings({ section, onThemeChange }: { section: SettingsSection; onThemeChange: (theme: Theme) => void }) {
  let body;
  switch (section) {
    case "services":
      body = <Services />;
      break;
    case "network":
      body = <Network />;
      break;
    case "sites":
      body = <Sites />;
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
  return (
    <main class="page page-reading settings-page">
      <div class="page-head">
        <h1>设置</h1>
      </div>
      <div class="settings-layout">
        <nav class="settings-nav" aria-label="设置分区">
          {SETTINGS_SECTIONS.map((item) => (
            <a key={item} class="settings-nav-link" href={href(`/settings/${item}`)} aria-current={section === item ? "page" : undefined}>
              {SECTION_LABELS[item]}
            </a>
          ))}
        </nav>
        <div class="settings-content" key={section}>
          {body}
        </div>
      </div>
    </main>
  );
}
