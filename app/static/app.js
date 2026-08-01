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
let recognitionTimer = null;
let recognitionPollFailures = 0;
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
let dashboardShelfSignature = "";

function applyTheme() {
  document.documentElement.dataset.theme = "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = "#f0f3f2";
}

const pageMeta = {
  dashboard: {eyebrow: "ARCHIVE / OVERVIEW", title: "电影库", lead: ""},
  playlists: {eyebrow: "LISTS / COLLECTIONS", title: "片单", lead: "查看影片与收藏进度。"},
  search: {eyebrow: "QUEUE / RESOURCE SEARCH", title: "资源搜索", lead: "搜索并挑选合适的版本。"},
  cart: {eyebrow: "QUEUE / DOWNLOAD LIST", title: "下载列表", lead: "确认即将下载的资源。"},
  rules: {eyebrow: "QUEUE / CANDIDATE POLICY", title: "候选规则", lead: "调整自动优选的评分方式。"},
  sites: {eyebrow: "MANAGEMENT / SITES & SERVICES", title: "站点与服务", lead: "管理 AutoList 搜索、Cookie 与站点健康状态。"},
  history: {eyebrow: "QUEUE / ACTIVITY", title: "下载历史", lead: "追踪提交、下载与入库状态。"},
};

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
  }
}

function navigate(page, updateHash = true, preserveScroll = false) {
  const target = pageMeta[page] ? page : "dashboard";
  const hashWillChange = updateHash && window.location.hash !== `#${target}`;
  const commit = () => {
    currentPage = target;
    document.body.dataset.page = target;
    $$(".app-page").forEach((section) => section.classList.toggle("active", section.dataset.page === target));
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.route === target;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    const meta = pageMeta[target];
    $("#page-eyebrow").textContent = meta.eyebrow;
    $("#page-title").textContent = meta.title;
    $("#page-lead").textContent = meta.lead;
    $("#page-lead").hidden = !meta.lead;
    $("#open-import").hidden = !["dashboard", "playlists", "search"].includes(target);
    document.title = `${meta.title} · AutoList`;
    setSidebarOpen(false, false);
  };
  // Let the hashchange handler perform the commit for link clicks so one
  // navigation cannot start two overlapping transitions.
  if (hashWillChange) {
    window.location.hash = target;
    return;
  }
  commit();
  if (!preserveScroll) window.scrollTo({top: 0, behavior: "auto"});
  if (!booting) requestAnimationFrame(() => $("#main-content")?.focus({preventScroll: true}));
  if (!booting && !hashWillChange) refreshPageData(target).catch((error) => showToast(error.message));
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
    $("#sidebar-status-label").textContent = status.ok ? "系统运行正常" : "部分服务异常";
    const rail = [["#rail-tmdb-status", providers.tmdb], ["#rail-tr-status", providers.transmission], ["#rail-emby-status", providers.emby], ["#rail-mp-status", providers.moviepilot]];
    rail.forEach(([selector, value]) => { const node = $(selector); if (node) node.textContent = value?.ok ? "连接正常" : value?.configured === false ? "未配置" : "连接失败"; });
  } catch (error) {
    topDot.className = "status-dot error";
    sidebarDot.className = "status-dot error";
    $("#top-service-label").textContent = "服务连接异常";
    $("#top-service-detail").textContent = "点击重新检测";
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
    $("#dashboard-task-bar").style.width = "0%";
    return;
  }
  const percent = task.total ? Math.round(task.completed / task.total * 100) : 0;
  pill.textContent = taskLabels[task.status] || task.status;
  pill.className = `pill ${taskPillClass(task.status)}`;
  $("#dashboard-task-title").textContent = `序号 ${task.range_start}–${task.range_end}`;
  $("#dashboard-task-copy").textContent = `${task.completed}/${task.total} 部已处理 · ${task.matched || 0} 个候选资源`;
  $("#dashboard-task-bar").style.width = `${percent}%`;
}

