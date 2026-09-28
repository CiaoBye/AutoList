/** 两套主题：馆藏档案（亮）与午夜放映（暗）。沿用 1.x 的存储键，早期的“编目索引”视为亮色。 */
export type Theme = "archive" | "cinema";

const THEME_KEY = "autolist-theme";

export const readTheme = (): Theme => {
  try {
    return localStorage.getItem(THEME_KEY) === "cinema" ? "cinema" : "archive";
  } catch {
    return "archive";
  }
};

export const applyTheme = (theme: Theme, persist = false): void => {
  document.documentElement.dataset.theme = theme;
  if (!persist) return;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // 无法持久化时仅对当前页面生效。
  }
};
