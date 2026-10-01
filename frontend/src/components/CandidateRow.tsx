import { useState } from "preact/hooks";
import { formatSize } from "../format";
import type { Candidate } from "../types";

const RECOMMENDATION_LABELS: Record<string, string> = {
  preferred: "推荐",
  fallback: "保底",
  manual: "人工确认",
};

type SiteOption = Candidate["site_options"][number];

function ToggleButton({ selected, available, busy, onClick }: { selected: boolean; available: boolean; busy: boolean; onClick: () => void }) {
  return (
    <button
      class={`btn btn-small${!selected && available ? " btn-primary" : ""}`}
      type="button"
      disabled={busy || (!selected && !available)}
      title={!selected && !available ? "候选已过期，请重新寻片" : undefined}
      onClick={onClick}
    >
      {selected ? "取消选定" : available ? "选定" : "已过期"}
    </button>
  );
}

/** 种子标题：有站点详情页时在新标签页打开。 */
function ReleaseTitle({ title, url, className }: { title: string; url: string | null; className: string }) {
  return url ? (
    <a class={className} href={url} target="_blank" rel="noopener noreferrer" title="在站点打开种子详情页">
      {title}
    </a>
  ) : (
    <span class={className}>{title}</span>
  );
}

/** 一条候选资源：同一发布跨站点折叠，显示做种最多的一条，其余站点可展开逐个选定。 */
export function CandidateRow({ candidate, onToggle, busy }: { candidate: Candidate; onToggle: (id: string) => void; busy: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const selected = Boolean(candidate.in_selection);
  const label = RECOMMENDATION_LABELS[candidate.recommendation] || "候选";
  const primary = candidate.site_options.find((option) => option.id === candidate.id);
  const others = candidate.site_options.filter((option) => option.id !== candidate.id);
  const selectedOther = others.find((option) => option.in_selection);
  // 已选定的是折叠的站点时，主行的按钮取消的是那一条。
  const toggleId = selected ? (primary?.in_selection ? candidate.id : selectedOther?.id ?? candidate.id) : candidate.id;
  const panelId = `sites-${candidate.id}`;
  return (
    <div class={`candidate${expanded ? " is-expanded" : ""}`}>
      <div class="candidate-main">
        <span style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          <span class={`badge ${candidate.recommendation === "preferred" ? "st-candidates" : "st-missing"}`}>{label}</span>
          {candidate.is_free ? <span class="badge st-in_library">免费</span> : null}
        </span>
        <ReleaseTitle className="candidate-name" title={candidate.title} url={candidate.detail_url} />
        <span class="candidate-meta">
          <span>{candidate.site_name || "未知站点"}</span>
          <span class="mono">{formatSize(candidate.size)}</span>
          <span class="mono">{candidate.seeders ?? 0} 做种</span>
          {candidate.recommendation_reason ? <span>{candidate.recommendation_reason}</span> : null}
          {others.length ? (
            <button
              class="candidate-more"
              type="button"
              aria-expanded={expanded}
              aria-controls={panelId}
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? "收起" : `另有 ${others.length} 个站点`}
              {selectedOther && !expanded ? `（已选定 ${selectedOther.site_name || "其他站点"}）` : ""}
            </button>
          ) : null}
        </span>
      </div>
      <ToggleButton selected={selected} available={candidate.context_available} busy={busy} onClick={() => onToggle(toggleId)} />
      {expanded ? (
        <ul class="candidate-sites" id={panelId}>
          {others.map((option: SiteOption) => (
            <li key={option.id}>
              <span class="candidate-site-text">
                <ReleaseTitle className="candidate-site-name" title={option.title} url={option.detail_url} />
                <span class="candidate-meta">
                  <span>{option.site_name || "未知站点"}</span>
                  <span class="mono">{formatSize(option.size)}</span>
                  <span class="mono">{option.seeders ?? 0} 做种</span>
                  {option.is_free ? <span>免费</span> : null}
                </span>
              </span>
              <ToggleButton
                selected={Boolean(option.in_selection)}
                available={option.context_available}
                busy={busy}
                onClick={() => onToggle(option.id)}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
