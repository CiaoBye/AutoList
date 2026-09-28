/** 与后端通信：附带访问令牌、超时与统一的中文错误信息。 */

// 本机访问令牌保存在 localStorage，键名沿用早期版本，升级后无需重新输入。
const TOKEN_KEY = "autolist-access-token";
const DEFAULT_TIMEOUT_MS = 30000;

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const getToken = (): string => {
  try {
    return (localStorage.getItem(TOKEN_KEY) || "").trim();
  } catch {
    return "";
  }
};

export const setToken = (value: string): void => {
  try {
    const token = value.trim();
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 浏览器禁用存储时只影响本次会话。
  }
};

/** 需要令牌时通知外壳切换到令牌页；只在本页面内广播，不携带令牌内容。 */
export const AUTH_EVENT = "autolist-auth-required";

const notifyAuthRequired = (message: string): void => {
  window.dispatchEvent(new CustomEvent<string>(AUTH_EVENT, { detail: message }));
};

const detailMessage = (detail: unknown, fallback: string): string => {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : ""))
      .filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  return fallback;
};

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const onAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onAbort, { once: true });
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["X-AutoList-Token"] = token;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      // 服务端要求非 GET 请求声明 Content-Length，空请求体也要显式发送。
      body: method === "GET" ? undefined : JSON.stringify(options.body ?? {}),
      signal: controller.signal,
    });
  } catch (error) {
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new ApiError("请求超时，请检查网络后重试", 0);
    }
    throw new ApiError(error instanceof Error ? error.message : "网络请求失败", 0);
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    if (response.status === 401) notifyAuthRequired("服务已启用访问令牌，请输入 AUTOLIST_ACCESS_TOKEN。");
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    const message = detailMessage(detail, `请求失败（HTTP ${response.status}）`);
    if (response.status === 503 && message.includes("访问令牌强度不足")) {
      notifyAuthRequired("服务端访问令牌强度不足，请管理员更换至少 32 个字符的随机令牌。");
    }
    throw new ApiError(message, response.status);
  }
  return data as T;
}