async function loadOverview() {
  const overview = await api("/api/overview");
  $("#metric-items").textContent = overview.item_count.toLocaleString("zh-CN");
  $("#metric-playlist").textContent = overview.playlist_name || "暂无片单";
  $("#dashboard-recognized").textContent = Number(overview.recognized_count || 0).toLocaleString("zh-CN");
  $("#dashboard-in-library").textContent = Number(overview.in_library_count || 0).toLocaleString("zh-CN");
  $("#dashboard-pending").textContent = Number(overview.pending_count || 0).toLocaleString("zh-CN");
  $("#dashboard-new-count").textContent = `${Number(overview.in_library_count || 0).toLocaleString("zh-CN")} 部电影`;
  $("#dashboard-history-count").textContent = Number(overview.history_count || 0).toLocaleString("zh-CN");
  const libraryPercent = Number(overview.item_count) ? Math.round(Number(overview.in_library_count || 0) / Number(overview.item_count) * 100) : 0;
  $("#dashboard-library-bar").style.width = `${Math.min(100, libraryPercent)}%`;
  const patterns = ["circle", "frame", "line", "circle", "line", "frame"];
  const accents = ["#c18b32", "#7898a4", "#bfd5cf", "#c9a36c", "#c7d7db", "#c3b27d"];
  const items = Array.isArray(overview.recent_items) ? overview.recent_items : [];
  const shelfSignature = JSON.stringify(items.map((item) => [item.id, item.poster_url, item.library_state, item.tmdb_title, item.tmdb_original_title, item.tmdb_year]));
  if (shelfSignature !== dashboardShelfSignature) {
    dashboardShelfSignature = shelfSignature;
    $("#dashboard-shelf").innerHTML = items.length ? items.map((item, index) => {
      const title = item.tmdb_title || item.chinese_title || item.tmdb_original_title || item.original_title || "未命名影片";
      const poster = item.poster_url
        ? `<img class="shelf-poster-image" src="${escapeHtml(item.poster_url)}" alt="${escapeHtml(title)} 海报" width="360" height="540" decoding="async" loading="${index < 2 ? "eager" : "lazy"}">`
        : "";
      return `<article class="shelf-item" aria-label="${escapeHtml(title)}"><div class="shelf-poster ${poster ? "has-image" : ""}" data-pattern="${patterns[index % patterns.length]}" style="--poster-accent:${accents[index % accents.length]}">${poster}</div></article>`;
    }).join("") : `<div class="empty-state compact"><strong>还没有影片</strong><p>导入影片后会在这里显示电影海报。</p></div>`;
    $("#dashboard-shelf").querySelectorAll(".shelf-poster-image").forEach((image) => {
      const frame = image.closest(".shelf-poster");
      const ready = () => frame?.classList.add("poster-ready");
      image.addEventListener("load", ready, {once: true});
      image.addEventListener("error", () => { frame?.classList.remove("has-image", "poster-ready"); image.remove(); }, {once: true});
      if (image.complete && image.naturalWidth) ready();
    });
  }
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
    $("#metric-playlist").textContent = lists[0].name;
    if (!$("#start").value) $("#start").value = 1;
    if (!$("#end").value) $("#end").value = Math.min(50, Number(lists[0].item_count));
  }
  // 片单默认保持折叠，只有用户主动点击卡片时才展开详情。
  $("#playlist-cards").innerHTML = lists.length
    ? lists.map((item, index) => `<article class="playlist-card ${item.id === expandedPlaylistId ? "active" : ""}">
        <button class="playlist-card-main" data-playlist-id="${item.id}" aria-expanded="${item.id === expandedPlaylistId}"><span>${escapeHtml(item.source_type || "片单")}</span><strong>${escapeHtml(item.name)}</strong><small>${Number(item.recognized_count || 0)}/${Number(item.item_count)} 已识别 · ${item.source_url ? `同步于 ${formatTime(item.last_synced_at)}` : `创建于 ${formatTime(item.created_at)}`}</small><i>${item.id === expandedPlaylistId ? "收起明细 ↑" : "展开明细 ↓"}</i></button>
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
  $("#download").disabled = available.length === 0;
  $("#download").textContent = expired ? `下载有效资源（${available.length}）` : "开始下载";
  $("#cart-list").innerHTML = cartCache.length
    ? cartCache.map((item) => `<article class="cart-item cart-item-page ${item.context_available ? "" : "cart-item-expired"}">
        <div class="cart-item-main"><span class="cart-item-icon">◇</span><div><strong>${escapeHtml(item.original_title)}</strong><small title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</small></div></div>
        <div class="cart-item-spec"><span class="tag ${item.context_available ? "tag-accent" : "tag-error"}">${item.context_available ? escapeHtml(item.resolution || "其他") : "搜索上下文已过期"}</span><span class="tag">${escapeHtml(item.site_name || "未知站点")}</span><span class="tag">${formatSize(item.size)}</span>${item.context_available ? "" : '<button class="text-link" data-route-target="search">重新搜索 →</button>'}</div>
        <button class="cart-remove" data-cart-remove="${escapeHtml(item.id)}" aria-label="移除 ${escapeHtml(item.original_title)}">移除</button>
      </article>`).join("")
    : "<div class='empty-state'><span>＋</span><strong>下载列表为空</strong><p>前往资源搜索，从候选中加入需要的资源。</p><button class='button button-secondary' data-route-target='search'>前往资源搜索</button></div>";
  if (expired) {
    $("#cart-result").textContent = `${expired} 个资源因服务重启已失效，请重新搜索；仍可提交其余 ${available.length} 个有效资源。`;
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

function candidateState(item) {
  if (item.library_state === "in_library") return ["已入库", "tag-library"];
  if (["strm", "not_found"].includes(item.library_state)) return ["待入库", "tag-strm"];
  return ["状态未知", ""];
}

function renderCandidates() {
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
  if (!filtered.length) {
    const {heading, copy, actions} = candidateEmptyState({candidateCache, activeTaskState, currentFilter});
    $("#candidates").innerHTML = `<div class="empty-state"><span>⌕</span><strong>${heading}</strong><p>${copy}</p>${actions}</div>`;
    return;
  }
  $("#candidates").innerHTML = filtered.map((item) => {
    const [stateLabel, stateClass] = candidateState(item);
    const excluded = item.eligibility === "excluded";
    const recommendation = {preferred: "首选", fallback: "保底", excluded: "已排除"}[item.recommendation] || (excluded ? "已排除" : "候选");
    const labels = Array.isArray(item.metadata?.labels) ? item.metadata.labels.slice(0, 2) : [];
    const breakdown = Array.isArray(item.score_breakdown) ? item.score_breakdown.slice(0, 3) : [];
    const movieTitle = item.tmdb_title || item.chinese_title || item.tmdb_original_title || item.original_title;
    return `<article class="candidate-row ${item.recommendation === "preferred" ? "is-best" : ""} ${excluded ? "is-excluded" : ""}">
      <div class="candidate-title"><strong>${escapeHtml(movieTitle)} <span class="candidate-meta">#${escapeHtml(item.rank_no)} · ${escapeHtml(item.tmdb_year || item.year || "")}</span></strong><small title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</small></div>
      <div class="recommendation-cell"><span class="recommendation-badge ${escapeHtml(item.recommendation)}">${recommendation}</span><small>${escapeHtml(item.recommendation_reason || "等待规则分析")}</small></div>
      <div class="candidate-source"><strong>${escapeHtml(item.site_name || "未知站点")} ${item.is_free ? '<em class="free-mark">FREE</em>' : ""}</strong><small class="site-selection-reason">${escapeHtml(item.site_selection_reason || "")}</small><div class="spec-stack"><span class="tag tag-accent">${escapeHtml(item.resolution || "其他")}</span><span class="tag">${escapeHtml(item.codec || "其他")}</span><span class="tag">${escapeHtml(item.group_name || "未知组")}</span><span class="tag">${Number(item.seeders || 0)} 做种</span><span class="tag">${formatSize(item.size)}</span><span class="tag ${stateClass}">${stateLabel}</span>${labels.map((label) => `<span class="tag tag-promo">${escapeHtml(label)}</span>`).join("")}</div>${item.site_count > 1 ? `<details class="site-options"><summary>另 ${item.site_count - 1} 个站点</summary>${item.site_options.slice(1).map((option) => `<div><strong>${escapeHtml(option.site_name || "未知")}</strong><span>优先级 ${Number(option.site_priority)} · ${option.volume_factor === 0 ? "FREE · " : option.volume_factor < 1 ? `下载 ${Math.round(option.volume_factor * 100)}% · ` : ""}${Number(option.seeders || 0)} 做种</span></div>`).join("")}</details>` : ""}</div>
      <div class="candidate-score"><strong>${excluded ? "—" : Number(item.score || 0)}</strong><small>${excluded ? escapeHtml(item.exclusion_reason || "不符合允许组合") : (breakdown.map((part) => `${escapeHtml(part.label)}${Number(part.score || 0) ? ` +${Number(part.score)}` : ""}`).join(" · ") || "策略匹配")}</small></div>
      ${excluded ? '<span class="candidate-blocked">不可加入</span>' : `<button class="candidate-action ${item.in_cart ? "selected" : ""}" data-candidate="${escapeHtml(item.id)}" ${!item.context_available && !item.in_cart ? "disabled" : ""}>${item.in_cart ? "移出下载列表" : item.context_available ? "加入下载列表" : "需重新搜索"}</button>`}
    </article>`;
  }).join("");
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
  $("#task-bar").style.width = `${percent}%`;
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
  let task;
  try {
    task = await api(`/api/search-tasks/${activeTask}`);
    candidatePollFailures = 0;
  } catch (error) {
    // 瞬时失败不终止轮询：退避重试，连续 10 次失败后停止避免刷屏。
    candidatePollFailures += 1;
    if (candidatePollFailures >= 10) return;
    clearTimeout(taskTimer);
    if (schedule) taskTimer = setTimeout(() => refreshCandidates(true), 3000);
    return;
  }
  setTaskState(task);
  try {
    candidateCache = await api(`/api/candidates?task_id=${activeTask}`);
  } catch (error) {
    showToast(`候选读取失败：${error.message}`);
  }
  try {
    await refreshTaskLogs();
  } catch (error) {
    showToast(`任务日志读取失败：${error.message}`);
  }
  renderCandidates();
  clearTimeout(taskTimer);
  if (schedule && ["queued", "running"].includes(task.status)) {
    taskTimer = setTimeout(() => refreshCandidates(true).catch((error) => showToast(error.message)), 1400);
  }
}

async function recoverLatestTask() {
  const tasks = await api("/api/search-tasks?limit=1");
  if (!tasks.length) return;
  activeTask = tasks[0].id;
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
  $("#dashboard-history").innerHTML = historyCache.length
    ? historyCache.slice(0, 2).map((item) => { const status = item.lifecycle_status || (item.success ? "submitted" : "failed"); const label = item.status_label || (item.success ? "已提交" : "失败"); return `<article class="activity-item"><span class="history-status ${lifecycleStatusClass(status)}" aria-label="${escapeHtml(label)}">${lifecycleStatusIcon(status)}</span><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.site_name || "未知站点")} · ${formatTime(item.created_at)} · ${escapeHtml(item.status_source || "状态检查")}</small></div><span class="activity-result">${escapeHtml(label)}</span></article>`; }).join("")
    : "<div class='empty-state compact'><strong>暂无下载记录</strong><p>提交的资源会显示在这里。</p></div>";
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

const SECRET_FIELDS = [
  ["settings-mp-key", "mp_api_key_configured"],
  ["settings-emby-key", "emby_api_key_configured"],
  ["settings-tmdb-key", "tmdb_api_key_configured"],
  ["settings-mdblist-key", "mdblist_api_key_configured"],
  ["settings-cookiecloud-key", "cookiecloud_key_configured"],
  ["settings-cookiecloud-password", "cookiecloud_password_configured"],
  ["settings-ai-key", "ai_api_key_configured"],
  ["settings-tr-password", "tr_password_configured"],
];

function setupSecretClearControls() {
  // 每个密钥输入框旁动态添加“清除已配置值”复选框：留空且未勾选 = 保留原值，
  // 避免用户只修改其他设置时意外清除已配置的密钥。
  SECRET_FIELDS.forEach(([inputId]) => {
    const input = $(`#${inputId}`);
    const wrap = input?.closest("label.field");
    if (!input || !wrap || $(`#${inputId}-clear`)) return;
    const clear = document.createElement("label");
    clear.className = "secret-clear";
    clear.id = `${inputId}-clear`;
    clear.hidden = true;
    clear.innerHTML = `<input type="checkbox"> 清除已配置值`;
    clear.querySelector("input").addEventListener("change", () => {
      input.disabled = clear.querySelector("input").checked;
    });
    wrap.appendChild(clear);
  });
}

