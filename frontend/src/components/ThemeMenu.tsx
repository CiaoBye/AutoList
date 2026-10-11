import { useEffect, useRef, useState } from "preact/hooks";
import { THEMES, type Theme } from "../theme";
import { ThemeIcon, ThemeTile } from "./ThemeIcon";

/** 顶栏的主题菜单：点开列出四套主题（色块、名称、一句话），直接选一套；Esc 或点别处收起。 */
export function ThemeMenu({ theme, onChange }: { theme: Theme; onChange: (theme: Theme) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const current = THEMES.find((item) => item.id === theme);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div class="theme-menu" ref={root}>
      <button
        ref={button}
        class="nav-btn icon-btn"
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={`主题：${current?.name ?? ""}，点开切换`}
        onClick={() => setOpen((value) => !value)}
      >
        <span class="theme-icon">
          <ThemeIcon theme={theme} />
        </span>
      </button>
      {open ? (
        <div class="theme-pop" role="radiogroup" aria-label="选择主题">
          {THEMES.map((item) => (
            <button
              key={item.id}
              class="theme-pop-item"
              type="button"
              role="radio"
              aria-checked={item.id === theme}
              onClick={() => {
                onChange(item.id);
                setOpen(false);
              }}
            >
              <ThemeTile theme={item.id} paper={item.paper} accent={item.accent} />
              <span class="theme-pop-text">
                <strong>{item.name}</strong>
                <small>{item.tagline}</small>
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
