import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../api";
import { formatSize, formatTime } from "../../format";
import type { MoviePilotSitesSynced, Site, SiteCheck, SiteChecks, SiteCookieRefreshed, SiteSaved } from "../../types";
import { CookieCloudPanel } from "./CookieCloudPanel";
import type { SettingsHealth } from "./health";
import { SecretField, SectionHead, SwitchRow, TextField, useAction } from "./shared";
import { DrawerLayer } from "../../components/DrawerLayer";
import { VerifyLink } from "../../components/VerifyLink";

const ADAPTER_LABELS: Record<string, string> = {
  mteam: "M-Team API",
  rss: "RSS 订阅",
  torznab: "Torznab",
};

const PROFILE_LABELS: Record<string, string> = {
  nexusphp: "NexusPHP 页面",
  alt_layout: "NexusPHP 页面（专用规则）",
  official_api_site: "官方 API",
};

const CONNECTION: Record<string, { label: string; className: string }> = {
  ok: { label: "正常", className: "st-in_library" },
  slow: { label: "缓慢", className: "st-candidates" },
  empty: { label: "搜不到", className: "st-candidates" },
  error: { label: "失败", className: "st-issue" },
  untested: { label: "未检测", className: "st-unchecked" },
};

/** 失败与搜不到的站点排在前面，其次缓慢、未检测，最后正常；停用的站点放最后。 */
const STATUS_ORDER: Record<string, number> = { error: 0, empty: 1, slow: 2, untested: 3, ok: 4 };

const COOKIE_SOURCE_LABELS: Record<string, string> = { cookiecloud: "CookieCloud", manual: "手动填写", moviepilot: "从 MoviePilot 同步" };

type Filter = "all" | "search" | "error" | "empty" | "disabled";
const FILTERS: { id: Filter; label: string; dot?: string }[] = [
  { id: "all", label: "全部" },
  { id: "search", label: "参与搜索" },
  { id: "error", label: "失败", dot: "dot-bad" },
  { id: "empty", label: "搜不到", dot: "dot-warn" },
  { id: "disabled", label: "已停用" },
];

const participates = (site: Site) => Boolean(site.enabled && site.search_enabled);
const matchesFilter = (site: Site, filter: Filter) => {
  if (filter === "search") return participates(site);
  if (filter === "error") return site.enabled && site.last_status === "error";
  if (filter === "empty") return site.enabled && site.last_status === "empty";
  if (filter === "disabled") return !participates(site);
  return true;
};

const domain = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/$/, "");
const parseLabel = (site: Site) => (site.adapter === "nexusphp" ? PROFILE_LABELS[site.profile] || site.profile : ADAPTER_LABELS[site.adapter] || site.adapter);
const officialApi = (site: Site) => site.adapter === "nexusphp" && site.profile === "official_api_site";

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
  supplement_only: boolean;
  limit_interval: string;
  limit_count: string;
}

const emptyForm = (): SiteForm => ({
  name: "", base_url: "", api_key: "", cookie: "", user_agent: "", priority: "100", timeout_seconds: "30",
  rss_url: "", icon_url: "", proxy: true, enabled: true, search_enabled: true, supplement_only: false,
  limit_interval: "", limit_count: "",
});

const formFromSite = (site: Site): SiteForm => ({
  name: site.name, base_url: site.base_url, api_key: "", cookie: "", user_agent: site.user_agent || "",
  priority: String(site.priority ?? 100), timeout_seconds: String(site.timeout_seconds ?? 30), rss_url: "",
  icon_url: site.icon_url && !site.icon_url.startsWith("data:") ? site.icon_url : "", proxy: Boolean(site.proxy),
  enabled: Boolean(site.enabled), search_enabled: Boolean(site.search_enabled), supplement_only: Boolean(site.supplement_only),
  limit_interval: site.limit_interval ? String(site.limit_interval) : "", limit_count: site.limit_count ? String(site.limit_count) : "",
});

/** 访问频率：留空表示不限（仍按全局每 3 秒一次）；只填秒数时视为两次请求之间的最小间隔。 */
const positiveOrNull = (value: string): number | null => {
  const parsed = Math.floor(Number(value));
  return Number.isFinite(parsed) && parsed >= 1 ? parsed : null;
};

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
  limit_interval: positiveOrNull(form.limit_interval),
  limit_count: positiveOrNull(form.limit_interval) ? positiveOrNull(form.limit_count) : null,
  enabled: form.enabled,
  search_enabled: form.search_enabled,
  supplement_only: form.supplement_only,
  clear_api_key: cleared.includes("api_key") && !form.api_key.trim(),
  clear_cookie: cleared.includes("cookie") && !form.cookie.trim(),
  clear_rss_url: cleared.includes("rss_url") && !form.rss_url.trim(),
});

