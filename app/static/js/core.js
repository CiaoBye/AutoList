/** Shared browser helpers for AutoList UI. */
export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

export const safeExternalUrl = (value) => {
  try {
    const url = new URL(String(value ?? ""));
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
};

const ACCESS_TOKEN_STORAGE_KEY = "autolist-access-token";

export const getAccessToken = () => {
  try {
    return String(localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) || "").trim();
  } catch {
    return "";
  }
};

export const setAccessToken = (token) => {
  const value = String(token || "").trim();
  try {
    if (value) localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, value);
    else localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch {
    /* private mode */
  }
};

export const promptAccessToken = (message) => {
  const next = window.prompt(message || "服务已启用访问令牌，请输入 AUTOLIST_ACCESS_TOKEN", getAccessToken());
  if (next === null) return null;
  setAccessToken(next);
  return getAccessToken();
};

const DEFAULT_API_TIMEOUT_MS = 30000;

export const api = async (path, options = {}, allowRetry = true) => {
  const method = String(options.method || "GET").toUpperCase();
  const timeoutMs = Number.isFinite(Number(options.timeoutMs))
    ? Math.max(1000, Number(options.timeoutMs))
    : DEFAULT_API_TIMEOUT_MS;
  const callerSignal = options.signal;
  const controller = new AbortController();
  let timedOut = false;
  let callerAbortHandler = null;
  const requestOptions = {...options};
  delete requestOptions.timeoutMs;
  delete requestOptions.signal;
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  const token = getAccessToken();
  if (token) headers["X-AutoList-Token"] = token;
  if (callerSignal) {
    callerAbortHandler = () => controller.abort(callerSignal.reason);
    if (callerSignal.aborted) callerAbortHandler();
    else callerSignal.addEventListener("abort", callerAbortHandler, {once: true});
  }
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  let response;
  let text;
  try {
    response = await fetch(path, {
      cache: method === "GET" ? "no-store" : undefined,
      ...requestOptions,
      headers,
      signal: controller.signal,
    });
    text = await response.text();
  } catch (error) {
    if (timedOut) {
      const timeoutError = new Error(`请求超时（${Math.ceil(timeoutMs / 1000)} 秒），请稍后重试`);
      timeoutError.name = "TimeoutError";
      timeoutError.code = "TIMEOUT";
      throw timeoutError;
    }
    if (error?.name === "AbortError") {
      const abortError = new Error("请求已取消");
      abortError.name = "AbortError";
      throw abortError;
    }
    const networkError = new Error("网络连接失败，请检查服务状态后重试");
    networkError.cause = error;
    throw networkError;
  } finally {
    window.clearTimeout(timer);
    if (callerSignal && callerAbortHandler) callerSignal.removeEventListener("abort", callerAbortHandler);
  }
  let data = {};
  if (text) {
    try { data = JSON.parse(text); }
    catch { data = {detail: response.ok ? "服务返回了无法识别的数据" : `请求失败（${response.status}）`}; }
  }
  if (response.status === 401 && allowRetry && path.startsWith("/api/") && path !== "/api/health") {
    const entered = promptAccessToken(data.detail || "需要有效的访问令牌");
    if (entered) return api(path, options, false);
  }
  if (!response.ok) {
    // pydantic 校验错误 detail 是数组，提取可读信息避免显示 [object Object]。
    let detail = data.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => (item && item.msg) || String(item)).join("；");
    }
    throw new Error(detail || data.message || `请求失败（${response.status}）`);
  }
  return data;
};

export const formatSize = (bytes) => {
  if (!bytes) return "0 GB";
  const gb = bytes / 1024 / 1024 / 1024;
  return `${gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)} GB`;
};

export const formatTime = (value) => {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}).format(parsed);
};

let toastTimer = null;
export const showToast = (message) => {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
};

export const setButtonLoading = (button, loading, text) => {
  if (!button) return;
  if (button.dataset.labelHtml == null) button.dataset.labelHtml = button.innerHTML;
  if (button.dataset.labelAria == null && button.hasAttribute("aria-label")) button.dataset.labelAria = button.getAttribute("aria-label");
  button.disabled = loading;
  button.setAttribute("aria-busy", String(loading));
  if (loading) {
    button.textContent = text || "处理中…";
    if (button.dataset.labelAria != null) button.setAttribute("aria-label", text || "处理中…");
  } else {
    button.innerHTML = button.dataset.labelHtml;
    if (button.dataset.labelAria != null) button.setAttribute("aria-label", button.dataset.labelAria);
  }
};

