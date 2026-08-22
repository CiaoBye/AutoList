// 站点地图状态与交互适配层：集中管理视野、节点位置、缩放和排布状态。
// 计算边界保持小而明确；DOM 同步留在这里，app.js 只负责页面路由和事件入口。
import { $, safeStorageGet, safeStorageSet } from "./core.js";

const siteNodePositionCache = new Map();
const siteNodePositionOverrides = new Map();
export const siteNodePositionStorageKey = "autolist-site-node-positions";
export const siteMapOrientationStorageKey = "autolist-site-map-orientation";
// 1.17: the previous viewport values were written before the canvas transform
// was applied. Start a clean view so an old 184%/240% value cannot suddenly be
// rendered after the interaction fix ships.
export const siteMapViewportStorageKey = "autolist-site-map-viewport-v2";
export const siteMapZoomMin = 0.78;
export const siteMapZoomMax = 2.4;
export let siteMapOrientation = safeStorageGet(siteMapOrientationStorageKey) === "vertical" ? "vertical" : "horizontal";
export let siteMapViewport = {scale: 1, x: 0, y: 0};
export let siteMapLayoutEdit = false;
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
export function persistSiteNodePositions() {
  safeStorageSet(siteNodePositionStorageKey, JSON.stringify(Object.fromEntries(siteNodePositionOverrides)));
}
export function persistSiteMapViewport() {
  safeStorageSet(siteMapViewportStorageKey, JSON.stringify(siteMapViewport));
}

// ES module 的导入绑定在 app.js 中是只读的。所有地图状态变更都集中在
// 这里，由动作函数更新导出的 live binding，渲染层只读取状态或调用动作。
export function setSiteMapOrientation(next) {
  siteMapOrientation = next === "vertical" ? "vertical" : "horizontal";
  safeStorageSet(siteMapOrientationStorageKey, siteMapOrientation);
  return siteMapOrientation;
}

export function getSiteMapOrientation() {
  return siteMapOrientation;
}

export function toggleSiteMapOrientation() {
  return setSiteMapOrientation(siteMapOrientation === "vertical" ? "horizontal" : "vertical");
}

export function setSiteMapLayoutEdit(enabled) {
  siteMapLayoutEdit = Boolean(enabled);
  return siteMapLayoutEdit;
}

export function toggleSiteMapLayoutEdit() {
  return setSiteMapLayoutEdit(!siteMapLayoutEdit);
}
export function siteMapViewportBounds(field, scaleOverride = siteMapViewport.scale) {
  const rect = field?.getBoundingClientRect?.();
  if (!rect?.width || !rect?.height) return {x: 120, y: 120};
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(scaleOverride) || 1));
  return {
    x: Math.max(90, ((scale - 1) * rect.width) / 2 + 90),
    y: Math.max(90, ((scale - 1) * rect.height) / 2 + 90),
  };
}
export function clampSiteMapViewport(next, field = $("#sites-list .site-map-field")) {
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(next.scale) || 1));
  const bounds = siteMapViewportBounds(field, scale);
  return {
    scale,
    x: Math.max(-bounds.x, Math.min(bounds.x, Number(next.x) || 0)),
    y: Math.max(-bounds.y, Math.min(bounds.y, Number(next.y) || 0)),
  };
}
export function syncSiteMapViewport() {
  const field = $("#sites-list .site-map-field");
  const canvas = $("#sites-list .site-map-canvas");
  if (!field || !canvas) return;
  siteMapViewport = clampSiteMapViewport(siteMapViewport, field);
  canvas.style.setProperty("--map-scale", String(siteMapViewport.scale));
  canvas.style.setProperty("--map-pan-x", `${siteMapViewport.x}px`);
  canvas.style.setProperty("--map-pan-y", `${siteMapViewport.y}px`);
  // Keep a concrete transform as the source of truth. Some Chromium builds
  // keep a var()-based transform at identity while the custom properties are
  // updated, which made zoom/pan controls report success without moving the map.
  canvas.style.transform = `translate3d(${siteMapViewport.x}px, ${siteMapViewport.y}px, 0) scale(${siteMapViewport.scale})`;
  field.dataset.zoom = String(Math.round(siteMapViewport.scale * 100));
  field.setAttribute("aria-label", `${siteMapCopy().ariaLabel}，当前缩放 ${Math.round(siteMapViewport.scale * 100)}%，滚轮或双指缩放，拖动画布平移`);
  field.setAttribute("aria-description", "滚轮或加减按钮缩放；桌面端可拖动画布，移动端请先开启地图操作后拖动或双指缩放；点击来源节点进入档案；编辑排布模式下可用方向键或拖动调整位置");
  const output = $("#sites-list .site-map-zoom-value");
  if (output) output.textContent = `${Math.round(siteMapViewport.scale * 100)}%`;
  $("#sites-list [data-site-map-zoom='out']")?.toggleAttribute("disabled", siteMapViewport.scale <= siteMapZoomMin);
  $("#sites-list [data-site-map-zoom='in']")?.toggleAttribute("disabled", siteMapViewport.scale >= siteMapZoomMax);
}

