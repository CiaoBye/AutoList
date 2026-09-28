import { useState } from "preact/hooks";
import { getToken, setToken } from "../../api";
import { useToast } from "../../hooks";
import { applyTheme, readTheme, type Theme } from "../../theme";
import { SectionHead, Toggle, useAction } from "./shared";
import { useSettingsForm } from "./useSettingsForm";

const THEMES: { id: Theme; name: string; note: string }[] = [
  { id: "archive", name: "馆藏档案", note: "纸张底色，适合白天与长时间管理" },
  { id: "cinema", name: "午夜放映", note: "深色，适合低亮度环境" },
];

const STRENGTH_TEXT: Record<string, string> = {
  missing: "服务端未启用访问令牌：同一网络内任何人都能访问。只适合可信内网；映射到公网前务必设置 AUTOLIST_ACCESS_TOKEN。",
  weak: "服务端访问令牌强度不足，请更换为至少 32 个字符的随机令牌。",
  strong: "服务端已启用访问令牌。",
};

export function Appearance({ onThemeChange }: { onThemeChange: (theme: Theme) => void }) {
  const toast = useToast();
  const [theme, setTheme] = useState<Theme>(readTheme());
  const [token, setTokenDraft] = useState("");
  const [hasToken, setHasToken] = useState(Boolean(getToken()));
  const { settings, form, set, dirty, save } = useSettingsForm(["dashboard_random_posters"]);
  const { busy, run } = useAction();

  const choose = (next: Theme) => {
    applyTheme(next, true);
    setTheme(next);
    onThemeChange(next);
  };

  return (
    <div class="settings-form">
      <SectionHead title="外观与访问" />

      <fieldset class="card settings-group">
        <legend class="settings-legend">主题</legend>
        <div class="theme-options" role="radiogroup" aria-label="主题">
          {THEMES.map((item) => (
            <label key={item.id} class={`theme-option theme-option-${item.id}`}>
              <input type="radio" name="theme" checked={theme === item.id} onChange={() => choose(item.id)} />
              <span class="theme-swatch" aria-hidden="true" />
              <span>
                <strong>{item.name}</strong>
                <small>{item.note}</small>
              </span>
            </label>
          ))}
        </div>
        <p class="muted settings-note">主题只保存在当前浏览器。动效会跟随系统的“减少动态效果”设置。</p>
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">首页</legend>
        {settings.data ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void run(save, "首页设置已保存");
            }}
          >
            <Toggle
              label="“最近入馆”每天随机展示"
              hint="关闭时按最近确认入馆的时间排列"
              checked={Boolean(form.dashboard_random_posters)}
              onChange={(value) => set("dashboard_random_posters", value)}
            />
            <div class="settings-actions">
              <button class="btn" type="submit" disabled={busy || !dirty}>
                保存
              </button>
            </div>
          </form>
        ) : (
          <p class="muted">正在读取设置……</p>
        )}
      </fieldset>

      <fieldset class="card settings-group">
        <legend class="settings-legend">访问令牌</legend>
        <p class={`notice${settings.data?.access_token_strength === "strong" ? "" : " notice-bad"}`}>
          {settings.data ? STRENGTH_TEXT[settings.data.access_token_strength] : "正在读取……"}
        </p>
        <form
          class="toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            setToken(token);
            setHasToken(Boolean(token.trim()));
            setTokenDraft("");
            toast.show(token.trim() ? "已保存到本机浏览器" : "已清除本机令牌");
          }}
        >
          <label class="field field-grow">
            <span>本机令牌（仅保存在当前浏览器）</span>
            <input
              type="password"
              autocomplete="current-password"
              value={token}
              placeholder={hasToken ? "已保存；输入新值替换" : "与 AUTOLIST_ACCESS_TOKEN 一致"}
              onInput={(event) => setTokenDraft((event.target as HTMLInputElement).value)}
            />
          </label>
          <button class="btn" type="submit" disabled={!token.trim()}>
            保存到本机
          </button>
          {hasToken ? (
            <button
              class="btn"
              type="button"
              onClick={() => {
                setToken("");
                setHasToken(false);
                toast.show("已清除本机令牌");
              }}
            >
              清除本机令牌
            </button>
          ) : null}
        </form>
      </fieldset>
    </div>
  );
}
