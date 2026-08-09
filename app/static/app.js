import {
  $, $$, api, escapeHtml, safeExternalUrl, getAccessToken, setAccessToken,
  formatSize, formatTime, showToast, setButtonLoading, fileToBase64,
  taskPillClass, lifecycleStatusClass, lifecycleStatusIcon,
  HISTORY_STATUS_OPTIONS, historyRowHtml, filterHistoryItems, candidateEmptyState,
} from "./js/core.js";

let activeTask = null;
let candidateCache = [];
let cartCache = [];
let currentFilter = "all";
let candidatePolicyCache = null;
let currentPage = "dashboard";
let taskTimer = null;
let libraryScanTimer = null;
let libraryScanFailures = 0;
let libraryScanTaskId = null;
let recognitionTimer = null;
let recognitionPollFailures = 0;
let recognitionTaskId = null;
let activeTaskState = null;
let booting = true;
let selectedPlaylistId = null;
let expandedPlaylistId = null;
let playlistItemsCache = [];
let playlistPage = 1;
function safeStorageGet(key) {
  try { return window.localStorage.getItem(key); } catch (_error) { return null; }
}
function safeStorageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (_error) { /* 浏览器禁用存储时保留当前会话值 */ }
}
function safeSessionStorageGet(key) {
  try { return window.sessionStorage.getItem(key); } catch (_error) { return null; }
}
function safeSessionStorageSet(key, value) {
  try { window.sessionStorage.setItem(key, value); } catch (_error) { /* 浏览器禁用会话存储时保留内存值 */ }
}

function setProgressValue(selector, value) {
  const percent = Math.max(0, Math.min(100, Number(value) || 0));
  const bar = $(selector);
  if (!bar) return percent;
  bar.style.width = `${percent}%`;
  bar.setAttribute("aria-valuenow", String(percent));
  return percent;
}

function setTaskPollError(message) {
  const box = $("#task-poll-error");
  const text = $("#task-poll-error-text");
  if (!box || !text) return;
  text.textContent = message;
  box.hidden = false;
}

function clearTaskPollError() {
  const box = $("#task-poll-error");
  if (box) box.hidden = true;
}

function setPlaylistPollError(message, retry) {
  const box = $("#playlist-task-error");
  const text = $("#playlist-task-error-text");
  const button = $("#retry-playlist-task");
  if (!box || !text) return;
  playlistPollRetry = typeof retry === "function" ? retry : null;
  text.textContent = message;
  if (button) button.hidden = !playlistPollRetry;
  box.hidden = false;
}

function clearPlaylistPollError() {
  const box = $("#playlist-task-error");
  if (box) box.hidden = true;
  playlistPollRetry = null;
}

const storedPlaylistPageSize = safeStorageGet("autolist-playlist-page-size");
let playlistPageSize = ["20", "50", "100", "200"].includes(storedPlaylistPageSize) ? storedPlaylistPageSize : "50";
let playlistItemTotal = 0;
let playlistPageCount = 1;
let playlistQueryTimer = null;
let playlistLibraryFilter = "all";
let playlistCache = [];
let currentSearchPlaylistId = null;
let searchQueueCache = null;
let siteCache = [];
let selectedSiteId = null;
let siteFilter = "all";
let importMode = "url";
let candidateRenderSignature = "";
let playlistPollRetry = null;
const routeScrollPositions = new Map();
let pendingHashNavigation = false;
if (window.history && "scrollRestoration" in window.history) window.history.scrollRestoration = "manual";

const dialogReturnFocus = new WeakMap();
function focusDialogControl(dialog) {
  const target = dialog.querySelector("input:not([type='hidden']), select, textarea, button:not([value='cancel'])");
  if (target && document.activeElement !== target) target.focus();
}
function showDialog(dialog, trigger = document.activeElement) {
  if (!dialog) return;
  dialogReturnFocus.set(dialog, trigger instanceof HTMLElement ? trigger : null);
  dialog.showModal();
  requestAnimationFrame(() => focusDialogControl(dialog));
  setTimeout(() => focusDialogControl(dialog), 0);
}
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("close", () => {
    const target = dialogReturnFocus.get(dialog);
    dialogReturnFocus.delete(dialog);
    if (target?.isConnected && !target.disabled) requestAnimationFrame(() => target.focus({preventScroll: true}));
  });
});

const routeScrollStorageKey = (page) => `autolist-route-scroll:${page}`;
function getHistoryRouteScroll(page) {
  const value = window.history?.state?.autolistRouteScroll?.[page];
  const scrollTop = Number(value);
  return Number.isFinite(scrollTop) ? scrollTop : null;
}
function getRouteScroll(page) {
  const memoryValue = routeScrollPositions.get(page);
  if (Number.isFinite(memoryValue)) return memoryValue;
  const historyValue = getHistoryRouteScroll(page);
  if (Number.isFinite(historyValue)) {
    routeScrollPositions.set(page, historyValue);
    return historyValue;
  }
  const storedValue = Number(safeSessionStorageGet(routeScrollStorageKey(page)));
  if (!Number.isFinite(storedValue)) return null;
  routeScrollPositions.set(page, storedValue);
  return storedValue;
}
function rememberRouteScroll(page, value = window.scrollY) {
  const scrollTop = Number(value);
  if (!Number.isFinite(scrollTop)) return;
  routeScrollPositions.set(page, scrollTop);
  try {
    if (window.history?.replaceState) {
      const state = window.history.state && typeof window.history.state === "object" ? {...window.history.state} : {};
      const positions = state.autolistRouteScroll && typeof state.autolistRouteScroll === "object" ? {...state.autolistRouteScroll} : {};
      positions[page] = scrollTop;
      state.autolistRouteScroll = positions;
      window.history.replaceState(state, "", window.location.href);
    }
  } catch (_error) { /* 浏览器不允许修改历史状态时保留内存与会话值 */ }
  safeSessionStorageSet(routeScrollStorageKey(page), String(scrollTop));
}

const themeController = window.AutoListTheme;

function themeDetails(theme) {
  return themeController?.themes?.[theme] || {
    label: "馆藏档案",
    shortLabel: "档案",
    description: "纸张、档案卡与深青色",
  };
}

function syncThemeControls(theme = themeController?.getTheme?.() || document.documentElement.dataset.theme || "archive") {
  const details = themeDetails(theme);
  $$('[data-theme-option]').forEach((option) => {
    const active = option.dataset.themeOption === theme;
    option.setAttribute("aria-pressed", String(active));
  });
  const label = $("#theme-menu-label");
  if (label) label.textContent = `主题 · ${details.shortLabel || details.label}`;
  const status = $("#settings-theme-status");
  if (status) status.textContent = details.label;
  const sceneToggle = $("#settings-scene-mode");
  if (sceneToggle) sceneToggle.checked = themeController?.getSceneMode?.() ?? document.documentElement.dataset.scene !== "off";
  const motionToggle = $("#settings-reduced-motion");
  if (motionToggle) motionToggle.checked = themeController?.getReducedMotion?.() ?? document.documentElement.dataset.motion === "reduced";
  if (document.body) document.body.dataset.scene = String(theme) + "-" + currentPage;
  const meta = resolvePageMeta(currentPage, theme);
  if ($("#page-eyebrow")) $("#page-eyebrow").textContent = meta.eyebrow;
  if ($("#page-title")) $("#page-title").textContent = meta.title;
  if ($("#page-lead")) { $("#page-lead").textContent = meta.lead; $("#page-lead").hidden = !meta.lead; }
  document.title = meta.title + " · AutoList";
  const dashboardLabels = {
    archive: {page: "电影藏馆首页", feature: "当前馆藏片单", taskProgress: "最近检索进度", libraryProgress: "馆藏完成度"},
    cinema: {page: "放映台首页", feature: "当前放映片单", taskProgress: "最近排片进度", libraryProgress: "放映准备度"},
    ledger: {page: "总册索引首页", feature: "当前登记片单", taskProgress: "最近登记进度", libraryProgress: "总册登记进度"},
  }[theme] || {};
  $("#dashboard-page")?.setAttribute("aria-label", dashboardLabels.page || "电影藏馆首页");
  $(".screening-feature")?.setAttribute("aria-label", dashboardLabels.feature || "当前主片单");
  $(".dashboard-progress")?.setAttribute("aria-label", dashboardLabels.taskProgress || "最近搜索进度");
  $(".collection-progress-bar")?.setAttribute("aria-label", dashboardLabels.libraryProgress || "馆藏完成度");
  const siteLinearList = $("#sites-list .site-linear-list");
  if (siteLinearList) siteLinearList.open = theme === "ledger";
  syncSiteMapCopy(theme);
  syncSiteMapOrientationControls();
  return theme;
}

function applyTheme(theme = themeController?.getTheme?.() || "archive") {
  const nextTheme = themeController?.apply
    ? themeController.apply(theme, {persist: false, announce: false})
    : (document.documentElement.dataset.theme = theme);
  syncThemeControls(nextTheme);
  return nextTheme;
}

function selectTheme(theme) {
  const nextTheme = themeController?.apply
    ? themeController.apply(theme)
    : applyTheme(theme);
  syncThemeControls(nextTheme);
  const menu = $("#theme-menu");
  if (menu) menu.open = false;
  showToast(`已切换到「${themeDetails(nextTheme).label}」主题`);
}

function setSceneMode(enabled) {
  if (themeController?.setSceneMode) themeController.setSceneMode(enabled);
  else document.documentElement.dataset.scene = enabled ? "on" : "off";
  syncThemeControls();
}

function setReducedMotion(enabled) {
  if (themeController?.setReducedMotion) themeController.setReducedMotion(enabled);
  else document.documentElement.dataset.motion = enabled ? "reduced" : "standard";
  syncThemeControls();
}

window.addEventListener("autolist-theme-change", (event) => syncThemeControls(event.detail?.theme));
window.addEventListener("autolist-scene-change", () => syncThemeControls());
window.addEventListener("autolist-motion-change", () => syncThemeControls());

const pageMeta = {
  dashboard: {eyebrow: "FILM ARCHIVE / COLLECTION ROOM", title: "电影藏馆", lead: "", themes: {cinema: {eyebrow: "MIDNIGHT PROGRAM / SCREENING FLOOR", title: "放映台"}, ledger: {eyebrow: "CATALOGUE / REGISTER DESK", title: "总册索引"}}},
  playlists: {eyebrow: "CATALOGUE / SHELF", title: "馆藏片单", lead: "", themes: {cinema: {eyebrow: "SCREENING / REEL LIST", title: "场次片单"}, ledger: {eyebrow: "CATALOGUE / FILM INDEX", title: "片目目录"}}},
  search: {eyebrow: "SCREENING / SOURCE DESK", title: "选片台", lead: "", themes: {cinema: {eyebrow: "SCREENING / BOOKING DESK", title: "排片搜索"}, ledger: {eyebrow: "REGISTER / SOURCE INDEX", title: "来源索引"}}},
  cart: {eyebrow: "SCREENING QUEUE / HOLDING BAY", title: "放映队列", lead: "", themes: {cinema: {eyebrow: "SCREENING QUEUE / READY ROOM", title: "放映队列"}, ledger: {eyebrow: "REGISTER / PENDING SHEET", title: "待登记"}}},
  rules: {eyebrow: "CURATION / SELECTION NOTES", title: "选片标准", lead: "", themes: {cinema: {eyebrow: "SCREENING / HOUSE RULES", title: "放映规则"}, ledger: {eyebrow: "CATALOGUE / RULE BOOK", title: "规则簿"}}},
  sites: {eyebrow: "SOURCE ROOM / PT NETWORK", title: "来源网络", lead: "", themes: {cinema: {eyebrow: "SCREENING FLOOR / SOURCE STAGE", title: "来源场"}, ledger: {eyebrow: "CATALOGUE / SOURCE DIRECTORY", title: "来源目录"}}},
  history: {eyebrow: "ARCHIVE / INTAKE RECORD", title: "入馆记录", lead: "", themes: {cinema: {eyebrow: "SCREENING / SHOW HISTORY", title: "放映履历"}, ledger: {eyebrow: "REGISTER / ENTRY LEDGER", title: "入馆台账"}}},
  logs: {eyebrow: "PROJECTION LOG / EVENT REEL", title: "放映日志", lead: "", themes: {cinema: {eyebrow: "SCREENING FLOOR / CREW LOG", title: "场务日志"}, ledger: {eyebrow: "CATALOGUE / OPERATION LOG", title: "操作记录"}}},
};

function resolvePageMeta(page, theme = themeController?.getTheme?.() || document.documentElement.dataset.theme || "archive") {
  const base = pageMeta[page] || pageMeta.dashboard;
  return {...base, ...(base.themes?.[theme] || {})};
}

