/**
 * 设置表单 → PUT /api/settings 请求体。
 *
 * 纯函数、无依赖：测试会用 Node 直接执行本文件，校验真实的组装逻辑。
 * 规则与服务端约定一致：
 * - 密钥输入框留空表示保留已保存的值（发送 null，服务端忽略）；
 * - 需要清除时发送 ``clear_<字段>: true``，且只在对应输入框为空时生效；
 * - 每个分区只发送自己的字段，服务端按“只更新已发送字段”合并。
 */

export const SECRET_FIELDS = [
  "mp_api_key",
  "emby_api_key",
  "tmdb_api_key",
  "fanart_api_key",
  "mdblist_api_key",
  "cookiecloud_key",
  "cookiecloud_password",
  "ai_api_key",
  "tr_password",
] as const;

/** 留空即“不修改”的普通字段（服务端不回传明文的用户名，以及地址类可选字段）。 */
export const KEEP_WHEN_BLANK_FIELDS = ["tr_username", "cookiecloud_url", "outbound_proxy_url"] as const;

export const TEXT_FIELDS = ["mp_base_url", "emby_base_url", "tmdb_language", "ai_base_url", "ai_model", "tr_base_url"] as const;

export const BOOLEAN_FIELDS = ["tmdb_proxy_enabled", "pt_proxy_enabled", "dashboard_random_posters"] as const;

/** 可以显式清除的字段（与服务端 RUNTIME_SETTING_CLEAR_FIELDS 对应）。 */
export const CLEARABLE_FIELDS = [
  "mp_api_key",
  "emby_api_key",
  "tmdb_api_key",
  "fanart_api_key",
  "mdblist_api_key",
  "cookiecloud_url",
  "cookiecloud_key",
  "cookiecloud_password",
  "outbound_proxy_url",
  "ai_api_key",
  "tr_username",
  "tr_password",
] as const;

export type SettingsForm = Record<string, string | boolean | number | null | undefined>;

export function buildSettingsPayload(
  form: SettingsForm,
  fields: readonly string[],
  cleared: readonly string[] = [],
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  const text = (name: string): string => String(form[name] ?? "").trim();
  for (const name of fields) {
    if ((SECRET_FIELDS as readonly string[]).includes(name) || (KEEP_WHEN_BLANK_FIELDS as readonly string[]).includes(name)) {
      const value = text(name);
      payload[name] = value || null;
      if (!value && cleared.includes(name) && (CLEARABLE_FIELDS as readonly string[]).includes(name)) {
        payload[`clear_${name}`] = true;
      }
    } else if ((BOOLEAN_FIELDS as readonly string[]).includes(name)) {
      payload[name] = Boolean(form[name]);
    } else if (name === "mp_timeout_seconds") {
      const value = Number(form[name]);
      payload[name] = Number.isFinite(value) && value > 0 ? value : 30;
    } else if (name === "tmdb_language") {
      payload[name] = text(name) || "zh-CN";
    } else {
      payload[name] = text(name);
    }
  }
  return payload;
}