function updateSecretClearState(inputId, configured) {
  const clear = $(`#${inputId}-clear`);
  if (!clear) return;
  clear.hidden = !configured;
  clear.querySelector("input").checked = false;
  $(`#${inputId}`).disabled = false;
}

function secretValue(inputId, configured) {
  const input = $(`#${inputId}`);
  const clear = $(`#${inputId}-clear`);
  const value = String(input?.value || "").trim();
  if (value) return value;
  if (configured && clear && clear.querySelector("input").checked) return "";
  return null; // 未修改，保留原值
}

async function loadSettings() {
  const [runtime, cookiecloud] = await Promise.all([api("/api/settings"), api("/api/cookiecloud/status")]);
  setupSecretClearControls();
  SECRET_FIELDS.forEach(([inputId, flag]) => updateSecretClearState(inputId, Boolean(runtime[flag])));
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
  $("#settings-proxy-url").value = "";
  $("#settings-proxy-url").placeholder = proxyConfigured ? "已配置；留空保留原值" : "http://127.0.0.1:7890";
  $("#settings-proxy-state").textContent = proxyConfigured ? "已配置" : "未配置";
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
  $("#settings-access-token-hint").textContent = tokenRequired
    ? "服务端已设置 AUTOLIST_ACCESS_TOKEN。在此保存的值只存在本机浏览器，不会写回服务器。"
    : "服务端未启用访问令牌。若之后开启，可在此预先保存本机令牌。";
  $("#settings-ai-url").value = runtime.ai_base_url || "";
  $("#settings-ai-key").value = runtime.ai_api_key || "";
  $("#settings-ai-model").value = runtime.ai_model || "";
  $("#settings-ai-status").textContent = runtime.ai_base_url && runtime.ai_api_key_configured && runtime.ai_model ? "已配置" : "未配置";
  $("#settings-tr-url").value = runtime.tr_base_url || "";
  $("#settings-tr-username").value = runtime.tr_username || "";
  $("#settings-tr-password").value = runtime.tr_password || "";
  $("#settings-random-posters").checked = Boolean(runtime.dashboard_random_posters);
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
  const runtime = await api("/api/settings");
  const runtimePayload = {
    mp_base_url: $("#settings-mp-url").value.trim(),
    mp_api_key: secretValue("settings-mp-key", runtime.mp_api_key_configured),
    mp_timeout_seconds: Number($("#settings-timeout").value),
    emby_base_url: $("#settings-emby-url").value.trim(),
    emby_api_key: secretValue("settings-emby-key", runtime.emby_api_key_configured),
    tmdb_api_key: secretValue("settings-tmdb-key", runtime.tmdb_api_key_configured),
    tmdb_language: $("#settings-tmdb-language").value.trim() || "zh-CN",
    mdblist_api_key: secretValue("settings-mdblist-key", runtime.mdblist_api_key_configured),
    cookiecloud_key: secretValue("settings-cookiecloud-key", runtime.cookiecloud_key_configured),
    cookiecloud_password: secretValue("settings-cookiecloud-password", runtime.cookiecloud_password_configured),
    outbound_proxy_url: $("#settings-proxy-url").value.trim() || null,
    tmdb_proxy_enabled: $("#settings-tmdb-proxy").checked,
    pt_proxy_enabled: $("#settings-pt-proxy").checked,
    ai_base_url: $("#settings-ai-url").value.trim(),
    ai_api_key: secretValue("settings-ai-key", runtime.ai_api_key_configured),
    ai_model: $("#settings-ai-model").value.trim(),
    tr_base_url: $("#settings-tr-url").value.trim(),
    tr_username: $("#settings-tr-username").value.trim(),
    tr_password: secretValue("settings-tr-password", runtime.tr_password_configured),
    dashboard_random_posters: $("#settings-random-posters").checked,
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
  if (selectedSiteId) loadSiteHealth(selectedSiteId).catch(() => {});
}

function siteConnectionState(site) {
  // 完全基于 AutoList 自身检测结果
  if (site.last_status === "slow") return "slow";
  if (site.last_status === "ok") return "normal";
  if (site.last_status === "error") return "failed";
  return "unknown";
}
function matchesSiteFilter(site) {
  const state = siteConnectionState(site);
  return siteFilter === "all" || (siteFilter === "active" && site.search_enabled) || (siteFilter === "inactive" && !site.search_enabled) || siteFilter === state;
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
  $("#sites-list").innerHTML = list.length ? list.map((site) => {
    const icon = site.icon_endpoint || site.icon_url || `${site.base_url.replace(/\/$/, "")}/favicon.ico`;
    const stats = site.local_stats || {}, account = site.account_stats || {};
    const state = siteConnectionState(site);
    return `<article class="mp-site-card ${selectedSiteId === site.id ? "selected" : ""}" data-open-site="${site.id}" role="button" tabindex="0" aria-label="查看 ${escapeHtml(site.name)}，${state === "normal" ? "连接正常" : state === "slow" ? "连接缓慢" : state === "failed" ? "连接失败" : "连接未知"}">
      <header><span class="site-logo" data-site-logo><img src="${escapeHtml(icon)}" alt=""><b>${escapeHtml(site.name.slice(0, 1))}</b></span><strong>${escapeHtml(site.name)}</strong><i class="mp-state ${state}" title="${escapeHtml(state)}"></i></header>
      <a class="site-url-link" href="${escapeHtml(safeExternalUrl(site.base_url))}" target="_blank" rel="noopener noreferrer" title="打开 ${escapeHtml(site.base_url)}">${escapeHtml(site.base_url)}</a>
      <div class="site-local-stats"><div><strong>${account.uploaded == null ? "—" : formatSize(account.uploaded)}</strong><span>上传量</span></div><div><strong>${account.downloaded == null ? "—" : formatSize(account.downloaded)}</strong><span>下载量</span></div><div><strong>${account.ratio == null ? "—" : Number(account.ratio).toFixed(2)}</strong><span>分享率</span></div></div>
      <div class="site-local-meta"><span>${site.search_enabled ? "参与搜索" : "不参与搜索"}</span><span>${site.cookie_configured ? "Cookie 已配置" : "Cookie 未配置"}</span><span>${site.user_agent_configured ? "UA 已配置" : "默认 UA"}</span></div>
    </article>`;
  }).join("") : "<div class='empty-state compact'><strong>没有符合条件的站点</strong></div>";
  bindSiteLogoFallback($("#sites-list"));
}
function renderSiteInspector(site) {
  if (!site) return;
  const stats = site.local_stats || {}, account = site.account_stats || {}, state = siteConnectionState(site);
  const icon = site.icon_endpoint || site.icon_url || `${site.base_url.replace(/\/$/, "")}/favicon.ico`;
  $("#site-inspector-content").innerHTML = `<header class="site-detail-head"><span class="site-logo" data-site-logo><img src="${escapeHtml(icon)}" alt=""><b>${escapeHtml(site.name.slice(0, 1))}</b></span><div><strong>${escapeHtml(site.name)}</strong><a class="site-detail-url" href="${escapeHtml(safeExternalUrl(site.base_url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(site.base_url)}</a></div><em class="site-state ${state}">${state === "normal" ? "连接正常" : state === "slow" ? "连接缓慢" : state === "failed" ? "连接失败" : "连接未知"}</em><button class="icon-button site-inspector-close" data-close-site-inspector aria-label="关闭">×</button></header>
    <div class="site-detail-stats"><div><span>上传量</span><strong>${account.uploaded == null ? "—" : formatSize(account.uploaded)}</strong></div><div><span>下载量</span><strong>${account.downloaded == null ? "—" : formatSize(account.downloaded)}</strong></div><div><span>分享率</span><strong>${account.ratio == null ? "—" : Number(account.ratio).toFixed(2)}</strong></div><div><span>做种数</span><strong>${account.seeding == null ? "—" : Number(account.seeding).toLocaleString()}</strong></div></div>
    <dl class="site-detail-list"><dt>参与资源搜索</dt><dd>${site.search_enabled ? "是" : "否"}</dd><dt>本地 Cookie</dt><dd>${site.cookie_configured ? "已配置" : "未配置"}</dd><dt>User-Agent</dt><dd class="site-ua-value">${escapeHtml(site.user_agent || "AutoList 默认 UA")}</dd><dt>最近连接检测</dt><dd>${site.last_tested_at ? `${formatTime(site.last_tested_at)}${site.last_duration_ms == null ? "" : ` · ${Number(site.last_duration_ms)}ms`}` : "尚未检测"}</dd><dt>账户统计更新</dt><dd>${escapeHtml(account.checked_at ? formatTime(account.checked_at) : "等待后台刷新")}${account.error ? ` · ${escapeHtml(account.error)}` : ""}</dd><dt>最近搜索</dt><dd>${escapeHtml(stats.last_attempt_at || "暂无记录")}</dd><dt>站点搜索表现</dt><dd id="site-health-summary">${stats.total ? `${Number(stats.success_rate || 0).toFixed(1)}% 成功 · ${Number(stats.average_ms || 0)}ms · ${Number(stats.total)} 次` : "等待搜索样本"}</dd></dl>
    <div class="site-detail-actions"><button class="button button-secondary" data-test-site="${site.id}">检测站点</button><button class="button button-secondary" data-refresh-site-cookie="${site.id}">刷新 Cookie</button><button class="button button-secondary" data-edit-site="${site.id}">编辑站点 / UA</button><button class="button button-danger" data-delete-site="${site.id}">删除站点</button></div>
    <p class="site-login-hint">CookieCloud 每次收到浏览器上传后会自动匹配域名并更新本站 Cookie；单站“刷新 Cookie”仅用于手动重试。AutoList 不保存站点密码。</p>`;
  bindSiteLogoFallback($("#site-inspector-content"));
}

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
  $("#site-dialog").showModal();
}

