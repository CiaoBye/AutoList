import { SectionHead, SecretField, TextField, ProviderBadge, useAction, useProviderTest } from "./shared";
import { useSettingsForm } from "./useSettingsForm";

const FIELDS = [
  "mp_base_url", "mp_api_key", "mp_timeout_seconds",
  "tmdb_api_key", "tmdb_language", "mdblist_api_key", "fanart_api_key",
  "emby_base_url", "emby_api_key",
  "tr_base_url", "tr_username", "tr_password",
  "ai_base_url", "ai_api_key", "ai_model",
];

export function Services() {
  const { settings, form, set, cleared, toggleClear, configured, dirty, save } = useSettingsForm(FIELDS);
  const { results, testing, test } = useProviderTest();
  const { busy, run } = useAction();
  const text = (name: string) => String(form[name] ?? "");
  const secret = (name: string, label: string, hint?: string) => (
    <SecretField
      label={label}
      value={text(name)}
      onInput={(value) => set(name, value)}
      configured={configured(name)}
      cleared={cleared.includes(name)}
      onToggleClear={() => toggleClear(name)}
      hint={hint}
    />
  );
  const testButton = (provider: string, label: string) => (
    <span class="service-status">
      <ProviderBadge status={results[provider]} />
      <button class="btn btn-small" type="button" disabled={testing !== null} onClick={() => void test(provider)}>
        {testing === provider ? "检测中…" : `检测 ${label}`}
      </button>
    </span>
  );

  if (settings.error && !settings.data) {
    return <div class="notice notice-bad" role="alert">设置读取失败：{settings.error.message}</div>;
  }
  if (!settings.data) return <p class="muted">正在读取设置……</p>;

  return (
    <form
      class="settings-form"
      onSubmit={(event) => {
        event.preventDefault();
        void run(save, "设置已保存", () => test());
      }}
    >
      <SectionHead
        title="服务连接"
        actions={
          <button class="btn" type="button" disabled={testing !== null} onClick={() => void test()}>
            {testing === "all" ? "检测中…" : "检测全部连接"}
          </button>
        }
      >
        检测使用已保存的配置；修改后请先保存。密钥只保存在服务端，界面只显示是否已设置。
      </SectionHead>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-moviepilot.svg" alt="" width="22" height="22" />
          MoviePilot
          {testButton("moviepilot", "MoviePilot")}
        </legend>
        <p class="muted settings-note">负责按分类规则把选定资源提交到 Transmission，是提交下载的必需服务。</p>
        <div class="field-grid">
          <TextField label="地址" value={text("mp_base_url")} onInput={(value) => set("mp_base_url", value)} placeholder="http://192.168.x.x:3000" inputMode="url" />
          {secret("mp_api_key", "API Key")}
          <TextField label="请求超时（秒）" type="number" value={text("mp_timeout_seconds")} onInput={(value) => set("mp_timeout_seconds", value)} hint="同时用于 TMDB、Emby、Transmission 请求，3–300 秒" />
        </div>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-tmdb.svg" alt="" width="22" height="22" />
          TMDB
          {testButton("tmdb", "TMDB")}
        </legend>
        <p class="muted settings-note">识别影片、生成搜索关键词与海报。支持 v3 API Key 或 v4 读取令牌。</p>
        <div class="field-grid">
          {secret("tmdb_api_key", "API Key / 读取令牌")}
          <TextField label="语言" value={text("tmdb_language")} onInput={(value) => set("tmdb_language", value)} placeholder="zh-CN" />
          {secret("mdblist_api_key", "MDBList API Key（可选）", "公开的 MDBList 片单无需填写")}
        </div>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-fanart.svg" alt="" width="22" height="22" />
          Fanart.tv
          {testButton("fanart", "Fanart")}
        </legend>
        <p class="muted settings-note">
          配置后海报优先取自 fanart.tv：按 TMDB 语言、英文、无字版的顺序挑选，同语言取点赞最多的一张；fanart.tv 没有的影片仍用 Emby 或 TMDB 海报。与 TMDB 共用代理开关。
        </p>
        <div class="field-grid">
          {secret("fanart_api_key", "Project API Key", "在 fanart.tv 登录后申请")}
        </div>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-emby.svg" alt="" width="22" height="22" />
          Emby
          {testButton("emby", "Emby")}
        </legend>
        <p class="muted settings-note">判断影片是否已入馆：只有实体媒体文件算入馆，.strm 视为未完成。</p>
        <div class="field-grid">
          <TextField label="地址" value={text("emby_base_url")} onInput={(value) => set("emby_base_url", value)} placeholder="http://192.168.x.x:8096" inputMode="url" />
          {secret("emby_api_key", "API Key")}
        </div>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-transmission.svg" alt="" width="22" height="22" />
          Transmission
          {testButton("transmission", "Transmission")}
        </legend>
        <p class="muted settings-note">用于确认下载进度与避免重复下载；下载本身经 MoviePilot 提交。</p>
        <div class="field-grid">
          <TextField label="地址" value={text("tr_base_url")} onInput={(value) => set("tr_base_url", value)} placeholder="http://192.168.x.x:9091" inputMode="url" />
          <SecretField
            label="用户名"
            value={text("tr_username")}
            onInput={(value) => set("tr_username", value)}
            configured={configured("tr_username")}
            cleared={cleared.includes("tr_username")}
            onToggleClear={() => toggleClear("tr_username")}
          />
          {secret("tr_password", "密码")}
        </div>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">
          <img src="/assets/service-ai.svg" alt="" width="22" height="22" />
          AI 辅助识别（可选）
        </legend>
        <p class="muted settings-note">TMDB 找不到时，用兼容 OpenAI 接口的模型纠正片名后再识别一次。</p>
        <div class="field-grid">
          <TextField label="接口地址" value={text("ai_base_url")} onInput={(value) => set("ai_base_url", value)} placeholder="https://api.example.com/v1" inputMode="url" />
          {secret("ai_api_key", "API Key")}
          <TextField label="模型" value={text("ai_model")} onInput={(value) => set("ai_model", value)} />
        </div>
      </fieldset>

      <div class="settings-actions">
        <button class="btn btn-primary btn-large" type="submit" disabled={busy || !dirty}>
          {busy ? "保存中…" : "保存服务设置"}
        </button>
        {dirty ? <span class="muted">有未保存的修改</span> : null}
      </div>
    </form>
  );
}
