import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../api";
import { formatSize, formatTime } from "../../format";
import type { MoviePilotSitesSynced, Site, SiteCheck, SiteChecks, SiteCookieRefreshed, SiteSaved } from "../../types";
import { CookieCloudPanel } from "./CookieCloudPanel";
import type { SettingsHealth } from "./health";
import { SecretField, SectionHead, SwitchRow, TextField, useAction } from "./shared";
import { DrawerLayer } from "../../components/DrawerLayer";

const ADAPTER_LABELS: Record<string, string> = {
  mteam: "M-Team API",
  rss: "RSS 订阅",
  torznab: "Torznab",
};

const PROFILE_LABELS: Record<string, string> = {
  nexusphp: "NexusPHP 页面",
  alt_layout: "NexusPHP 页面（站点A）",
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

/** 检测列的补充说明：失败或搜不到时显示原因，否则显示耗时与检测时间。 */
const testNote = (site: Site): string => {
  if (!site.last_tested_at) return "尚未检测";
  if (site.last_status === "error" || site.last_status === "empty") return site.last_message || "";
  const seconds = site.last_duration_ms ? `${(site.last_duration_ms / 1000).toFixed(1)} 秒 · ` : "";
  return `${seconds}${formatTime(site.last_tested_at)}`;
};

/** Cookie 列：来源与更新时间；不在 CookieCloud 中或缺少 Cookie 时提醒。 */
const cookieNote = (site: Site, missing: string[]): { text: string; warn: boolean } => {
  if (officialApi(site)) return { text: "官方 API，不需要 Cookie", warn: false };
  if (site.adapter !== "nexusphp") return { text: "不需要 Cookie", warn: false };
  if (!site.cookie_configured) return { text: "未配置 Cookie", warn: true };
  const source = COOKIE_SOURCE_LABELS[site.cookie_source || ""] || "已保存";
  const when = site.cookie_updated_at ? ` · ${formatTime(site.cookie_updated_at)}` : "";
  if (missing.includes(site.name)) return { text: `${source}${when} · 不在 CookieCloud 中`, warn: true };
  return { text: `${source}${when}`, warn: false };
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
  const official_api_site = /official_api_site\.com/.test(form.base_url);

  return (
    <DrawerLayer>
      <button class="drawer-backdrop" type="button" aria-label="关闭站点编辑" tabIndex={-1} onClick={close} />
      <aside class="drawer drawer-site" role="dialog" aria-modal="true" aria-labelledby="site-drawer-title">
        <div class="site-drawer-head">
          <SiteIcon site={site} />
          <div class="site-drawer-title">
            <h2 id="site-drawer-title">{site ? site.name : "添加站点"}</h2>
            {site ? (
              <span class="muted">
                {domain(site.base_url)} · {parseLabel(site)} · {connection?.label}
                {site.last_tested_at && site.last_duration_ms ? ` ${(site.last_duration_ms / 1000).toFixed(1)} 秒` : ""}
              </span>
            ) : (
              <span class="muted">协议与解析方式按站点地址自动识别</span>
            )}
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
                {site.last_status === "error" || site.last_status === "empty" ? <span class="notice notice-bad">{site.last_message || connection?.label}</span> : null}
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
                hint="NexusPHP 页面搜索需要；配置 CookieCloud 后会自动更新"
              />
              <SecretField
                label="API Key"
                value={form.api_key}
                onInput={(value) => set("api_key", value)}
                configured={Boolean(site?.api_key_configured)}
                cleared={cleared.includes("api_key")}
                onToggleClear={site ? () => toggleClear("api_key") : undefined}
                hint={official_api_site ? "填写后改走站点D官方搜索接口，不受网页二次验证影响（在站点控制面板生成）" : "M-Team、Torznab 与站点D官方接口需要"}
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
            </div>

            <div class="drawer-section-title">搜索</div>
            <div class="field-grid">
              <TextField label="优先级（1 最高）" type="number" value={form.priority} onInput={(value) => set("priority", value)} />
              <TextField label="超时（秒）" type="number" value={form.timeout_seconds} onInput={(value) => set("timeout_seconds", value)} />
            </div>
            <div class="card site-switches">
              <SwitchRow label="启用" hint="停用后不参与检测、统计与搜索" checked={form.enabled} onChange={(value) => set("enabled", value)} />
              <SwitchRow label="参与资源搜索" hint="寻片时搜索这个站点" checked={form.search_enabled} onChange={(value) => set("search_enabled", value)} />
              <SwitchRow label="经代理访问" hint="需先在“服务连接 · 网络代理”配置代理并允许 PT 站点走代理" checked={form.proxy} onChange={(value) => set("proxy", value)} />
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

export function Sites({ health }: { health: SettingsHealth }) {
  const sites = health.sites;
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
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
      >
        协议与解析方式按地址自动识别；检测用《The Godfather》做一次真实搜索。点任意一行编辑。
      </SectionHead>

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
      </div>

      {sites.error && !sites.data ? <div class="notice notice-bad" role="alert">站点读取失败：{sites.error.message}</div> : null}
      {!sites.data && !sites.error ? <p class="muted">正在读取站点……</p> : null}
      {sites.data && !list.length ? (
        <div class="card empty">
          <strong>{all.length ? "没有符合条件的站点" : "还没有站点"}</strong>
          <span>{all.length ? "换一个筛选或关键词。" : "可以从 MoviePilot 一键同步，或手动添加。"}</span>
        </div>
      ) : null}

      {list.length ? (
        <div class="card site-table-card">
          <table class="site-table">
            <thead>
              <tr>
                <th scope="col">站点</th>
                <th scope="col" class="site-col-detail">解析方式</th>
                <th scope="col">检测</th>
                <th scope="col" class="site-col-detail">近 30 天搜索</th>
                <th scope="col" class="site-col-detail">分享率</th>
                <th scope="col" class="site-col-wide">Cookie</th>
                <th scope="col">参与搜索</th>
                <th scope="col" class="site-col-detail"><span class="visually-hidden">编辑</span></th>
              </tr>
            </thead>
            <tbody>
              {list.map((site) => {
                const connection = CONNECTION[site.last_status] || CONNECTION.untested;
                const cookie = cookieNote(site, missing);
                return (
                  <tr key={site.id} class={site.enabled ? undefined : "is-disabled"} onClick={() => setEditing(site.id)}>
                    <td>
                      <span class="site-cell">
                        <SiteIcon site={site} />
                        <span class="site-cell-text">
                          <button type="button" class="link-button" onClick={(event) => { event.stopPropagation(); setEditing(site.id); }}>
                            <strong>{site.name}</strong>
                          </button>
                          <small>{domain(site.base_url)}{site.enabled ? "" : " · 已停用"}</small>
                        </span>
                      </span>
                    </td>
                    <td class="site-col-detail">{officialApi(site) ? <span class="badge st-searching">官方 API</span> : parseLabel(site)}</td>
                    <td>
                      <span class={`badge ${connection.className}`}>{connection.label}</span>
                      <small class="site-note">{testNote(site)}</small>
                    </td>
                    <td class="site-col-detail num">
                      {site.local_stats.total ? `${site.local_stats.success_rate ?? 0}% · ${site.local_stats.total} 次` : "—"}
                    </td>
                    <td class="site-col-detail num">{site.account_stats.ratio == null ? "—" : Number(site.account_stats.ratio).toFixed(2)}</td>
                    <td class="site-col-wide">
                      <small class={`site-note${cookie.warn ? " text-warn" : ""}`}>{cookie.text}</small>
                    </td>
                    <td onClick={(event) => event.stopPropagation()}>
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
                    <td class="site-col-detail site-edit">编辑</td>
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