const taskLabels = {
  queued: "排队中",
  running: "搜索中",
  completed: "搜索完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

async function refreshPageData(page) {
  if (page === "dashboard") {
    await Promise.all([loadOverview(), loadConnection(), refreshCart(), refreshHistory()]);
  } else if (page === "playlists") {
    await loadPlaylists(true);
  } else if (page === "search") {
    await loadPlaylists();
    if (activeTask) await refreshCandidates(false);
  } else if (page === "cart") {
    await refreshCart();
  } else if (page === "rules") {
    await loadRules();
  } else if (page === "sites") {
    await loadSites();
  } else if (page === "history") {
    await refreshHistory();
  } else if (page === "logs") {
    await loadLogEvents();
  }
}

function navigate(page, updateHash = true, preserveScroll = false) {
  const target = pageMeta[page] ? page : "dashboard";
  const hashWillChange = updateHash && window.location.hash !== `#${target}`;
  const commit = () => {
    currentPage = target;
    document.body.dataset.page = target;
    document.body.dataset.scene = String(themeController?.getTheme?.() || document.documentElement.dataset.theme || "archive") + "-" + target;
    $$(".app-page").forEach((section) => section.classList.toggle("active", section.dataset.page === target));
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.route === target;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    const meta = resolvePageMeta(target);
    $("#page-eyebrow").textContent = meta.eyebrow;
    $("#page-title").textContent = meta.title;
    $("#page-lead").textContent = meta.lead;
    $("#page-lead").hidden = !meta.lead;
    $("#open-import").hidden = !["dashboard", "playlists", "search"].includes(target);
    document.title = `${meta.title} · AutoList`;
    closeSiteInspector(false);
    setSidebarOpen(false, false);
    const mobileMore = $("#mobile-more");
    const moreActive = ["rules", "history", "sites", "logs"].includes(target);
    mobileMore?.classList.toggle("active", moreActive);
    if (moreActive) {
      mobileMore?.setAttribute("aria-current", "page");
      mobileMore?.setAttribute("aria-label", `更多导航，当前页面：${meta.title}`);
    } else {
      mobileMore?.removeAttribute("aria-current");
      mobileMore?.setAttribute("aria-label", "打开更多导航");
    }
  };
  // Let the hashchange handler perform the commit for link clicks so one
  // navigation cannot start two overlapping transitions.
  if (hashWillChange) {
    rememberRouteScroll(currentPage);
    pendingHashNavigation = true;
    window.location.hash = target;
    return;
  }
  commit();
  const savedScroll = preserveScroll === true ? getRouteScroll(target) : null;
  // Use the two-argument form so CSS `scroll-behavior: smooth` cannot delay
  // or get overridden by the browser's own history restoration.
  const targetScroll = Number.isFinite(savedScroll) ? savedScroll : 0;
  window.scrollTo(0, targetScroll);
  if (!booting) requestAnimationFrame(() => {
    $("#main-content")?.focus({preventScroll: true});
    // Some WebViews still scroll a focused element despite preventScroll.
    window.scrollTo(0, targetScroll);
  });
  if (preserveScroll === true) {
    const enforceScroll = () => {
      if (currentPage === target) window.scrollTo(0, targetScroll);
    };
    requestAnimationFrame(enforceScroll);
    window.setTimeout(enforceScroll, 120);
  }
  if (!booting && !hashWillChange) {
    refreshPageData(target)
      .then(() => {
        if (currentPage === target) window.scrollTo(0, targetScroll);
      })
      .catch((error) => showToast(error.message));
  }
}

async function loadConnection() {
  const topDot = $("#top-service-dot");
  const sidebarDot = $("#sidebar-status-dot");
  try {
    const status = await api(`/api/connection?at=${Date.now()}`);
    const okClass = status.ok ? "ok" : "error";
    const providers = status.providers || {};
    const names = [["TMDB", providers.tmdb], ["TR", providers.transmission], ["Emby", providers.emby], ["MP", providers.moviepilot]];
    const onlineCount = names.filter(([, value]) => value?.ok).length;
    const detail = names.map(([name, value]) => `${name} ${value?.ok ? "✓" : "–"}`).join(" · ");
    topDot.className = `status-dot ${okClass}`;
    sidebarDot.className = `status-dot ${okClass}`;
    $("#top-service-label").textContent = `${onlineCount}/${names.length} 服务在线`;
    $("#top-service-detail").textContent = detail;
    $("#service-status")?.setAttribute("data-short-label", `${onlineCount}/${names.length}`);
    $("#service-status")?.setAttribute("aria-label", `服务状态：${onlineCount}/${names.length} 服务在线，${detail}，查看详情`);
    $("#sidebar-status-label").textContent = status.ok ? "系统运行正常" : "部分服务异常";
    const rail = [["#rail-tmdb-status", providers.tmdb], ["#rail-tr-status", providers.transmission], ["#rail-emby-status", providers.emby], ["#rail-mp-status", providers.moviepilot]];
    rail.forEach(([selector, value]) => { const node = $(selector); if (node) node.textContent = value?.ok ? "连接正常" : value?.configured === false ? "未配置" : "连接失败"; });
  } catch (error) {
    topDot.className = "status-dot error";
    sidebarDot.className = "status-dot error";
    $("#top-service-label").textContent = "服务连接异常";
    $("#top-service-detail").textContent = "点击重新检测";
    $("#service-status")?.setAttribute("data-short-label", "异常");
    $("#service-status")?.setAttribute("aria-label", "服务状态：连接异常，点击重新检测查看详情");
    $("#sidebar-status-label").textContent = "服务连接异常";
    ["#rail-tmdb-status", "#rail-tr-status", "#rail-emby-status", "#rail-mp-status"].forEach((selector) => { const node = $(selector); if (node) node.textContent = "连接失败"; });
  }
}

function updateDashboardTask(task) {
  const pill = $("#dashboard-task-pill");
  if (!task) {
    pill.textContent = "暂无任务";
    pill.className = "pill pill-neutral";
    $("#dashboard-task-title").textContent = "还没有搜索记录";
    $("#dashboard-task-copy").textContent = "导入片单后，可以按序号范围批量搜索资源。";
    setProgressValue("#dashboard-task-bar", 0);
    $(".dashboard-progress")?.setAttribute("aria-valuenow", "0");
    return;
  }
  const percent = task.total ? Math.round(task.completed / task.total * 100) : 0;
  pill.textContent = taskLabels[task.status] || task.status;
  pill.className = `pill ${taskPillClass(task.status)}`;
  $("#dashboard-task-title").textContent = `序号 ${task.range_start}–${task.range_end}`;
  const taskPlaylist = task.playlist_name ? `「${task.playlist_name}」 · ` : "";
  $("#dashboard-task-copy").textContent = `${taskPlaylist}${task.completed}/${task.total} 部已处理 · ${task.matched || 0} 个候选资源`;
  setProgressValue("#dashboard-task-bar", percent);
  $(".dashboard-progress")?.setAttribute("aria-valuenow", String(percent));
}

async function loadOverview() {
  const overview = await api("/api/overview");
  $("#metric-items").textContent = overview.item_count.toLocaleString("zh-CN");
  $("#metric-items-progress").textContent = overview.item_count.toLocaleString("zh-CN");
  $("#metric-playlist").textContent = overview.playlist_name || "暂无片单";
  $("#dashboard-recognized").textContent = Number(overview.recognized_count || 0).toLocaleString("zh-CN");
  $("#dashboard-in-library").textContent = Number(overview.in_library_count || 0).toLocaleString("zh-CN");
  $("#dashboard-in-library-progress").textContent = Number(overview.in_library_count || 0).toLocaleString("zh-CN");
  $("#dashboard-pending").textContent = Number(overview.not_in_library_count ?? overview.pending_count ?? 0).toLocaleString("zh-CN");
  $("#dashboard-new-count").textContent = `${Number(overview.in_library_count || 0).toLocaleString("zh-CN")} 部电影`;
  $("#dashboard-history-count").textContent = Number(overview.history_count || 0).toLocaleString("zh-CN");
  const libraryPercent = Number(overview.item_count) ? Math.round(Number(overview.in_library_count || 0) / Number(overview.item_count) * 100) : 0;
  setProgressValue("#dashboard-library-bar", libraryPercent);
  $(".collection-progress-bar")?.setAttribute("aria-valuenow", String(Math.min(100, libraryPercent)));
  if (overview.latest_task) {
    $("#metric-search").textContent = `${overview.latest_task.completed}/${overview.latest_task.total}`;
    $("#metric-search-state").textContent = taskLabels[overview.latest_task.status] || overview.latest_task.status;
  } else {
    $("#metric-search").textContent = "0";
    $("#metric-search-state").textContent = "暂无任务";
  }
  updateDashboardTask(overview.latest_task);
  $("#metric-candidates").textContent = Number(overview.latest_candidate_count || 0);
}

async function loadPlaylists(showDetails = false) {
  const lists = await api("/api/playlists");
  playlistCache = lists;
  $("#playlist").innerHTML = lists.length
    ? lists.map((item) => `<option value="${Number(item.id)}">${escapeHtml(item.name)} · ${Number(item.item_count)} 部</option>`).join("")
    : "<option value=''>暂无片单</option>";
  if (lists.length) {
    selectedPlaylistId = selectedPlaylistId && lists.some((item) => item.id === selectedPlaylistId) ? selectedPlaylistId : lists[0].id;
    currentSearchPlaylistId = currentSearchPlaylistId && lists.some((item) => item.id === currentSearchPlaylistId) ? currentSearchPlaylistId : lists[0].id;
    $("#playlist").value = String(currentSearchPlaylistId);
    $("#metric-items").textContent = Number(lists[0].item_count).toLocaleString("zh-CN");
    $("#metric-items-progress").textContent = Number(lists[0].item_count).toLocaleString("zh-CN");
    $("#metric-playlist").textContent = lists[0].name;
    if (!$("#start").value) $("#start").value = 1;
    if (!$("#end").value) $("#end").value = Math.min(50, Number(lists[0].item_count));
  }
  // 片单默认保持折叠，只有用户主动点击卡片时才展开详情。
  $("#playlist-cards").innerHTML = lists.length
    ? lists.map((item, index) => `<article class="playlist-card ${item.id === expandedPlaylistId ? "active" : ""}">
        <button class="playlist-card-main" data-playlist-id="${item.id}" aria-expanded="${item.id === expandedPlaylistId}" aria-controls="playlist-detail-panel"><span>${escapeHtml(item.source_type || "片单")}</span><strong>${escapeHtml(item.name)}</strong><small>${Number(item.recognized_count || 0)}/${Number(item.item_count)} 已识别 · ${item.source_url ? `同步于 ${formatTime(item.last_synced_at)}` : `创建于 ${formatTime(item.created_at)}`}</small><i>${item.id === expandedPlaylistId ? "收起明细 ↑" : "展开明细 ↓"}</i></button>
        <div class="playlist-card-actions">${item.source_url ? `<button data-sync-playlist="${item.id}">增量同步</button><button data-toggle-sync="${item.id}">${item.sync_enabled ? "停用定时" : "启用定时"}</button>` : ""}<button data-rename-playlist="${item.id}">改名</button><button data-order-playlist="${item.id}" data-direction="up" ${index === 0 ? "disabled" : ""}>上移</button><button data-order-playlist="${item.id}" data-direction="down" ${index === lists.length - 1 ? "disabled" : ""}>下移</button></div>
      </article>`).join("")
    : "<div class='empty-state'><strong>还没有片单</strong><p>导入文件或常见电影网站片单开始使用。</p></div>";
  $("#delete-playlist").disabled = !lists.length;
  $("#recognize-playlist").disabled = !lists.length;
  $("#playlist-detail-panel").hidden = !expandedPlaylistId;
  if (lists.length) await loadSearchableQueue();
  if (showDetails && expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId);
}

function updateSearchScope() {
  const pending = $("#search-scope").value === "pending";
  const form = document.querySelector(".search-form");
  form?.classList.toggle("is-pending", pending);
  form?.classList.toggle("is-range", !pending);
  $("#search-count-field").hidden = !pending;
  $("#search-start-field").hidden = pending;
  $("#search-range-arrow").hidden = pending;
  $("#search-end-field").hidden = pending;
  if (!pending && !$("#start").value) $("#start").value = 1;
}

function renderSearchQueue() {
  const queue = searchQueueCache || {};
  $("#search-total-count").textContent = Number(queue.total_count || 0).toLocaleString("zh-CN");
  $("#search-library-count").textContent = Number(queue.in_library_count || 0).toLocaleString("zh-CN");
  $("#search-downloading-count").textContent = Number(queue.downloading_count || 0).toLocaleString("zh-CN");
  $("#search-pending-count").textContent = Number(queue.pending_count || 0).toLocaleString("zh-CN");
  const count = $("#search-count");
  if (count) {
    count.max = String(Math.max(1, Number(queue.pending_count || 1)));
    count.value = Math.min(Number(count.value || 50), Number(count.max));
  }
  const playlist = playlistCache.find((item) => item.id === Number(currentSearchPlaylistId));
  $("#toggle-playlist-auto").textContent = playlist?.automation_enabled ? "关闭新片自动搜索" : "开启新片自动搜索";
  $("#playlist-automation-state").textContent = playlist ? (playlist.automation_enabled ? "新片自动搜索：已开启" : "新片自动搜索：未开启") : "";
}

async function loadSearchableQueue() {
  if (!currentSearchPlaylistId) return;
  searchQueueCache = await api(`/api/playlists/${currentSearchPlaylistId}/searchable-items`);
  renderSearchQueue();
}

async function loadPlaylistItems(playlistId, query = $("#playlist-item-query").value) {
  const params = new URLSearchParams({
    page: String(playlistPage), page_size: playlistPageSize,
    query: query.trim(), library_state: playlistLibraryFilter,
  });
  const result = await api(`/api/playlists/${playlistId}/items?${params}`);
  playlistItemsCache = result.items;
  playlistItemTotal = Number(result.total || 0);
  playlistPage = Number(result.page || 1);
  playlistPageCount = Number(result.pages || 1);
  renderPlaylistItems();
}

function renderPlaylistItems() {
  const pageSize = Number(playlistPageSize);
  const start = (playlistPage - 1) * pageSize;
  const selected = $(`[data-playlist-id="${expandedPlaylistId}"]`);
  $("#playlist-detail-title").textContent = selected?.querySelector("strong")?.textContent || "片单明细";
  $("#playlist-items").innerHTML = playlistItemsCache.length
    ? playlistItemsCache.map((item) => { const [label, klass] = playlistLibraryState(item.library_state); const title = item.tmdb_title || item.chinese_title || item.original_title; const original = item.tmdb_original_title || item.original_title; return `<tr><td data-label="序号">${Number(item.rank_no)}</td><td class="history-title"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(original)}</small></td><td data-label="年份">${escapeHtml(item.tmdb_year || item.year || "—")}</td><td data-label="IMDb">${escapeHtml(item.tmdb_imdb_id || item.imdb_id || "—")}</td><td data-label="TMDB">${escapeHtml(item.tmdb_id || "待识别")}</td><td data-label="入库"><span class="tag ${klass}">${label}</span></td></tr>`; }).join("")
    : "<tr><td colspan='6'><div class='empty-state compact'><strong>没有匹配影片</strong></div></td></tr>";
  $("#playlist-page-size").value = playlistPageSize;
  $("#playlist-library-filter").value = playlistLibraryFilter;
  $("#playlist-page-summary").textContent = playlistItemTotal ? `${start + 1}–${Math.min(start + playlistItemsCache.length, playlistItemTotal)} / ${playlistItemTotal} 条 · 第 ${playlistPage}/${playlistPageCount} 页` : "0 条";
  $("#playlist-page-prev").disabled = playlistPage <= 1;
  $("#playlist-page-next").disabled = playlistPage >= playlistPageCount;
  $("#refresh-library").disabled = !expandedPlaylistId;
}

function playlistLibraryState(state) {
  if (state === "in_library") return ["● 已入库", "tag-library"];
  if (["strm", "not_found"].includes(state)) return ["◌ 待入库", "tag-strm"];
  return ["未检查", "tag-muted"];
}

function renderCart() {
  const total = cartCache.reduce((sum, item) => sum + Number(item.size || 0), 0);
  const available = cartCache.filter((item) => item.context_available);
  const expired = cartCache.length - available.length;
  $("#nav-cart-count").textContent = cartCache.length;
  $("#cart-count").textContent = cartCache.length;
  $("#cart-checkout-count").textContent = `${cartCache.length} 个`;
  $("#metric-cart").textContent = cartCache.length;
  $("#metric-cart-size").textContent = formatSize(total);
  $("#cart-total-size").textContent = formatSize(total);
  const downloadButton = $("#download");
  downloadButton.disabled = available.length === 0;
  downloadButton.textContent = available.length
    ? (expired ? `下载有效资源（${available.length}）` : "开始下载")
    : (expired ? "请先重新搜索" : "暂无可下载资源");
  downloadButton.title = expired && !available.length ? "下载列表中的搜索上下文已失效，请先重新搜索" : "提交当前有效资源";
  $("#cart-list").innerHTML = cartCache.length
    ? cartCache.map((item) => {
        const movieTitle = item.tmdb_title || item.chinese_title || item.tmdb_original_title || item.original_title;
        return `<article class="cart-item cart-item-page ${item.context_available ? "" : "cart-item-expired"}">
        <div class="cart-item-main"><span class="cart-item-icon" aria-hidden="true">◇</span><div><strong>${escapeHtml(movieTitle)} <span class="candidate-meta">#${escapeHtml(item.rank_no)} · ${escapeHtml(item.tmdb_year || item.year || "")}</span></strong><small title="${escapeHtml(item.title)}">${torrentLinkHtml(item.detail_url, item.title)}</small></div></div>
        <div class="cart-item-spec"><span class="tag ${item.context_available ? "tag-accent" : "tag-error"}">${item.context_available ? escapeHtml(item.resolution || "其他") : "搜索上下文已过期"}</span><span class="tag">${escapeHtml(item.site_name || "未知站点")}</span><span class="tag">${formatSize(item.size)}</span>${item.context_available ? "" : '<button class="text-link" data-route-target="search">重新搜索 →</button>'}</div>
        <button class="cart-remove" data-cart-remove="${escapeHtml(item.id)}" aria-label="移除 ${escapeHtml(movieTitle)}">移除</button>
      </article>`; }).join("")
    : "<div class='empty-state'><span aria-hidden='true'>＋</span><strong>下载列表为空</strong><p>前往资源搜索，从候选中加入需要的资源。</p><button class='button button-secondary' type='button' data-route-target='search'>前往资源搜索</button></div>";
  if (expired && available.length) {
    $("#cart-result").textContent = `${expired} 个资源因服务重启已失效，请点击对应条目的“重新搜索”；仍可提交其余 ${available.length} 个有效资源。`;
    $("#cart-result").className = "inline-message cart-message warning";
  } else if (expired) {
    $("#cart-result").textContent = `${expired} 个资源的搜索上下文已失效，无法直接下载；请点击对应条目的“重新搜索”后再加入下载列表。`;
    $("#cart-result").className = "inline-message cart-message warning";
  } else if ($("#cart-result").classList.contains("warning")) {
    $("#cart-result").textContent = "";
    $("#cart-result").className = "inline-message cart-message";
  }
}

async function refreshCart() {
  cartCache = await api("/api/cart");
  renderCart();
}

const LOG_EVENT_LABELS = {
  movie_search_summary: "电影搜索摘要",
  search_task_finished: "搜索任务结束",
  site_search_failed: "站点搜索失败",
  download_submit: "下载提交",
};

function formatLogTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return escapeHtml(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}/${pad(parsed.getMonth() + 1)}/${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

function logEventLine(event) {
  const level = String(event.level || "INFO").toUpperCase();
  const parts = [];
  if (event.movie) parts.push(`<strong>${escapeHtml(event.movie)}</strong>${event.rank ? ` <span class="candidate-meta">#${escapeHtml(event.rank)}</span>` : ""}`);
  if (event.site) parts.push(`站点 ${escapeHtml(event.site)}`);
  if (event.status) parts.push(`状态 ${escapeHtml(event.status)}`);
  if (event.results != null) parts.push(`返回 ${Number(event.results)} 条`);
  if (event.kept != null) parts.push(`保留 ${Number(event.kept)} 个`);
  if (event.submitted != null) parts.push(`提交 ${Number(event.submitted)}`);
  if (event.skipped != null) parts.push(`跳过 ${Number(event.skipped)}`);
  if (event.needs_research != null) parts.push(`需重搜 ${Number(event.needs_research)}`);
  if (event.failed != null && Number(event.failed) > 0) parts.push(`失败 ${Number(event.failed)}`);
  if (event.error) parts.push(`<span class="log-error-text">${escapeHtml(String(event.error).slice(0, 160))}</span>`);
  if (event.task_id != null) parts.push(`任务 #${escapeHtml(event.task_id)}`);
  return `<article class="log-row log-${level.toLowerCase()}">
    <time>${formatLogTime(event.ts)}</time>
    <span class="log-level log-level-${level.toLowerCase()}">${escapeHtml(level)}</span>
    <div class="log-body"><span class="log-event">${escapeHtml(LOG_EVENT_LABELS[event.event] || event.event)}</span>${parts.length ? `<span class="log-meta">${parts.join(" · ")}</span>` : ""}</div>
  </article>`;
}

function renderLogEvents(events) {
  const list = $("#logs-list");
  if (!list) return;
  if (!events.length) {
    list.innerHTML = "<div class='empty-state'><span aria-hidden='true'>⌁</span><strong>暂无日志</strong><p>服务运行后会自动记录事件日志。</p></div>";
    return;
  }
  list.innerHTML = events.map(logEventLine).join("");
}

async function loadLogEvents() {
  const level = $("#logs-level")?.value || "";
  const query = $("#logs-query")?.value?.trim() || "";
  const params = new URLSearchParams({limit: "300"});
  if (level) params.set("level", level);
  if (query) params.set("query", query);
  const events = await api(`/api/logs/events?${params}`);
  renderLogEvents(events);
}

function candidateState(item) {
  if (item.library_state === "in_library") return ["已入库", "tag-library"];
  if (["strm", "not_found"].includes(item.library_state)) return ["待入库", "tag-strm"];
  return ["状态未知", ""];
}

function formatPublishDate(value) {
  if (!value) return "";
  const text = String(value).trim();
  const match = text.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if (match) return `${match[1]}/${match[2].padStart(2, "0")}/${match[3].padStart(2, "0")}`;
  const parsed = new Date(text);
  if (!Number.isNaN(parsed.getTime()) && parsed.getFullYear() > 1990) {
    const month = String(parsed.getMonth() + 1).padStart(2, "0");
    const day = String(parsed.getDate()).padStart(2, "0");
    return `${parsed.getFullYear()}/${month}/${day}`;
  }
  return "";
}

function torrentLinkHtml(url, title) {
  const href = safeExternalUrl(url);
  const label = escapeHtml(title);
  return href === "#" ? label : `<a class="torrent-link" href="${href}" target="_blank" rel="noopener noreferrer" title="${label}">${label} ↗</a>`;
}

function captureCandidateViewState(container) {
  const expanded = new Map([...container.querySelectorAll("[data-candidate-details]")].map((details) => [details.dataset.candidateDetails, details.open]));
  const active = document.activeElement;
  const button = active?.closest("[data-candidate]");
  const details = active?.closest("[data-candidate-details]");
  return {
    expanded,
    scrollY: window.scrollY,
    focus: button ? {type: "button", id: button.dataset.candidate} : details ? {type: "details", id: details.dataset.candidateDetails} : null,
  };
}

function restoreCandidateViewState(container, state) {
  container.querySelectorAll("[data-candidate-details]").forEach((details) => {
    const key = details.dataset.candidateDetails;
    if (state.expanded.has(key)) details.open = state.expanded.get(key);
  });
  const focus = state.focus;
  if (focus) {
    const target = focus.type === "button"
      ? [...container.querySelectorAll("[data-candidate]")].find((button) => button.dataset.candidate === focus.id)
      : [...container.querySelectorAll("[data-candidate-details]")].find((details) => details.dataset.candidateDetails === focus.id)?.querySelector("summary");
    if (target && !target.disabled) requestAnimationFrame(() => target.focus({preventScroll: true}));
  }
  if (Number.isFinite(state.scrollY)) window.scrollTo(0, state.scrollY);
}

function renderCandidates() {
  const container = $("#candidates");
  const filtered = candidateCache.filter((item) => {
    if (currentFilter === "all") return item.eligibility !== "excluded";
    if (currentFilter === "preferred") return item.recommendation === "preferred";
    if (currentFilter === "2160p") return item.resolution === "2160p";
    if (currentFilter === "x265") return item.codec === "x265";
    if (currentFilter === "fallback") return item.recommendation === "fallback";
    if (currentFilter === "excluded") return item.eligibility === "excluded";
    return true;
  });
  const eligibleCount = candidateCache.filter((item) => item.eligibility !== "excluded").length;
  $("#metric-candidates").textContent = eligibleCount;
  const signature = JSON.stringify({
    filter: currentFilter,
    task: activeTaskState?.status || "",
    failed: Number(activeTaskState?.attempt_summary?.failed || 0),
    items: filtered.map((item) => [item.id, item.in_cart, item.context_available, item.eligibility, item.recommendation, item.score, item.site_count, Array.isArray(item.site_options) ? item.site_options.length : 0]),
  });
  if (signature === candidateRenderSignature && container.childElementCount) return;
  const viewState = captureCandidateViewState(container);
  candidateRenderSignature = signature;
  container.setAttribute("aria-busy", "true");
  if (!filtered.length) {
    const {heading, copy, actions} = candidateEmptyState({candidateCache, activeTaskState, currentFilter});
    container.innerHTML = `<div class="empty-state"><span aria-hidden="true">⌕</span><strong>${heading}</strong><p>${copy}</p>${actions}</div>`;
    container.setAttribute("aria-busy", "false");
    return;
  }
  let previousMovieId = null;
  container.innerHTML = filtered.map((item) => {
    const [stateLabel, stateClass] = candidateState(item);
    const movieStart = previousMovieId !== null && item.playlist_item_id !== previousMovieId;
    previousMovieId = item.playlist_item_id;
    const excluded = item.eligibility === "excluded";
    const recommendation = {preferred: "首选", fallback: "保底", excluded: "已排除"}[item.recommendation] || (excluded ? "已排除" : "候选");
    const labels = Array.isArray(item.metadata?.labels) ? item.metadata.labels.slice(0, 2) : [];
    const breakdown = Array.isArray(item.score_breakdown) ? item.score_breakdown.slice(0, 3) : [];
    const movieTitle = item.tmdb_title || item.chinese_title || item.tmdb_original_title || item.original_title;
    const candidateIndex = item.rank_no == null ? "—" : String(item.rank_no).padStart(2, "0");
    const siteOptions = Array.isArray(item.site_options) ? item.site_options : [];
    const hasSiteOptions = Number(item.site_count || siteOptions.length) > 1 && siteOptions.length > 1;
    return `<article class="candidate-row ${movieStart ? "movie-start " : ""}${item.recommendation === "preferred" ? "is-best" : ""} ${excluded ? "is-excluded" : ""}">
      <span class="candidate-index" aria-hidden="true">${escapeHtml(candidateIndex)}</span>
      <div class="candidate-title"><strong>${escapeHtml(movieTitle)} <span class="candidate-meta">#${escapeHtml(item.rank_no)} · ${escapeHtml(item.tmdb_year || item.year || "")}</span></strong><small title="${escapeHtml(item.title)}">${torrentLinkHtml(item.detail_url, item.title)}</small></div>
      <div class="recommendation-cell"><span class="recommendation-badge ${escapeHtml(item.recommendation)}">${recommendation}</span><small>${escapeHtml(item.recommendation_reason || "等待规则分析")}</small></div>
      <div class="candidate-source"><strong>${escapeHtml(item.site_name || "未知站点")} ${formatPublishDate(item.metadata?.publish_time) ? `<span class="publish-date">${formatPublishDate(item.metadata?.publish_time)}</span>` : ""} ${item.is_free ? '<em class="free-mark">FREE</em>' : ""}</strong><small class="site-selection-reason">${escapeHtml(item.site_selection_reason || "")}</small><div class="spec-stack"><span class="tag tag-accent">${escapeHtml(item.resolution || "其他")}</span><span class="tag">${escapeHtml(item.codec || "其他")}</span><span class="tag">${escapeHtml(item.group_name || "未知组")}</span><span class="tag">${Number(item.seeders || 0)} 做种</span><span class="tag">${formatSize(item.size)}</span><span class="tag ${stateClass}">${stateLabel}</span>${labels.map((label) => `<span class="tag tag-promo">${escapeHtml(label)}</span>`).join("")}</div>${hasSiteOptions ? `<details class="site-options" data-candidate-details="${escapeHtml(item.id)}"><summary>另 ${siteOptions.length - 1} 个站点</summary>${siteOptions.slice(1).map((option) => `<div><strong>${torrentLinkHtml(option.detail_url, option.site_name || "未知")}</strong><span>${formatPublishDate(option.publish_time)}${formatPublishDate(option.publish_time) ? " · " : ""}优先级 ${Number(option.site_priority)} · ${option.volume_factor === 0 ? "FREE · " : option.volume_factor < 1 ? `下载 ${Math.round(option.volume_factor * 100)}% · ` : ""}${Number(option.seeders || 0)} 做种</span></div>`).join("")}</details>` : ""}</div>
      <div class="candidate-score"><strong>${excluded ? "—" : Number(item.score || 0)}</strong><small>${excluded ? escapeHtml(item.exclusion_reason || "不符合允许组合") : (breakdown.map((part) => `${escapeHtml(part.label)}${Number(part.score || 0) ? ` +${Number(part.score)}` : ""}`).join(" · ") || "策略匹配")}</small></div>
      ${excluded ? '<span class="candidate-blocked">不可加入</span>' : `<button class="candidate-action ${item.in_cart ? "selected" : ""}" data-candidate="${escapeHtml(item.id)}" ${!item.context_available && !item.in_cart ? "disabled" : ""}>${item.in_cart ? "移出下载列表" : item.context_available ? "加入下载列表" : "需重新搜索"}</button>`}
    </article>`;
  }).join("");
  container.setAttribute("aria-busy", "false");
  restoreCandidateViewState(container, viewState);
}

function setTaskState(task) {
  activeTaskState = task;
  const pill = $("#task-pill");
  const progress = $("#task-progress");
  const errorSummary = $("#task-error-summary");
  const percent = task.total ? Math.round(task.completed / task.total * 100) : 0;
  pill.textContent = taskLabels[task.status] || task.status;
  pill.className = `pill ${taskPillClass(task.status)}`;
  if (errorSummary) {
    const hasTaskError = ["failed", "partial"].includes(task.status) && task.error_message;
    errorSummary.hidden = !hasTaskError;
    errorSummary.textContent = hasTaskError ? `任务提示：${task.error_message}` : "";
  }
  progress.hidden = false;
  $("#task-text").textContent = `${task.completed}/${task.total} 部 · ${task.matched} 个候选`;
  $("#task-percent").textContent = `${percent}%`;
  setProgressValue("#task-bar", percent);
  $("#task-progress-bar")?.setAttribute("aria-valuenow", String(percent));
  $("#cancel-task").hidden = !["queued", "running"].includes(task.status);
  $("#retry-task").hidden = !["partial", "failed", "completed"].includes(task.status) || !Number(task.attempt_summary?.failed || 0);
  $("#restart-task").hidden = !["partial", "interrupted", "cancelled", "failed"].includes(task.status);
  const attempts = task.attempt_summary || {};
  $("#task-attempt-summary").textContent = `${Number(attempts.succeeded || 0)} 次成功 · ${Number(attempts.failed || 0)} 次失败`;
  $("#metric-search").textContent = `${task.completed}/${task.total}`;
  $("#metric-search-state").textContent = taskLabels[task.status] || task.status;
  updateDashboardTask(task);
  if (["failed", "partial"].includes(task.status)) $("#task-log-panel").open = true;
}

async function refreshTaskLogs() {
  if (!activeTask) return;
  const [logs, attempts] = await Promise.all([
    api(`/api/search-tasks/${activeTask}/logs`),
    api(`/api/search-tasks/${activeTask}/attempts`),
  ]);
  $("#task-log-count").textContent = logs.length;
  $("#task-attempts").innerHTML = attempts.sites?.length ? attempts.sites.map((site) => `<span><strong>${escapeHtml(site.site_name)}</strong> ${Number(site.succeeded || 0)}/${Number(site.total || 0)} · ${Number(site.average_ms || 0)}ms</span>`).join("") : "";
  $("#task-logs").innerHTML = logs.length ? logs.map((item) => `<article class="task-log ${escapeHtml(item.level)}"><time>${formatTime(item.created_at)}</time><span>${escapeHtml(item.stage)}</span><p>${escapeHtml(item.message)}</p></article>`).join("") : "<p>暂无日志。</p>";
}

let candidatePollFailures = 0;
async function refreshCandidates(schedule = true) {
  if (!activeTask) return;
  const container = $("#candidates");
  if (container) container.setAttribute("aria-busy", "true");
  let task;
  try {
    task = await api(`/api/search-tasks/${activeTask}`);
    candidatePollFailures = 0;
    clearTaskPollError();
  } catch (error) {
    candidatePollFailures += 1;
    setTaskPollError(`搜索状态读取失败（第 ${candidatePollFailures} 次）：${error.message}`);
    clearTimeout(taskTimer);
    if (schedule && candidatePollFailures < 10) taskTimer = setTimeout(() => refreshCandidates(true).catch((pollError) => setTaskPollError(pollError.message)), 3000);
    if (container) container.setAttribute("aria-busy", "false");
    return;
  }
  setTaskState(task);
  let pollError = false;
  try {
    candidateCache = await api(`/api/candidates?task_id=${activeTask}`);
  } catch (error) {
    pollError = true;
    setTaskPollError(`候选结果读取失败：${error.message}`);
  }
  try {
    await refreshTaskLogs();
  } catch (error) {
    pollError = true;
    setTaskPollError(`任务日志读取失败：${error.message}`);
  }
  renderCandidates();
  if (!pollError && ["completed", "partial", "failed", "cancelled", "interrupted"].includes(task.status)) clearTaskPollError();
  clearTimeout(taskTimer);
  if (schedule && ["queued", "running"].includes(task.status)) {
    taskTimer = setTimeout(() => refreshCandidates(true).catch((error) => showToast(error.message)), 1400);
  }
  if (container) container.setAttribute("aria-busy", "false");
}

async function recoverLatestTask() {
  const tasks = await api("/api/search-tasks?limit=1");
  if (!tasks.length) return;
  activeTask = tasks[0].id;
  candidateRenderSignature = "";
  await refreshCandidates(true);
}

let historyCache = [];
let historyStatusFilter = "all";

function renderHistoryTable() {
  const filtered = filterHistoryItems(historyCache, historyStatusFilter).slice(0, 50);
  const emptyLabel = historyCache.length ? "没有符合筛选的记录" : "暂无下载历史";
  $("#history").innerHTML = filtered.length
    ? filtered.map(historyRowHtml).join("")
    : `<tr><td colspan='4'><div class='empty-state compact'><strong>${emptyLabel}</strong></div></td></tr>`;
  const note = $("#history-truncate-note");
  if (note) {
    const truncated = historyCache.length > 50;
    note.textContent = truncated ? `仅显示最近 50 条，共 ${historyCache.length} 条记录。` : "";
    note.hidden = !truncated;
  }
  const filter = $("#history-status-filter");
  if (filter && filter.value !== historyStatusFilter) filter.value = historyStatusFilter;
}

async function refreshHistory() {
  historyCache = await api("/api/history");
  renderHistoryTable();
}

const setServiceStatus = (selector, result, optional = false) => {
  const element = $(selector);
  element.className = "";
  if (result?.ok) {
    element.textContent = "连接正常";
    element.classList.add("ok");
    return;
  }
  if (optional && result?.configured === false) {
    element.textContent = "未配置";
    return;
  }
  element.textContent = "连接失败";
  element.classList.add("error");
};

function secretValue(inputId) {
  const input = $(`#${inputId}`);
  const value = String(input?.value || "").trim();
  return value || null; // 密钥输入框留空时保留服务端已有值
}

async function loadSettings() {
  const [runtime, cookiecloud] = await Promise.all([api("/api/settings"), api("/api/cookiecloud/status")]);
  $("#settings-mp-url").value = runtime.mp_base_url || "";
  $("#settings-mp-key").value = runtime.mp_api_key || "";
  $("#settings-timeout").value = runtime.mp_timeout_seconds || 30;
  $("#settings-emby-url").value = runtime.emby_base_url || "";
  $("#settings-emby-key").value = runtime.emby_api_key || "";
  $("#settings-tmdb-key").value = runtime.tmdb_api_key || "";
  $("#settings-tmdb-language").value = runtime.tmdb_language || "zh-CN";
  $("#settings-mdblist-key").value = runtime.mdblist_api_key || "";
  $("#settings-mdblist-status").textContent = runtime.mdblist_api_key_configured ? "已配置" : "公开片单可用";
  $("#settings-cookiecloud-key").value = runtime.cookiecloud_key || "";
  $("#settings-cookiecloud-key").placeholder = runtime.cookiecloud_key_configured ? "已配置；更换请直接输入新值" : "与 Chrome 扩展保持一致";
  $("#settings-cookiecloud-password").value = runtime.cookiecloud_password || "";
  $("#settings-cookiecloud-endpoint").value = `${window.location.origin}${runtime.cookiecloud_endpoint || "/cookiecloud"}`;
  $("#settings-cookiecloud-status").textContent = cookiecloud.received ? "已收到 Chrome 数据" : cookiecloud.configured ? "等待首次同步" : "未配置";
  $("#settings-cookiecloud-status").className = cookiecloud.received ? "ok" : "";
  const proxyConfigured = Boolean(runtime.outbound_proxy_url_configured || runtime.outbound_proxy_configured);
  const proxyUrl = String(runtime.outbound_proxy_url || "");
  $("#settings-proxy-url").value = proxyUrl;
  $("#settings-proxy-url").placeholder = proxyConfigured ? "已配置；留空保留原值" : "http://127.0.0.1:7890";
  $("#settings-proxy-state").textContent = proxyConfigured ? (proxyUrl ? "地址可见" : "已配置") : "未配置";
  $("#settings-proxy-status").textContent = proxyConfigured ? "已配置" : "未配置";
  $("#settings-tmdb-proxy").checked = Boolean(runtime.tmdb_proxy_enabled);
  $("#settings-pt-proxy").checked = Boolean(runtime.pt_proxy_enabled);
  const tokenRequired = Boolean(runtime.access_token_required);
  const localToken = getAccessToken();
  $("#settings-access-token").value = localToken;
  $("#settings-access-token-status").textContent = tokenRequired
    ? (localToken ? "服务端已启用 · 本机已保存" : "服务端已启用 · 本机未保存")
    : (localToken ? "服务端未启用 · 本机有缓存" : "服务端未启用");
  $("#settings-access-token-status").className = tokenRequired && localToken ? "ok" : tokenRequired ? "error" : "";
  $("#settings-ai-url").value = runtime.ai_base_url || "";
  $("#settings-ai-key").value = runtime.ai_api_key || "";
  $("#settings-ai-model").value = runtime.ai_model || "";
  $("#settings-ai-status").textContent = runtime.ai_base_url && runtime.ai_api_key_configured && runtime.ai_model ? "已配置" : "未配置";
  $("#settings-tr-url").value = runtime.tr_base_url || "";
  $("#settings-tr-username").value = runtime.tr_username || "";
  $("#settings-tr-password").value = runtime.tr_password || "";
  syncThemeControls();
}

async function testSettings() {
  const output = $("#settings-result");
  const result = await api("/api/settings/test", {method: "POST"});
  setServiceStatus("#settings-mp-status", result.moviepilot);
  setServiceStatus("#settings-emby-status", result.emby, true);
  setServiceStatus("#settings-tmdb-status", result.tmdb);
  setServiceStatus("#settings-tr-status", result.transmission);
  const summary = [["TMDB", result.tmdb], ["Transmission", result.transmission], ["Emby", result.emby], ["MoviePilot", result.moviepilot]];
  output.textContent = summary.map(([name, value]) => `${name} ${value?.ok ? "正常" : value?.configured === false ? "未配置" : "失败"}`).join(" · ");
  output.className = `inline-message ${result.tmdb?.ok && result.moviepilot?.ok ? "" : "error"}`;
  return result;
}

async function saveSettings() {
  const runtimePayload = {
    mp_base_url: $("#settings-mp-url").value.trim(),
    mp_api_key: secretValue("settings-mp-key"),
    mp_timeout_seconds: Number($("#settings-timeout").value),
    emby_base_url: $("#settings-emby-url").value.trim(),
    emby_api_key: secretValue("settings-emby-key"),
    tmdb_api_key: secretValue("settings-tmdb-key"),
    tmdb_language: $("#settings-tmdb-language").value.trim() || "zh-CN",
    mdblist_api_key: secretValue("settings-mdblist-key"),
    cookiecloud_key: secretValue("settings-cookiecloud-key"),
    cookiecloud_password: secretValue("settings-cookiecloud-password"),
    outbound_proxy_url: $("#settings-proxy-url").value.trim() || null,
    tmdb_proxy_enabled: $("#settings-tmdb-proxy").checked,
    pt_proxy_enabled: $("#settings-pt-proxy").checked,
    ai_base_url: $("#settings-ai-url").value.trim(),
    ai_api_key: secretValue("settings-ai-key"),
    ai_model: $("#settings-ai-model").value.trim(),
    tr_base_url: $("#settings-tr-url").value.trim(),
    tr_username: $("#settings-tr-username").value.trim(),
    tr_password: secretValue("settings-tr-password"),
  };
  await api("/api/settings", {method: "PUT", body: JSON.stringify(runtimePayload)});
  await loadSettings();
  await Promise.all([testSettings(), loadConnection()]);
}

async function loadRules() {
  const [rules, catalog] = await Promise.all([api("/api/config"), api("/api/config/release-groups")]);
  try { candidatePolicyCache = JSON.parse(rules.candidate_policy || "{}"); } catch { candidatePolicyCache = {}; }
  const policy = candidatePolicyCache || {};
  const profiles = Array.isArray(policy.profiles) ? policy.profiles : [];
  const primary = profiles.find((item) => item.id === "primary_x265") || profiles[0] || {groups: ["ADE", "FRDS", "HDS", "CHD"]};
  const fallback = profiles.find((item) => item.id === "fallback_x264") || profiles[1] || {groups: ["CMCT"]};
  $("#rules-primary-groups").value = (primary.groups || []).join(", ");
  $("#rules-fallback-groups").value = (fallback.groups || []).join(", ");
  const order = policy.resolution_order || ["2160p", "1080p"];
  $("#rules-resolution-first").value = order[0] || "2160p";
  $("#rules-resolution-second").value = order[1] || (order[0] === "2160p" ? "1080p" : "2160p");
  $("#rules-limit").value = Number(policy.candidate_limit || rules.candidate_limit || 6);
  $("#rules-custom-groups").value = (policy.custom_release_groups || []).join("\n");
  const exclusions = Array.isArray(policy.hard_exclusions) ? policy.hard_exclusions : [];
  $("#hard-exclusion-options").innerHTML = exclusions.map((rule) => `<label class="exclusion-option"><input type="checkbox" data-exclusion-id="${escapeHtml(rule.id)}" ${rule.enabled !== false ? "checked" : ""}><span><strong>${escapeHtml(rule.label)}</strong><small>命中即排除</small></span></label>`).join("");
  $("#release-group-count").textContent = `${Number(catalog.merged_count)} 条 · 内置 ${Number(catalog.builtin_count)} / 自定义 ${Number(catalog.custom_count)}`;
  $("#builtin-release-groups").innerHTML = (catalog.builtin_names || []).map((name) => `<span>${escapeHtml(name)}</span>`).join("");
}

function splitGroups(value) {
  return [...new Set(value.split(/[,，\n]+/).map((item) => item.trim().toUpperCase()).filter(Boolean))];
}

function collectCandidatePolicy() {
  const first = $("#rules-resolution-first").value;
  const second = $("#rules-resolution-second").value;
  return {
    profiles: [
      {id: "primary_x265", label: "首选 x265", enabled: true, codecs: ["x265"], groups: splitGroups($("#rules-primary-groups").value), tier: 1},
      {id: "fallback_x264", label: "保底 x264", enabled: true, codecs: ["x264"], groups: splitGroups($("#rules-fallback-groups").value), tier: 2},
    ],
    resolution_order: [...new Set([first, second])],
    hard_exclusions: $$("[data-exclusion-id]").map((input) => ({id: input.dataset.exclusionId, enabled: input.checked})),
    custom_release_groups: $("#rules-custom-groups").value.split("\n").map((item) => item.trim()).filter(Boolean),
    candidate_limit: Number($("#rules-limit").value || 6),
  };
}

async function saveRules() {
  const policy = collectCandidatePolicy();
  if (!policy.profiles[0].groups.length || !policy.profiles[1].groups.length) throw new Error("首选与保底制作组都不能为空");
  if (policy.resolution_order.length < 2) throw new Error("两个分辨率顺序不能相同");
  const payload = {candidate_policy: policy, candidate_limit: policy.candidate_limit};
  await api("/api/config", {method: "PUT", body: JSON.stringify(payload)});
  await loadRules();
  $("#rules-result").textContent = "规则已保存，将应用于下一次搜索。";
}

async function loadSites() {
  const list = await api("/api/sites");
  siteCache = list;
  if (selectedSiteId && !list.some((site) => site.id === selectedSiteId)) selectedSiteId = null;
  if (!selectedSiteId && list.length) selectedSiteId = list[0].id;
  renderSites();
  renderSiteInspector(list.find((site) => site.id === selectedSiteId));
  syncSiteInspectorMode();
  if (selectedSiteId) {
    const siteId = selectedSiteId;
    loadSiteHealth(siteId).catch((error) => {
      if (selectedSiteId === siteId && $("#site-health-summary")) $("#site-health-summary").textContent = `统计读取失败：${error.message}`;
    });
  }
}

function siteConnectionState(site) {
  // 完全基于 AutoList 自身检测结果
  if (site.last_status === "slow") return "slow";
  if (site.last_status === "ok") return "normal";
  if (site.last_status === "error") return "failed";
  return "unknown";
}
function siteMonogram(name) {
  const chars = [...String(name || "").trim()].filter((char) => !/\s/.test(char));
  return (chars.slice(0, 2).join("").toUpperCase() || "?");
}
function siteStateLabel(state) {
  return state === "normal" ? "正常连接" : state === "slow" ? "连接缓慢" : state === "failed" ? "连接失败" : "连接未知";
}
const siteNodePositionCache = new Map();
const siteNodePositionOverrides = new Map();
const siteNodePositionStorageKey = "autolist-site-node-positions";
const siteMapOrientationStorageKey = "autolist-site-map-orientation";
const siteMapViewportStorageKey = "autolist-site-map-viewport";
const siteMapZoomMin = 0.78;
const siteMapZoomMax = 2.4;
let siteMapOrientation = safeStorageGet(siteMapOrientationStorageKey) === "vertical" ? "vertical" : "horizontal";
let siteMapViewport = {scale: 1, x: 0, y: 0};
try {
  const storedPositions = JSON.parse(safeStorageGet(siteNodePositionStorageKey) || "{}");
  Object.entries(storedPositions).forEach(([siteId, position]) => {
    if (!Array.isArray(position) || position.length < 2) return;
    const left = Number(position[0]), top = Number(position[1]);
    if (Number.isFinite(left) && Number.isFinite(top)) siteNodePositionOverrides.set(String(siteId), [Math.max(8, Math.min(92, left)), Math.max(12, Math.min(88, top))]);
  });
} catch (_error) { /* 浏览器存储损坏时回退到稳定的默认排布 */ }
try {
  const storedViewport = JSON.parse(safeStorageGet(siteMapViewportStorageKey) || "{}");
  const scale = Number(storedViewport.scale);
  const x = Number(storedViewport.x), y = Number(storedViewport.y);
  if (Number.isFinite(scale) && Number.isFinite(x) && Number.isFinite(y)) {
    siteMapViewport = {scale: Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, scale)), x, y};
  }
} catch (_error) { /* 浏览器存储损坏时回退到默认视野 */ }
function persistSiteNodePositions() {
  safeStorageSet(siteNodePositionStorageKey, JSON.stringify(Object.fromEntries(siteNodePositionOverrides)));
}
function persistSiteMapViewport() {
  safeStorageSet(siteMapViewportStorageKey, JSON.stringify(siteMapViewport));
}
function siteMapViewportBounds(field) {
  const rect = field?.getBoundingClientRect?.();
  if (!rect?.width || !rect?.height) return {x: 120, y: 120};
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(siteMapViewport.scale) || 1));
  return {
    x: Math.max(90, ((scale - 1) * rect.width) / 2 + 90),
    y: Math.max(90, ((scale - 1) * rect.height) / 2 + 90),
  };
}
function clampSiteMapViewport(next, field = $("#sites-list .site-map-field")) {
  const bounds = siteMapViewportBounds(field);
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(next.scale) || 1));
  return {
    scale,
    x: Math.max(-bounds.x, Math.min(bounds.x, Number(next.x) || 0)),
    y: Math.max(-bounds.y, Math.min(bounds.y, Number(next.y) || 0)),
  };
}
function syncSiteMapViewport() {
  const field = $("#sites-list .site-map-field");
  const canvas = $("#sites-list .site-map-canvas");
  if (!field || !canvas) return;
  siteMapViewport = clampSiteMapViewport(siteMapViewport, field);
  canvas.style.setProperty("--map-scale", String(siteMapViewport.scale));
  canvas.style.setProperty("--map-pan-x", `${siteMapViewport.x}px`);
  canvas.style.setProperty("--map-pan-y", `${siteMapViewport.y}px`);
  field.dataset.zoom = String(Math.round(siteMapViewport.scale * 100));
  field.setAttribute("aria-label", `${siteMapCopy().ariaLabel}，当前缩放 ${Math.round(siteMapViewport.scale * 100)}%，滚轮或双指缩放，拖动画布平移`);
  field.setAttribute("aria-description", "拖动画布平移；滚轮、双指或加减按钮缩放；点击来源节点进入档案；Alt 加拖动可调整节点位置");
  const output = $("#sites-list .site-map-zoom-value");
  if (output) output.textContent = `${Math.round(siteMapViewport.scale * 100)}%`;
  $("#sites-list [data-site-map-zoom='out']")?.toggleAttribute("disabled", siteMapViewport.scale <= siteMapZoomMin);
  $("#sites-list [data-site-map-zoom='in']")?.toggleAttribute("disabled", siteMapViewport.scale >= siteMapZoomMax);
}
function resetSiteMapViewport() {
  siteMapViewport = {scale: 1, x: 0, y: 0};
  persistSiteMapViewport();
  syncSiteMapViewport();
}
function zoomSiteMap(nextScale, anchorX = null, anchorY = null) {
  const field = $("#sites-list .site-map-field");
  const current = siteMapViewport;
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(nextScale) || 1));
  if (!field || scale === current.scale) return;
  const rect = field.getBoundingClientRect();
  const x = Number.isFinite(anchorX) ? anchorX - rect.left - rect.width / 2 : 0;
  const y = Number.isFinite(anchorY) ? anchorY - rect.top - rect.height / 2 : 0;
  siteMapViewport = clampSiteMapViewport({
    scale,
    x: current.x + (current.scale - scale) * x,
    y: current.y + (current.scale - scale) * y,
  }, field);
  persistSiteMapViewport();
  syncSiteMapViewport();
}
function focusSiteNode(siteId) {
  const field = $("#sites-list .site-map-field");
  const node = [...($("#sites-list")?.querySelectorAll(".site-star-node") || [])].find((item) => String(item.dataset.openSite) === String(siteId));
  if (!field || !node) return;
  const rect = field.getBoundingClientRect();
  const left = Number.parseFloat(node.style.getPropertyValue("--node-left")) || 50;
  const top = Number.parseFloat(node.style.getPropertyValue("--node-top")) || 50;
  const scale = Math.max(siteMapViewport.scale, 1.12);
  siteMapViewport = clampSiteMapViewport({
    scale,
    x: -((left / 100) - .5) * rect.width * scale,
    y: -((top / 100) - .5) * rect.height * scale,
  }, field);
  persistSiteMapViewport();
  syncSiteMapViewport();
}
function resetSiteNodePositions() {
  siteNodePositionOverrides.clear();
  siteNodePositionCache.clear();
  persistSiteNodePositions();
}
function siteNodePosition(siteId) {
  const key = String(siteId);
  if (siteNodePositionOverrides.has(key)) return siteNodePositionOverrides.get(key);
  if (siteNodePositionCache.has(key)) return siteNodePositionCache.get(key);
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  const occupied = [...siteNodePositionCache.values()];
  let position = null;
  for (let attempt = 0; attempt < 96; attempt += 1) {
    const mixed = Math.imul(hash ^ Math.imul(attempt + 1, 0x9e3779b9), 2654435761) >>> 0;
    const x = 14 + (mixed % 7201) / 100;
    const y = 17 + (((mixed >>> 8) % 6601) / 100);
    if (occupied.every(([otherX, otherY]) => Math.hypot(x - otherX, y - otherY) >= 8.5)) {
      position = [x, y];
      break;
    }
  }
  position ||= [14 + (hash % 7201) / 100, 17 + (((hash >>> 8) % 6601) / 100)];
  siteNodePositionCache.set(key, position);
  return position;
}
function setSiteNodePosition(siteId, left, top) {
  const position = [Math.max(8, Math.min(92, Number(left))), Math.max(12, Math.min(88, Number(top)))];
  siteNodePositionOverrides.set(String(siteId), position);
  siteNodePositionCache.set(String(siteId), position);
}
function matchesSiteFilter(site) {
  const state = siteConnectionState(site);
  return siteFilter === "all" || (siteFilter === "active" && site.enabled) || (siteFilter === "inactive" && !site.enabled) || siteFilter === state;
}
function siteMapCopy(theme = themeController?.getTheme?.() || document.documentElement.dataset.theme || "archive") {
  const copy = {
    archive: {kicker: "SOURCE ROOM / ARCHIVE NETWORK", heading: "座来源档案室", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入档案，Alt+拖动可重新排布。", ariaLabel: "可操作的来源档案地图"},
    cinema: {kicker: "SCREENING FLOOR / SOURCE MAP", heading: "个放映来源", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入场务档案，Alt+拖动可重新排布。", ariaLabel: "可操作的放映来源地图"},
    ledger: {kicker: "CATALOGUE / SOURCE REGISTER", heading: "条来源记录", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入目录条目，Alt+拖动可重新排布。", ariaLabel: "可操作的来源目录地图"},
  }[theme] || null;
  return copy || siteMapCopy("archive");
}
function syncSiteMapCopy(theme = themeController?.getTheme?.() || document.documentElement.dataset.theme || "archive") {
  const mapHeading = $("#sites-list .site-map-heading");
  if (!mapHeading) return;
  const copy = siteMapCopy(theme);
  const count = siteCache.filter(matchesSiteFilter).length;
  mapHeading.querySelector(".section-kicker")?.replaceChildren(document.createTextNode(copy.kicker));
  mapHeading.querySelector("h2")?.replaceChildren(document.createTextNode(String(count) + copy.heading));
  mapHeading.querySelector("p:not(.section-kicker)")?.replaceChildren(document.createTextNode(copy.description));
  $("#sites-list .site-map-field")?.setAttribute("aria-label", copy.ariaLabel + "，滚轮或双指缩放，拖动画布平移，点击节点进入档案");
}
function syncSiteMapOrientationControls() {
  const map = $("#sites-list");
  map?.classList.toggle("is-vertical", siteMapOrientation === "vertical");
  if (map) map.dataset.orientation = siteMapOrientation;
  const button = $("#toggle-site-orientation");
  if (!button) return;
  const vertical = siteMapOrientation === "vertical";
  const label = vertical ? "切换为横向排布" : "切换为纵向排布";
  button.setAttribute("aria-pressed", String(vertical));
  button.setAttribute("aria-label", label);
  button.textContent = label;
}
function bindSiteLogoFallback(scope = document) {
  scope.querySelectorAll("[data-site-logo] img").forEach((image) => {
    const fallback = () => image.closest("[data-site-logo]")?.classList.add("image-failed");
    image.addEventListener("error", fallback, {once: true});
    if (image.complete && image.naturalWidth === 0) fallback();
  });
}
function renderSites() {
  const list = siteCache.filter(matchesSiteFilter);
  if (list.length && !list.some((site) => site.id === selectedSiteId)) selectedSiteId = list[0].id;
  if (!list.length) selectedSiteId = null;
  const normalCount = list.filter((site) => siteConnectionState(site) === "normal").length;
  const slowCount = list.filter((site) => siteConnectionState(site) === "slow").length;
  const failedCount = list.filter((site) => siteConnectionState(site) === "failed").length;
  const unknownCount = list.length - normalCount - slowCount - failedCount;
  const mobileInspector = window.matchMedia("(max-width: 900px)").matches;
  const nodeButtonAttributes = (selected) => mobileInspector
    ? `aria-haspopup="dialog" aria-controls="site-inspector-panel" aria-expanded="${selected ? "true" : "false"}"`
    : `aria-pressed="${selected ? "true" : "false"}"`;
  const nodeHtml = (site) => {
    const icon = site.icon_endpoint || site.icon_url || `${site.base_url.replace(/\/$/, "")}/favicon.ico`;
    const state = siteConnectionState(site);
    const [left, top] = siteNodePosition(site.id);
    const selected = selectedSiteId === site.id;
    const stateLabel = siteStateLabel(state);
    return `<button class="site-star-node ${state}${selected ? " selected" : ""}" type="button" data-open-site="${site.id}" ${nodeButtonAttributes(selected)} aria-label="查看 ${escapeHtml(site.name)}，${stateLabel}" title="${escapeHtml(site.name)} · ${stateLabel}" style="--node-left:${left}%;--node-top:${top}%;--node-delay:${list.indexOf(site) * 35}ms"><span class="site-node-core site-logo" data-site-logo><img src="${escapeHtml(icon)}" alt=""><b>${escapeHtml(siteMonogram(site.name))}</b></span><strong class="site-node-name">${escapeHtml(site.name)}</strong><span class="site-node-state">${stateLabel}</span></button>`;
  };
  $("#sites-list").innerHTML = list.length ? `<header class="site-map-heading"><div><p class="section-kicker">LIVE SOURCE NETWORK</p><h2>${list.length} 个来源在片源网络中</h2><p>滚轮或双指缩放，拖动画布平移；点击节点进入来源档案。按住 Alt 再拖动节点可重新排布。</p></div><span class="site-map-updated" role="status" aria-live="polite">${normalCount} 正常 · ${slowCount} 缓慢 · ${failedCount} 失败 · ${unknownCount} 未知</span></header><div class="site-map-field" role="region" aria-label="片源网络" tabindex="0"><div class="site-map-viewport"><div class="site-map-canvas"><span class="site-map-orbit orbit-a" aria-hidden="true"></span><span class="site-map-orbit orbit-b" aria-hidden="true"></span><span class="site-map-link link-a" aria-hidden="true"></span><span class="site-map-link link-b" aria-hidden="true"></span><span class="site-map-link link-c" aria-hidden="true"></span>${list.map(nodeHtml).join("")}</div></div><div class="site-map-zoom-tools" role="group" aria-label="地图缩放"><button type="button" data-site-map-zoom="out" aria-label="缩小地图" title="缩小地图">−</button><output class="site-map-zoom-value" aria-live="polite">100%</output><button type="button" data-site-map-zoom="in" aria-label="放大地图" title="放大地图">＋</button><button type="button" data-site-map-zoom="reset" aria-label="重置地图视野" title="重置地图视野">重置</button></div><div class="site-constellation-legend" role="group" aria-label="站点状态图例"><span><i class="normal"></i>正常连接</span><span><i class="slow"></i>连接缓慢</span><span><i class="failed"></i>连接失败</span><span><i class="unknown"></i>未知</span></div></div><details class="site-linear-list"><summary>以线性列表查看全部来源</summary><div class="site-linear-list-items">${list.map((site) => `<button type="button" data-open-site="${site.id}" aria-label="查看 ${escapeHtml(site.name)}，${siteStateLabel(siteConnectionState(site))}"><span class="site-linear-name">${escapeHtml(site.name)}</span><span class="site-linear-state ${siteConnectionState(site)}">${siteStateLabel(siteConnectionState(site))}</span><small>${site.enabled ? "已启用" : "已停用"} · ${site.search_enabled ? "参与搜索" : "不参与搜索"}</small></button>`).join("")}</div></details>` : "<div class='empty-state compact'><strong>没有符合条件的站点</strong><p>切换筛选条件或添加新的站点来源。</p></div>";
  syncSiteMapCopy();
  $("#sites-list .site-linear-list summary")?.replaceChildren(document.createTextNode("打开线性来源目录（键盘可用）"));
  const linearList = $("#sites-list .site-linear-list");
  if (linearList) linearList.open = (themeController?.getTheme?.() || document.documentElement.dataset.theme) === "ledger";
  syncSiteMapOrientationControls();
  syncSiteMapViewport();
  renderSiteInspector(siteCache.find((site) => site.id === selectedSiteId));
  bindSiteLogoFallback($("#sites-list"));
}
function renderSiteInspector(site) {
  if (!site) {
    $("#site-inspector-content").innerHTML = "<div class='empty-state compact'><strong>没有符合条件的站点</strong><p>切换筛选条件或添加新的站点来源。</p></div>";
    return;
  }
  const stats = site.local_stats || {}, account = site.account_stats || {}, state = siteConnectionState(site);
  const icon = site.icon_endpoint || site.icon_url || `${site.base_url.replace(/\/$/, "")}/favicon.ico`;
  $("#site-inspector-content").innerHTML = `<header class="site-detail-head"><span class="site-logo" data-site-logo><img src="${escapeHtml(icon)}" alt=""><b>${escapeHtml(siteMonogram(site.name))}</b></span><div><strong>${escapeHtml(site.name)}</strong><a class="site-detail-url" href="${escapeHtml(safeExternalUrl(site.base_url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(site.base_url)}</a></div><em class="site-state ${state}">${state === "normal" ? "连接正常" : state === "slow" ? "连接缓慢" : state === "failed" ? "连接失败" : "连接未知"}</em><button class="icon-button site-inspector-close" data-close-site-inspector aria-label="关闭">×</button></header>
    <div class="site-detail-stats"><div><span>上传量</span><strong>${account.uploaded == null ? "—" : formatSize(account.uploaded)}</strong></div><div><span>下载量</span><strong>${account.downloaded == null ? "—" : formatSize(account.downloaded)}</strong></div><div><span>分享率</span><strong>${account.ratio == null ? "—" : Number(account.ratio).toFixed(2)}</strong></div><div><span>做种数</span><strong>${account.seeding == null ? "—" : Number(account.seeding).toLocaleString()}</strong></div></div>
    <dl class="site-detail-list"><dt>参与资源搜索</dt><dd>${site.search_enabled ? "是" : "否"}</dd><dt>本地 Cookie</dt><dd>${site.cookie_configured ? "已配置" : "未配置"}</dd><dt>User-Agent</dt><dd class="site-ua-value">${escapeHtml(site.user_agent || "AutoList 默认 UA")}</dd><dt>最近连接检测</dt><dd>${site.last_tested_at ? `${formatTime(site.last_tested_at)}${site.last_duration_ms == null ? "" : ` · ${Number(site.last_duration_ms)}ms`}` : "尚未检测"}</dd><dt>账户统计更新</dt><dd>${escapeHtml(account.checked_at ? formatTime(account.checked_at) : "等待后台刷新")}${account.error ? ` · ${escapeHtml(account.error)}` : ""}</dd><dt>最近搜索</dt><dd>${escapeHtml(stats.last_attempt_at || "暂无记录")}</dd><dt>站点搜索表现</dt><dd id="site-health-summary">${stats.total ? `${Number(stats.success_rate || 0).toFixed(1)}% 成功 · ${Number(stats.average_ms || 0)}ms · ${Number(stats.total)} 次` : "等待搜索样本"}</dd></dl>
    <div class="site-detail-actions"><button class="button button-secondary" data-test-site="${site.id}">检测站点</button><button class="button button-secondary" data-refresh-site-cookie="${site.id}">刷新 Cookie</button><button class="button button-secondary" data-edit-site="${site.id}">编辑站点 / UA</button><button class="button button-danger" data-delete-site="${site.id}">删除站点</button></div>
`;
  $("#site-inspector-content").querySelector(".site-detail-head strong")?.setAttribute("id", "site-inspector-title");
  bindSiteLogoFallback($("#site-inspector-content"));
}
const siteInspectorFocusableSelector = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
let siteInspectorReturnFocus = null;
let siteNodeDragState = null;
let siteMapPanState = null;
const siteMapPointers = new Map();
let siteMapPinchState = null;
function siteMapPositionFromPointer(event, node) {
  const field = node.closest(".site-map-field");
  if (!field) return null;
  const rect = field.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  // 节点位于经过 scale + translate 的画布内；拖动时先把屏幕坐标还原到
  // 画布坐标，否则在放大或平移后拖动节点会产生明显的偏移。
  const scale = Math.max(siteMapZoomMin, Number(siteMapViewport.scale) || 1);
  const localX = (event.clientX - rect.left - rect.width / 2 - siteMapViewport.x) / scale + rect.width / 2;
  const localY = (event.clientY - rect.top - rect.height / 2 - siteMapViewport.y) / scale + rect.height / 2;
  return {
    left: Math.max(8, Math.min(92, (localX / rect.width) * 100)),
    top: Math.max(12, Math.min(88, (localY / rect.height) * 100)),
  };
}
function finishSiteNodeDrag(event) {
  if (!siteNodeDragState) return;
  if (event?.pointerId != null && event.pointerId !== siteNodeDragState.pointerId) return;
  const state = siteNodeDragState;
  siteNodeDragState = null;
  const {node, pointerId, captureTarget} = state;
  node.classList.remove("dragging");
  try { captureTarget?.releasePointerCapture?.(pointerId); } catch (_error) { /* 指针捕获可能已在取消事件中释放 */ }
  $("#sites-list")?.classList.remove("is-node-dragging");
  if (state.moved) {
    persistSiteNodePositions();
    node.dataset.suppressSiteOpen = "true";
    window.setTimeout(() => {
      if (node.isConnected) delete node.dataset.suppressSiteOpen;
    }, 0);
    showToast("来源节点已重新排布，可用“恢复节点排布”撤销");
  }
}
function isPrimarySitePointer(event) {
  return event.isPrimary !== false && (event.button === 0 || event.pointerType === "touch" || event.pointerType === "pen");
}
function beginSiteNodeDrag(node, event, captureTarget = node) {
  if (!node || !isPrimarySitePointer(event)) return false;
  siteNodeDragState = {
    node,
    field: node.closest(".site-map-field"),
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    moved: false,
    captureTarget,
  };
  node.classList.add("dragging");
  $("#sites-list")?.classList.add("is-node-dragging");
  captureTarget.setPointerCapture?.(event.pointerId);
  event.preventDefault();
  return true;
}
function beginSiteMapPan(field, event) {
  if (!field || !isPrimarySitePointer(event)) return false;
  siteMapPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  siteMapPanState = {
    field,
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: siteMapViewport.x,
    originY: siteMapViewport.y,
    moved: false,
  };
  field.classList.add("is-panning", "is-gesturing");
  $("#sites-list")?.classList.add("is-panning");
  field.setPointerCapture?.(event.pointerId);
  event.preventDefault();
  return true;
}
function siteMapPointerDistance(points) {
  if (points.length < 2) return 0;
  return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
}
function siteMapPointerMidpoint(points) {
  return {x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
}
function finishSiteMapGesture(event) {
  const pointerId = event?.pointerId;
  if (siteNodeDragState && (pointerId == null || pointerId === siteNodeDragState.pointerId)) finishSiteNodeDrag(event);
  if (pointerId != null) siteMapPointers.delete(pointerId);
  if (siteMapPinchState && siteMapPointers.size < 2) {
    const pinchField = siteMapPinchState.field;
    siteMapPinchState = null;
    const remaining = [...siteMapPointers.entries()][0];
    if (remaining) {
      pinchField?.classList.add("is-panning");
      $("#sites-list")?.classList.add("is-panning");
      siteMapPanState = {
        field: pinchField || $("#sites-list .site-map-field"),
        pointerId: remaining[0],
        startX: remaining[1].x,
        startY: remaining[1].y,
        originX: siteMapViewport.x,
        originY: siteMapViewport.y,
        moved: false,
      };
    }
  }
  if (siteMapPanState && (pointerId == null || pointerId === siteMapPanState.pointerId) && siteMapPointers.size === 0) {
    try { siteMapPanState.field?.releasePointerCapture?.(siteMapPanState.pointerId); } catch (_error) { /* 指针捕获可能已释放 */ }
    siteMapPanState = null;
  }
  if (siteMapPointers.size < 2) siteMapPinchState = null;
  if (!siteMapPointers.size) {
    const field = $("#sites-list .site-map-field");
    field?.classList.remove("is-panning", "is-gesturing");
    $("#sites-list")?.classList.remove("is-panning");
    persistSiteMapViewport();
  }
}
$("#sites-list")?.addEventListener("pointerdown", (event) => {
  const node = event.target.closest(".site-star-node");
  if (node) {
    if (event.altKey || event.metaKey) beginSiteNodeDrag(node, event);
    return;
  }
  const field = event.target.closest(".site-map-field");
  const isTouchPointer = event.pointerType === "touch";
  if (!field || event.target.closest(".site-constellation-legend, .site-map-zoom-tools") || (!isPrimarySitePointer(event) && !isTouchPointer)) return;
  if (event.pointerType === "touch" && siteMapPointers.size === 1) {
    siteMapPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    const points = [...siteMapPointers.values()];
    const distance = siteMapPointerDistance(points);
    if (!distance) return;
    siteMapPanState = null;
    siteMapPinchState = {field, initialDistance: distance, initialScale: siteMapViewport.scale, initialX: siteMapViewport.x, initialY: siteMapViewport.y};
    field.classList.add("is-gesturing");
    try { field.setPointerCapture?.(event.pointerId); } catch (_error) { /* 触控设备可能不支持捕获 */ }
    event.preventDefault();
    return;
  }
  beginSiteMapPan(field, event);
});
$("#sites-list")?.addEventListener("pointermove", (event) => {
  if (siteNodeDragState && event.pointerId === siteNodeDragState.pointerId) {
    const distance = Math.hypot(event.clientX - siteNodeDragState.startX, event.clientY - siteNodeDragState.startY);
    if (distance < 5 && !siteNodeDragState.moved) return;
    const position = siteMapPositionFromPointer(event, siteNodeDragState.node);
    if (!position) return;
    siteNodeDragState.moved = true;
    setSiteNodePosition(siteNodeDragState.node.dataset.openSite, position.left, position.top);
    siteNodeDragState.node.style.setProperty("--node-left", String(position.left) + "%");
    siteNodeDragState.node.style.setProperty("--node-top", String(position.top) + "%");
    event.preventDefault();
    return;
  }
  if (siteMapPointers.has(event.pointerId)) siteMapPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (siteMapPinchState && siteMapPointers.size >= 2) {
    const field = siteMapPinchState.field;
    const points = [...siteMapPointers.values()];
    const rect = field?.getBoundingClientRect?.();
    const distance = siteMapPointerDistance(points);
    if (!rect?.width || !rect.height || !distance) return;
    const midpoint = siteMapPointerMidpoint(points);
    const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, siteMapPinchState.initialScale * distance / siteMapPinchState.initialDistance));
    siteMapViewport = clampSiteMapViewport({
      scale,
      x: siteMapPinchState.initialX + (siteMapPinchState.initialScale - scale) * (midpoint.x - rect.left - rect.width / 2),
      y: siteMapPinchState.initialY + (siteMapPinchState.initialScale - scale) * (midpoint.y - rect.top - rect.height / 2),
    }, field);
    syncSiteMapViewport();
    event.preventDefault();
    return;
  }
  if (!siteMapPanState || event.pointerId !== siteMapPanState.pointerId) return;
  const distance = Math.hypot(event.clientX - siteMapPanState.startX, event.clientY - siteMapPanState.startY);
  if (distance < 3 && !siteMapPanState.moved) return;
  siteMapPanState.moved = true;
  siteMapViewport = clampSiteMapViewport({
    scale: siteMapViewport.scale,
    x: siteMapPanState.originX + event.clientX - siteMapPanState.startX,
    y: siteMapPanState.originY + event.clientY - siteMapPanState.startY,
  }, siteMapPanState.field);
  syncSiteMapViewport();
  event.preventDefault();
});
$("#sites-list")?.addEventListener("wheel", (event) => {
  const field = event.target.closest(".site-map-field");
  if (!field) return;
  event.preventDefault();
  zoomSiteMap(siteMapViewport.scale + (event.deltaY < 0 ? .12 : -.12), event.clientX, event.clientY);
}, {passive: false});
$("#sites-list")?.addEventListener("keydown", (event) => {
  const field = event.target.closest(".site-map-field");
  if (!field || event.target !== field) return;
  if (["+", "=", "PageUp"].includes(event.key)) { event.preventDefault(); zoomSiteMap(siteMapViewport.scale + .12); }
  else if (["-", "_", "PageDown"].includes(event.key)) { event.preventDefault(); zoomSiteMap(siteMapViewport.scale - .12); }
  else if (event.key === "0" || event.key === "Home") { event.preventDefault(); resetSiteMapViewport(); }
  else if (event.key === "ArrowLeft") { event.preventDefault(); siteMapViewport = clampSiteMapViewport({...siteMapViewport, x: siteMapViewport.x + 32}, field); syncSiteMapViewport(); persistSiteMapViewport(); }
  else if (event.key === "ArrowRight") { event.preventDefault(); siteMapViewport = clampSiteMapViewport({...siteMapViewport, x: siteMapViewport.x - 32}, field); syncSiteMapViewport(); persistSiteMapViewport(); }
  else if (event.key === "ArrowUp") { event.preventDefault(); siteMapViewport = clampSiteMapViewport({...siteMapViewport, y: siteMapViewport.y + 32}, field); syncSiteMapViewport(); persistSiteMapViewport(); }
  else if (event.key === "ArrowDown") { event.preventDefault(); siteMapViewport = clampSiteMapViewport({...siteMapViewport, y: siteMapViewport.y - 32}, field); syncSiteMapViewport(); persistSiteMapViewport(); }
});
$("#sites-list")?.addEventListener("pointerup", finishSiteMapGesture);
$("#sites-list")?.addEventListener("pointercancel", finishSiteMapGesture);
$("#sites-list")?.addEventListener("lostpointercapture", finishSiteMapGesture);
function syncSiteInspectorMode() {
  const panel = $("#site-inspector-panel");
  if (!panel) return;
  const mobile = window.matchMedia("(max-width: 900px)").matches;
  if (mobile) {
    panel.setAttribute("aria-hidden", String(!panel.classList.contains("open")));
    if (panel.classList.contains("open")) panel.removeAttribute("inert");
    else panel.setAttribute("inert", "");
  } else {
    siteInspectorReturnFocus?.setAttribute("aria-expanded", "false");
    siteInspectorReturnFocus = null;
    document.body.classList.remove("site-inspector-open");
    panel.classList.remove("open");
    panel.setAttribute("role", "complementary");
    panel.setAttribute("aria-hidden", "false");
    panel.removeAttribute("aria-modal");
    panel.removeAttribute("aria-labelledby");
    panel.removeAttribute("inert");
  }
}
function openSiteInspector(trigger) {
  const panel = $("#site-inspector-panel");
  if (!panel || !window.matchMedia("(max-width: 900px)").matches) {
    trigger?.setAttribute("aria-expanded", "false");
    syncSiteInspectorMode();
    return;
  }
  siteInspectorReturnFocus = trigger || document.activeElement;
  document.body.classList.add("site-inspector-open");
  panel.classList.add("open");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "site-inspector-title");
  panel.setAttribute("aria-hidden", "false");
  panel.removeAttribute("inert");
  trigger?.setAttribute("aria-expanded", "true");
  requestAnimationFrame(() => panel.querySelector(siteInspectorFocusableSelector)?.focus({preventScroll: true}));
}
function closeSiteInspector(restoreFocus = true) {
  const panel = $("#site-inspector-panel");
  if (!panel) return;
  const returnFocus = siteInspectorReturnFocus;
  returnFocus?.setAttribute("aria-expanded", "false");
  document.body.classList.remove("site-inspector-open");
  panel.classList.remove("open");
  panel.setAttribute("role", "complementary");
  panel.removeAttribute("aria-modal");
  panel.removeAttribute("aria-labelledby");
  const mobile = window.matchMedia("(max-width: 900px)").matches;
  panel.setAttribute("aria-hidden", mobile ? "true" : "false");
  if (mobile) panel.setAttribute("inert", "");
  siteInspectorReturnFocus = null;
  if (restoreFocus && returnFocus?.isConnected) requestAnimationFrame(() => returnFocus.focus({preventScroll: true}));
}
window.addEventListener("resize", syncSiteInspectorMode);
syncSiteInspectorMode();
$("#site-inspector-backdrop")?.addEventListener("click", () => closeSiteInspector());

async function loadSiteHealth(siteId) {
  const data = await api(`/api/sites/${siteId}/health-history?limit=30`);
  if (selectedSiteId !== siteId || !$("#site-health-summary")) return;
  const summary = data.summary || {};
  $("#site-health-summary").textContent = summary.total ? `${Number(summary.success_rate || 0).toFixed(1)}% 成功 · ${Number(summary.average_ms || 0)}ms · ${Number(summary.total)} 次` : "等待搜索样本";
}

function openSiteDialog(site = null) {
  $("#site-dialog-title").textContent = site ? "编辑站点" : "添加站点";
  $("#site-id").value = site?.id || "";
  $("#site-name").value = site?.name || "";
  $("#site-url").value = site?.base_url || "";
  $("#site-api-key").value = "";
  $("#site-api-key").placeholder = site?.api_key_configured ? "已配置；填写新值可替换" : "可选";
  $("#site-cookie").value = "";
  $("#site-cookie").placeholder = site?.cookie_configured ? "已配置；填写新值可替换" : "NexusPHP 站点请求头 Cookie";
  $("#site-user-agent").value = site?.user_agent || "";
  $("#site-priority").value = site?.priority || 100;
  $("#site-timeout").value = site?.timeout_seconds || 30;
  $("#site-rss-url").value = "";
  $("#site-rss-url").placeholder = site?.rss_url_configured ? "已配置；填写新地址可替换" : "可选";
  $("#site-icon-url").value = site?.icon_url || "";
  $("#site-enabled").checked = site?.enabled ?? true;
  $("#site-search-enabled").checked = Boolean(site?.search_enabled);
  $("#site-proxy").checked = Boolean(site?.proxy);
  $("#site-render").checked = Boolean(site?.render);
  $("#site-clear-fields").hidden = !site;
  $("#site-clear-rss-wrap").hidden = !site;
  $("#site-clear-api-key").checked = false;
  $("#site-clear-cookie").checked = false;
  $("#site-clear-rss-url").checked = false;
  $("#site-result").textContent = site ? `${site.api_key_configured ? "API 已配置" : "API 未配置"} · ${site.cookie_configured ? "Cookie 已配置" : "Cookie 未配置"}` : "";
  showDialog($("#site-dialog"));
}

function activateImportMode(button, focus = false) {
  if (!button) return;
  importMode = button.dataset.importMode;
  $$("[data-import-mode]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.tabIndex = active ? 0 : -1;
  });
  $$("[data-import-panel]").forEach((panel) => { panel.hidden = panel.dataset.importPanel !== importMode; });
  $("#import-preview").hidden = true;
  if (focus) button.focus();
}

