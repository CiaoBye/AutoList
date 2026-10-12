import { api } from "../api";
import { formatSize, formatTime } from "../format";
import { useLoad } from "../hooks";
import { href, type Route } from "../router";
import type { SiteStatusPage, SiteStatusRow, UpgradeFilm } from "../types";

const COOKIE_TONE: Record<SiteStatusRow["cookie"]["state"], string> = {
  ok: "is-ok", expired: "is-bad", missing: "is-warn", failing: "is-bad", none: "",
};

const SOURCE_TEXT: Record<string, string> = { cookiecloud: "CookieCloud", manual: "手动填写", moviepilot: "MoviePilot" };

/** 近两周每天的搜索次数：柱高是次数，失败的天数用警示色。 */
function Trend({ history }: { history: SiteStatusRow["history"] }) {
  const peak = Math.max(1, ...history.map((day) => day.total));
  return (
    <span class="status-trend" role="img" aria-label={`近 ${history.length} 天搜索次数：${history.map((day) => day.total).join("、")}`}>
      {history.map((day) => (
        <span
          key={day.day}
          class={day.failed ? "is-failed" : day.total ? "is-ok" : ""}
          style={{ height: `${day.total ? Math.max(12, (day.total / peak) * 100) : 6}%` }}
          title={`${day.day}：搜索 ${day.total} 次${day.failed ? `，失败 ${day.failed} 次` : ""}`}
        />
      ))}
    </span>
  );
}

const cookieNote = (site: SiteStatusRow): string => {
  const parts: string[] = [];
  if (site.cookie.source) parts.push(SOURCE_TEXT[site.cookie.source] ?? site.cookie.source);
  if (site.cookie.age_days !== null) parts.push(site.cookie.age_days === 0 ? "今天更新" : `${site.cookie.age_days} 天前更新`);
  return parts.join(" · ");
};

function SiteRow({ site, stats }: { site: SiteStatusRow; stats: number }) {
  const search = site.search;
  return (
    <li class={`status-row${site.problem ? " is-problem" : ""}${site.enabled ? "" : " is-off"}`}>
      <span class="status-site">
        <img src={site.icon_endpoint} alt="" width="20" height="20" loading="lazy" />
        <strong>{site.name}</strong>
        {!site.enabled ? <span class="muted">已停用</span> : !site.search_enabled ? <span class="muted">不参与搜索</span> : null}
      </span>

      <span class="status-cell status-cookie">
        <span class={`status-pill ${COOKIE_TONE[site.cookie.state]}`}>{site.cookie.text}</span>
        <small class="muted">{cookieNote(site) || "—"}</small>
      </span>

      <span class="status-cell status-trend-cell">
        <Trend history={site.history} />
        <small class="muted">
          {search.total ? `近 ${stats} 天 ${search.success_rate ?? 0}% · ${search.total} 次${search.average_ms ? ` · ${(search.average_ms / 1000).toFixed(1)}s` : ""}` : `近 ${stats} 天无搜索`}
        </small>
      </span>

      <span class="status-cell">
        <strong class="num">{site.submissions.total}</strong>
        <small class="muted">
          {site.submissions.total ? `提交 · 成功 ${site.submissions.succeeded}` : "提交"}
        </small>
      </span>

      <span class="status-cell">
        <strong class="num">{site.seeding ? site.seeding.count : "—"}</strong>
        <small class="muted">{site.seeding ? `做种 · ${formatSize(site.seeding.size)}` : "做种"}</small>
      </span>

      <span class="status-cell">
        <strong class="num">{site.account.ratio == null ? "—" : Number(site.account.ratio).toFixed(2)}</strong>
        <small class="muted">分享率</small>
      </span>

      <span class="status-last muted">
        {site.failure_streak ? `连续失败 ${site.failure_streak >= 30 ? "30+" : site.failure_streak} 次` : site.last_success_at ? `上次成功 ${formatTime(site.last_success_at)}` : ""}
      </span>
    </li>
  );
}