document.addEventListener("click", async (event) => {
  const importModeButton = event.target.closest("[data-import-mode]");
  if (importModeButton) {
    importMode = importModeButton.dataset.importMode;
    $$('[data-import-mode]').forEach((button) => button.classList.toggle("active", button === importModeButton));
    $$('[data-import-panel]').forEach((panel) => { panel.hidden = panel.dataset.importPanel !== importMode; });
    $("#import-preview").hidden = true;
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
    $("#import-dialog").showModal();
    return;
  }
  const renamePlaylist = event.target.closest("[data-rename-playlist]");
  if (renamePlaylist) {
    const playlist = playlistCache.find((item) => item.id === Number(renamePlaylist.dataset.renamePlaylist));
    const name = window.prompt("输入新的片单名称", playlist?.name || "");
    if (!name?.trim()) return;
    await api(`/api/playlists/${renamePlaylist.dataset.renamePlaylist}`, {method: "PUT", body: JSON.stringify({name: name.trim()})});
    await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
    showToast("片单名称已更新");
    return;
  }
  const refreshPlaylistSource = event.target.closest("[data-refresh-playlist-source]");
  if (refreshPlaylistSource) {
    refreshPlaylistSource.disabled = true;
    try {
      const result = await api(`/api/playlists/${refreshPlaylistSource.dataset.refreshPlaylistSource}/refresh-source`, {method: "POST"});
      await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
      showToast(result.message);
    } catch (error) { showToast(error.message); } finally { refreshPlaylistSource.disabled = false; }
    return;
  }
  const syncPlaylist = event.target.closest("[data-sync-playlist]");
  if (syncPlaylist) {
    syncPlaylist.disabled = true;
    try {
      const result = await api(`/api/playlists/${syncPlaylist.dataset.syncPlaylist}/sync-now`, {method: "POST"});
      await Promise.all([loadPlaylists(currentPage === "playlists"), loadOverview()]);
      showToast(result.message);
    } catch (error) { showToast(error.message); } finally { syncPlaylist.disabled = false; }
    return;
  }
  const toggleSync = event.target.closest("[data-toggle-sync]");
  if (toggleSync) {
    const playlist = playlistCache.find((item) => item.id === Number(toggleSync.dataset.toggleSync));
    await api(`/api/playlists/${playlist.id}/sync-settings`, {method: "PUT", body: JSON.stringify({enabled: !playlist.sync_enabled, interval_hours: Number(playlist.sync_interval_hours || 24)})});
    await loadPlaylists(currentPage === "playlists");
    showToast(playlist.sync_enabled ? "已停用定时同步" : "已启用每 24 小时增量同步");
    return;
  }
  const orderPlaylist = event.target.closest("[data-order-playlist]");
  if (orderPlaylist) {
    const ids = playlistCache.map((item) => item.id);
    const index = ids.indexOf(Number(orderPlaylist.dataset.orderPlaylist));
    const target = orderPlaylist.dataset.direction === "up" ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    await api("/api/playlists/reorder", {method: "POST", body: JSON.stringify({ids})});
    await loadPlaylists(currentPage === "playlists");
    showToast("片单顺序已更新");
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
  if (editSiteButton) {
    $("#site-inspector-panel").classList.remove("open");
    openSiteDialog(siteCache.find((site) => site.id === Number(editSiteButton.dataset.editSite)) || null);
    return;
  }
  const siteCard = event.target.closest("[data-open-site]");
  if (siteCard && !event.target.closest("button, a")) {
    selectedSiteId = Number(siteCard.dataset.openSite);
    renderSites();
    renderSiteInspector(siteCache.find((site) => site.id === selectedSiteId));
    loadSiteHealth(selectedSiteId).catch(() => {});
    if (window.matchMedia("(max-width: 900px)").matches) $("#site-inspector-panel").classList.add("open");
    return;
  }
  if (event.target.closest("[data-close-site-inspector]")) {
    $("#site-inspector-panel").classList.remove("open");
    return;
  }
  const refreshSiteCookie = event.target.closest("[data-refresh-site-cookie]");
  if (refreshSiteCookie) {
    refreshSiteCookie.disabled = true;
    try {
      const result = await api(`/api/sites/${refreshSiteCookie.dataset.refreshSiteCookie}/refresh-cookie`, {method: "POST"});
      showToast(result.message);
      await loadSites();
    } catch (error) { showToast(error.message); }
    finally { refreshSiteCookie.disabled = false; }
    return;
  }
  const testSiteButton = event.target.closest("[data-test-site]");
  if (testSiteButton) {
    testSiteButton.disabled = true;
    try {
      const result = await api(`/api/sites/${testSiteButton.dataset.testSite}/test`, {method: "POST"});
      showToast(result.message || (result.ok ? "站点连接正常" : "站点连接失败"));
      await loadSites();
    } catch (error) { showToast(error.message); }
    finally {
      testSiteButton.disabled = false;
    }
    return;
  }
  if (deleteSiteButton) {
    if (!window.confirm("确认删除这个站点配置？")) return;
    await api(`/api/sites/${deleteSiteButton.dataset.deleteSite}`, {method: "DELETE"});
    $("#site-inspector-panel").classList.remove("open");
    await loadSites();
    showToast("站点已删除");
    return;
  }
  const candidateButton = event.target.closest("[data-candidate]");
  if (candidateButton) {
    candidateButton.disabled = true;
    try {
      await api(`/api/cart/items/${encodeURIComponent(candidateButton.dataset.candidate)}`, {method: "POST"});
      await Promise.all([refreshCandidates(false), refreshCart()]);
    } catch (error) {
      showToast(error.message);
    } finally {
      candidateButton.disabled = false;
    }
    return;
  }
  const taskAction = event.target.closest("[data-task-action]");
  if (taskAction) {
    taskAction.disabled = true;
    const action = taskAction.dataset.taskAction;
    try {
      await followupSearch(action, action === "retry" ? "重试" : "重新搜索整项");
    } finally {
      taskAction.disabled = false;
    }
    return;
  }
  const removeButton = event.target.closest("[data-cart-remove]");
  if (removeButton) {
    removeButton.disabled = true;
    try {
      await api(`/api/cart/items/${encodeURIComponent(removeButton.dataset.cartRemove)}`, {method: "POST"});
      await refreshCart();
      if (activeTask) await refreshCandidates(false);
      showToast("已从下载列表移除");
    } catch (error) {
      showToast(error.message);
    } finally {
      removeButton.disabled = false;
    }
  }
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
  if (event.key === "Escape") {
    const dialog = document.querySelector("dialog[open]");
    if (dialog) {
      event.preventDefault();
      dialog.close();
      return;
    }
  }
  const siteCard = event.target.closest?.("[data-open-site]");
  if (!siteCard || event.target.closest("a, button, input, select, textarea, summary")) return;
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    siteCard.click();
  }
});