$$("[data-import-mode]").forEach((button) => {
  button.addEventListener("keydown", (event) => {
    if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = $$("[data-import-mode]");
    const current = tabs.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
      : (current + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
    activateImportMode(tabs[next], true);
  });
});

async function handleDocumentClick(event) {
  const importModeButton = event.target.closest("[data-import-mode]");
  if (importModeButton) {
    activateImportMode(importModeButton);
    return;
  }
  const routeTarget = event.target.closest("[data-route-target], [data-route]");
  if (routeTarget) {
    event.preventDefault();
    navigate(routeTarget.dataset.routeTarget || routeTarget.dataset.route);
    return;
  }
  const importTarget = event.target.closest("[data-open-import]");
  if (importTarget) {
    showDialog($("#import-dialog"), importTarget);
    return;
  }
  const renamePlaylist = event.target.closest("[data-rename-playlist]");
  if (renamePlaylist) {
    const playlist = playlistCache.find((item) => item.id === Number(renamePlaylist.dataset.renamePlaylist));
    const name = window.prompt("输入新的片单名称", playlist?.name || "");
    if (!name?.trim()) return;
    setButtonLoading(renamePlaylist, true, "保存中…");
    try {
      await api(`/api/playlists/${renamePlaylist.dataset.renamePlaylist}`, {method: "PUT", body: JSON.stringify({name: name.trim()})});
      await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
      showToast("片单名称已更新");
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(renamePlaylist, false);
    }
    return;
  }
  const refreshPlaylistSource = event.target.closest("[data-refresh-playlist-source]");
  if (refreshPlaylistSource) {
    setButtonLoading(refreshPlaylistSource, true, "刷新中…");
    try {
      const result = await api(`/api/playlists/${refreshPlaylistSource.dataset.refreshPlaylistSource}/refresh-source`, {method: "POST"});
      await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
      showToast(result.message);
    } catch (error) { showToast(error.message); } finally { setButtonLoading(refreshPlaylistSource, false); }
    return;
  }
  const syncPlaylist = event.target.closest("[data-sync-playlist]");
  if (syncPlaylist) {
    setButtonLoading(syncPlaylist, true, "同步中…");
    try {
      const result = await api(`/api/playlists/${syncPlaylist.dataset.syncPlaylist}/sync-now`, {method: "POST"});
      await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
      showToast(result.message);
    } catch (error) { showToast(error.message); } finally { setButtonLoading(syncPlaylist, false); }
    return;
  }
  const toggleSync = event.target.closest("[data-toggle-sync]");
  if (toggleSync) {
    const playlist = playlistCache.find((item) => item.id === Number(toggleSync.dataset.toggleSync));
    if (!playlist) return;
    setButtonLoading(toggleSync, true, "保存中…");
    try {
      await api(`/api/playlists/${playlist.id}/sync-settings`, {method: "PUT", body: JSON.stringify({enabled: !playlist.sync_enabled, interval_hours: Number(playlist.sync_interval_hours || 24)})});
      await loadPlaylists(currentPage === "playlists");
      showToast(playlist.sync_enabled ? "已停用定时同步" : "已启用每 24 小时增量同步");
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(toggleSync, false);
    }
    return;
  }
  const orderPlaylist = event.target.closest("[data-order-playlist]");
  if (orderPlaylist) {
    const ids = playlistCache.map((item) => item.id);
    const index = ids.indexOf(Number(orderPlaylist.dataset.orderPlaylist));
    const target = orderPlaylist.dataset.direction === "up" ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setButtonLoading(orderPlaylist, true, "保存中…");
    try {
      await api("/api/playlists/reorder", {method: "POST", body: JSON.stringify({ids})});
      await loadPlaylists(currentPage === "playlists");
      showToast("片单顺序已更新");
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(orderPlaylist, false);
    }
    return;
  }
  const playlistCard = event.target.closest("[data-playlist-id]");
  if (playlistCard) {
    selectedPlaylistId = Number(playlistCard.dataset.playlistId);
    expandedPlaylistId = expandedPlaylistId === selectedPlaylistId ? null : selectedPlaylistId;
    $$("[data-playlist-id]").forEach((item) => {
      const active = Number(item.dataset.playlistId) === expandedPlaylistId;
      item.closest(".playlist-card")?.classList.toggle("active", active);
      item.setAttribute("aria-expanded", String(active));
      const hint = item.querySelector("i");
      if (hint) hint.textContent = active ? "收起明细 ↑" : "展开明细 ↓";
    });
    $("#playlist-detail-panel").hidden = !expandedPlaylistId;
    if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId, $("#playlist-item-query").value);
    return;
  }
  const deleteSiteButton = event.target.closest("[data-delete-site]");
  const editSiteButton = event.target.closest("[data-edit-site]");
  const toggleSiteOrientation = event.target.closest("#toggle-site-orientation");
  const resetSiteLayout = event.target.closest("#reset-site-layout");
  const siteMapZoomButton = event.target.closest("[data-site-map-zoom]");
  if (siteMapZoomButton) {
    const action = siteMapZoomButton.dataset.siteMapZoom;
    if (action === "in") zoomSiteMap(siteMapViewport.scale + .12);
    else if (action === "out") zoomSiteMap(siteMapViewport.scale - .12);
    else if (action === "reset") resetSiteMapViewport();
    return;
  }
  if (toggleSiteOrientation) {
    siteMapOrientation = siteMapOrientation === "vertical" ? "horizontal" : "vertical";
    safeStorageSet(siteMapOrientationStorageKey, siteMapOrientation);
    renderSites();
    syncSiteMapOrientationControls();
    showToast(siteMapOrientation === "vertical" ? "来源地图已切换为纵向排布" : "来源地图已切换为横向排布");
    return;
  }
  if (resetSiteLayout) {
    resetSiteNodePositions();
    resetSiteMapViewport();
    renderSites();
    showToast("来源节点与地图视野已恢复默认");
    return;
  }
  if (editSiteButton) {
    closeSiteInspector(false);
    openSiteDialog(siteCache.find((site) => site.id === Number(editSiteButton.dataset.editSite)) || null);
    return;
  }
  const siteCard = event.target.closest("[data-open-site]");
  if (siteCard) {
    if (siteCard.dataset.suppressSiteOpen === "true") {
      delete siteCard.dataset.suppressSiteOpen;
      return;
    }
    selectedSiteId = Number(siteCard.dataset.openSite);
    renderSites();
    renderSiteInspector(siteCache.find((site) => site.id === selectedSiteId));
    loadSiteHealth(selectedSiteId).catch((error) => {
      if ($("#site-health-summary")) $("#site-health-summary").textContent = `统计读取失败：${error.message}`;
    });
    const currentSiteButton = [...($("#sites-list")?.querySelectorAll(".site-star-node") || [])].find((button) => Number(button.dataset.openSite) === selectedSiteId)
      || $$("[data-open-site]").find((button) => Number(button.dataset.openSite) === selectedSiteId);
    requestAnimationFrame(() => focusSiteNode(selectedSiteId));
    openSiteInspector(currentSiteButton);
    return;
  }
  if (event.target.closest("[data-close-site-inspector], #site-inspector-backdrop")) {
    closeSiteInspector();
    return;
  }
  const refreshSiteCookie = event.target.closest("[data-refresh-site-cookie]");
  if (refreshSiteCookie) {
    setButtonLoading(refreshSiteCookie, true, "刷新中…");
    try {
      const result = await api(`/api/sites/${refreshSiteCookie.dataset.refreshSiteCookie}/refresh-cookie`, {method: "POST"});
      showToast(result.message);
      await loadSites();
    } catch (error) { showToast(error.message); }
    finally { setButtonLoading(refreshSiteCookie, false); }
    return;
  }
  const testSiteButton = event.target.closest("[data-test-site]");
  if (testSiteButton) {
    setButtonLoading(testSiteButton, true, "检测中…");
    try {
      const result = await api(`/api/sites/${testSiteButton.dataset.testSite}/test`, {method: "POST"});
      showToast(result.message || (result.ok ? "站点连接正常" : "站点连接失败"));
      await loadSites();
    } catch (error) { showToast(error.message); }
    finally {
      setButtonLoading(testSiteButton, false);
    }
    return;
  }
  if (deleteSiteButton) {
    if (!window.confirm("确认删除这个站点配置？")) return;
    setButtonLoading(deleteSiteButton, true, "删除中…");
    try {
      await api(`/api/sites/${deleteSiteButton.dataset.deleteSite}`, {method: "DELETE"});
      closeSiteInspector(false);
      await loadSites();
      showToast("站点已删除");
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(deleteSiteButton, false);
    }
    return;
  }
  const candidateButton = event.target.closest("[data-candidate]");
  if (candidateButton) {
    setButtonLoading(candidateButton, true, "处理中…");
    try {
      await api(`/api/cart/items/${encodeURIComponent(candidateButton.dataset.candidate)}`, {method: "POST"});
      await Promise.all([refreshCandidates(false), refreshCart()]);
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(candidateButton, false);
    }
    return;
  }
  const taskAction = event.target.closest("[data-task-action]");
  if (taskAction) {
    setButtonLoading(taskAction, true, "启动中…");
    const action = taskAction.dataset.taskAction;
    try {
      await followupSearch(action, action === "retry" ? "重试" : "重新搜索整项");
    } finally {
      setButtonLoading(taskAction, false);
    }
    return;
  }
  const removeButton = event.target.closest("[data-cart-remove]");
  if (removeButton) {
    setButtonLoading(removeButton, true, "移除中…");
    try {
      await api(`/api/cart/items/${encodeURIComponent(removeButton.dataset.cartRemove)}`, {method: "POST"});
      await refreshCart();
      if (activeTask) await refreshCandidates(false);
      showToast("已从下载列表移除");
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonLoading(removeButton, false);
    }
  }
}

