import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../../api";
import { useLoad } from "../../hooks";
import { href } from "../../router";
import type { CandidateAnalysis, ReleaseGroupCatalog } from "../../types";
import { CardHead, DirtyNote, SectionHead, useAction } from "./shared";

interface Profile {
  id: string;
  label: string;
  enabled: boolean;
  codecs: string[];
  groups: string[];
  tier: number;
}

interface Policy {
  profiles: Profile[];
  resolution_order: string[];
  hard_exclusions: { id: string; label: string; enabled: boolean }[];
  custom_release_groups: string[];
  candidate_limit: number;
}

interface Draft {
  policy: Policy;
  groupText: Record<string, string>;
  customText: string;
}

const RESOLUTIONS = ["2160p", "1080p", "720p"];

const splitGroups = (value: string): string[] => [
  ...new Set(value.split(/[,，\s]+/).map((item) => item.trim().toUpperCase()).filter(Boolean)),
];

const draftOf = (policy: Policy): Draft => ({
  policy,
  groupText: Object.fromEntries((policy.profiles || []).map((profile) => [profile.id, profile.groups.join(", ")])),
  customText: (policy.custom_release_groups || []).join("\n"),
});

/** 页面上的规则（含未保存的修改）整理成提交给服务端的格式。 */
const collect = ({ policy, groupText, customText }: Draft): Policy => ({
  ...policy,
  profiles: policy.profiles.map((profile) => ({ ...profile, groups: splitGroups(groupText[profile.id] ?? profile.groups.join(",")) })),
  custom_release_groups: customText.split("\n").map((item) => item.trim()).filter(Boolean),
});

const VERDICT: Record<string, { text: string; className: string }> = {
  preferred: { text: "可入馆 · 推荐", className: "is-ok" },
  fallback: { text: "可入馆 · 保底，需要你确认", className: "is-warn" },
  excluded: { text: "不能入馆", className: "is-bad" },
};

