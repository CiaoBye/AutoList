export const formatSize = (bytes: number | null | undefined): string => {
  if (!bytes) return "—";
  const gb = bytes / 1024 ** 3;
  // 与站点上显示的体积一致（两位小数），不四舍五入成整数。
  if (gb >= 1024) return `${(gb / 1024).toFixed(2)} TB`;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${Math.max(0.01, bytes / 1024 ** 2).toFixed(2)} MB`;
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

/** 传输速度：B/s、KB/s、MB/s。 */
export const formatRate = (bytesPerSecond: number): string => {
  if (bytesPerSecond >= 1024 ** 2) return `${(bytesPerSecond / 1024 ** 2).toFixed(1)} MB/s`;
  if (bytesPerSecond >= 1024) return `${Math.round(bytesPerSecond / 1024)} KB/s`;
  return `${bytesPerSecond} B/s`;
};

/** 剩余时间（Transmission 的 eta 秒数）；未知时返回 null。 */
export const formatEta = (seconds: number | null | undefined): string | null => {
  if (seconds == null || seconds < 0) return null;
  if (seconds >= 86400) return `剩余约 ${Math.round(seconds / 86400)} 天`;
  if (seconds >= 3600) return `剩余 ${Math.floor(seconds / 3600)} 小时 ${Math.round((seconds % 3600) / 60)} 分`;
  return `剩余 ${Math.max(1, Math.round(seconds / 60))} 分钟`;
};

export const percent = (part: number, whole: number): number => (whole > 0 ? (part / whole) * 100 : 0);

/** 百分比的显示文字：最多一位小数，整数不带小数点（2.8、68、<0.1），避免浮点误差露出 2.8000000000000003。 */
export const percentLabel = (part: number, whole: number): string => {
  const value = percent(part, whole);
  if (value > 0 && value < 0.1) return "<0.1";
  return String(Math.round(value * 10) / 10);
};

/** 按影片编号稳定选取海报占位色，同一部电影每次颜色一致。 */
export const posterTone = (id: number): number => (Math.abs(id) % 6) + 1;
