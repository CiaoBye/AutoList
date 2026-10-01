import { useState } from "preact/hooks";
import type { SettingsHealth } from "./health";
import { DirtyNote, SwitchRow, useAction } from "./shared";
import { useSettingsDraft } from "./useSettingsForm";

const FIELDS = ["outbound_proxy_url", "tmdb_proxy_enabled", "pt_proxy_enabled"];

/** 服务连接页底部的网络代理：和其他服务一样折叠成一行，点开编辑，单独保存。 */
export function ProxySettings({ health }: { health: SettingsHealth }) {
  const { form, set, cleared, toggleClear, configured, dirty, save, reset } = useSettingsDraft(FIELDS, health.settings);
  const { busy, run } = useAction();
  const [open, setOpen] = useState(false);
  const clearing = cleared.includes("outbound_proxy_url");
  const settings = health.settings.data;

  if (!settings) return null;
  const savedUrl = String(settings.outbound_proxy_url || "");
  const scopes = [settings.tmdb_proxy_enabled ? "TMDB / Fanart" : "", settings.pt_proxy_enabled ? "PT 站点" : ""].filter(Boolean);
  const active = Boolean(savedUrl) && scopes.length > 0;
  const summary = savedUrl ? `${savedUrl} · ${scopes.length ? `${scopes.join("、")}走代理` : "未启用任何范围"}` : "未设置代理地址";
  return (
    <div class="card service-list">
      <div class={`service-row${open ? " is-open" : ""}`}>
        <button class="service-row-head" type="button" aria-expanded={open} aria-controls="proxy-panel" onClick={() => setOpen(!open)}>
          <span class="service-row-name">
            <span class={`dot ${active ? "dot-ok" : "dot-off"}`} aria-hidden="true" />
            <img src="/assets/service-proxy.svg" alt="" width="20" height="20" />
            网络代理
          </span>
          <span class="service-row-summary">{summary}</span>
          {dirty ? (
            <span class="badge st-candidates">未保存</span>
          ) : active ? (
            <span class="badge st-in_library">已启用</span>
          ) : (
            <span class="badge st-missing">未启用 · 可选</span>
          )}
          <span class="service-row-toggle">{open ? "收起" : "编辑"}</span>
        </button>
        {open ? (
          <form
            id="proxy-panel"
            class="service-row-body proxy-body"
            onSubmit={(event) => {
              event.preventDefault();
              void run(save, "代理设置已保存", () => setOpen(false));
            }}
          >
            <p class="muted settings-note">出站代理用于访问 TMDB、fanart.tv、片单来源或 PT 站点。</p>
            <div class="setting-row setting-row-field">
              <label class="setting-row-text" for="proxy-url">
                <strong>代理地址</strong>
                <small>{clearing ? "保存后删除已保存的地址" : "不支持在地址里写用户名或密码；留空保存即保留当前代理"}</small>
              </label>
              <span class="setting-row-control">
                <input
                  id="proxy-url"
                  type="url"
                  inputMode="url"
                  autocomplete="off"
                  value={String(form.outbound_proxy_url ?? "")}
                  placeholder="http://192.168.x.x:7890"
                  onInput={(event) => set("outbound_proxy_url", (event.target as HTMLInputElement).value)}
                />
                {configured("outbound_proxy_url") ? (
                  <button class="btn btn-small" type="button" aria-pressed={clearing} onClick={() => toggleClear("outbound_proxy_url")}>
                    {clearing ? "撤销清除" : "清除"}
                  </button>
                ) : null}
              </span>
            </div>
            <SwitchRow
              label="TMDB / Fanart 走代理"
              hint="TMDB、fanart.tv 与片单来源（TMDB / MDBList / Letterboxd / IMDb）的请求经代理"
              checked={Boolean(form.tmdb_proxy_enabled)}
              onChange={(value) => set("tmdb_proxy_enabled", value)}
            />
            <SwitchRow
              label="PT 站点允许走代理"
              hint="开启后，站点里勾选“经代理访问”的站点才会走代理"
              checked={Boolean(form.pt_proxy_enabled)}
              onChange={(value) => set("pt_proxy_enabled", value)}
            />
            <div class="service-row-actions">
              <DirtyNote dirty={dirty} onReset={reset} />
              <button class="btn btn-primary" type="submit" disabled={busy || !dirty}>
                {busy ? "保存中…" : "保存网络代理"}
              </button>
            </div>
          </form>
        ) : null}
      </div>
    </div>
  );
}
