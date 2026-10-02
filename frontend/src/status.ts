import type { FilmIssue, FilmStatus } from "./types";

/** 状态名、说明与颜色变量：所有页面共用同一套定义。 */
export const STATUS_ORDER: FilmStatus[] = [
  "missing",
  "unrecognized",
  "unchecked",
  "searching",
  "candidates",
  "selected",
  "downloading",
  "in_library",
];

export const STATUS_LABELS: Record<FilmStatus, string> = {
  unrecognized: "待识别",
  unchecked: "待核对",
  missing: "缺片",
  searching: "寻片中",
  candidates: "有候选",
  selected: "已选定",
  downloading: "下载中",
  in_library: "已入馆",
};

export const STATUS_HINTS: Record<FilmStatus, string> = {
  unrecognized: "尚未对上 TMDB",
  unchecked: "还没有确认 Emby 中是否已有",
  missing: "Emby 中没有实体文件",
  searching: "正在各站点搜索",
  candidates: "找到合格资源，等你挑选",
  selected: "在待入馆清单里，尚未提交",
  downloading: "已提交下载",
  in_library: "Emby 中已有实体文件",
};

export const ISSUE_LABELS: Record<FilmIssue, string> = {
  no_eligible: "无合格资源",
  submit_failed: "提交失败",
  context_expired: "候选已过期",
  organize_failed: "整理失败",
  download_stalled: "下载停滞",
};

/** 进度条与图例使用的颜色变量（与徽标文字色一致）。 */
export const STATUS_BAR_COLOR: Record<FilmStatus, string> = {
  in_library: "var(--st-in_library-fg)",
  downloading: "var(--st-downloading-fg)",
  selected: "var(--st-selected-fg)",
  candidates: "var(--st-candidates-fg)",
  searching: "var(--st-searching-fg)",
  missing: "var(--bar-missing)",
  unchecked: "var(--line-strong)",
  unrecognized: "var(--st-unrecognized-fg)",
};

export const TRANSFER_LABELS: Record<string, string> = {
  active: "Transmission 下载中",
  stalled: "下载停滞",
  paused: "已在 Transmission 中暂停",
  error: "Transmission 报错",
  waiting_library: "等待整理入库",
  unknown: "暂时无法确认 Transmission 状态",
};