window.addEventListener("hashchange", () => navigate(window.location.hash.slice(1), false, true));

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

$("#open-settings").addEventListener("click", async () => {
  const dialog = $("#settings-dialog");
  const output = $("#settings-result");
  activateSettingsTab("recognition");
  dialog.showModal();
  output.textContent = "正在读取设置…";
  output.className = "inline-message";
  try {
    await loadSettings();
    await testSettings();
  } catch (error) {
    output.textContent = error.message;
    output.className = "inline-message error";
  }
});

$("#service-status").addEventListener("click", async () => {
  try {
    await loadConnection();
    showToast("服务状态已刷新");
  } catch (error) {
    showToast(error.message);
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
    requestAnimationFrame(() => sidebar?.querySelector(sidebarFocusableSelector)?.focus({preventScroll: true}));
    return;
  }
  document.body.classList.remove("sidebar-open");
  menuButton?.setAttribute("aria-expanded", "false");
  menuButton?.setAttribute("aria-label", "打开导航");
  moreButton?.setAttribute("aria-expanded", "false");
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

$("#settings-access-token-save")?.addEventListener("click", async () => {
  setAccessToken($("#settings-access-token").value);
  showToast(getAccessToken() ? "本机访问令牌已保存" : "本机访问令牌已清空");
  try {
    await loadSettings();
  } catch (error) {
    showToast(error.message);
  }
});

$("#settings-access-token-clear")?.addEventListener("click", async () => {
  setAccessToken("");
  $("#settings-access-token").value = "";
  showToast("本机访问令牌已清除");
  try {
    await loadSettings();
  } catch (error) {
    /* server may require token again */
    $("#settings-access-token-status").textContent = "本机已清除";
    $("#settings-access-token-status").className = "";
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
  output.textContent = "正在解析…";
  try {
    const result = await api("/api/config/score-preview", {method: "POST", body: JSON.stringify({title: $("#rules-preview-title").value.trim(), seeders: Number($("#rules-preview-seeders").value || 0), candidate_policy: collectCandidatePolicy()})});
    const state = {preferred: "首选", fallback: "保底", excluded: "已排除"}[result.recommendation] || result.recommendation;
    output.innerHTML = `<strong>${escapeHtml(state)}${result.score ? ` · ${Number(result.score)} 匹配度` : ""}</strong><span>${escapeHtml(result.resolution)} · ${escapeHtml(result.codec)} · ${escapeHtml(result.group || "未知制作组")}</span><div>${(result.breakdown || []).map((item) => `<i>${escapeHtml(item.label)}</i>`).join("")}</div>${result.exclusion_reason ? `<small class="preview-exclusion">${escapeHtml(result.exclusion_reason)}</small>` : ""}`;
  } catch (error) { output.textContent = error.message; }
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
  if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId);
});
async function pollLibraryScan(taskId) {
  let task;
  try {
    task = await api(`/api/library-scan-tasks/${taskId}`);
  } catch (error) {
    // 瞬时失败不终止轮询：退避重试，连续 10 次失败后停止。
    libraryScanFailures += 1;
    if (libraryScanFailures >= 10) {
      const button = $("#refresh-library");
      if (button) button.disabled = !expandedPlaylistId;
      return;
    }
    clearTimeout(libraryScanTimer);
    libraryScanTimer = setTimeout(() => pollLibraryScan(taskId).catch(() => {}), 3000);
    return;
  }
  libraryScanFailures = 0;
  const button = $("#refresh-library");
  button.textContent = ["queued", "running"].includes(task.status) ? `刷新中 ${task.completed}/${task.total}` : "刷新 Emby 状态";
  if (["queued", "running"].includes(task.status)) {
    button.disabled = true;
    clearTimeout(libraryScanTimer);
    libraryScanTimer = setTimeout(() => pollLibraryScan(taskId).catch((error) => showToast(error.message)), 1000);
    return;
  }
  button.disabled = !expandedPlaylistId;
  if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId, $("#playlist-item-query").value);
  showToast(task.status === "completed" ? `Emby 状态已刷新：已入库 ${task.in_library} · 待入库 ${Math.max(0, Number(task.total) - Number(task.in_library))}` : "Emby 状态刷新未完成");
}
$("#refresh-library").addEventListener("click", async () => {
  if (!expandedPlaylistId) return;
  const button = $("#refresh-library");
  button.disabled = true;
  try {
    const task = await api(`/api/playlists/${expandedPlaylistId}/library-scan`, {method: "POST"});
    showToast(task.message);
    await pollLibraryScan(task.id);
  } catch (error) { button.disabled = false; showToast(error.message); }
});
async function pollRecognition(taskId) {
  if (!taskId) {
    $("#recognition-progress").hidden = true;
    await loadPlaylists(true);
    return;
  }
  let task;
  try {
    task = await api(`/api/recognition-tasks/${taskId}`);
  } catch (error) {
    recognitionPollFailures += 1;
    if (recognitionPollFailures >= 10) {
      $("#recognition-progress").hidden = true;
      return;
    }
    clearTimeout(recognitionTimer);
    recognitionTimer = setTimeout(() => pollRecognition(taskId).catch(() => {}), 3000);
    return;
  }
  recognitionPollFailures = 0;
  const percent = task.total ? Math.round(task.completed / task.total * 100) : 100;
  $("#recognition-progress").hidden = false;
  $("#recognition-text").textContent = `TMDB 识别 ${task.completed}/${task.total} · 成功 ${task.matched}`;
  $("#recognition-bar").style.width = `${percent}%`;
  clearTimeout(recognitionTimer);
  if (["queued", "running"].includes(task.status)) {
    recognitionTimer = setTimeout(() => pollRecognition(taskId).catch((error) => showToast(error.message)), 1200);
  } else {
    await loadPlaylists(true);
    showToast(task.status === "completed" ? "TMDB 识别完成" : `TMDB 识别${task.status === "partial" ? "部分完成" : "失败"}`);
  }
}
$("#recognize-playlist").addEventListener("click", async () => {
  if (!expandedPlaylistId) return;
  const button = $("#recognize-playlist");
  button.disabled = true;
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
    button.disabled = false;
  }
});
$("#playlist-page-size").addEventListener("change", async () => {
  playlistPageSize = $("#playlist-page-size").value;
  safeStorageSet("autolist-playlist-page-size", playlistPageSize);
  playlistPage = 1;
  if (expandedPlaylistId) await loadPlaylistItems(expandedPlaylistId);
});
$("#playlist-page-prev").addEventListener("click", async () => { playlistPage = Math.max(1, playlistPage - 1); await loadPlaylistItems(expandedPlaylistId); });
$("#playlist-page-next").addEventListener("click", async () => { playlistPage = Math.min(playlistPageCount, playlistPage + 1); await loadPlaylistItems(expandedPlaylistId); });

