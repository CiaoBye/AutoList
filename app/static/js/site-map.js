// 站点地图计算层（审计 3-10）：视野/节点位置/缩放等纯计算逻辑，
// 与 app.js 的页面渲染、事件绑定解耦，便于独立测试与维护。
import { $, safeStorageGet, safeStorageSet } from "./core.js";

const siteNodePositionCache = new Map();
const siteNodePositionOverrides = new Map();
export const siteNodePositionStorageKey = "autolist-site-node-positions";
export const siteMapOrientationStorageKey = "autolist-site-map-orientation";
export const siteMapViewportStorageKey = "autolist-site-map-viewport";
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
  const x = Number.isFinite(anchorX) ? anchorX - rect.left - rect.width / 2 : 0;
  const y = Number.isFinite(anchorY) ? anchorY - rect.top - rect.height / 2 : 0;
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

export function siteMapCopy(theme = document.documentElement.dataset.theme || "archive") {
  const copy = {
    archive: {kicker: "SOURCE ROOM / ARCHIVE NETWORK", heading: "来源档案室", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入档案。打开“编辑节点排布”后可直接拖动节点。", ariaLabel: "可操作的来源档案地图"},
    cinema: {kicker: "SCREENING FLOOR / SOURCE MAP", heading: "放映来源", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入场务档案。打开“编辑节点排布”后可直接拖动节点。", ariaLabel: "可操作的放映来源地图"},
    ledger: {kicker: "CATALOGUE / SOURCE REGISTER", heading: "来源记录", description: "滚轮或双指缩放，拖动画布浏览来源；点击节点进入目录条目。打开“编辑节点排布”后可直接拖动节点。", ariaLabel: "可操作的来源目录地图"},
  }[theme] || null;
  return copy || siteMapCopy("archive");
}

export function resetSiteNodePositions() {
  siteNodePositionOverrides.clear();
  siteNodePositionCache.clear();
  persistSiteNodePositions();
}

export function siteNodePosition(siteId) {
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

export function setSiteNodePosition(siteId, left, top) {
  const position = [Math.max(8, Math.min(92, Number(left))), Math.max(12, Math.min(88, Number(top)))];
  siteNodePositionOverrides.set(String(siteId), position);
  siteNodePositionCache.set(String(siteId), position);
}