document.addEventListener("click", (event) => {
  handleDocumentClick(event).catch((error) => showToast(error.message || "操作失败，请稍后重试"));
});

document.addEventListener("keydown", (event) => {
  if (document.body.classList.contains("sidebar-open")) {
    const sidebar = $("#sidebar");
    if (event.key === "Escape") {
      event.preventDefault();
      setSidebarOpen(false);
      return;
    }
    if (event.key === "Tab" && sidebar) {
      const focusable = [...sidebar.querySelectorAll(sidebarFocusableSelector)];
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!sidebar.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
      return;
    }
  }
  if (document.body.classList.contains("site-inspector-open")) {
    const panel = $("#site-inspector-panel");
    if (event.key === "Escape") {
      event.preventDefault();
      closeSiteInspector();
      return;
    }
    if (event.key === "Tab" && panel) {
      const focusable = [...panel.querySelectorAll(siteInspectorFocusableSelector)];
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!panel.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
      return;
    }
  }
  if (event.key === "Escape") {
    const dialog = document.querySelector("dialog[open]");
    if (dialog) {
      event.preventDefault();
      dialog.close();
      return;
    }
  }
  if (event.target.closest?.("[data-open-site]")) return;
});

window.addEventListener("hashchange", () => {
  const requested = window.location.hash.slice(1);
  const target = pageMeta[requested] ? requested : "dashboard";
  if (requested !== target) history.replaceState(history.state, "", "#dashboard");
  const restoreScroll = !pendingHashNavigation && Number.isFinite(getRouteScroll(target));
  pendingHashNavigation = false;
  navigate(target, false, restoreScroll);
});