$("#delete-playlist").addEventListener("click", async () => {
  if (!selectedPlaylistId || !window.confirm("删除片单会同时删除它的搜索任务和候选，确认继续？")) return;
  try {
    await api(`/api/playlists/${selectedPlaylistId}`, {method: "DELETE"});
    selectedPlaylistId = null;
    expandedPlaylistId = null;
    playlistItemsCache = [];
    await Promise.all([loadPlaylists(true), loadOverview()]);
    showToast("片单已删除");
  } catch (error) {
    showToast(error.message);
  }
});

$("#open-import").addEventListener("click", () => $("#import-dialog").showModal());
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
  try {
    await api(`/api/playlists/${currentSearchPlaylistId}/automation`, {method: "PUT", body: JSON.stringify({
      enabled: !playlist.automation_enabled, auto_cart: false, batch_size: Number(playlist.automation_batch_size || 50),
    })});
    await loadPlaylists();
    showToast(playlist.automation_enabled ? "已关闭新片自动搜索" : "已开启新片自动搜索");
  } catch (error) { showToast(error.message); }
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
  if (!activeTask) return;
  try {
    await api(`/api/search-tasks/${activeTask}/cancel`, {method: "POST"});
    await refreshCandidates(false);
    showToast("搜索任务已取消");
  } catch (error) {
    showToast(error.message);
  }
});

