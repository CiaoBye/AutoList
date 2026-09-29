import { formatSize } from "../format";
import type { Candidate } from "../types";

const RECOMMENDATION_LABELS: Record<string, string> = {
  preferred: "推荐",
  fallback: "保底",
  manual: "人工确认",
};

/** 一条候选资源：同种发布跨站点折叠为一行，已选定时取消的是清单中那个站点的条目。 */
export function CandidateRow({ candidate, onToggle, busy }: { candidate: Candidate; onToggle: (id: string) => void; busy: boolean }) {
  const selected = Boolean(candidate.in_selection);
  const toggleId = selected ? candidate.site_options.find((option) => option.in_selection)?.id ?? candidate.id : candidate.id;
  const label = RECOMMENDATION_LABELS[candidate.recommendation] || "候选";
  return (
    <div class="candidate">
      <div style={{ display: "flex", flexDirection: "column", gap: "6px", minWidth: 0 }}>
        <span style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          <span class={`badge ${candidate.recommendation === "preferred" ? "st-candidates" : "st-missing"}`}>{label}</span>
          {candidate.is_free ? <span class="badge st-in_library">免费</span> : null}
        </span>
        <span class="candidate-name">{candidate.title}</span>
        <span class="candidate-meta">
          <span>{candidate.site_name || "未知站点"}{candidate.site_count > 1 ? ` 等 ${candidate.site_count} 个站点` : ""}</span>
          <span class="mono">{formatSize(candidate.size)}</span>
          <span class="mono">{candidate.seeders ?? 0} 做种</span>
          {candidate.recommendation_reason ? <span>{candidate.recommendation_reason}</span> : null}
        </span>
      </div>
      <button
        class={`btn btn-small${!selected && candidate.context_available ? " btn-primary" : ""}`}
        type="button"
        disabled={busy || (!selected && !candidate.context_available)}
        title={!selected && !candidate.context_available ? "候选已过期，请重新寻片" : undefined}
        onClick={() => onToggle(toggleId)}
      >
        {selected ? "取消选定" : candidate.context_available ? "选定" : "已过期"}
      </button>
    </div>
  );
}
