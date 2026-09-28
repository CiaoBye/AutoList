import { ISSUE_LABELS, STATUS_LABELS } from "../status";
import type { FilmIssue, FilmStatus } from "../types";

export function StatusBadge({ status }: { status: FilmStatus }) {
  return <span class={`badge st-${status}`}>{STATUS_LABELS[status]}</span>;
}

export function IssueBadges({ issues }: { issues: FilmIssue[] }) {
  if (!issues.length) return null;
  return (
    <>
      {issues.map((issue) => (
        <span key={issue} class="badge st-issue">
          {ISSUE_LABELS[issue]}
        </span>
      ))}
    </>
  );
}
