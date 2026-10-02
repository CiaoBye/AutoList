import { api } from "../../api";
import { formatTime } from "../../format";
import { href } from "../../router";
import { readTheme } from "../../theme";
import type { CookieCloudSynced } from "../../types";
import { STRENGTH_TEXT, THEMES } from "./Appearance";
import { CHECKED_SERVICES, dotClass, serviceDetail, type SettingsHealth } from "./health";
import { eventLabel, describe, LEVEL_CLASS, LEVEL_TEXT, levelOf, shortStamp } from "./logText";
import { SectionHead, useAction } from "./shared";
import { VerifyLink } from "../../components/VerifyLink";

const clockOf = (date: Date | null) =>
  date ? `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}` : "";

/** 设置首页：服务、站点、Cookie 来源、最近的问题与访问安全一页看完，有异常的点进对应分区处理。 */
export function Overview({ health }: { health: SettingsHealth }) {
  const { busy, run } = useAction();
  const statuses = health.services.data || {};
  const okCount = Object.values(statuses).filter((item) => item.ok).length;
  const aiConfigured = Boolean(health.settings.data?.ai_base_url && health.settings.data?.ai_api_key_configured);
  const sites = (health.sites.data || []).filter((site) => site.enabled);
  const usable = sites.filter((site) => site.last_status === "ok" || site.last_status === "slow").length;
  const lastTested = sites.map((site) => site.last_tested_at).filter(Boolean).sort().pop();
  const cc = health.cookiecloud.data;
  const missing = cc?.last_sync?.missing || [];
  const strength = health.settings.data?.access_token_strength;
  const theme = THEMES.find((item) => item.id === readTheme());

  return (
    <div class="settings-form">
      <SectionHead
        title="概览"
        actions={
          <button class="btn" type="button" disabled={health.services.loading} onClick={health.reloadAll}>
            {health.services.loading ? "检测中…" : "重新检测"}
          </button>
        }
      />

      <section class="card overview-card" aria-labelledby="overview-services">
        <header class="overview-head">
          <h3 id="overview-services">服务连接</h3>
          {health.services.data ? <span class="badge st-in_library">{okCount} 个正常</span> : null}
          {health.failingServices ? <span class="badge st-issue">{health.failingServices} 个异常</span> : null}
          {health.checkedAt ? <span class="muted overview-meta">检测于 {clockOf(health.checkedAt)}</span> : null}
          <a class="overview-more" href={href("/settings/services")}>管理服务 →</a>
        </header>
        {health.services.error && !health.services.data ? (
          <p class="notice notice-bad overview-body">检测失败：{health.services.error.message}</p>
        ) : (
          <div class="service-tiles">
            {CHECKED_SERVICES.map((service) => (
              <a key={service.key} class="service-tile" href={href("/settings/services")}>
                <span class="service-tile-name">
                  <span class={`dot ${dotClass(statuses[service.key])}`} aria-hidden="true" />
                  {service.label}
                </span>
                <span class="muted">
                  {service.role} · {serviceDetail(service.key, statuses[service.key])}
                </span>
              </a>
            ))}
            <a class="service-tile" href={href("/settings/services")}>
              <span class="service-tile-name">
                <span class={`dot ${aiConfigured ? "dot-ok" : "dot-off"}`} aria-hidden="true" />
                AI 辅助识别
              </span>
              <span class="muted">{aiConfigured ? "已配置" : "未配置 · 可选"}</span>
            </a>
          </div>
        )}
      </section>

      <div class="overview-grid">
        <section class="card overview-card" aria-labelledby="overview-sites">
          <header class="overview-head">
            <h3 id="overview-sites">站点</h3>
            <a class="overview-more" href={href("/settings/sites")}>查看站点 →</a>
          </header>
          {!health.sites.data ? (
            <p class="muted overview-body">正在读取站点……</p>
          ) : (
            <>
              <div class="overview-figure">
                <strong>{usable}</strong>
                <span class="muted">/ {sites.length} 可用</span>
                <span class="grow" />
                {lastTested ? <span class="muted overview-meta">最近检测 {formatTime(lastTested)}</span> : null}
              </div>
              <ul class="overview-rows">
                {health.failingSites.map((site) => (
                  <li key={site.id}>
                    <span class="dot dot-bad" aria-hidden="true" />
                    <strong>{site.name}</strong>
                    <span class="muted overview-clip">{site.last_message || "检测失败"}</span>
                    {site.verify_url ? <VerifyLink url={site.verify_url} siteId={site.id} onChecked={() => void health.sites.reload()} /> : <span class="badge st-issue">失败</span>}
                  </li>
                ))}
                {health.emptySites.map((site) => (
                  <li key={site.id}>
                    <span class="dot dot-warn" aria-hidden="true" />
                    <strong>{site.name}</strong>
                    <span class="muted overview-clip">登录正常，但搜索没有解析到结果</span>
                    <span class="badge st-candidates">搜不到</span>
                  </li>
                ))}
                {missing.map((name) => (
                  <li key={`missing-${name}`}>
                    <span class="dot dot-warn" aria-hidden="true" />
                    <strong>{name}</strong>
                    <span class="muted overview-clip">不在 CookieCloud 中，Cookie 只能手动更新</span>
                    <span class="badge st-candidates">注意</span>
                  </li>
                ))}
                {!health.failingSites.length && !health.emptySites.length && !missing.length ? (
                  <li class="muted">所有已启用的站点都能正常搜索。</li>
                ) : null}
              </ul>
            </>
          )}
        </section>

        <section class="card overview-card" aria-labelledby="overview-cookie">
          <header class="overview-head">
            <h3 id="overview-cookie">Cookie 来源 · CookieCloud</h3>
            <a class="overview-more" href={href("/settings/sites")}>同步设置 →</a>
          </header>
          {!cc ? (
            <p class="muted overview-body">正在读取 CookieCloud 状态……</p>
          ) : !cc.configured ? (
            <p class="notice overview-body">尚未配置 CookieCloud</p>
          ) : (
            <>
              <dl class="overview-facts">
                <div><dt>同步方式</dt><dd>每 {cc.pull_interval_minutes} 分钟拉取</dd></div>
                <div>
                  <dt>最近同步</dt>
                  <dd>
                    {cc.last_sync
                      ? `${formatTime(cc.last_sync.at)} · 更新 ${cc.last_sync.updated.length} 个，${cc.last_sync.unchanged} 个已是最新`
                      : "服务重启后还没有同步过"}
                  </dd>
                </div>
                {missing.length ? <div><dt>未覆盖</dt><dd>{missing.join("、")}</dd></div> : null}
              </dl>
              <div class="overview-body">
                <button
                  class="btn btn-small"
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => api<CookieCloudSynced>("/api/sites/sync-cookiecloud", { method: "POST", timeoutMs: 60000 }),
                      (result) => result.message || "已同步站点 Cookie",
                      () => {
                        void health.cookiecloud.reload();
                        void health.sites.reload();
                      },
                    )
                  }
                >
                  立即同步
                </button>
              </div>
            </>
          )}
        </section>
      </div>

      <div class="overview-grid">
        <section class="card overview-card" aria-labelledby="overview-problems">
          <header class="overview-head">
            <h3 id="overview-problems">最近的警告与错误</h3>
            <a class="overview-more" href={href("/settings/logs")}>诊断日志 →</a>
          </header>
          {!health.problems.data ? (
            <p class="muted overview-body">正在读取日志……</p>
          ) : health.recentProblems.length ? (
            <ul class="overview-rows">
              {health.recentProblems.slice(0, 5).map((event, index) => {
                const level = levelOf(event);
                return (
                  <li key={`${event.ts}-${index}`}>
                    <span class="mono muted overview-time">{shortStamp(event.ts)}</span>
                    <span class={`badge ${LEVEL_CLASS[level]}`}>{LEVEL_TEXT[level]}</span>
                    <span class="overview-clip">
                      <strong>{eventLabel(event)}</strong>
                      {describe(event) ? <span class="muted"> · {describe(event)}</span> : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p class="muted overview-body">最近 24 小时没有警告或错误。</p>
          )}
        </section>

        <section class="card overview-card" aria-labelledby="overview-access">
          <header class="overview-head">
            <h3 id="overview-access">访问与外观</h3>
            <a class="overview-more" href={href("/settings/appearance")}>外观与访问 →</a>
          </header>
          <div class="overview-body overview-stack">
            {strength && strength !== "missing" ? <p class={`notice${strength === "strong" ? "" : " notice-bad"}`}>{STRENGTH_TEXT[strength]}</p> : null}
            <dl class="overview-facts overview-facts-flat">
              <div><dt>主题</dt><dd>{theme ? theme.name : "—"}</dd></div>
            </dl>
          </div>
        </section>
      </div>
    </div>
  );
}
