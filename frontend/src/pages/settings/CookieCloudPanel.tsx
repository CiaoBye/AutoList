import { api } from "../../api";
import { formatTime } from "../../format";
import { useLoad, useToast } from "../../hooks";
import type { CookieCloudStatus } from "../../types";
import { ProviderBadge, SecretField, TextField, useAction, useProviderTest } from "./shared";
import { useSettingsForm } from "./useSettingsForm";

const FIELDS = ["cookiecloud_url", "cookiecloud_key", "cookiecloud_password"];
const ORIGIN_LABELS: Record<string, string> = { pull: "定时拉取", push: "插件推送", manual: "手动同步" };

/** 站点 Cookie 的来源：同步方式、最近一次同步的结果，以及可展开的连接配置。 */
export function CookieCloudPanel({ onSynced }: { onSynced: () => void }) {
  const toast = useToast();
  const { settings, form, set, cleared, toggleClear, configured, dirty, save } = useSettingsForm(FIELDS);
  const status = useLoad<CookieCloudStatus>((signal) => api<CookieCloudStatus>("/api/cookiecloud/status", { signal }), []);
  const { results, testing, test } = useProviderTest();
  const { busy, run } = useAction();
  const text = (name: string) => String(form[name] ?? "");
  const cc = status.data;
  const endpoint = `${window.location.origin}${settings.data?.cookiecloud_endpoint || "/cookiecloud"}`;

  const modes = cc
    ? [cc.url_configured ? "每小时从服务器拉取" : null, cc.received ? `接收插件推送（最近 ${formatTime(cc.updated_at)}）` : null].filter(Boolean)
    : [];
  const last = cc?.last_sync;
  const summary = last
    ? `${formatTime(last.at)} ${ORIGIN_LABELS[last.origin] || "同步"}：更新 ${last.updated.length} 个站点${last.updated.length ? `（${last.updated.join("、")}）` : ""}，${last.unchanged} 个已是最新`
    : "服务重启后还没有同步过，定时拉取每小时一次";

  const copyEndpoint = async () => {
    try {
      await navigator.clipboard.writeText(endpoint);
      toast.show("已复制推送地址");
    } catch {
      toast.show("复制失败，请手动选择地址复制");
    }
  };

  return (
    <section class="card settings-group cookiecloud-panel" aria-labelledby="cookiecloud-title">
      <div class="settings-legend" id="cookiecloud-title">
        <img src="/assets/service-cookiecloud.svg" alt="" width="22" height="22" />
        Cookie 来源 · CookieCloud
        <span class="service-status">
          <ProviderBadge status={results.cookiecloud} />
          <button class="btn btn-small" type="button" disabled={testing !== null} onClick={() => void test("cookiecloud")}>
            {testing === "cookiecloud" ? "检测中…" : "检测"}
          </button>
          <button
            class="btn btn-small"
            type="button"
            disabled={busy || !cc?.configured}
            onClick={() =>
              void run(
                () => api<{ message: string }>("/api/sites/sync-cookiecloud", { method: "POST", timeoutMs: 60000 }),
                (result) => (result as { message?: string }).message || "已同步站点 Cookie",
                () => {
                  void status.reload();
                  onSynced();
                },
              )
            }
          >
            立即同步
          </button>
        </span>
      </div>
      {!cc ? (
        <p class="muted settings-note">正在读取 CookieCloud 状态……</p>
      ) : !cc.configured ? (
        <p class="notice">尚未配置用户 KEY 与端对端加密密码，站点 Cookie 只能手动填写。</p>
      ) : (
        <ul class="cookiecloud-facts">
          <li>
            <span class="muted">同步方式</span>
            {modes.length ? modes.join("；") : "已配置，但还没有拉取地址，也没有收到插件推送"}
          </li>
          <li>
            <span class="muted">最近同步</span>
            {summary}
          </li>
          {last?.missing.length ? (
            <li>
              <span class="muted">不在 CookieCloud 中</span>
              {last.missing.join("、")} —— 这些站点的 Cookie 只能在站点里手动更新，过期后不会自动恢复
            </li>
          ) : null}
          <li class="muted">Cookie 有变化的站点会自动重新检测。检测仍提示“Cookie 已失效”时，说明浏览器里的登录也已过期，需要在 Chrome 重新登录该站点后再同步。</li>
        </ul>
      )}

      <details class="cookiecloud-config">
        <summary>连接设置</summary>
        <p class="muted settings-note">
          两种方式任选其一：填写与 Chrome 插件相同的服务器地址，AutoList 每小时拉取并解密；或留空服务器地址，把插件的服务器地址改为下面的推送地址。两种方式都需要相同的用户 KEY 与端对端加密密码。
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void run(save, "CookieCloud 设置已保存", () => status.reload());
          }}
        >
          <div class="field-grid">
            <TextField
              label="服务器地址（拉取）"
              value={text("cookiecloud_url")}
              onInput={(value) => set("cookiecloud_url", value)}
              placeholder="http://192.168.x.x:3000/cookiecloud"
              inputMode="url"
              hint="留空保存即保留当前地址"
            />
            {configured("cookiecloud_url") ? (
              <span class="field clear-action">
                <button class="btn btn-small" type="button" aria-pressed={cleared.includes("cookiecloud_url")} onClick={() => toggleClear("cookiecloud_url")}>
                  {cleared.includes("cookiecloud_url") ? "撤销清除" : "清除服务器地址"}
                </button>
                {cleared.includes("cookiecloud_url") ? <small class="field-hint">保存后删除已保存的地址</small> : null}
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
            <label class="field">
              <span>AutoList 推送地址（推送方式）</span>
              <span class="secret-wrap">
                <input type="url" readOnly value={endpoint} onFocus={(event) => (event.target as HTMLInputElement).select()} />
                <button class="btn btn-small" type="button" onClick={() => void copyEndpoint()}>
                  复制
                </button>
              </span>
            </label>
          </div>
          <div class="settings-actions">
            <button class="btn btn-primary" type="submit" disabled={busy || !dirty}>
              {busy ? "保存中…" : "保存 CookieCloud 设置"}
            </button>
            {dirty ? <span class="muted">有未保存的修改</span> : null}
          </div>
        </form>
      </details>
    </section>
  );
}
