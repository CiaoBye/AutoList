/** 当前片单：在首页或片单页最后选的那份，首页与片单页默认都显示它（只保存在当前浏览器）。 */
const KEY = "autolist.current-playlist";

export function readCurrentPlaylist(): string | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return value && /^\d+$/.test(value) ? value : null;
  } catch {
    return null;
  }
}

export function rememberCurrentPlaylist(value: string | number | null | undefined): void {
  if (value === null || value === undefined || !/^\d+$/.test(String(value))) return;
  try {
    window.localStorage.setItem(KEY, String(value));
  } catch {
    // 浏览器禁用存储时只是不记住，不影响使用。
  }
}

/** 记住的片单已被删除时忘掉它，回到默认（排在最前的片单）。 */
export function forgetCurrentPlaylist(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // 同上。
  }
}
