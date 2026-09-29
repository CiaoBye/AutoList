import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../api";
import { formatSize, formatTime } from "../../format";
import { useLoad } from "../../hooks";
import type { MoviePilotSitesSynced, Site, SiteCheck, SiteChecks, SiteCookieRefreshed, SiteSaved } from "../../types";
import { SecretField, SectionHead, TextField, Toggle, useAction } from "./shared";
import { CookieCloudPanel } from "./CookieCloudPanel";

const ADAPTER_LABELS: Record<string, string> = {
  nexusphp: "NexusPHP 页面",
  mteam: "M-Team API",
  rss: "RSS 订阅",
  torznab: "Torznab",
};

const CONNECTION: Record<string, { label: string; className: string }> = {
  ok: { label: "正常", className: "st-in_library" },
  slow: { label: "缓慢", className: "st-candidates" },
  empty: { label: "搜不到", className: "st-candidates" },
  error: { label: "失败", className: "st-issue" },
  untested: { label: "未检测", className: "st-unchecked" },
};

const PROFILE_LABELS: Record<string, string> = {
  nexusphp: "通用 NexusPHP 页面",
  totheglory: "听听歌专用",
  hddolby: "高清杜比官方 API",
};

const COOKIE_SOURCE_LABELS: Record<string, string> = { cookiecloud: "由 CookieCloud 更新", manual: "手动填写", moviepilot: "从 MoviePilot 同步" };

/** 连接失败或登录正常却搜不到结果的站点，都需要处理。 */
const NEEDS_ATTENTION = ["error", "empty"];

type Filter = "all" | "enabled" | "disabled" | "failed";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "enabled", label: "参与搜索" },
  { id: "disabled", label: "已停用" },
  { id: "failed", label: "需要处理" },
];

interface SiteForm {
  name: string;
  base_url: string;
  api_key: string;
  cookie: string;
  user_agent: string;
  priority: string;
  timeout_seconds: string;
  rss_url: string;
  icon_url: string;
  proxy: boolean;
  enabled: boolean;
  search_enabled: boolean;
}

const emptyForm = (): SiteForm => ({
  name: "", base_url: "", api_key: "", cookie: "", user_agent: "", priority: "100", timeout_seconds: "30",
  rss_url: "", icon_url: "", proxy: false, enabled: true, search_enabled: true,
});

const formFromSite = (site: Site): SiteForm => ({
  name: site.name, base_url: site.base_url, api_key: "", cookie: "", user_agent: site.user_agent || "",
  priority: String(site.priority ?? 100), timeout_seconds: String(site.timeout_seconds ?? 30), rss_url: "",
  icon_url: site.icon_url && !site.icon_url.startsWith("data:") ? site.icon_url : "", proxy: Boolean(site.proxy),
  enabled: Boolean(site.enabled), search_enabled: Boolean(site.search_enabled),
});

/** 站点保存请求：密钥类字段留空表示保留，清除需显式标记（与服务端 SitePayload 约定一致）。 */
const sitePayload = (form: SiteForm, site: Site | null, cleared: string[]) => ({
  name: form.name.trim(),
  base_url: form.base_url.trim(),
  api_key: form.api_key.trim() || null,
  cookie: form.cookie.trim() || null,
  user_agent: form.user_agent.trim(),
  priority: Math.min(999, Math.max(1, Number(form.priority) || 100)),
  timeout_seconds: Math.min(300, Math.max(3, Number(form.timeout_seconds) || 30)),
  rss_url: form.rss_url.trim(),
  // 自定义图标以 data: 形式保存时不在表单里回显，保存时沿用原值。
  icon_url: form.icon_url.trim() || (site?.icon_url?.startsWith("data:") ? site.icon_url : ""),
  proxy: form.proxy,
  render: Boolean(site?.render),
  limit_interval: site?.limit_interval ?? null,
  limit_count: site?.limit_count ?? null,
  enabled: form.enabled,
  search_enabled: form.search_enabled,
  clear_api_key: cleared.includes("api_key") && !form.api_key.trim(),
  clear_cookie: cleared.includes("cookie") && !form.cookie.trim(),
  clear_rss_url: cleared.includes("rss_url") && !form.rss_url.trim(),
});

