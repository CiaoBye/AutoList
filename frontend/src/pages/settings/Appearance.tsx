import { useState } from "preact/hooks";
import { getToken, setToken } from "../../api";
import { useToast } from "../../hooks";
import { applyTheme, readTheme, type Theme } from "../../theme";
import { CardHead, DirtyNote, SectionHead, SwitchRow, useAction } from "./shared";
import { useSettingsForm } from "./useSettingsForm";

export const THEMES: { id: Theme; name: string }[] = [
  { id: "archive", name: "馆藏档案" },
  { id: "cinema", name: "午夜放映" },
];

export const STRENGTH_TEXT: Record<string, string> = {
  missing: "服务端未启用访问令牌：同一网络内任何人都能访问。只适合可信内网；映射到公网前务必设置 AUTOLIST_ACCESS_TOKEN。",
  weak: "服务端访问令牌强度不足，请更换为至少 32 个字符的随机令牌。",
  strong: "服务端已启用访问令牌。",
};

export function Appearance({ onThemeChange }: { onThemeChange: (theme: Theme) => void }) {
  const toast = useToast();
  const [theme, setTheme] = useState<Theme>(readTheme());
  const [token, setTokenDraft] = useState("");
  const [hasToken, setHasToken] = useState(Boolean(getToken()));
  const { settings, form, set, dirty, save, reset } = useSettingsForm(["dashboard_random_posters"]);
  const { busy, run } = useAction();

  const choose = (next: Theme) => {
    applyTheme(next, true);
    setTheme(next);
    onThemeChange(next);
  };

  return (
    <div class="settings-form">
      <SectionHead title="外观与访问" />

      <section class="card setting-card" aria-labelledby="appearance-theme">
        <CardHead id="appearance-theme" title="主题" />
        <div class="setting-card-body">
          <div class="theme-options" role="radiogroup" aria-labelledby="appearance-theme">
            {THEMES.map((item) => (
              <label key={item.id} class={`theme-option theme-option-${item.id}`}>
                <input type="radio" name="theme" checked={theme === item.id} onChange={() => choose(item.id)} />
                <span class="theme-swatch" aria-hidden="true" />
                <span>
                  <strong>{item.name}</strong>
                </span>
              </label>
            ))}
          </div>
        </div>
      </section>

      <form
        class="card setting-card"
        aria-labelledby="appearance-home"
        onSubmit={(event) => {
          event.preventDefault();
          void run(save, "首页设置已保存");
        }}
      >
        <CardHead id="appearance-home" title="藏馆首页" />
        {settings.data ? (
          <>
            <SwitchRow
              label="“最近入馆”每天随机展示"
              checked={Boolean(form.dashboard_random_posters)}
              onChange={(value) => set("dashboard_random_posters", value)}
            />
            <div class="setting-row setting-row-actions">
              <DirtyNote dirty={dirty} onReset={reset} />
              <button class="btn btn-primary" type="submit" disabled={busy || !dirty}>
                {busy ? "保存中…" : "保存首页设置"}
              </button>
            </div>
          </>
        ) : (
          <p class="muted setting-card-body">正在读取设置……</p>
        )}
      </form>

      {/* 服务端没有启用访问令牌（本地使用）时不显示这一块；本机存过令牌的仍显示，方便清除。 */}
      {settings.data?.access_token_strength !== "missing" || hasToken ? (
      <form
        class="card setting-card"
        aria-labelledby="appearance-token"
        onSubmit={(event) => {
          event.preventDefault();
          setToken(token);
          setHasToken(Boolean(token.trim()));
          setTokenDraft("");
          toast.show(token.trim() ? "已保存到本机浏览器" : "已清除本机令牌");
        }}
      >
        <CardHead id="appearance-token" title="访问令牌" />
        <div class="setting-card-body">
          <p class={`notice${settings.data?.access_token_strength === "strong" ? "" : " notice-bad"}`}>
            {settings.data ? STRENGTH_TEXT[settings.data.access_token_strength] : "正在读取……"}
          </p>
        </div>
        <div class="setting-row setting-row-field">
          <label class="setting-row-text" for="local-token">
            <strong>本机令牌</strong>
            <small>{hasToken ? "已保存在当前浏览器；输入新值替换" : "与服务端 AUTOLIST_ACCESS_TOKEN 一致，仅保存在当前浏览器"}</small>
          </label>
          <span class="setting-row-control">
            <input
              id="local-token"
              type="password"
              autocomplete="current-password"
              value={token}
              placeholder={hasToken ? "已保存" : "未设置"}
              onInput={(event) => setTokenDraft((event.target as HTMLInputElement).value)}
            />
            <button class="btn btn-small" type="submit" disabled={!token.trim()}>
              保存到本机
            </button>
            {hasToken ? (
              <button
                class="btn btn-small"
                type="button"
                onClick={() => {
                  setToken("");
                  setHasToken(false);
                  toast.show("已清除本机令牌");
                }}
              >
                清除
              </button>
            ) : null}
          </span>
        </div>
      </form>
      ) : null}
    </div>
  );
}
