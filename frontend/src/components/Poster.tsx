import { useState } from "preact/hooks";
import { posterTone } from "../format";
import { ISSUE_LABELS, STATUS_BAR_COLOR, STATUS_LABELS } from "../status";
import type { FilmIssue, FilmStatus } from "../types";

interface PosterProps {
  id: number;
  title: string;
  rank?: number | null;
  url: string | null;
  /** 传入时在海报右下角标出状态；海报墙与首页使用，详情与挑选页不传。 */
  status?: FilmStatus;
  issues?: FilmIssue[];
}

/** 常态只用小圆点，需要处理或正在进行的状态才显示文字，避免整面海报墙都是标签。 */
const QUIET_STATUSES: FilmStatus[] = ["in_library", "missing", "unchecked"];
/** 还没入馆、也没有进展的影片海报降低饱和度，一眼看出馆藏进度。 */
const MUTED_STATUSES: FilmStatus[] = ["missing", "unchecked", "unrecognized"];

/** 有海报时显示海报，加载失败或没有海报时显示排版占位。 */
export function Poster({ id, title, rank, url, status, issues = [] }: PosterProps) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const showImage = Boolean(url) && !failed;
  const classes = [
    "poster",
    `poster-tone-${posterTone(id)}`,
    showImage ? "has-image" : "",
    loaded ? "is-loaded" : "",
    status && MUTED_STATUSES.includes(status) && !issues.length ? "is-muted" : "",
  ];
  return (
    <span class={classes.filter(Boolean).join(" ")}>
      {showImage && (
        <img
          src={url!}
          alt=""
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
        />
      )}
      <span class="poster-rank">{rank ? `#${rank}` : ""}</span>
      <span class="poster-title">{title}</span>
      {status ? <PosterState status={status} issues={issues} /> : null}
    </span>
  );
}

function PosterState({ status, issues }: { status: FilmStatus; issues: FilmIssue[] }) {
  if (issues.length) {
    return (
      <span class="poster-state poster-state-issue" aria-hidden="true">
        {ISSUE_LABELS[issues[0]]}
      </span>
    );
  }
  if (QUIET_STATUSES.includes(status)) {
    return (
      <span
        class="poster-dot"
        style={{ background: STATUS_BAR_COLOR[status] }}
        title={STATUS_LABELS[status]}
        aria-hidden="true"
      />
    );
  }
  return (
    <span class={`poster-state st-${status}`} aria-hidden="true">
      {STATUS_LABELS[status]}
    </span>
  );
}