function SiteEditor({ site, onClose, onSaved }: { site: Site | null; onClose: () => void; onSaved: (id?: number) => Promise<void> }) {
  const [form, setForm] = useState<SiteForm>(site ? formFromSite(site) : emptyForm());
  const [cleared, setCleared] = useState<string[]>([]);
  const { busy, run } = useAction();
  const closeRef = useRef<HTMLButtonElement>(null);
  const health = useLoad<{ summary: { total: number; succeeded: number | null; success_rate: number | null; average_ms: number | null; last_attempt_at: string | null } }>(
    (signal) => (site ? api(`/api/sites/${site.id}/health-history?limit=30`, { signal }) : Promise.resolve({ summary: { total: 0, succeeded: 0, success_rate: null, average_ms: null, last_attempt_at: null } })),
    [site?.id],
  );

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const set = <K extends keyof SiteForm>(name: K, value: SiteForm[K]) => setForm((current) => ({ ...current, [name]: value }));
  const toggleClear = (name: "api_key" | "cookie" | "rss_url") => {
    setCleared((current) => (current.includes(name) ? current.filter((item) => item !== name) : [...current, name]));
    set(name, "");
  };
  const valid = form.name.trim() && form.base_url.trim();

  const submit = (event: Event) => {
    event.preventDefault();
    if (!valid) return;
    const body = sitePayload(form, site, cleared);
    void run(
      () => (site ? api(`/api/sites/${site.id}`, { method: "PUT", body }) : api<SiteSaved>("/api/sites", { method: "POST", body })),
      site ? "站点已保存" : "站点已添加",
      async () => onSaved(site ? site.id : undefined),
    );
  };

  const account = site?.account_stats;
  const summary = health.data?.summary;

  return (
    <>
      <button class="drawer-backdrop" type="button" aria-label="关闭站点编辑" tabIndex={-1} onClick={onClose} />
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="site-editor-title">
        <div class="drawer-head">
          <h2 id="site-editor-title" class="section-title">{site ? site.name : "添加站点"}</h2>
          <button ref={closeRef} class="btn icon-btn" type="button" aria-label="关闭" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>

        {site ? (
          <section class="panel">
            <div class="stat-row">
              <span><strong class="num">{account?.uploaded == null ? "—" : formatSize(account.uploaded)}</strong><small>上传量</small></span>
              <span><strong class="num">{account?.downloaded == null ? "—" : formatSize(account.downloaded)}</strong><small>下载量</small></span>
              <span><strong class="num">{account?.ratio == null ? "—" : Number(account.ratio).toFixed(2)}</strong><small>分享率</small></span>
              <span><strong class="num">{summary?.success_rate == null ? "—" : `${summary.success_rate}%`}</strong><small>搜索成功率</small></span>
            </div>
            <span class="muted" style={{ fontSize: "13px" }}>
              协议：{ADAPTER_LABELS[site.adapter] || site.adapter} · 解析方式：{PROFILE_LABELS[site.profile] || site.profile}
              {site.last_tested_at ? ` · 最近检测 ${formatTime(site.last_tested_at)}：${site.last_message || CONNECTION[site.last_status]?.label || ""}` : " · 尚未检测"}
              {site.cookie_updated_at ? ` · Cookie ${COOKIE_SOURCE_LABELS[site.cookie_source || ""] || "更新"}于 ${formatTime(site.cookie_updated_at)}` : ""}
              {account?.error ? ` · 账户统计：${account.error}` : ""}
              {summary?.total ? ` · 近期搜索 ${summary.total} 次，平均 ${summary.average_ms ?? 0}ms` : ""}
            </span>
            {site.migration_note ? <span class="notice notice-bad">{site.migration_note}</span> : null}
            <span class="actions">
              <button class="btn btn-small" type="button" disabled={busy} onClick={() => void run(() => api<SiteCheck>(`/api/sites/${site.id}/test`, { method: "POST", timeoutMs: 60000 }), (result) => result.message || "检测完成", () => onSaved(site.id))}>
                检测连接
              </button>
              <button class="btn btn-small" type="button" disabled={busy} onClick={() => void run(() => api<SiteCookieRefreshed>(`/api/sites/${site.id}/refresh-cookie`, { method: "POST", timeoutMs: 60000 }), (result) => result.message || "Cookie 已刷新", () => onSaved(site.id))}>
                从 CookieCloud 刷新 Cookie
              </button>
            </span>
          </section>
        ) : null}

        <form class="settings-form" onSubmit={submit}>
          <div class="field-grid">
            <TextField label="名称" value={form.name} onInput={(value) => set("name", value)} />
            <TextField label="站点地址" value={form.base_url} onInput={(value) => set("base_url", value)} placeholder="https://example.org" inputMode="url" />
            <SecretField
              label="Cookie"
              multiline
              value={form.cookie}
              onInput={(value) => set("cookie", value)}
              configured={Boolean(site?.cookie_configured)}
              cleared={cleared.includes("cookie")}
              onToggleClear={site ? () => toggleClear("cookie") : undefined}
              hint="NexusPHP 页面搜索需要；配置 CookieCloud 后会自动更新"
            />
            <SecretField
              label="API Key"
              value={form.api_key}
              onInput={(value) => set("api_key", value)}
              configured={Boolean(site?.api_key_configured)}
              cleared={cleared.includes("api_key")}
              onToggleClear={site ? () => toggleClear("api_key") : undefined}
              hint={
                /hddolby\.com/.test(form.base_url)
                  ? "填写后改走高清杜比官方搜索接口，不受网页二次验证影响（在站点控制面板生成）"
                  : "M-Team、Torznab 与高清杜比官方接口需要"
              }
            />
            <SecretField
              label="RSS 地址"
              value={form.rss_url}
              onInput={(value) => set("rss_url", value)}
              configured={Boolean(site?.rss_url_configured)}
              cleared={cleared.includes("rss_url")}
              onToggleClear={site ? () => toggleClear("rss_url") : undefined}
              hint="只有 RSS、没有 Cookie 的站点按 RSS 订阅搜索；地址里的密钥只保存在服务端"
            />
            <TextField label="User-Agent" value={form.user_agent} onInput={(value) => set("user_agent", value)} placeholder="留空使用 AutoList 默认" />
            <TextField label="优先级（1 最高）" type="number" value={form.priority} onInput={(value) => set("priority", value)} />
            <TextField label="超时（秒）" type="number" value={form.timeout_seconds} onInput={(value) => set("timeout_seconds", value)} />
            <TextField label="图标地址（可选）" value={form.icon_url} onInput={(value) => set("icon_url", value)} placeholder="留空使用站点 favicon" inputMode="url" />
          </div>
          <div class="toggle-list">
            <Toggle label="启用" checked={form.enabled} onChange={(value) => set("enabled", value)} />
            <Toggle label="参与资源搜索" checked={form.search_enabled} onChange={(value) => set("search_enabled", value)} />
            <Toggle label="经代理访问" hint="需先在“网络与 CookieCloud”中配置代理并允许 PT 站点走代理" checked={form.proxy} onChange={(value) => set("proxy", value)} />
          </div>
          <div class="settings-actions">
            <button class="btn btn-primary btn-large" type="submit" disabled={busy || !valid}>
              {busy ? "处理中…" : site ? "保存站点" : "添加站点"}
            </button>
            {site ? (
              <button
                class="btn btn-danger"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm(`确定删除站点“${site.name}”？搜索记录会保留，站点配置无法恢复。`)) return;
                  void run(() => api(`/api/sites/${site.id}`, { method: "DELETE" }), "站点已删除", async () => {
                    await onSaved();
                    onClose();
                  });
                }}
              >
                删除站点
              </button>
            ) : null}
          </div>
        </form>
      </aside>
    </>
  );
}

