/* AutoList theme bootstrap: keep the selected visual system before the main app loads. */
(() => {
  const THEMES = Object.freeze({
    archive: Object.freeze({label: "馆藏档案", shortLabel: "档案", description: "片目、来源与纸张档案", color: "#f0f3f2"}),
    cinema: Object.freeze({label: "午夜放映", shortLabel: "放映", description: "暗场、场次与放映队列", color: "#0d171c"}),
    ledger: Object.freeze({label: "编目索引", shortLabel: "索引", description: "目录、编号与高信息密度", color: "#eee8dd"}),
  });
  const THEME_KEYS = Object.freeze(Object.keys(THEMES));
  const THEME_STORAGE_KEY = "autolist-theme";
  const SCENE_STORAGE_KEY = "autolist-scene-mode";
  const MOTION_STORAGE_KEY = "autolist-motion";
  const root = document.documentElement;

  function read(key) {
    try { return window.localStorage.getItem(key); } catch (_error) { return null; }
  }

  function write(key, value) {
    try {
      if (value === null || value === undefined) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (_error) { /* 存储被禁用时仍保留当前会话的主题 */ }
  }

  function validTheme(value) {
    return THEME_KEYS.includes(value) ? value : "archive";
  }

  const initialTheme = validTheme(read(THEME_STORAGE_KEY) || root.dataset.theme);
  const initialSceneMode = read(SCENE_STORAGE_KEY) !== "off";
  const initialMotion = read(MOTION_STORAGE_KEY) === "reduced";

  function updateThemeColor(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = THEMES[theme].color;
    root.style.colorScheme = theme === "cinema" ? "dark" : "light";
  }

  function apply(theme, {persist = true, announce = true} = {}) {
    const nextTheme = validTheme(theme);
    root.dataset.theme = nextTheme;
    updateThemeColor(nextTheme);
    if (persist) write(THEME_STORAGE_KEY, nextTheme);
    if (announce) window.dispatchEvent(new CustomEvent("autolist-theme-change", {detail: {theme: nextTheme}}));
    return nextTheme;
  }

  function setSceneMode(enabled, {persist = true, announce = true} = {}) {
    const active = Boolean(enabled);
    root.dataset.scene = active ? "on" : "off";
    if (persist) write(SCENE_STORAGE_KEY, active ? "on" : "off");
    if (announce) window.dispatchEvent(new CustomEvent("autolist-scene-change", {detail: {enabled: active}}));
    return active;
  }

  function setReducedMotion(enabled, {persist = true, announce = true} = {}) {
    const reduced = Boolean(enabled);
    root.dataset.motion = reduced ? "reduced" : "standard";
    if (persist) write(MOTION_STORAGE_KEY, reduced ? "reduced" : "standard");
    if (announce) window.dispatchEvent(new CustomEvent("autolist-motion-change", {detail: {reduced}}));
    return reduced;
  }

  root.dataset.theme = initialTheme;
  updateThemeColor(initialTheme);
  setSceneMode(initialSceneMode, {persist: false, announce: false});
  setReducedMotion(initialMotion, {persist: false, announce: false});

  window.AutoListTheme = Object.freeze({
    themes: THEMES,
    keys: THEME_KEYS,
    getTheme: () => validTheme(root.dataset.theme),
    apply,
    getSceneMode: () => root.dataset.scene !== "off",
    setSceneMode,
    getReducedMotion: () => root.dataset.motion === "reduced",
    setReducedMotion,
  });
})();
