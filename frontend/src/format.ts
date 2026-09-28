export const formatSize = (bytes: number | null | undefined): string => {
  if (!bytes) return "—";
  const gb = bytes / 1024 ** 3;
  if (gb >= 1024) {
    const tb = gb / 1024;
    return `${tb >= 100 ? tb.toFixed(0) : tb.toFixed(1)} TB`;
  }
  if (gb >= 1) return `${gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
};

export const formatTime = (value: string | null | undefined): string => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

export const percent = (part: number, whole: number): number => (whole > 0 ? (part / whole) * 100 : 0);

/** 按影片编号稳定选取海报占位色，同一部电影每次颜色一致。 */
export const posterTone = (id: number): number => (Math.abs(id) % 6) + 1;