window.addEventListener("pagehide", () => rememberRouteScroll(currentPage), {capture: true});

function activateSettingsTab(name, focus = false) {
  const tabs = $$("[data-settings-tab]");
  tabs.forEach((tab) => {
    const active = tab.dataset.settingsTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  $$("[data-settings-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.settingsPanel !== name;
  });
}

$$("[data-settings-tab]").forEach((tab) => {
  tab.addEventListener("click", () => activateSettingsTab(tab.dataset.settingsTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = $$("[data-settings-tab]");
    const current = tabs.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
      : (current + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
    activateSettingsTab(tabs[next].dataset.settingsTab, true);
  });
});

$$('[data-theme-option]').forEach((option) => {
  option.addEventListener("click", () => selectTheme(option.dataset.themeOption));
});
$("#settings-scene-mode")?.addEventListener("change", (event) => setSceneMode(event.currentTarget.checked));
$("#settings-reduced-motion")?.addEventListener("change", (event) => setReducedMotion(event.currentTarget.checked));

$("#open-settings").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const dialog = $("#settings-dialog");
  const output = $("#settings-result");
  setButtonLoading(button, true, "读取中…");
  activateSettingsTab("recognition");
  showDialog(dialog, button);
  output.textContent = "正在读取设置…";
  output.className = "inline-message";
  try {
    await loadSettings();
    await testSettings();
  } catch (error) {
    output.textContent = error.message;
    output.className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#service-status").addEventListener("click", async () => {
  const button = $("#service-status");
  setButtonLoading(button, true, "检测中…");
  try {
    await loadConnection();
    showToast("服务状态已刷新");
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
    renderPlaylistItems();
  }
});

let sidebarReturnFocus = null;
const sidebarFocusableSelector = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const setSidebarOpen = (open, restoreFocus = true) => {
  const menuButton = $("#mobile-menu");
  const moreButton = $("#mobile-more");
  const sidebar = $("#sidebar");
  if (open) {
    if (document.activeElement === menuButton || document.activeElement === moreButton) sidebarReturnFocus = document.activeElement;
    document.body.classList.add("sidebar-open");
    menuButton?.setAttribute("aria-expanded", "true");
    menuButton?.setAttribute("aria-label", "关闭导航");
    moreButton?.setAttribute("aria-expanded", "true");
    moreButton?.setAttribute("aria-label", "关闭更多导航");
    requestAnimationFrame(() => sidebar?.querySelector(sidebarFocusableSelector)?.focus({preventScroll: true}));
    return;
  }
  document.body.classList.remove("sidebar-open");
  menuButton?.setAttribute("aria-expanded", "false");
  menuButton?.setAttribute("aria-label", "打开导航");
  moreButton?.setAttribute("aria-expanded", "false");
  const moreActive = ["rules", "history", "sites", "logs"].includes(currentPage);
  moreButton?.setAttribute("aria-label", moreActive ? `更多导航，当前页面：${pageMeta[currentPage].title}` : "打开更多导航");
  if (restoreFocus) {
    const target = document.activeElement === menuButton || document.activeElement === moreButton
      ? document.activeElement : sidebarReturnFocus;
    if (target && typeof target.focus === "function") target.focus({preventScroll: true});
  }
  sidebarReturnFocus = null;
};
$("#mobile-menu").addEventListener("click", () => setSidebarOpen(!document.body.classList.contains("sidebar-open")));
$("#mobile-more").addEventListener("click", () => setSidebarOpen(true));
$("#sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false));

$("#test-settings").addEventListener("click", async () => {
  const button = $("#test-settings");
  setButtonLoading(button, true, "正在检测…");
  try {
    await testSettings();
  } catch (error) {
    $("#settings-result").textContent = error.message;
    $("#settings-result").className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#save-settings").addEventListener("click", async () => {
  const button = $("#save-settings");
  const output = $("#settings-result");
  setButtonLoading(button, true, "正在保存…");
  try {
    await saveSettings();
    showToast("设置已保存并完成连接检测");
  } catch (error) {
    output.textContent = error.message;
    output.className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#settings-access-token-save")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "保存中…");
  try {
    setAccessToken($("#settings-access-token").value);
    showToast(getAccessToken() ? "本机访问令牌已保存" : "本机访问令牌已清空");
    await loadSettings();
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});

$("#settings-access-token-clear")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "清除中…");
  try {
    setAccessToken("");
    $("#settings-access-token").value = "";
    showToast("本机访问令牌已清除");
    await loadSettings();
  } catch (error) {
    /* server may require token again */
    $("#settings-access-token-status").textContent = "本机已清除";
    $("#settings-access-token-status").className = "";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#save-rules").addEventListener("click", async () => {
  const button = $("#save-rules");
  setButtonLoading(button, true, "正在保存…");
  try {
    await saveRules();
    showToast("候选规则已保存");
  } catch (error) {
    $("#rules-result").textContent = error.message;
    $("#rules-result").className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#run-score-preview").addEventListener("click", async () => {
  const output = $("#score-preview-result");
  const button = $("#run-score-preview");
  setButtonLoading(button, true, "解析中…");
  output.textContent = "正在解析…";
  try {
    const result = await api("/api/config/score-preview", {method: "POST", body: JSON.stringify({title: $("#rules-preview-title").value.trim(), seeders: Number($("#rules-preview-seeders").value || 0), candidate_policy: collectCandidatePolicy()})});
    const state = {preferred: "首选", fallback: "保底", excluded: "已排除"}[result.recommendation] || result.recommendation;
    output.className = "score-preview-result";
    output.innerHTML = `<strong>${escapeHtml(state)}${result.score ? ` · ${Number(result.score)} 匹配度` : ""}</strong><span>${escapeHtml(result.resolution)} · ${escapeHtml(result.codec)} · ${escapeHtml(result.group || "未知制作组")}</span><div>${(result.breakdown || []).map((item) => `<i>${escapeHtml(item.label)}</i>`).join("")}</div>${result.exclusion_reason ? `<small class="preview-exclusion">${escapeHtml(result.exclusion_reason)}</small>` : ""}`;
  } catch (error) { output.textContent = error.message; output.className = "score-preview-result error"; }
  finally { setButtonLoading(button, false); }
});

$("#open-site-dialog").addEventListener("click", () => openSiteDialog());
$("#test-all-sites").addEventListener("click", async () => {
  const button = $("#test-all-sites");
  setButtonLoading(button, true, "检测中…");
  try {
    const result = await api("/api/sites/test", {method: "POST"});
    await loadSites();
    showToast(`检测完成：${result.ok}/${result.total} 个站点正常`);
  } catch (error) { showToast(error.message); } finally { setButtonLoading(button, false); }
});
function activateSiteFilter(button, focus = false) {
  siteFilter = button.dataset.siteFilter;
  $$('[data-site-filter]').forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.tabIndex = active ? 0 : -1;
  });
  $("#sites-list")?.setAttribute("aria-labelledby", button.id || "site-filter-all");
  renderSites();
  if (focus) button.focus();
}
$$('[data-site-filter]').forEach((button) => {
  button.addEventListener("click", () => activateSiteFilter(button));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = $$('[data-site-filter]');
    const current = tabs.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
      : (current + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
    activateSiteFilter(tabs[next], true);
  });
});
$("#save-site").addEventListener("click", async () => {
  const output = $("#site-result");
  const button = $("#save-site");
  setButtonLoading(button, true, "保存中…");
  try {
    const siteId = $("#site-id").value;
    const existing = siteCache.find((site) => site.id === Number(siteId));
    const payload = {
      name: $("#site-name").value.trim(),
      base_url: $("#site-url").value.trim(), api_key: $("#site-api-key").value.trim() || null,
      cookie: $("#site-cookie").value.trim() || null, user_agent: $("#site-user-agent").value.trim(),
      priority: Number($("#site-priority").value || 100), timeout_seconds: Number($("#site-timeout").value || 30),
      rss_url: $("#site-rss-url").value.trim(), icon_url: $("#site-icon-url").value.trim(),
      proxy: $("#site-proxy").checked, render: $("#site-render").checked, enabled: $("#site-enabled").checked, search_enabled: $("#site-search-enabled").checked,
      limit_interval: existing?.limit_interval || null, limit_count: existing?.limit_count || null,
      clear_api_key: Boolean(siteId && $("#site-clear-api-key").checked),
      clear_cookie: Boolean(siteId && $("#site-clear-cookie").checked),
      clear_rss_url: Boolean(siteId && $("#site-clear-rss-url").checked),
    };
    await api(siteId ? `/api/sites/${siteId}` : "/api/sites", {method: siteId ? "PUT" : "POST", body: JSON.stringify(payload)});
    $("#site-dialog").close();
    await loadSites();
    showToast(siteId ? "站点配置已更新" : "站点已添加");
  } catch (error) {
    output.textContent = error.message;
    output.className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#playlist-item-query").addEventListener("input", () => {
  playlistPage = 1;
  clearTimeout(playlistQueryTimer);
  playlistQueryTimer = setTimeout(() => loadPlaylistItems(expandedPlaylistId).catch((error) => showToast(error.message)), 250);
});
$("#playlist-library-filter").addEventListener("change", async () => {
  playlistLibraryFilter = $("#playlist-library-filter").value;
  playlistPage = 1;
  try {
    if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId);
  } catch (error) {
    showToast(error.message);
  }
});
async function pollLibraryScan(taskId) {
  libraryScanTaskId = taskId;
  let task;
  try {
    task = await api(`/api/library-scan-tasks/${taskId}`);
  } catch (error) {
    libraryScanFailures += 1;
    setPlaylistPollError(`Emby 状态读取失败（第 ${libraryScanFailures} 次）：${error.message}`, () => pollLibraryScan(taskId));
    const button = $("#refresh-library");
    if (button) button.disabled = libraryScanFailures < 10;
    if (libraryScanFailures >= 10) button?.removeAttribute("aria-busy");
    clearTimeout(libraryScanTimer);
    if (libraryScanFailures < 10) libraryScanTimer = setTimeout(() => pollLibraryScan(taskId).catch((pollError) => setPlaylistPollError(`Emby 状态读取失败：${pollError.message}`, () => pollLibraryScan(taskId))), 3000);
    return;
  }
  libraryScanFailures = 0;
  clearPlaylistPollError();
  const button = $("#refresh-library");
  button.textContent = ["queued", "running"].includes(task.status) ? `刷新中 ${task.completed}/${task.total}` : "刷新 Emby 状态";
  if (["queued", "running"].includes(task.status)) {
    button.disabled = true;
    clearTimeout(libraryScanTimer);
    libraryScanTimer = setTimeout(() => pollLibraryScan(taskId).catch((pollError) => setPlaylistPollError(`Emby 状态读取失败：${pollError.message}`, () => pollLibraryScan(taskId))), 1000);
    return;
  }
  libraryScanTaskId = null;
  button.disabled = !expandedPlaylistId;
  button.removeAttribute("aria-busy");
  try {
    if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId, $("#playlist-item-query").value);
  } catch (error) {
    setPlaylistPollError(`Emby 状态已完成，但明细读取失败：${error.message}`, () => loadPlaylistItems(expandedPlaylistId, $("#playlist-item-query").value));
    return;
  }
  showToast(task.status === "completed" ? `Emby 状态已刷新：已入库 ${task.in_library} · 待入库 ${Math.max(0, Number(task.total) - Number(task.in_library))}` : "Emby 状态刷新未完成");
}
$("#refresh-library").addEventListener("click", async () => {
  if (!expandedPlaylistId || libraryScanTaskId) return;
  const button = $("#refresh-library");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  libraryScanFailures = 0;
  clearPlaylistPollError();
  try {
    const task = await api(`/api/playlists/${expandedPlaylistId}/library-scan`, {method: "POST"});
    libraryScanTaskId = task.id;
    showToast(task.message);
    await pollLibraryScan(task.id);
  } catch (error) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    showToast(error.message);
  }
});
async function pollRecognition(taskId) {
  if (!taskId) {
    recognitionTaskId = null;
    setButtonLoading($("#recognize-playlist"), false);
    $("#recognition-progress").hidden = true;
    await loadPlaylists(true);
    return;
  }
  recognitionTaskId = taskId;
  let task;
  try {
    task = await api(`/api/recognition-tasks/${taskId}`);
  } catch (error) {
    recognitionPollFailures += 1;
    setPlaylistPollError(`TMDB 识别状态读取失败（第 ${recognitionPollFailures} 次）：${error.message}`, () => pollRecognition(taskId));
    clearTimeout(recognitionTimer);
    if (recognitionPollFailures < 10) recognitionTimer = setTimeout(() => pollRecognition(taskId).catch((pollError) => setPlaylistPollError(`TMDB 识别状态读取失败：${pollError.message}`, () => pollRecognition(taskId))), 3000);
    return;
  }
  recognitionPollFailures = 0;
  clearPlaylistPollError();
  const percent = task.total ? Math.round(task.completed / task.total * 100) : 100;
  $("#recognition-progress").hidden = false;
  $("#recognition-text").textContent = `TMDB 识别 ${task.completed}/${task.total} · 成功 ${task.matched}`;
  setProgressValue("#recognition-bar", percent);
  $("#recognition-progress-bar")?.setAttribute("aria-valuenow", String(percent));
  clearTimeout(recognitionTimer);
  if (["queued", "running"].includes(task.status)) {
    recognitionTimer = setTimeout(() => pollRecognition(taskId).catch((pollError) => setPlaylistPollError(`TMDB 识别状态读取失败：${pollError.message}`, () => pollRecognition(taskId))), 1200);
  } else {
    recognitionTaskId = null;
    try {
      await loadPlaylists(true);
    } catch (error) {
      setPlaylistPollError(`TMDB 识别已结束，但片单刷新失败：${error.message}`, () => loadPlaylists(true));
      return;
    }
    setButtonLoading($("#recognize-playlist"), false);
    showToast(task.status === "completed" ? "TMDB 识别完成" : `TMDB 识别${task.status === "partial" ? "部分完成" : "失败"}`);
  }
}
$("#recognize-playlist").addEventListener("click", async () => {
  if (!expandedPlaylistId || recognitionTaskId) return;
  const button = $("#recognize-playlist");
  setButtonLoading(button, true, "识别中…");
  try {
    const result = await api(`/api/playlists/${expandedPlaylistId}/recognize`, {method: "POST"});
    if (!result.id) {
      showToast(result.message || "片单已全部识别");
      return;
    }
    await pollRecognition(result.id);
  } catch (error) {
    showToast(error.message);
  } finally {
    if (recognitionTaskId) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    } else {
      setButtonLoading(button, false);
    }
  }
});
$("#retry-playlist-task")?.addEventListener("click", async (event) => {
  const retry = playlistPollRetry;
  if (!retry) return;
  const button = event.currentTarget;
  setButtonLoading(button, true, "读取中…");
  try {
    clearPlaylistPollError();
    await retry();
  } catch (error) {
    setPlaylistPollError(`状态读取失败：${error.message}`, retry);
  } finally {
    setButtonLoading(button, false);
  }
});
$("#playlist-page-size").addEventListener("change", async () => {
  playlistPageSize = $("#playlist-page-size").value;
  safeStorageSet("autolist-playlist-page-size", playlistPageSize);
  playlistPage = 1;
  try {
    if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId);
  } catch (error) {
    showToast(error.message);
  }
});
$("#playlist-page-prev").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "读取中…");
  try {
    playlistPage = Math.max(1, playlistPage - 1);
    await loadPlaylistItems(expandedPlaylistId);
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
    renderPlaylistItems();
  }
});
$("#playlist-page-next").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "读取中…");
  try {
    playlistPage = Math.min(playlistPageCount, playlistPage + 1);
    await loadPlaylistItems(expandedPlaylistId);
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
    renderPlaylistItems();
  }
});