function Upgrades() {
  const list = useLoad<UpgradeFilm[]>((signal) => api<UpgradeFilm[]>("/api/upgrades", { signal }), [], () => 60000, "upgrades");
  const rows = list.data ?? [];
  return (
    <section class="upgrade-list-wrap" aria-label="升级候选">
      <p class="muted status-note">
        在“书目”里打开已入馆的影片，点“找更高画质”，会按 Emby 里现有的最高画质只找更高的资源；找到的候选会列在这里（保留 7 天），选定后到“遴选”提交。
      </p>
      {list.loading && !list.data ? <p class="muted">读取中…</p> : null}
      {list.data && !rows.length ? <p class="muted">还没有升级候选。</p> : null}
      {rows.length ? (
        <ul class="status-list">
          {rows.map((film) => (
            <li key={film.id} class="upgrade-row">
              <a class="upgrade-title" href={href(`/films/${film.id}`)}>
                <strong>{film.title}</strong>
                {film.year ? <span class="muted"> · {film.year}</span> : null}
              </a>
              <span class="muted">现有 {film.from_tier}p → 找到 {film.candidate_count} 个更高画质{film.best_resolution ? `（最高 ${film.best_resolution}）` : ""}</span>
              <span class="muted">{film.selected_count ? `已选 ${film.selected_count} 个，去遴选提交` : `搜索于 ${formatTime(film.searched_at)}`}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

export function Status({ route }: { route: Route }) {
  const tab = route.query.get("tab") === "upgrade" ? "upgrade" : "sites";
  const page = useLoad<SiteStatusPage>((signal) => api<SiteStatusPage>("/api/site-status", { signal, timeoutMs: 30000 }), [], () => 60000, "site-status");
  const data = page.data;
  const problems = data?.sites.filter((site) => site.problem) ?? [];

  return (
    <main class="page status-page">
      <div class="page-head">
        <h1>站况</h1>
        <span class="page-head-sub">各站点的 Cookie 健康、搜索与提交、做种情况</span>
      </div>

      <nav class="chips status-tabs" aria-label="站况分类">
        <a class="chip" aria-current={tab === "sites" ? "true" : undefined} href={href("/status")}>站点</a>
        <a class="chip" aria-current={tab === "upgrade" ? "true" : undefined} href={href("/status", { tab: "upgrade" })}>升级</a>
      </nav>

      {tab === "upgrade" ? <Upgrades /> : null}
      {tab === "sites" && page.error && !data ? <p class="muted">读取失败：{page.error.message}</p> : null}
      {tab === "sites" && page.loading && !data ? <p class="muted">读取中…</p> : null}

      {tab === "sites" && data ? (
        <>
          {problems.length ? (
            <section class="status-alerts" aria-label="需要处理">
              <h2>需要处理 · {problems.length} 个站点</h2>
              <ul>
                {problems.map((site) => (
                  <li key={site.id}>
                    <strong>{site.name}</strong>
                    <span>{site.cookie.text}</span>
                    {site.last_message && site.cookie.state !== "expired" ? <span class="muted">{site.last_message}</span> : null}
                  </li>
                ))}
              </ul>
              <a class="btn btn-small" href={href("/settings/sites")}>去站点修复</a>
            </section>
          ) : (
            <p class="status-clear muted">所有启用的站点 Cookie 与搜索都正常。</p>
          )}

          {data.sites.length ? (
            <ul class="status-list">
              {data.sites.map((site) => <SiteRow key={site.id} site={site} stats={data.stats_days} />)}
            </ul>
          ) : (
            <p class="muted">还没有站点，先到“馆务 → 站点”添加。</p>
          )}
          {!data.transmission_available ? <p class="muted status-note">Transmission 未连接，做种数暂不显示。</p> : null}
        </>
      ) : null}
    </main>
  );
}
