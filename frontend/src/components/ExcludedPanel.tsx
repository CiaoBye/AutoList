import type { ComponentChildren } from "preact";
import { formatSize, formatTime, excludedReasons, excludedSentence } from "../format";
import type { FilmDetail } from "../types";

type Excluded = FilmDetail["excluded_candidates"][number];

/**
 * 等待资源：寻片后没有合格候选时的说明。列出被排除的原因（一句话），告知下一次自动寻片的时间；
 * 其中“确实是这部影片、只是不符合入馆标准”的资源可以人工放行（需要确认）。
 */
export function ExcludedPanel({ summary, count, candidates, nextSearchAt, busy, onRelease, identity, onFixIdentity, identityHref, children }: {
  summary: FilmDetail["excluded_summary"];
  count: number;
  candidates: Excluded[];
  nextSearchAt: string | null | undefined;
  busy: boolean;
  onRelease: (candidate: Excluded) => void;
  /** TMDB 识别与片单来源对不上时的标记；只提示，改不改由人决定。 */
  identity?: FilmDetail["identity_flag"];
  onFixIdentity?: (tmdbId: number) => void;
  identityHref?: string;
  children?: ComponentChildren;
}) {
  return (
    <div class="excluded-panel">
      <div class="excluded-head">
        <div class="excluded-text">
          <span title={excludedReasons(summary) || undefined}>{excludedSentence(summary, count)}</span>
          {nextSearchAt ? <small class="muted">下次自动寻片 {formatTime(nextSearchAt)}</small> : null}
        </div>
        {children ? <span class="actions">{children}</span> : null}
      </div>
      {identity ? (
        <div class="identity-flag" role="status">
          <strong>TMDB 识别可能有误</strong>
          <span>{identity.reason}</span>
          {identity.suggested ? (
            <span>
              TMDB 反查：《{identity.suggested.title}》{identity.suggested.year ? `（${identity.suggested.year}）` : ""}
            </span>
          ) : null}
          <span class="actions">
            {identity.suggested && onFixIdentity ? (
              <button class="btn btn-small" type="button" disabled={busy} onClick={() => onFixIdentity(identity.suggested!.tmdb_id)}>
                改用这部
              </button>
            ) : null}
            {identityHref ? (
              <a class="btn btn-small" href={identityHref}>
                核对识别
              </a>
            ) : null}
          </span>
        </div>
      ) : null}
      {candidates.length ? (
        <details class="excluded-release">
          <summary title="确实是这部影片、只是不符合入馆标准时，可以手动放行并选定">人工放行（{candidates.length}）</summary>
          <ul>
            {candidates.map((candidate) => (
              <li key={candidate.id}>
                <span class="excluded-release-text">
                  <span class="mono excluded-release-title">{candidate.title}</span>
                  <small class="muted">
                    {candidate.site_name || "未知站点"} · {formatSize(candidate.size)} · {candidate.seeders ?? 0} 做种 · {candidate.exclusion_reason}
                  </small>
                </span>
                <button
                  class="btn btn-small"
                  type="button"
                  disabled={busy || !candidate.context_available}
                  title={candidate.context_available ? undefined : "候选已过期，请重新寻片"}
                  onClick={() => onRelease(candidate)}
                >
                  {candidate.context_available ? "放行并选定" : "已过期"}
                </button>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