/** 标题试算：右侧固定，规则或输入变化后自动按当前页面上的规则重算。 */
function Trial({ rules, valid, order }: { rules: Policy | null; valid: boolean; order: string[] }) {
  const [trial, setTrial] = useState({ title: "", seeders: "10", free: false });
  const [preview, setPreview] = useState<{ result?: CandidateAnalysis; error?: string } | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const rulesKey = rules ? JSON.stringify(rules) : "";
  const title = trial.title.trim();

  useEffect(() => {
    if (!title || !rules || !valid) {
      setPreview(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setEvaluating(true);
      try {
        const result = await api<CandidateAnalysis>("/api/config/score-preview", {
          method: "POST",
          signal: controller.signal,
          body: { title, seeders: Number(trial.seeders) || 0, volume_factor: trial.free ? 0 : 1, candidate_policy: rules },
        });
        setPreview({ result });
      } catch (error) {
        if (controller.signal.aborted) return;
        setPreview({ error: error instanceof ApiError ? error.message : "试算失败" });
      } finally {
        if (!controller.signal.aborted) setEvaluating(false);
      }
    }, 350);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [title, trial.seeders, trial.free, rulesKey, valid]);

  const result = preview?.result;
  const verdict = result ? VERDICT[result.recommendation] || VERDICT.excluded : null;
  const rank = result ? order.indexOf(result.resolution) : -1;

  return (
    <section class="card setting-card rules-trial" aria-labelledby="trial-title">
      <CardHead id="trial-title" title="标题试算" note="按左侧当前规则，含未保存的修改" />
      <div class="setting-card-body">
        <label class="field">
          <span>发布名</span>
          <input
            value={trial.title}
            placeholder="Casablanca 1942 1080p BluRay x265 10bit-CHD"
            onInput={(event) => { const next = (event.target as HTMLInputElement).value; setTrial((current) => ({ ...current, title: next })); }}
          />
        </label>
        <div class="rules-trial-inputs">
          <label class="field">
            <span>做种人数</span>
            <input type="number" min={0} value={trial.seeders} onInput={(event) => { const next = (event.target as HTMLInputElement).value; setTrial((current) => ({ ...current, seeders: next })); }} />
          </label>
          <label class="field">
            <span>免费</span>
            <input class="switch" type="checkbox" role="switch" checked={trial.free} onChange={(event) => { const next = (event.target as HTMLInputElement).checked; setTrial((current) => ({ ...current, free: next })); }} />
          </label>
        </div>
        {!title ? (
          <p class="muted settings-note">输入一个发布名，结果会随规则的修改自动更新。</p>
        ) : !valid ? (
          <p class="notice notice-bad">左侧规则有误，修正后再试算。</p>
        ) : preview?.error ? (
          <p class="notice notice-bad">{preview.error}</p>
        ) : result && verdict ? (
          <>
            <div class={`trial-verdict ${verdict.className}`} role="status" aria-busy={evaluating}>
              <strong>{verdict.text}</strong>
              <span>{result.eligible ? `命中“${result.profile_label}”` : result.exclusion_reason}</span>
            </div>
            <dl class="trial-facts">
              <div><dt>分辨率</dt><dd>{result.resolution} · {rank >= 0 ? `第 ${rank + 1} 优先` : "不在顺序里，排在最后"}</dd></div>
              <div><dt>编码</dt><dd class="mono">{result.codec}</dd></div>
              <div><dt>制作组</dt><dd class="mono">{result.group || "未识别"}</dd></div>
              <div><dt>来源</dt><dd>{result.source || "—"}</dd></div>
              <div><dt>做种与优惠</dt><dd>{Number(trial.seeders) || 0} 人做种{trial.free ? " · 免费" : ""}</dd></div>
            </dl>
          </>
        ) : (
          <p class="muted settings-note">试算中……</p>
        )}
      </div>
    </section>
  );
}

export function Rules() {
  const config = useLoad<{ candidate_policy?: string; candidate_limit?: string }>((signal) => api("/api/config", { signal }), []);
  const catalog = useLoad<ReleaseGroupCatalog>((signal) => api<ReleaseGroupCatalog>("/api/config/release-groups", { signal }), []);
  const [saved, setSaved] = useState<Policy | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const { busy, run } = useAction();

  useEffect(() => {
    if (!config.data) return;
    try {
      const parsed = JSON.parse(config.data.candidate_policy || "{}") as Policy;
      setSaved(parsed);
      setDraft(draftOf(parsed));
    } catch {
      // 规则解析失败时保持读取中的提示。
    }
  }, [config.data]);

  const rules = draft ? collect(draft) : null;
  const dirty = Boolean(rules && saved && JSON.stringify(rules) !== JSON.stringify(collect(draftOf(saved))));
  const orderValid = rules ? new Set(rules.resolution_order).size === rules.resolution_order.length : false;
  const profilesValid = rules ? rules.profiles.every((profile) => profile.groups.length > 0) : false;
  const valid = orderValid && profilesValid;

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (config.error && !config.data) return <div class="notice notice-bad" role="alert">规则读取失败：{config.error.message}</div>;
  if (!draft || !saved || !rules) return <p class="muted">正在读取入馆标准……</p>;

  const { policy, groupText, customText } = draft;
  const order = policy.resolution_order;
  // 基于最新草稿修改，避免连续操作时用到旧值。
  const update = (change: (current: Draft) => Draft) => setDraft((current) => (current ? change(current) : current));
  const updatePolicy = (change: (current: Policy) => Policy) => update((current) => ({ ...current, policy: change(current.policy) }));
  const replaceAt = <T,>(list: T[], index: number, change: (item: T) => T) => list.map((item, position) => (position === index ? change(item) : item));

  const save = (event: Event) => {
    event.preventDefault();
    void run(
      () => api("/api/config", { method: "PUT", body: { candidate_policy: rules, candidate_limit: rules.candidate_limit } }),
      "入馆标准已保存，将用于下一次寻片",
      async () => {
        await Promise.all([config.reload(), catalog.reload()]);
      },
    );
  };

  return (
    <div class="settings-form">
      <SectionHead title="入馆标准">
        先判断能否入馆（硬性排除、允许组合），再给合格资源排序：推荐组合优先于保底 → 站点优先级（数字越小越靠前，在
        <a href={href("/settings/sites")}>站点</a>里设置）→ 分辨率顺序 → 做种人数多 → 免费与折扣。
      </SectionHead>

      <div class="rules-layout">
        <form class="rules-main" onSubmit={save}>
          <section class="card setting-card" aria-labelledby="rules-exclusions">
            <CardHead id="rules-exclusions" step={1} title="硬性排除" note="点亮的类型会被排除，不出现在挑选台" />
            <div class="setting-card-body">
              <div class="rule-tags">
                {policy.hard_exclusions.map((rule, index) => (
                  <button
                    key={rule.id}
                    class="rule-tag"
                    type="button"
                    aria-pressed={rule.enabled}
                    onClick={() =>
                      updatePolicy((current) => ({ ...current, hard_exclusions: replaceAt(current.hard_exclusions, index, (item) => ({ ...item, enabled: !item.enabled })) }))
                    }
                  >
                    {rule.label}
                  </button>
                ))}
              </div>
              <p class="muted settings-note">
                普通 BluRay 压制不受影响。另有三条固定规则：0 人做种、制作组未识别、知名制作组与其惯用编码不符（疑似冒用组名，如 Fury 应为 x265、SPM 应为 x264）也会排除。
              </p>
            </div>
          </section>

          <section class="card setting-card" aria-labelledby="rules-profiles">
            <CardHead id="rules-profiles" step={2} title="允许组合" note="编码与制作组必须同时命中；推荐直接可选，保底需要你确认" />
            {policy.profiles.map((profile, index) => {
              const empty = splitGroups(groupText[profile.id] ?? "").length === 0;
              return (
                <div key={profile.id} class={`setting-row setting-row-field${profile.enabled ? "" : " is-off"}`}>
                  <label class="setting-row-text" for={`profile-${profile.id}`}>
                    <strong>
                      {profile.label} <span class={`badge ${profile.tier === 1 ? "st-in_library" : "st-candidates"}`}>{profile.tier === 1 ? "推荐" : "保底"}</span>
                    </strong>
                    <small class={empty ? "text-issue" : undefined}>
                      编码 <span class="mono">{profile.codecs.join(" / ")}</span>
                      {empty ? " · 至少需要一个制作组" : ""}
                    </small>
                  </label>
                  <span class="setting-row-control">
                    <input
                      id={`profile-${profile.id}`}
                      class="mono"
                      value={groupText[profile.id] ?? ""}
                      aria-invalid={empty}
                      placeholder="制作组，逗号或空格分隔"
                      title="制作组，逗号或空格分隔"
                      onInput={(event) => {
                        const value = (event.target as HTMLInputElement).value;
                        update((current) => ({ ...current, groupText: { ...current.groupText, [profile.id]: value } }));
                      }}
                    />
                    <input
                      class="switch"
                      type="checkbox"
                      role="switch"
                      aria-label={`启用${profile.label}`}
                      checked={profile.enabled}
                      onChange={(event) => {
                        const enabled = (event.target as HTMLInputElement).checked;
                        updatePolicy((current) => ({ ...current, profiles: replaceAt(current.profiles, index, (item) => ({ ...item, enabled })) }));
                      }}
                    />
                  </span>
                </div>
              );
            })}
          </section>

          <section class="card setting-card" aria-labelledby="rules-order">
            <CardHead id="rules-order" step={3} title="分辨率与数量" />
            <div class="setting-row setting-row-field">
              <span class="setting-row-text">
                <strong>分辨率优先顺序</strong>
                <small class={orderValid ? undefined : "text-issue"}>{orderValid ? "不在顺序里的分辨率仍可入馆，排在最后" : "分辨率顺序不能重复"}</small>
              </span>
              <span class="setting-row-control rules-order">
                {order.map((value, index) => (
                  <select key={index} aria-label={`第 ${index + 1} 优先`} aria-invalid={!orderValid} value={value} onChange={(event) => {
                      const value = (event.target as HTMLSelectElement).value;
                      updatePolicy((current) => ({ ...current, resolution_order: replaceAt(current.resolution_order, index, () => value) }));
                    }}>
                    {RESOLUTIONS.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                ))}
              </span>
            </div>
            <div class="setting-row">
              <label class="setting-row-text" for="rules-limit">
                <strong>每部影片保留候选数</strong>
                <small>只保留排序靠前的若干个候选，1–20</small>
              </label>
              <span class="setting-row-control setting-row-control-narrow">
                <input
                  id="rules-limit"
                  type="number"
                  min={1}
                  max={20}
                  value={policy.candidate_limit}
                  onInput={(event) => {
                    const limit = Math.min(20, Math.max(1, Number((event.target as HTMLInputElement).value) || 6));
                    updatePolicy((current) => ({ ...current, candidate_limit: limit }));
                  }}
                />
              </span>
            </div>
          </section>

          <section class="card setting-card" aria-labelledby="rules-groups">
            <CardHead
              id="rules-groups"
              step={4}
              title="制作组词表"
              note={catalog.data ? `内置 ${catalog.data.builtin_names.length} 个制作组（${catalog.data.builtin_count} 条识别规则），自定义 ${catalog.data.custom_count} 条` : "正在读取词表……"}
            />
            <div class="setting-card-body">
              <label class="field">
                <span>自定义制作组规则</span>
                <textarea
                  rows={4}
                  class="mono"
                  value={customText}
                  placeholder={"例如：MyGroup(?:HD|WEB)?"}
                  onInput={(event) => {
                    const value = (event.target as HTMLTextAreaElement).value;
                    update((current) => ({ ...current, customText: value }));
                  }}
                />
                <small class="field-hint">每行一条正则，不区分大小写；会拒绝可能导致性能问题的写法。</small>
              </label>
              {catalog.data ? (
                <details>
                  <summary class="muted">查看内置制作组（{catalog.data.builtin_names.length} 个）</summary>
                  <div class="chip-cloud">
                    {catalog.data.builtin_names.map((name) => (
                      <span key={name} class="badge st-missing">
                        {name}
                      </span>
                    ))}
                  </div>
                </details>
              ) : null}
            </div>
          </section>

          <div class={`card save-bar${dirty ? " is-dirty" : ""}`}>
            <DirtyNote dirty={dirty} onReset={() => setDraft(draftOf(saved))} />
            <button class="btn btn-primary" type="submit" disabled={busy || !dirty || !valid}>
              {busy ? "保存中…" : "保存入馆标准"}
            </button>
          </div>
        </form>

        <Trial rules={rules} valid={valid} order={order} />
      </div>
    </div>
  );
}