/** 检测列的补充说明：失败或搜不到时显示原因，否则显示耗时；检测时间放进提示。 */
const testNote = (site: Site): { text: string; title: string } => {
  if (!site.last_tested_at) return { text: "尚未检测", title: "" };
  const when = `检测于 ${formatTime(site.last_tested_at)}`;
  if (site.last_status === "error" || site.last_status === "empty") return { text: site.last_message || "", title: `${site.last_message || ""}\n${when}` };
  const seconds = site.last_duration_ms ? `${(site.last_duration_ms / 1000).toFixed(1)} 秒` : "";
  return { text: seconds, title: when };
};

/** Cookie 列：来源；更新时间放进提示。不在 CookieCloud 中或缺少 Cookie 时提醒。 */
const cookieNote = (site: Site, missing: string[]): { text: string; title: string; warn: boolean } => {
  if (officialApi(site) || site.adapter !== "nexusphp") return { text: "不需要", title: "", warn: false };
  if (!site.cookie_configured) return { text: "未配置", title: "", warn: true };
  const source = COOKIE_SOURCE_LABELS[site.cookie_source || ""] || "已保存";
  const title = site.cookie_updated_at ? `更新于 ${formatTime(site.cookie_updated_at)}` : "";
  if (missing.includes(site.name)) return { text: "不在 CookieCloud 中", title, warn: true };
  return { text: source, title, warn: false };
};

function SiteIcon({ site }: { site: Site | null }) {
  return site ? <img class="site-icon" src={site.icon_endpoint} alt="" width="28" height="28" loading="lazy" /> : <span class="site-icon" aria-hidden="true" />;
}

