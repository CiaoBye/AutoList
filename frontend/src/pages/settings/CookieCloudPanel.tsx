import { useState } from "preact/hooks";
import { api } from "../../api";
import { formatTime } from "../../format";
import type { CookieCloudSynced } from "../../types";
import type { SettingsHealth } from "./health";
import { DirtyNote, ProviderBadge, SecretField, TextField, useAction, useProviderTest } from "./shared";
import { useSettingsDraft } from "./useSettingsForm";

const FIELDS = ["cookiecloud_url", "cookiecloud_key", "cookiecloud_password"];
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
          `每 ${cc.pull_interval_minutes} 分钟从 ${cc.url || "CookieCloud 服务器"} 拉取`,
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
          <div class="field-grid">
            <TextField
              label="服务器地址"
              value={text("cookiecloud_url")}
              onInput={(value) => set("cookiecloud_url", value)}
              placeholder="http://192.168.x.x:3000/cookiecloud"
              inputMode="url"
              hint={cleared.includes("cookiecloud_url") ? "保存后删除已保存的地址" : "留空保存即保留当前地址"}
            />
            {configured("cookiecloud_url") ? (
              <span class="field clear-action">
                <button class="btn btn-small" type="button" aria-pressed={cleared.includes("cookiecloud_url")} onClick={() => toggleClear("cookiecloud_url")}>
                  {cleared.includes("cookiecloud_url") ? "撤销清除" : "清除服务器地址"}
                </button>
              </span>
            ) : null}
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