$("#delete-playlist").addEventListener("click", async () => {
  if (!selectedPlaylistId || !window.confirm("删除片单会同时删除它的搜索任务和候选，确认继续？")) return;
  const button = $("#delete-playlist");
  setButtonLoading(button, true, "删除中…");
  try {
    await api(`/api/playlists/${selectedPlaylistId}`, {method: "DELETE"});
    selectedPlaylistId = null;
    expandedPlaylistId = null;
    playlistItemsCache = [];
    await Promise.all([loadPlaylists(true), loadOverview()]);
    showToast("片单已删除");
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});

$("#open-import").addEventListener("click", (event) => showDialog($("#import-dialog"), event.currentTarget));
$("#file").addEventListener("change", () => {
  $("#file-label").textContent = $("#file").files[0]?.name || "选择片单文件";
});

async function buildImportPayload() {
  const payload = {name: $("#name").value.trim() || null, limit: Number($("#import-limit").value || 5000)};
  if (importMode === "url") {
    payload.source_url = $("#import-url").value.trim();
    if (!payload.source_url) throw new Error("请输入片单网址");
  } else if (importMode === "paste") {
    const content = $("#import-content").value.trim();
    if (!content) throw new Error("请粘贴 JSON 或 CSV 内容");
    if ($("#import-content-csv").checked) payload.csv_text = content;
    else payload.json_data = JSON.parse(content);
  } else {
    const file = $("#file").files[0];
    if (!file) throw new Error("请选择 XLSX、JSON 或 CSV 文件");
    const lower = file.name.toLowerCase();
    if (lower.endsWith(".json")) payload.json_data = JSON.parse(await file.text());
    else if (lower.endsWith(".csv")) payload.csv_text = await file.text();
    else if (lower.endsWith(".xlsx")) payload.xlsx_base64 = await fileToBase64(file);
    else throw new Error("仅支持 XLSX、JSON 或 CSV 文件");
  }
  return payload;
}

$("#preview-import").addEventListener("click", async () => {
  const button = $("#preview-import"), output = $("#import-result"), preview = $("#import-preview");
  setButtonLoading(button, true, "读取中…");
  try {
    const result = await api("/api/playlists/import/preview", {method: "POST", body: JSON.stringify(await buildImportPayload())});
    preview.hidden = false;
    preview.innerHTML = `<strong>${escapeHtml(result.name || "未命名片单")} · ${Number(result.count)} 部</strong><small>${escapeHtml(result.source_type || "文件")}</small><div>${(result.sample || []).map((item) => `<span>${Number(item.rank_no)}. ${escapeHtml(item.chinese_title || item.original_title)}${item.year ? ` (${Number(item.year)})` : ""}</span>`).join("")}</div>`;
    output.textContent = "预览完成，确认后才会写入片单。";
    output.className = "inline-message";
  } catch (error) { output.textContent = error.message; output.className = "inline-message error"; } finally { setButtonLoading(button, false); }
});

$("#import").addEventListener("click", async () => {
  const button = $("#import");
  const output = $("#import-result");
  setButtonLoading(button, true, "正在导入…");
  try {
    const payload = await buildImportPayload();
    const result = await api("/api/playlists/import", {method: "POST", body: JSON.stringify(payload)});
    output.textContent = `已导入 ${result.name}，共 ${result.count} 部`;
    output.className = "inline-message";
    await Promise.all([loadPlaylists(), loadOverview()]);
    showToast(`片单 ${result.name} 导入完成`);
    setTimeout(() => $("#import-dialog").close(), 700);
  } catch (error) {
    output.textContent = error.message;
    output.className = "inline-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

$("#playlist").addEventListener("change", async () => {
  currentSearchPlaylistId = Number($("#playlist").value) || null;
  await loadSearchableQueue().catch((error) => showToast(error.message));
});

$("#search-scope").addEventListener("change", updateSearchScope);

$("#run-playlist-completion").addEventListener("click", async () => {
  if (!currentSearchPlaylistId) return;
  const button = $("#run-playlist-completion");
  setButtonLoading(button, true, "开始识别和搜索…");
  try {
    const result = await api(`/api/playlists/${currentSearchPlaylistId}/automation/run`, {method: "POST"});
    showToast(result.message);
  } catch (error) { showToast(error.message); } finally { setButtonLoading(button, false); }
});

$("#toggle-playlist-auto").addEventListener("click", async () => {
  if (!currentSearchPlaylistId) return;
  const playlist = playlistCache.find((item) => item.id === currentSearchPlaylistId);
  if (!playlist) return;
  const button = $("#toggle-playlist-auto");
  setButtonLoading(button, true, "保存中…");
  try {
    await api(`/api/playlists/${currentSearchPlaylistId}/automation`, {method: "PUT", body: JSON.stringify({
      enabled: !playlist.automation_enabled, auto_cart: false, batch_size: Number(playlist.automation_batch_size || 50),
    })});
    await loadPlaylists();
    showToast(playlist.automation_enabled ? "已关闭新片自动搜索" : "已开启新片自动搜索");
  } catch (error) { showToast(error.message); } finally { setButtonLoading(button, false); }
});

$("#search").addEventListener("click", async () => {
  const button = $("#search");
  setButtonLoading(button, true, "创建任务…");
  try {
    const task = await api("/api/search-tasks", {method: "POST", body: JSON.stringify({
      playlist_id: Number($("#playlist").value),
      scope: $("#search-scope").value,
      count: Number($("#search-count").value),
      range_start: Number($("#start").value),
      range_end: Number($("#end").value),
    })});
    activeTask = task.id;
    candidateCache = [];
    candidateRenderSignature = "";
    renderCandidates();
    await refreshCandidates(true);
    showToast(`搜索任务 #${task.id} 已开始`);
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});

updateSearchScope();

$("#cancel-task").addEventListener("click", async () => {
  if (!activeTask || !["queued", "running"].includes(activeTaskState?.status)) return;
  const button = $("#cancel-task");
  setButtonLoading(button, true, "取消中…");
  try {
    await api(`/api/search-tasks/${activeTask}/cancel`, {method: "POST"});
    await refreshCandidates(false);
    showToast("搜索任务已取消");
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});

async function followupSearch(action, label, button = null) {
  if (!activeTask) return;
  if (button) setButtonLoading(button, true, "启动中…");
  try {
    const task = await api(`/api/search-tasks/${activeTask}/${action}`, {method: "POST"});
    activeTask = task.id;
    candidateCache = [];
    candidateRenderSignature = "";
    await refreshCandidates(true);
    showToast(`${label}任务 #${task.id} 已开始`);
  } catch (error) {
    showToast(error.message);
  } finally {
    if (button) setButtonLoading(button, false);
  }
}

$("#retry-task").addEventListener("click", (event) => followupSearch("retry", "重试", event.currentTarget));
$("#restart-task").addEventListener("click", (event) => followupSearch("restart", "重新搜索整项", event.currentTarget));
$("#retry-task-poll")?.addEventListener("click", async (event) => {
  if (!activeTask) return;
  const button = event.currentTarget;
  setButtonLoading(button, true, "读取中…");
  try {
    candidatePollFailures = 0;
    clearTaskPollError();
    await refreshCandidates(true);
  } catch (error) {
    setTaskPollError(`搜索状态读取失败：${error.message}`);
  } finally {
    setButtonLoading(button, false);
  }
});

$$(".filter-chip").forEach((button) => button.addEventListener("click", () => {
  currentFilter = button.dataset.filter;
  $$(".filter-chip").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  renderCandidates();
}));

$("#refresh-history").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "刷新中…");
  try {
    await refreshHistory();
    showToast("历史已刷新");
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});
$("#clear-history")?.addEventListener("click", async () => {
  const select = $("#history-status-filter");
  const status = select?.value || "all";
  const label = select?.selectedOptions?.[0]?.textContent || "全部状态";
  if (!window.confirm(`确定清除“${label}”中的下载历史吗？此操作不会删除下载任务或媒体文件。`)) return;
  const button = $("#clear-history");
  setButtonLoading(button, true, "清除中…");
  try {
    const result = await api(`/api/history?status=${encodeURIComponent(status)}`, {method: "DELETE"});
    await Promise.all([refreshHistory(), loadOverview()]);
    showToast(`已清除 ${Number(result.deleted || 0)} 条下载历史`);
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});
$("#history-status-filter")?.addEventListener("change", (event) => {
  historyStatusFilter = event.target.value || "all";
  renderHistoryTable();
});

$("#refresh-logs")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "刷新中…");
  try {
    await loadLogEvents();
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});
$("#clear-logs")?.addEventListener("click", async (event) => {
  if (!window.confirm("确定清空全部事件日志？此操作不可恢复。")) return;
  const button = event.currentTarget;
  setButtonLoading(button, true, "…");
  try {
    await api("/api/logs/events", {method: "DELETE"});
    await loadLogEvents();
    showToast("事件日志已清空");
  } catch (error) {
    showToast(error.message);
  } finally {
    setButtonLoading(button, false);
  }
});
$("#logs-level")?.addEventListener("change", () => loadLogEvents().catch((error) => showToast(error.message)));
$("#logs-query")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadLogEvents().catch((error) => showToast(error.message));
});


$("#download").addEventListener("click", (event) => {
  const available = cartCache.filter((item) => item.context_available);
  const total = available.reduce((sum, item) => sum + Number(item.size || 0), 0);
  const expired = cartCache.length - available.length;
  $("#confirm-copy").textContent = `${available.length} 个有效资源，预计 ${formatSize(total)}${expired ? `；另有 ${expired} 个资源需要重新搜索` : ""}。确认后将按现有分类规则开始下载。`;
  showDialog($("#confirm-dialog"), event.currentTarget);
});

$("#confirm-download").addEventListener("click", async (event) => {
  event.preventDefault();
  const button = $("#confirm-download");
  setButtonLoading(button, true, "正在提交…");
  try {
    const result = await api("/api/cart/download", {method: "POST"});
    const submitted = Number(result.submitted || 0);
    const needs = Number(result.needs_research || 0);
    const skipped = Number(Array.isArray(result.skipped) ? result.skipped.length : 0);
    const summary = submitted
      ? `已提交 ${submitted} 个资源给 MoviePilot；Transmission 负责下载，Emby 确认入库后会显示为已整理。`
      : "没有成功提交的资源。";
    const skipNote = skipped ? ` ${skipped} 个资源已提交过或正在下载/入库，已自动跳过。` : "";
    const extra = needs ? `另有 ${needs} 个因搜索上下文失效需重新搜索。` : "";
    $("#confirm-dialog").close();
    $("#cart-result").textContent = `${summary}${skipNote}${extra ? ` ${extra}` : ""}`;
    $("#cart-result").className = "inline-message cart-message";
    await Promise.all([refreshCart(), refreshHistory(), loadOverview()]);
    if (activeTask) await refreshCandidates(false);
    showToast(submitted ? `已提交 ${submitted} 个下载任务` : (extra || "提交完成"));
  } catch (error) {
    $("#cart-result").textContent = error.message;
    $("#cart-result").className = "inline-message cart-message error";
  } finally {
    setButtonLoading(button, false);
  }
});

(async () => {
  applyTheme(document.documentElement.dataset.theme);
  // 历史状态选项由 core.js 单一来源生成，避免与 HTML 硬编码重复维护。
  const historyFilterSelect = $("#history-status-filter");
  if (historyFilterSelect) {
    historyFilterSelect.innerHTML = HISTORY_STATUS_OPTIONS
      .map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`)
      .join("");
  }
  // 弹窗内 Enter 键不触发 method=dialog 的隐式提交（保存逻辑由按钮处理），
  // 避免未保存的输入被静默丢弃。
  $$("form[method='dialog']").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.submitter === null) event.preventDefault();
    });
  });
  const initialPage = window.location.hash.slice(1);
  const initialTarget = pageMeta[initialPage] ? initialPage : "dashboard";
  if (initialPage !== initialTarget) history.replaceState(history.state, "", "#dashboard");
  const initialScroll = getRouteScroll(initialTarget);
  navigate(initialTarget, false, Number.isFinite(initialScroll));
  try {
    const startup = [loadSettings(), refreshPageData(initialTarget)];
    if (initialTarget !== "dashboard") startup.push(loadConnection());
    if (!["dashboard", "cart"].includes(initialTarget)) startup.push(refreshCart());
    await Promise.all(startup);
    await recoverLatestTask();
  } catch (error) {
    showToast(error.message);
  } finally {
    booting = false;
    if (Number.isFinite(initialScroll)) requestAnimationFrame(() => window.scrollTo(0, initialScroll));
    if (!pageMeta[initialPage]) history.replaceState(history.state, "", "#dashboard");
  }
})();
