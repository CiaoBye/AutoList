import { useState } from "preact/hooks";
import { api } from "../../api";
import { formatTime } from "../../format";
import type { CookieCloudSynced } from "../../types";
import type { SettingsHealth } from "./health";
import { DirtyNote, ProviderBadge, SecretField, useAction, useProviderTest } from "./shared";
import { useSettingsDraft } from "./useSettingsForm";

const FIELDS = ["cookiecloud_url", "cookiecloud_key", "cookiecloud_password", "cookiecloud_interval_minutes"];
/** 定时拉取的间隔选项：0 表示不定时，只在 Cookie 失效或手动同步时拉取（与 MoviePilot 一样默认一天一次）。 */
const INTERVALS: { value: number; label: string }[] = [
  { value: 60, label: "每小时" },
  { value: 360, label: "每 6 小时" },
  { value: 1440, label: "每天（默认）" },
  { value: 0, label: "不定时，只在 Cookie 失效或手动同步时" },
];
export const pullLabel = (minutes: number): string =>
  minutes <= 0 ? "只在 Cookie 失效或手动同步时拉取" : minutes % 1440 === 0 ? `每 ${minutes / 1440} 天拉取` : minutes % 60 === 0 ? `每 ${minutes / 60} 小时拉取` : `每 ${minutes} 分钟拉取`;
const ORIGIN_LABELS: Record<string, string> = { schedule: "定时拉取", expired: "Cookie 失效后重新拉取", manual: "手动同步" };

/** 站点页顶部的 Cookie 来源：一条状态（拉取方式、最近一次同步、未覆盖的站点），连接设置按需展开。 */
export function CookieCloudPanel({ health, onSynced }: { health: SettingsHealth; onSynced: () => void }) {
  const { form, set, cleared, toggleClear, configured, dirty, save, reset } = useSettingsDraft(FIELDS, health.settings);
  const { results, testing, test } = useProviderTest();
  const { busy, run } = useAction();
  const cc = health.cookiecloud.data;
  const [open, setOpen] = useState(false);
  const expanded = open || (cc !== null && !cc.configured);
  const text = (name: string) => String(form[name] ?? "");
  const last = cc?.last_sync;
  const missing = last?.missing || [];

  const facts = !cc
    ? "正在读取 CookieCloud 状态……"
    : !cc.configured
      ? "尚未配置"
      : [
          `${pullLabel(cc.pull_interval_minutes)}（来自 ${cc.url || "CookieCloud 服务器"}）`,
          last
            ? `最近 ${formatTime(last.at)} ${ORIGIN_LABELS[last.origin] || "同步"}，更新 ${last.updated.length} 个、${last.unchanged} 个已是最新`
            : "服务重启后还没有同步过",
          missing.length ? `${missing.join("、")} 不在 CookieCloud 中` : "",
        ].filter(Boolean).join(" · ");

  return (
    <section class="card cc-strip" aria-labelledby="cookiecloud-title">
      <div class="cc-strip-main">
        <img class="cc-strip-icon" src="/assets/service-cookiecloud.svg" alt="" width="22" height="22" />
        <div class="cc-strip-facts">
          <strong id="cookiecloud-title">Cookie 来源 · CookieCloud</strong>
          <span>{facts}</span>
        </div>
        {results.cookiecloud ? (
          <ProviderBadge status={results.cookiecloud} />
        ) : cc ? (
          <span class={`badge ${cc.configured ? (last ? "st-in_library" : "st-unchecked") : "st-missing"}`}>
            {cc.configured ? (last ? "同步正常" : "等待首次同步") : "未配置"}
          </span>
        ) : null}
        <span class="cc-strip-actions">
          <button
            class="btn btn-small"
            type="button"
            disabled={busy || !cc?.configured}
            onClick={() =>
              void run(
                () => api<CookieCloudSynced>("/api/sites/sync-cookiecloud", { method: "POST", timeoutMs: 60000 }),
                (result) => result.message || "已同步站点 Cookie",
                () => {
                  void health.cookiecloud.reload();
                  onSynced();
                },
              )
            }
          >
            立即同步
          </button>
          <button class="btn btn-small" type="button" aria-expanded={expanded} onClick={() => setOpen(!open)} disabled={cc !== null && !cc.configured}>
            连接设置
          </button>
        </span>
      </div>
      {expanded ? (
        <form
          class="cc-config"
          onSubmit={(event) => {
            event.preventDefault();
            void run(save, "CookieCloud 设置已保存", () => health.cookiecloud.reload());
          }}
        >
          <div class="field-grid cc-fields">
            <label class="field">
              <span class="field-label-row">
                服务器地址
                {cleared.includes("cookiecloud_url") ? <span class="badge st-issue">将清除</span> : null}
              </span>
              <span class={`secret-wrap${configured("cookiecloud_url") ? " has-clear" : ""}`}>
                <input
                  type="text"
                  inputMode="url"
                  autocomplete="off"
                  value={text("cookiecloud_url")}
                  placeholder="http://192.168.x.x:3000/cookiecloud"
                  onInput={(event) => set("cookiecloud_url", (event.target as HTMLInputElement).value)}
                />
                {configured("cookiecloud_url") ? (
                  <button class="btn btn-small secret-clear" type="button" aria-pressed={cleared.includes("cookiecloud_url")} onClick={() => toggleClear("cookiecloud_url")} title={cleared.includes("cookiecloud_url") ? "撤销清除" : "保存时清除已保存的地址"}>
                    {cleared.includes("cookiecloud_url") ? "撤销" : "清除"}
                  </button>
                ) : null}
              </span>
              <small class="field-hint">{cleared.includes("cookiecloud_url") ? "保存后删除已保存的地址" : "留空保存即保留当前地址"}</small>
            </label>
            <SecretField
              label="用户 KEY"
              value={text("cookiecloud_key")}
              onInput={(value) => set("cookiecloud_key", value)}
              configured={configured("cookiecloud_key")}
              cleared={cleared.includes("cookiecloud_key")}
              onToggleClear={() => toggleClear("cookiecloud_key")}
              hint="5–128 位字母、数字、_ 或 -"
            />
            <SecretField
              label="端对端加密密码"
              value={text("cookiecloud_password")}
              onInput={(value) => set("cookiecloud_password", value)}
              configured={configured("cookiecloud_password")}
              cleared={cleared.includes("cookiecloud_password")}
              onToggleClear={() => toggleClear("cookiecloud_password")}
            />
            <label class="field">
              <span>定时拉取</span>
              <select value={String(form.cookiecloud_interval_minutes ?? 1440)} onChange={(event) => set("cookiecloud_interval_minutes", Number((event.target as HTMLSelectElement).value))}>
                {INTERVALS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
              <small class="field-hint">站点的 Cookie 失效时会立即补拉一次，所以定时拉取不必太频繁。</small>
            </label>
          </div>
          <div class="service-row-actions">
            <DirtyNote dirty={dirty} onReset={reset} />
            <button class="btn" type="button" disabled={testing !== null} title={dirty ? "检测使用已保存的配置" : undefined} onClick={() => void test("cookiecloud")}>
              {testing === "cookiecloud" ? "检测中…" : "检测"}
            </button>
            <button class="btn btn-primary" type="submit" disabled={busy || !dirty}>
              {busy ? "保存中…" : "保存 CookieCloud 设置"}
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
