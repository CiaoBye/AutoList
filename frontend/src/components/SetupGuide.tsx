import { api } from "../api";
import { useLoad } from "../hooks";
import { href } from "../router";
import type { ConnectionStatus, Site } from "../types";

interface Step {
  key: string;
  title: string;
  hint: string;
  done: boolean;
  required: boolean;
  action: { label: string; href?: string; onClick?: () => void };
}

/** 第一次使用：按顺序列出要做的几步（连接 TMDB 与 MoviePilot、添加站点、导入片单），做完一步自动打勾。 */
export function SetupGuide({ onImport }: { onImport: () => void }) {
  const status = useLoad<ConnectionStatus>(
    (signal) => api<ConnectionStatus>("/api/connection?force_refresh=true", { signal, timeoutMs: 20000 }),
    [],
    (data) => (data && !(data.providers.tmdb?.ok && data.providers.moviepilot?.ok) ? 8000 : null),
  );
  const sites = useLoad<Site[]>((signal) => api<Site[]>("/api/sites", { signal }), [], undefined, "sites");
  const providers = status.data?.providers;
  const tmdb = Boolean(providers?.tmdb?.ok);
  const moviepilot = Boolean(providers?.moviepilot?.ok);
  const emby = Boolean(providers?.emby?.ok);
  const siteCount = (sites.data || []).filter((site) => site.enabled).length;
  const steps: Step[] = [
    {
      key: "tmdb", title: "连接 TMDB", required: true, done: tmdb,
      hint: "识别片单里的影片、取海报，需要一个 TMDB API Key。",
      action: { label: "去填写", href: href("/settings/services") },
    },
    {
      key: "moviepilot", title: "连接 MoviePilot", required: true, done: moviepilot,
      hint: "挑好的资源统一交给 MoviePilot 提交下载与整理。",
      action: { label: "去填写", href: href("/settings/services") },
    },
    {
      key: "emby", title: "连接 Emby（可选）", required: false, done: emby,
      hint: "用它判断影片是否已入库；不连接时所有影片都按“缺片”处理。",
      action: { label: "去填写", href: href("/settings/services") },
    },
    {
      key: "sites", title: "添加站点", required: true, done: siteCount > 0,
      hint: "寻片会在你添加的站点里搜索资源，至少添加一个。",
      action: { label: "去添加", href: href("/settings/sites") },
    },
    {
      key: "import", title: "导入片单", required: true, done: false,
      hint: "支持 TMDB 列表、Letterboxd、IMDb、MDBList 地址，或 XLSX / CSV / JSON 文件。",
      action: { label: "导入片单", onClick: onImport },
    },
  ];
  const finished = steps.filter((step) => step.done).length;
  return (
    <div class="card setup-guide">
      <header class="setup-head">
        <h1 class="serif">欢迎使用电影藏馆</h1>
        <p class="muted">先完成下面几步，就可以开始寻片了。</p>
        <span class="num setup-progress">{finished} / {steps.length}</span>
      </header>
      <ol class="setup-steps">
        {steps.map((step, index) => (
          <li key={step.key} class={`setup-step${step.done ? " is-done" : ""}`}>
            <span class="setup-mark" aria-hidden="true">{step.done ? "✓" : index + 1}</span>
            <div class="setup-body">
              <strong>{step.title}</strong>
              <span class="muted">{step.hint}</span>
            </div>
            {step.done ? (
              <span class="badge">已完成</span>
            ) : step.action.href ? (
              <a class="btn btn-small" href={step.action.href}>{step.action.label}</a>
            ) : (
              <button class="btn btn-small btn-primary" type="button" onClick={step.action.onClick}>{step.action.label}</button>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