export function setSiteMapViewport(next, field = $("#sites-list .site-map-field"), options = {}) {
  siteMapViewport = clampSiteMapViewport(next, field);
  if (options.persist !== false) persistSiteMapViewport();
  if (options.sync !== false) syncSiteMapViewport();
  return siteMapViewport;
}

export function panSiteMapViewport(deltaX = 0, deltaY = 0, field = $("#sites-list .site-map-field"), options = {}) {
  return setSiteMapViewport(
    {
      ...siteMapViewport,
      x: siteMapViewport.x + Number(deltaX || 0),
      y: siteMapViewport.y + Number(deltaY || 0),
    },
    field,
    options,
  );
}
export function resetSiteMapViewport() {
  setSiteMapViewport({scale: 1, x: 0, y: 0});
}
export function zoomSiteMap(nextScale, anchorX = null, anchorY = null) {
  const field = $("#sites-list .site-map-field");
  const current = siteMapViewport;
  const scale = Math.max(siteMapZoomMin, Math.min(siteMapZoomMax, Number(nextScale) || 1));
  if (!field || scale === current.scale) return;
  const rect = field.getBoundingClientRect();
  const x = Number.isFinite(anchorX)
    ? (anchorX - rect.left - rect.width / 2 - current.x) / current.scale
    : 0;
  const y = Number.isFinite(anchorY)
    ? (anchorY - rect.top - rect.height / 2 - current.y) / current.scale
    : 0;
  setSiteMapViewport({
    scale,
    x: current.x + (current.scale - scale) * x,
    y: current.y + (current.scale - scale) * y,
  }, field);
}
export function focusSiteNode(siteId) {
  const field = $("#sites-list .site-map-field");
  const node = [...($("#sites-list")?.querySelectorAll(".site-star-node") || [])].find((item) => String(item.dataset.openSite) === String(siteId));
  if (!field || !node) return;
  const rect = field.getBoundingClientRect();
  const left = Number.parseFloat(node.style.getPropertyValue("--node-left")) || 50;
  const top = Number.parseFloat(node.style.getPropertyValue("--node-top")) || 50;
  const scale = Math.max(siteMapViewport.scale, 1.12);
  setSiteMapViewport({
    scale,
    x: -((left / 100) - .5) * rect.width * scale,
    y: -((top / 100) - .5) * rect.height * scale,
  }, field);
}

export function siteMapCopy() {
  return {
    kicker: "SOURCE NETWORK",
    heading: "来源档案室",
    description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入来源档案。打开“编辑节点排布”后可直接拖动节点。",
    ariaLabel: "可操作的来源档案地图",
  };
}

export function resetSiteNodePositions() {
  siteNodePositionOverrides.clear();
  siteNodePositionCache.clear();
  persistSiteNodePositions();
}

// 默认排布为自适应编目网格：列数随站点数收敛（宽画布取约 1.6:1 的格距比），
// 每格叠加站点哈希微抖动避免呆板；列表顺序稳定时位置可复现，增删站点只影响
// 后续槽位。手动排布覆盖与“恢复视野与排布”行为不受影响。
export function siteNodePosition(siteId, index = null, total = null) {
  const key = String(siteId);
  if (siteNodePositionOverrides.has(key)) return siteNodePositionOverrides.get(key);
  if (siteNodePositionCache.has(key)) return siteNodePositionCache.get(key);
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  let position;
  if (Number.isFinite(index) && Number.isFinite(total) && total > 0) {
    const cols = Math.min(7, Math.max(3, Math.ceil(Math.sqrt(total * 1.6))));
    const rows = Math.ceil(total / cols);
    const col = index % cols;
    const row = Math.floor(index / cols);
    // 网格区域：横向 12%–88%，纵向 16%–84%，末行不满也保持居中观感。
    const cellW = 76 / cols;
    const cellH = 68 / rows;
    const jitterX = (((hash % 100) / 100) - 0.5) * Math.min(3.2, cellW * 0.22);
    const jitterY = ((((hash >>> 7) % 100) / 100) - 0.5) * Math.min(2.8, cellH * 0.24);
    const x = 12 + cellW * (col + 0.5) + jitterX;
    const y = 16 + cellH * (row + 0.5) + jitterY;
    position = [
      Math.round(Math.min(90, Math.max(10, x)) * 10) / 10,
      Math.round(Math.min(86, Math.max(14, y)) * 10) / 10,
    ];
  } else {
    position = [14 + (hash % 7201) / 100, 17 + (((hash >>> 8) % 6601) / 100)];
  }
  siteNodePositionCache.set(key, position);
  return position;
}

export function setSiteNodePosition(siteId, left, top) {
  const position = [Math.max(8, Math.min(92, Number(left))), Math.max(12, Math.min(88, Number(top)))];
  siteNodePositionOverrides.set(String(siteId), position);
  siteNodePositionCache.set(String(siteId), position);
}
