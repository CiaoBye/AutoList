import { href } from "../../router";
import { SectionHead, TextField, Toggle, useAction } from "./shared";
import { useSettingsForm } from "./useSettingsForm";

const FIELDS = ["outbound_proxy_url", "tmdb_proxy_enabled", "pt_proxy_enabled"];

export function Network() {
  const { settings, form, set, cleared, toggleClear, configured, dirty, save } = useSettingsForm(FIELDS);
  const { busy, run } = useAction();
  const text = (name: string) => String(form[name] ?? "");
  // 地址类字段留空表示“保留”，要删除已保存的地址需显式清除。
  const clearAddress = (name: string, label: string) =>
    configured(name) ? (
      <span class="field clear-action">
        <button class="btn btn-small" type="button" aria-pressed={cleared.includes(name)} onClick={() => toggleClear(name)}>
          {cleared.includes(name) ? "撤销清除" : label}
        </button>
        {cleared.includes(name) ? <small class="field-hint">保存后删除已保存的地址</small> : null}
      </span>
    ) : null;

  if (settings.error && !settings.data) {
    return <div class="notice notice-bad" role="alert">设置读取失败：{settings.error.message}</div>;
  }
  if (!settings.data) return <p class="muted">正在读取设置……</p>;
  return (
    <form
      class="settings-form"
      onSubmit={(event) => {
        event.preventDefault();
        void run(save, "代理设置已保存");
      }}
    >
      <SectionHead title="代理">出站代理用于访问 TMDB、fanart.tv、片单来源或 PT 站点。</SectionHead>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-proxy.svg" alt="" width="22" height="22" />
          出站代理
        </legend>
        <div class="field-grid">
          <TextField
            label="代理地址"
            value={text("outbound_proxy_url")}
            onInput={(value) => set("outbound_proxy_url", value)}
            placeholder="http://192.168.x.x:7890"
            inputMode="url"
            hint="不支持在地址里写用户名或密码；留空保存即保留当前代理"
          />
          {clearAddress("outbound_proxy_url", "清除代理地址")}
        </div>
        <div class="toggle-list">
          <Toggle label="TMDB / Fanart 走代理" checked={Boolean(form.tmdb_proxy_enabled)} onChange={(value) => set("tmdb_proxy_enabled", value)} />
          <Toggle label="PT 站点允许走代理" hint="还需要在站点里单独开启代理" checked={Boolean(form.pt_proxy_enabled)} onChange={(value) => set("pt_proxy_enabled", value)} />
        </div>
      </fieldset>

      <p class="muted settings-note">
        CookieCloud 已移到 <a href={href("/settings/sites")}>站点</a> 页顶部，与站点 Cookie 放在一起。
      </p>

      <div class="settings-actions">
        <button class="btn btn-primary btn-large" type="submit" disabled={busy || !dirty}>
          {busy ? "保存中…" : "保存代理设置"}
        </button>
        {dirty ? <span class="muted">有未保存的修改</span> : null}
      </div>
    </form>
  );
}