export function Sites() {
  const sites = useLoad<Site[]>((signal) => api<Site[]>("/api/sites", { signal }), []);
  const [filter, setFilter] = useState<Filter>("all");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const { busy, run } = useAction();

  const list = (sites.data || []).filter((site) => {
    if (filter === "enabled") return site.enabled && site.search_enabled;
    if (filter === "disabled") return !site.enabled || !site.search_enabled;
    if (filter === "failed") return NEEDS_ATTENTION.includes(site.last_status);
    return true;
  });
  const counts: Record<Filter, number> = {
    all: sites.data?.length ?? 0,
    enabled: sites.data?.filter((site) => site.enabled && site.search_enabled).length ?? 0,
    disabled: sites.data?.filter((site) => !site.enabled || !site.search_enabled).length ?? 0,
    failed: sites.data?.filter((site) => NEEDS_ATTENTION.includes(site.last_status)).length ?? 0,
  };
  const editingSite = typeof editing === "number" ? sites.data?.find((site) => site.id === editing) ?? null : null;

  const toggleSearch = (site: Site) =>
    void run(
      () => api(`/api/sites/${site.id}`, { method: "PUT", body: sitePayload({ ...formFromSite(site), search_enabled: !site.search_enabled }, site, []) }),
      site.search_enabled ? `${site.name} 不再参与搜索` : `${site.name} 已参与搜索`,
      () => sites.reload(),
    );

  return (
    <div class="settings-form">
      <SectionHead
        title="站点"
        actions={
          <>
            <button class="btn" type="button" disabled={busy} onClick={() => void run(() => api<MoviePilotSitesSynced>("/api/sites/sync-moviepilot", { method: "POST", timeoutMs: 120000 }), (result) => result.message, () => sites.reload())}>
              从 MoviePilot 同步
            </button>
            <button class="btn" type="button" disabled={busy} onClick={() => void run(() => api<SiteChecks>("/api/sites/test", { method: "POST", timeoutMs: 180000 }), (result) => `检测完成：${result.ok} / ${result.total} 个可用`, () => sites.reload())}>
              {busy ? "处理中…" : "检测全部"}
            </button>
            <button class="btn btn-primary" type="button" onClick={() => setEditing("new")}>
              添加站点
            </button>
          </>
        }
      >
        协议按地址自动识别，检测用《The Godfather》做一次真实搜索。点击任意一行编辑。
      </SectionHead>

      <CookieCloudPanel onSynced={() => void sites.reload()} />

      <div class="chips" role="group" aria-label="筛选站点">
        {FILTERS.map((item) => (
          <button key={item.id} type="button" class={`chip${item.id === "failed" ? " chip-issue" : ""}`} aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>
            {item.label} <span class="count">{counts[item.id]}</span>
          </button>
        ))}
      </div>

      {sites.error && !sites.data ? <div class="notice notice-bad" role="alert">站点读取失败：{sites.error.message}</div> : null}
      {!sites.data && !sites.error ? <p class="muted">正在读取站点……</p> : null}
      {sites.data && !list.length ? (
        <div class="card empty">
          <strong>{sites.data.length ? "这个筛选下没有站点" : "还没有站点"}</strong>
          <span>可以从 MoviePilot 一键同步，或手动添加。</span>
        </div>
      ) : null}

      {list.length ? (
        <div class="table-scroll">
          <table class="film-table site-table">
            <thead>
              <tr>
                <th scope="col">站点</th>
                <th scope="col" class="col-optional">协议</th>
                <th scope="col">连接</th>
                <th scope="col" class="col-optional">分享率</th>
                <th scope="col" class="col-optional">优先级</th>
                <th scope="col">参与搜索</th>
              </tr>
            </thead>
            <tbody>
              {list.map((site) => {
                const connection = CONNECTION[site.last_status] || CONNECTION.untested;
                return (
                  <tr key={site.id} onClick={() => setEditing(site.id)}>
                    <td class="title-cell">
                      <button type="button" class="link-button" onClick={(event) => { event.stopPropagation(); setEditing(site.id); }}>
                        <strong>{site.name}</strong>
                      </button>
                      <span>{site.base_url.replace(/^https?:\/\//, "").replace(/\/$/, "")}{!site.cookie_configured && site.adapter === "nexusphp" ? " · 未配置 Cookie" : ""}</span>
                    </td>
                    <td class="col-optional">{ADAPTER_LABELS[site.adapter] || site.adapter}</td>
                    <td>
                      <span class={`badge ${connection.className}`} title={site.last_message || undefined}>{connection.label}</span>
                    </td>
                    <td class="mono col-optional">{site.account_stats.ratio == null ? "—" : Number(site.account_stats.ratio).toFixed(2)}</td>
                    <td class="mono col-optional">{site.priority}</td>
                    <td>
                      <label class="switch" onClick={(event) => event.stopPropagation()}>
                        <input type="checkbox" checked={Boolean(site.enabled && site.search_enabled)} disabled={busy || !site.enabled} onChange={() => toggleSearch(site)} />
                        <span class="visually-hidden">{site.name} 参与搜索</span>
                      </label>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {editing !== null ? (
        <SiteEditor
          key={String(editing)}
          site={editing === "new" ? null : editingSite}
          onClose={() => setEditing(null)}
          onSaved={async (id) => {
            await sites.reload();
            if (editing === "new") setEditing(id ?? null);
          }}
        />
      ) : null}
    </div>
  );
}