function SiteDrawer({ site, onClose, onSaved }: { site: Site | null; onClose: () => void; onSaved: (id?: number) => Promise<void> }) {
  const initial = site ? formFromSite(site) : emptyForm();
  const [form, setForm] = useState<SiteForm>(initial);
  const [cleared, setCleared] = useState<string[]>([]);
  // 保存成功后等站点列表重新读取，再用最新的站点数据重置表单（密钥输入框清空、“未保存”消失）。
  const [resetPending, setResetPending] = useState(false);
  const { busy, run } = useAction();
  const closeRef = useRef<HTMLButtonElement>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(initial) || cleared.length > 0;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const close = () => {
    if (dirtyRef.current && !window.confirm("站点有未保存的修改，确定放弃并关闭？")) return;
    onClose();
  };

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!resetPending || !site) return;
    setForm(formFromSite(site));
    setResetPending(false);
  }, [site]);

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
    let savedId = site?.id;
    void run(
      async () => {
        const result = await (site ? api<SiteSaved>(`/api/sites/${site.id}`, { method: "PUT", body }) : api<SiteSaved>("/api/sites", { method: "POST", body }));
        savedId = result.id;
        return result;
      },
      site ? "站点已保存" : "站点已添加",
      async () => {
        setCleared([]);
        setResetPending(true);
        // 新站点保存后，抽屉切换到这个站点继续编辑或检测。
        await onSaved(savedId);
      },
    );
  };

  const account = site?.account_stats;
  const connection = site ? CONNECTION[site.last_status] || CONNECTION.untested : null;

  return (
    <DrawerLayer>
      <button class="drawer-backdrop" type="button" aria-label="关闭站点编辑" tabIndex={-1} onClick={close} />
      <aside class="drawer drawer-site site-dialog" role="dialog" aria-modal="true" aria-labelledby="site-drawer-title">
        <div class="site-drawer-head">
          <SiteIcon site={site} />
          <div class="site-drawer-title">
            <h2 id="site-drawer-title">{site ? site.name : "添加站点"}</h2>
            {site ? (
              <span class="muted">
                {domain(site.base_url)} · {parseLabel(site)} · {connection?.label}
                {site.last_tested_at && site.last_duration_ms ? ` ${(site.last_duration_ms / 1000).toFixed(1)} 秒` : ""}
              </span>
            ) : null}
          </div>
          <button ref={closeRef} class="btn icon-btn" type="button" aria-label="关闭" onClick={close}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>

        <form class="site-drawer-form" onSubmit={submit}>
          <div class="site-drawer-body">
            {site ? (
              <section class="panel">
                <div class="stat-row">
                  <span><strong class="num">{account?.uploaded == null ? "—" : formatSize(account.uploaded)}</strong><small>上传量</small></span>
                  <span><strong class="num">{account?.downloaded == null ? "—" : formatSize(account.downloaded)}</strong><small>下载量</small></span>
                  <span><strong class="num">{account?.ratio == null ? "—" : Number(account.ratio).toFixed(2)}</strong><small>分享率</small></span>
                  <span>
                    <strong class="num">{site.local_stats.success_rate == null ? "—" : `${site.local_stats.success_rate}%`}</strong>
                    <small>近 30 天搜索 {site.local_stats.total} 次</small>
                  </span>
                </div>
                {site.last_status === "error" || site.last_status === "empty" ? (
                  <span class="notice notice-bad">
                    {site.last_message || connection?.label}
                    {site.verify_url ? <VerifyLink url={site.verify_url} siteId={site.id} onChecked={() => void onSaved(site.id)} /> : null}
                  </span>
                ) : null}
                {site.migration_note ? <span class="notice notice-bad">{site.migration_note}</span> : null}
                {account?.error ? <span class="muted site-drawer-note">账户统计：{account.error}</span> : null}
              </section>
            ) : null}

            <div class="drawer-section-title">基本</div>
            <div class="field-grid">
              <TextField label="名称" value={form.name} onInput={(value) => set("name", value)} />
              <TextField label="站点地址" value={form.base_url} onInput={(value) => set("base_url", value)} placeholder="https://example.org" inputMode="url" />
              <TextField label="图标地址（可选）" value={form.icon_url} onInput={(value) => set("icon_url", value)} placeholder="留空使用站点 favicon" inputMode="url" />
            </div>

            <div class="drawer-section-title">认证</div>
            {site ? (
              <div class="site-cookie-card">
                <span class="setting-row-text">
                  <strong>Cookie</strong>
                  <small>
                    {site.cookie_configured
                      ? `${COOKIE_SOURCE_LABELS[site.cookie_source || ""] || "已保存"}${site.cookie_updated_at ? ` · ${formatTime(site.cookie_updated_at)} 更新` : ""}`
                      : "未配置"}
                  </small>
                </span>
                <button
                  class="btn btn-small"
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => api<SiteCookieRefreshed>(`/api/sites/${site.id}/refresh-cookie`, { method: "POST", timeoutMs: 60000 }), (result) => result.message || "Cookie 已刷新", () => onSaved(site.id))}
                >
                  从 CookieCloud 刷新
                </button>
              </div>
            ) : null}
            <div class="field-grid">
              <SecretField
                label={site ? "手动填写 Cookie" : "Cookie"}
                multiline
                value={form.cookie}
                onInput={(value) => set("cookie", value)}
                configured={Boolean(site?.cookie_configured)}
                cleared={cleared.includes("cookie")}
                onToggleClear={site ? () => toggleClear("cookie") : undefined}
              />
              <SecretField
                label="API Key"
                value={form.api_key}
                onInput={(value) => set("api_key", value)}
                configured={Boolean(site?.api_key_configured)}
                cleared={cleared.includes("api_key")}
                onToggleClear={site ? () => toggleClear("api_key") : undefined}
              />
              <SecretField
                label="RSS 地址"
                value={form.rss_url}
                onInput={(value) => set("rss_url", value)}
                configured={Boolean(site?.rss_url_configured)}
                cleared={cleared.includes("rss_url")}
                onToggleClear={site ? () => toggleClear("rss_url") : undefined}
              />
            </div>

            <div class="drawer-section-title">搜索</div>
            <div class="field-grid">
              <TextField label="优先级（1 最高）" type="number" value={form.priority} onInput={(value) => set("priority", value)} />
              <TextField label="超时（秒）" type="number" value={form.timeout_seconds} onInput={(value) => set("timeout_seconds", value)} />
              <TextField
                label="访问频率：每 N 秒"
                type="number"
                value={form.limit_interval}
                placeholder="不限"
                hint="搜索有频率限制的站点填 30"
                onInput={(value) => set("limit_interval", value)}
              />
              <TextField
                label="最多 M 次"
                type="number"
                value={form.limit_count}
                placeholder="1"
                hint="与左边一起：N 秒内最多搜索 M 次"
                onInput={(value) => set("limit_count", value)}
              />
            </div>
            <div class="card site-switches">
              <SwitchRow label="启用" checked={form.enabled} onChange={(value) => set("enabled", value)} />
              <SwitchRow label="参与资源搜索" checked={form.search_enabled} onChange={(value) => set("search_enabled", value)} />
              {form.search_enabled ? (
                <SwitchRow
                  label="仅补缺"
                  hint="其他站点搜完后，只为可选种子不足 3 个的影片补搜一次"
                  checked={form.supplement_only}
                  onChange={(value) => set("supplement_only", value)}
                />
              ) : null}
            </div>

            <div class="drawer-section-title">网络</div>
            <div class="field-grid">
              <TextField label="User-Agent" value={form.user_agent} onInput={(value) => set("user_agent", value)} placeholder="留空使用 AutoList 默认" />
            </div>
            <div class="card site-switches">
              <SwitchRow
                label="经代理访问"
                hint="需要在“设置 → 服务连接”底部的网络代理里填好代理地址并打开“站点允许走代理”；直连 Cloudflare 后面的站点常常很慢"
                checked={form.proxy}
                onChange={(value) => set("proxy", value)}
              />
            </div>
          </div>

          <div class="site-drawer-foot">
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
            <span class="grow">
              {dirty ? (
                <span class="service-row-dirty">
                  <span class="dot dot-warn" aria-hidden="true" />
                  有未保存的修改
                </span>
              ) : null}
            </span>
            {site ? (
              <button
                class="btn"
                type="button"
                disabled={busy}
                title={dirty ? "检测使用已保存的配置" : undefined}
                onClick={() => void run(() => api<SiteCheck>(`/api/sites/${site.id}/test`, { method: "POST", timeoutMs: 60000 }), (result) => result.message || "检测完成", () => onSaved(site.id))}
              >
                检测
              </button>
            ) : null}
            <button class="btn btn-primary" type="submit" disabled={busy || !valid || (site !== null && !dirty)}>
              {busy ? "处理中…" : site ? "保存" : "添加站点"}
            </button>
          </div>
        </form>
      </aside>
    </DrawerLayer>
  );
}


