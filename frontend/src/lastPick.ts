/** 最近一次选定的影片：切到别的页面再回到挑选台时，直接滚到它那里（只保存在当前浏览器）。 */
const KEY = "autolist.last-pick";

export function rememberLastPick(filmId: number | null | undefined): void {
  if (!filmId) return;
  try {
    window.sessionStorage.setItem(KEY, String(filmId));
  } catch {
    // 浏览器禁用存储时只是不记住，不影响使用。
  }
}

export function readLastPick(): number | null {
  try {
    const value = Number(window.sessionStorage.getItem(KEY));
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

export function forgetLastPick(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // 同上。
  }
}