export const fileToBase64 = async (file) => {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
};

export const taskPillClass = (status) => {
  if (["queued", "running"].includes(status)) return "pill-running";
  if (["completed", "partial"].includes(status)) return "pill-success";
  if (["failed", "cancelled", "interrupted"].includes(status)) return "pill-error";
  return "pill-neutral";
};

export const lifecycleStatusClass = (status) => ({
  organized: "organized",
  downloading: "downloading",
  pending_library: "pending",
  pending_confirmation: "pending",
  submitted: "submitted",
  failed: "failed",
}[status] || "pending");

export const lifecycleStatusIcon = (status) => ({
  organized: "✓",
  downloading: "↓",
  pending_library: "◌",
  pending_confirmation: "?",
  submitted: "→",
  failed: "×",
}[status] || "?");

export const HISTORY_STATUS_OPTIONS = [
  {value: "all", label: "全部状态"},
  {value: "submitted", label: "已提交"},
  {value: "downloading", label: "下载中"},
  {value: "pending_library", label: "待入库"},
  {value: "pending_confirmation", label: "待确认"},
  {value: "organized", label: "已整理/已入库"},
  {value: "failed", label: "失败"},
];

export function historyRowHtml(item) {
  const status = item.lifecycle_status || (item.success ? "submitted" : "failed");
  const label = item.status_label || (item.success ? "已提交" : "失败");
  const detail = item.status_reason || item.message;
  return `<tr data-history-status="${escapeHtml(status)}"><td><span class="history-status ${lifecycleStatusClass(status)}" aria-label="${escapeHtml(label)}">${lifecycleStatusIcon(status)}</span><small class="history-status-label">${escapeHtml(label)}</small></td><td class="history-title"><strong>${escapeHtml(item.title)}</strong><small title="${escapeHtml(item.torrent_name)}">${escapeHtml(item.torrent_name)}</small>${detail ? `<small class="history-message ${status === "failed" ? "failed" : ""}">${escapeHtml(detail)}</small>` : ""}</td><td>${escapeHtml(item.site_name || "—")}</td><td>${formatTime(item.created_at)}</td></tr>`;
}

export function filterHistoryItems(list, statusFilter) {
  if (!statusFilter || statusFilter === "all") return list;
  return list.filter((item) => {
    const status = item.lifecycle_status || (item.success ? "submitted" : "failed");
    return status === statusFilter;
  });
}

export function candidateEmptyState({candidateCache, activeTaskState, currentFilter}) {
  const taskStatus = activeTaskState?.status;
  const hasFailures = Number(activeTaskState?.attempt_summary?.failed || 0) > 0;
  const recoverable = ["partial", "failed", "interrupted", "cancelled"].includes(taskStatus);
  const running = ["queued", "running"].includes(taskStatus);
  const completedClean = taskStatus === "completed" || (taskStatus === "partial" && !hasFailures);
  let heading, copy;
  if (candidateCache.length) {
    heading = "没有符合筛选的候选";
    copy = currentFilter === "excluded" ? "当前没有被策略排除的资源。" : "切换筛选条件查看其他候选，或调整候选规则后再搜索。";
  } else if (running) {
    heading = "正在搜索";
    copy = "候选会在各站点返回后实时出现，请稍候。";
  } else if (taskStatus === "partial" && hasFailures) {
    heading = "部分站点搜索失败";
    copy = "已有站点失败且暂无可用候选。可以只重试失败站点，或重新搜索整项。";
  } else if (["failed", "interrupted", "cancelled"].includes(taskStatus)) {
    heading = "搜索任务未完成";
    copy = "没有可用候选。重新搜索整项以获得新的结果。";
  } else if (completedClean) {
    heading = "无可下载候选";
    copy = "搜索已结束，但没有通过候选规则的资源。可放宽规则、切换站点后重新搜索整项。";
  } else {
    heading = "等待搜索结果";
    copy = "选择片单和搜索范围后开始搜索，候选会在这里出现。";
  }
  const actions = recoverable
    ? `<div class="empty-state-actions">${hasFailures ? '<button class="button button-secondary" data-task-action="retry">重试失败站点</button>' : ""}<button class="button button-primary" data-task-action="restart">重新搜索整项</button></div>`
    : (completedClean ? `<div class="empty-state-actions"><button class="button button-secondary" data-task-action="restart">重新搜索整项</button><button class="button button-secondary" data-route-target="rules">调整候选规则</button></div>` : "");
  return {heading, copy, actions};
}