/** 站点卡片：图标与名称、域名与解析方式、上传 / 下载量、检测结果与近 30 天成功率，点击打开编辑。 */
function SiteCard({ site, maxUp, maxDown, busy, missing, onEdit, onToggle, onCheck, onReload }: {
  site: Site;
  maxUp: number;
  maxDown: number;
  busy: boolean;
  missing: string[];
  onEdit: () => void;
  onToggle: () => void;
  onCheck: () => void;
  onReload: () => void;
}) {
  const connection = CONNECTION[site.last_status] || CONNECTION.untested;
  const note = testNote(site);
  const cookie = cookieNote(site, missing);
  const account = site.account_stats;
  const tone = !site.enabled ? "off" : site.last_status === "ok" ? "ok" : site.last_status === "error" ? "bad" : site.last_status === "untested" ? "idle" : "warn";
  const share = (value: number | null | undefined, max: number) => (value && max ? Math.max(2, Math.round((value / max) * 100)) : 0);
  return (
    <article class={`site-card is-${tone}`} onClick={onEdit}>
      <header class="site-card-head">
        <SiteIcon site={site} />
        <div class="site-card-title">
          <a class="site-name" href={site.base_url} target="_blank" rel="noopener noreferrer" title={`打开 ${site.name} 官网`} onClick={(event) => event.stopPropagation()}>
            {site.name}
          </a>
          <small>{domain(site.base_url)}</small>
        </div>
        <span class="site-card-switch" onClick={(event) => event.stopPropagation()}>
          {participates(site) && site.supplement_only ? <small class="site-supplement">仅补缺</small> : null}
          <input
            class="switch"
            type="checkbox"
            role="switch"
            aria-label={`${site.name} 参与搜索`}
            checked={participates(site)}
            disabled={busy || !site.enabled}
            onChange={onToggle}
          />
        </span>
      </header>
      <div class="site-card-tags">
        <span class="site-tag">{officialApi(site) ? "官方 API" : parseLabel(site)}</span>
        {site.proxy ? <span class="site-tag is-accent">代理</span> : null}
        {!site.enabled ? <span class="site-tag">已停用</span> : null}
      </div>
      <div class="site-card-traffic">
        <span class="site-meter is-up" title="上传量">
          <small>↑</small>
          <span class="site-meter-bar"><span style={{ width: `${share(account.uploaded, maxUp)}%` }} /></span>
          <strong class="num">{account.uploaded == null ? "—" : formatSize(account.uploaded)}</strong>
        </span>
        <span class="site-meter is-down" title="下载量">
          <small>↓</small>
          <span class="site-meter-bar"><span style={{ width: `${share(account.downloaded, maxDown)}%` }} /></span>
          <strong class="num">{account.downloaded == null ? "—" : formatSize(account.downloaded)}</strong>
        </span>
      </div>
      <div class="site-card-status" title={note.title || undefined}>
        <span class={`badge ${connection.className}`}>{site.verify_url ? "需要人机验证" : connection.label}</span>
        {site.verify_url ? <span onClick={(event) => event.stopPropagation()}><VerifyLink url={site.verify_url} siteId={site.id} onChecked={onReload} /></span> : note.text ? <small class="site-note">{note.text}</small> : null}
      </div>
      <footer class="site-card-foot" onClick={(event) => event.stopPropagation()}>
        <small class="muted">
          {site.local_stats.total ? `近 30 天 ${site.local_stats.success_rate ?? 0}% · ${site.local_stats.total} 次` : "近 30 天无搜索"}
          {cookie.warn ? <span class="text-warn"> · {cookie.text}</span> : null}
        </small>
        <span class="site-card-actions">
          <button type="button" class="btn btn-small" disabled={busy} onClick={onCheck}>检测</button>
          <button type="button" class="btn btn-small" onClick={onEdit}>编辑</button>
        </span>
      </footer>
    </article>
  );
}

