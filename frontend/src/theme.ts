/** 四套主题：宣纸朱砂、青绿山水（浅），玄漆描金、暗房红灯（深）。沿用 1.x 的存储键；早期的 archive 视为宣纸朱砂，cinema 视为玄漆描金。 */
export type Theme = "paper" | "jade" | "lacquer" | "darkroom";

/** paper / accent 只用来画主题选择里的图标块（每套主题自己的底色与点缀色，与 tokens.css 里的取值一致）。 */
export const THEMES: { id: Theme; name: string; tagline: string; dark: boolean; paper: string; accent: string }[] = [
  { id: "paper", name: "宣纸朱砂", tagline: "典籍与印章", dark: false, paper: "#f3ede0", accent: "#b3261e" },
  { id: "jade", name: "青绿山水", tagline: "卷轴与签条", dark: false, paper: "#eef1ea", accent: "#2f6f6a" },
  { id: "lacquer", name: "玄漆描金", tagline: "漆器与装裱", dark: true, paper: "#14110f", accent: "#d4a24c" },
  { id: "darkroom", name: "暗房红灯", tagline: "胶片与场记", dark: true, paper: "#120c0c", accent: "#e0453a" },
];

const THEME_KEY = "autolist-theme";
const LEGACY: Record<string, Theme> = { archive: "paper", cinema: "lacquer" };

export const readTheme = (): Theme => {
  try {
    const stored = localStorage.getItem(THEME_KEY) || "";
    if (THEMES.some((item) => item.id === stored)) return stored as Theme;
    return LEGACY[stored] ?? "paper";
  } catch {
    return "paper";
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
