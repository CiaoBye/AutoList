import type { Theme } from "../theme";

/** 每套主题一个图标：宣纸朱砂是印章，青绿山水是远山与水，玄漆描金是描金装裱框，暗房红灯是胶片格。线条取当前文字色。 */
export function ThemeIcon({ theme, size = 20 }: { theme: Theme; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      {theme === "paper" ? (
        <>
          <rect x="3.5" y="3.5" width="13" height="13" rx="1.5" />
          <path d="M7 7.5h6M10 7.5v5.5M7.5 13h5" />
        </>
      ) : theme === "jade" ? (
        <>
          <path d="M2.5 14.5l4.5-7.5 3.2 5 2.3-3.2 5 5.7z" />
          <path d="M3 17.2c2-1.1 3-1.1 5 0s3 1.1 5 0 3-1.1 4 0" />
        </>
      ) : theme === "lacquer" ? (
        <>
          <rect x="3" y="3" width="14" height="14" rx="0.5" />
          <rect x="6" y="6" width="8" height="8" rx="0.5" />
          <path d="M10 8.4l1.6 1.6-1.6 1.6-1.6-1.6z" />
        </>
      ) : (
        <>
          <rect x="3.5" y="4" width="13" height="12" rx="1" />
          <path d="M3.5 7.5H5.5M3.5 10.5H5.5M3.5 13.5H5.5M14.5 7.5h2M14.5 10.5h2M14.5 13.5h2" />
          <circle cx="10" cy="10" r="2.4" />
        </>
      )}
    </svg>
  );
}

/** 主题的图标块：该主题自己的底色上画它自己的点缀色图标，主题菜单与外观页共用。 */
export function ThemeTile({ theme, paper, accent, size = 32 }: { theme: Theme; paper: string; accent: string; size?: number }) {
  return (
    <span class="theme-tile" style={{ background: paper, color: accent, width: `${size}px`, height: `${size}px` }} aria-hidden="true">
      <ThemeIcon theme={theme} size={Math.round(size * 0.62)} />
    </span>
  );
}