const VIEW_KEY = "autolist.sites-view";
const readView = (): "cards" | "table" => {
  try {
    return window.localStorage.getItem(VIEW_KEY) === "table" ? "table" : "cards";
  } catch {
    return "cards";
  }
};

export function Sites({ health }: { health: SettingsHealth }) {
  const sites = health.sites;
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [view, setViewState] = useState<"cards" | "table">(readView);
  const setView = (next: "cards" | "table") => {
    setViewState(next);
    try {
      window.localStorage.setItem(VIEW_KEY, next);
    } catch {
      // 浏览器禁用存储时只对当前页面生效。
    }
  };
  const { busy, run } = useAction();
  const missing = health.cookiecloud.data?.last_sync?.missing || [];

  const needle = query.trim().toLowerCase();
  const all = sites.data || [];
  const list = all
    .filter((site) => matchesFilter(site, filter))
    .filter((site) => !needle || site.name.toLowerCase().includes(needle) || site.base_url.toLowerCase().includes(needle))
    .sort((a, b) =>
      Number(!a.enabled) - Number(!b.enabled)
      || (STATUS_ORDER[a.last_status] ?? 3) - (STATUS_ORDER[b.last_status] ?? 3)
      || a.priority - b.priority
      || a.id - b.id,
    );
  const counts = Object.fromEntries(FILTERS.map((item) => [item.id, all.filter((site) => matchesFilter(site, item.id)).length])) as Record<Filter, number>;
  const maxUp = Math.max(0, ...all.map((site) => site.account_stats.uploaded ?? 0));
  const maxDown = Math.max(0, ...all.map((site) => site.account_stats.downloaded ?? 0));
  const checkSite = (site: Site) =>
    void run(() => api<SiteCheck>(`/api/sites/${site.id}/test`, { method: "POST", timeoutMs: 60000 }), (result) => result.message || "检测完成", () => sites.reload());
  const editingSite = typeof editing === "number" ? all.find((site) => site.id === editing) ?? null : null;

  const toggleSearch = (site: Site) =>
    void run(
      () => api<SiteSaved>(`/api/sites/${site.id}`, { method: "PUT", body: sitePayload({ ...formFromSite(site), search_enabled: !site.search_enabled }, site, []) }),
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
      />

      <CookieCloudPanel health={health} onSynced={() => void sites.reload()} />

      <div class="site-filters">
        <div class="chips" role="group" aria-label="筛选站点">
          {FILTERS.map((item) => (
            <button key={item.id} type="button" class="chip" aria-current={filter === item.id ? "true" : undefined} onClick={() => setFilter(item.id)}>
              {item.dot ? <span class={`dot ${item.dot}`} aria-hidden="true" /> : null}
              {item.label} <span class="count">{counts[item.id]}</span>
            </button>
          ))}
        </div>
        <input class="log-search" type="search" value={query} placeholder="查找站点" aria-label="查找站点" onInput={(event) => setQuery((event.target as HTMLInputElement).value)} />
        <div class="segmented" role="group" aria-label="显示方式">
          <button type="button" aria-pressed={view === "cards"} onClick={() => setView("cards")}>卡片</button>
          <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}>表格</button>
        </div>
      </div>

      {sites.error && !sites.data ? <div class="notice notice-bad" role="alert">站点读取失败：{sites.error.message}</div> : null}
      {!sites.data && !sites.error ? <p class="muted">正在读取站点……</p> : null}
      {sites.data && !list.length ? (
        <div class="card empty">
          <strong>{all.length ? "没有符合条件的站点" : "还没有站点"}</strong>
          <span>{all.length ? "换一个筛选或关键词。" : "可以从 MoviePilot 一键同步，或手动添加。"}</span>
        </div>
      ) : null}

      {list.length && view === "cards" ? (
        <div class="site-grid">
          {list.map((site) => (
            <SiteCard
              key={site.id}
              site={site}
              maxUp={maxUp}
              maxDown={maxDown}
              busy={busy}
              missing={missing}
              onEdit={() => setEditing(site.id)}
              onToggle={() => toggleSearch(site)}
              onCheck={() => checkSite(site)}
              onReload={() => void sites.reload()}
            />
          ))}
        </div>
      ) : null}

      {list.length && view === "table" ? (
        <div class="card site-table-card">
          <table class="site-table">
            <thead>
              <tr>
                <th scope="col">站点</th>
                <th scope="col">检测</th>
                <th scope="col" class="site-col-detail">近 30 天搜索</th>
                <th scope="col" class="site-col-detail">分享率</th>
                <th scope="col" class="site-col-detail">Cookie</th>
                <th scope="col">参与搜索</th>
                <th scope="col" class="site-col-edit"><span class="visually-hidden">编辑</span></th>
              </tr>
            </thead>
            <tbody>
              {list.map((site) => {
                const connection = CONNECTION[site.last_status] || CONNECTION.untested;
                const cookie = cookieNote(site, missing);
                const note = testNote(site);
                return (
                  <tr key={site.id} class={site.enabled ? undefined : "is-disabled"} onClick={() => setEditing(site.id)}>
                    <td>
                      <span class="site-cell">
                        <SiteIcon site={site} />
                        <span class="site-cell-text">
                          <a
                            class="site-name"
                            href={site.base_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={`打开 ${site.name} 官网`}
                            onClick={(event) => event.stopPropagation()}
                          >
                            {site.name}
                          </a>
                          <small>{officialApi(site) ? "官方 API" : parseLabel(site)} · {domain(site.base_url)}{site.enabled ? "" : " · 已停用"}</small>
                        </span>
                      </span>
                    </td>
                    <td>
                      <span class="site-status" title={note.title || undefined}>
                        <span class={`badge ${connection.className}`}>{site.verify_url ? "需要人机验证" : connection.label}</span>
                        {site.verify_url ? <VerifyLink url={site.verify_url} siteId={site.id} onChecked={() => void sites.reload()} /> : note.text ? <small class="site-note">{note.text}</small> : null}
                      </span>
                    </td>
                    <td class="site-col-detail num">
                      {site.local_stats.total ? `${site.local_stats.success_rate ?? 0}% · ${site.local_stats.total} 次` : "—"}
                    </td>
                    <td class="site-col-detail num">{site.account_stats.ratio == null ? "—" : Number(site.account_stats.ratio).toFixed(2)}</td>
                    <td class="site-col-detail">
                      <small class={`site-note${cookie.warn ? " text-warn" : ""}`} title={cookie.title || undefined}>{cookie.text}</small>
                    </td>
                    <td class="site-col-switch" onClick={(event) => event.stopPropagation()}>
                      {participates(site) && site.supplement_only ? <small class="site-supplement">仅补缺</small> : null}
                      <input
                        class="switch"
                        type="checkbox"
                        role="switch"
                        aria-label={`${site.name} 参与搜索`}
                        checked={participates(site)}
                        disabled={busy || !site.enabled}
                        onChange={() => toggleSearch(site)}
                      />
                    </td>
                    <td class="site-col-edit">
                      <button type="button" class="site-edit" onClick={(event) => { event.stopPropagation(); setEditing(site.id); }}>编辑</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div class="site-table-foot muted">按检测结果排序：失败与搜不到在前，停用的站点在最后。</div>
        </div>
      ) : null}

      {editing !== null ? (
        <SiteDrawer
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