async function followupSearch(action, label) {
  if (!activeTask) return;
  try {
    const task = await api(`/api/search-tasks/${activeTask}/${action}`, {method: "POST"});
    activeTask = task.id;
    await refreshCandidates(true);
    showToast(`${label}任务 #${task.id} 已开始`);
  } catch (error) {
    showToast(error.message);
  }
}

$("#retry-task").addEventListener("click", () => followupSearch("retry", "重试"));
$("#restart-task").addEventListener("click", () => followupSearch("restart", "重新搜索整项"));

$$(".filter-chip").forEach((button) => button.addEventListener("click", () => {
  currentFilter = button.dataset.filter;
  $$(".filter-chip").forEach((item) => item.classList.toggle("active", item === button));
  renderCandidates();
}));

$("#refresh-history").addEventListener("click", () => refreshHistory().then(() => showToast("历史已刷新")).catch((error) => showToast(error.message)));
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


$("#download").addEventListener("click", () => {
  const available = cartCache.filter((item) => item.context_available);
  const total = available.reduce((sum, item) => sum + Number(item.size || 0), 0);
  const expired = cartCache.length - available.length;
  $("#confirm-copy").textContent = `${available.length} 个有效资源，预计 ${formatSize(total)}${expired ? `；另有 ${expired} 个资源需要重新搜索` : ""}。确认后将按现有分类规则开始下载。`;
  $("#confirm-dialog").showModal();
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
  navigate(initialTarget, false);
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
    if (!pageMeta[initialPage]) history.replaceState(null, "", "#dashboard");
  }
})();
