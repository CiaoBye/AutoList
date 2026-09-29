import { useEffect, useState } from "preact/hooks";
import { api } from "../../api";
import { useLoad } from "../../hooks";
import { href } from "../../router";
import type { CandidateAnalysis, ReleaseGroupCatalog } from "../../types";
import { SectionHead, TextField, useAction } from "./shared";

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

const RESOLUTIONS = ["2160p", "1080p", "720p"];

const splitGroups = (value: string): string[] => [
  ...new Set(value.split(/[,，\s]+/).map((item) => item.trim().toUpperCase()).filter(Boolean)),
];

export function Rules() {
  const config = useLoad<{ candidate_policy?: string; candidate_limit?: string }>((signal) => api("/api/config", { signal }), []);
  const catalog = useLoad<ReleaseGroupCatalog>((signal) => api<ReleaseGroupCatalog>("/api/config/release-groups", { signal }), []);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [groupText, setGroupText] = useState<Record<string, string>>({});
  const [customText, setCustomText] = useState("");
  const [trial, setTrial] = useState({ title: "", seeders: "10", free: false });
  const [preview, setPreview] = useState<CandidateAnalysis | null>(null);
  const { busy, run } = useAction();

  useEffect(() => {
    if (!config.data) return;
    let parsed: Policy;
    try {
      parsed = JSON.parse(config.data.candidate_policy || "{}") as Policy;
    } catch {
      return;
    }
    setPolicy(parsed);
    setGroupText(Object.fromEntries((parsed.profiles || []).map((profile) => [profile.id, profile.groups.join(", ")])));
    setCustomText((parsed.custom_release_groups || []).join("\n"));
  }, [config.data]);

  if (config.error && !config.data) return <div class="notice notice-bad" role="alert">规则读取失败：{config.error.message}</div>;
  if (!policy) return <p class="muted">正在读取入馆标准……</p>;

  const collect = (): Policy => ({
    ...policy,
    profiles: policy.profiles.map((profile) => ({ ...profile, groups: splitGroups(groupText[profile.id] ?? profile.groups.join(",")) })),
    custom_release_groups: customText.split("\n").map((item) => item.trim()).filter(Boolean),
  });

  const order = policy.resolution_order;
  const setOrder = (index: number, value: string) => {
    const next = [...order];
    next[index] = value;
    setPolicy({ ...policy, resolution_order: next });
  };
  const orderValid = new Set(order).size === order.length;
  const profilesValid = policy.profiles.every((profile) => splitGroups(groupText[profile.id] ?? "").length > 0);

  const save = (event: Event) => {
    event.preventDefault();
    const next = collect();
    void run(
      () => api("/api/config", { method: "PUT", body: { candidate_policy: next, candidate_limit: next.candidate_limit } }),
      "入馆标准已保存，将用于下一次寻片",
      async () => {
        await Promise.all([config.reload(), catalog.reload()]);
      },
    );
  };

  const runTrial = (event: Event) => {
    event.preventDefault();
    if (!trial.title.trim()) return;
    void run(
      async () => {
        const result = await api<CandidateAnalysis>("/api/config/score-preview", {
          method: "POST",
          body: { title: trial.title.trim(), seeders: Number(trial.seeders) || 0, volume_factor: trial.free ? 0 : 1, candidate_policy: collect() },
        });
        setPreview(result);
      },
      "已按当前（未保存的）规则试算",
    );
  };

  return (
    <div class="settings-form">
      <SectionHead title="入馆标准">
        先判断能否入馆（硬性排除、允许组合），再给合格资源排序：推荐组合优先于保底 → 站点优先级（数字越小越靠前，在
        <a href={href("/settings/sites")}>站点</a>里设置）→ 分辨率顺序 → 做种人数多 → 免费与折扣。
      </SectionHead>

      <form class="settings-form" onSubmit={save}>
        <fieldset class="card settings-group">
          <legend class="settings-legend">1 · 硬性排除</legend>
          <p class="muted settings-note">
            命中即排除，不会出现在挑选台；普通 BluRay 压制不受影响。另有三条固定规则：0 人做种、制作组未识别、知名制作组与其惯用编码不符（疑似冒用组名，如
            Fury 应为 x265、SPM 应为 x264）也会排除。
          </p>
          <div class="check-grid">
            {policy.hard_exclusions.map((rule, index) => (
              <label key={rule.id} class="check">
                <input
                  type="checkbox"
                  checked={rule.enabled}
                  onChange={(event) => {
                    const next = [...policy.hard_exclusions];
                    next[index] = { ...rule, enabled: (event.target as HTMLInputElement).checked };
                    setPolicy({ ...policy, hard_exclusions: next });
                  }}
                />
                <span>{rule.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset class="card settings-group">
          <legend class="settings-legend">2 · 允许组合</legend>
          <p class="muted settings-note">编码与制作组必须同时命中；第一层推荐，其后作为保底，需要你确认。</p>
          {policy.profiles.map((profile, index) => (
            <div key={profile.id} class="profile-row">
              <label class="check">
                <input
                  type="checkbox"
                  checked={profile.enabled}
                  onChange={(event) => {
                    const next = [...policy.profiles];
                    next[index] = { ...profile, enabled: (event.target as HTMLInputElement).checked };
                    setPolicy({ ...policy, profiles: next });
                  }}
                />
                <span>
                  <strong>{profile.label}</strong> · 编码 <span class="mono">{profile.codecs.join(" / ")}</span>
                  {profile.tier === 1 ? " · 推荐" : " · 保底"}
                </span>
              </label>
              <TextField
                label="制作组（逗号或空格分隔）"
                value={groupText[profile.id] ?? ""}
                onInput={(value) => setGroupText({ ...groupText, [profile.id]: value })}
              />
            </div>
          ))}
          {!profilesValid ? <span class="notice notice-bad">每个组合至少需要一个制作组</span> : null}
        </fieldset>

        <fieldset class="card settings-group">
          <legend class="settings-legend">3 · 分辨率与数量</legend>
          <p class="muted settings-note">不在顺序里的分辨率仍可入馆，排在最后；每部影片只保留排序靠前的若干个候选。</p>
          <div class="field-grid">
            {order.map((value, index) => (
              <label key={index} class="field">
                <span>第 {index + 1} 优先</span>
                <select value={value} onChange={(event) => setOrder(index, (event.target as HTMLSelectElement).value)}>
                  {RESOLUTIONS.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <TextField
              label="每部影片保留候选数"
              type="number"
              value={policy.candidate_limit}
              onInput={(value) => setPolicy({ ...policy, candidate_limit: Math.min(20, Math.max(1, Number(value) || 6)) })}
              hint="1–20"
            />
          </div>
          {!orderValid ? <span class="notice notice-bad">分辨率顺序不能重复</span> : null}
        </fieldset>

        <fieldset class="card settings-group">
          <legend class="settings-legend">4 · 制作组词表</legend>
          <p class="muted settings-note">
            {catalog.data
              ? `内置 ${catalog.data.builtin_names.length} 个制作组（${catalog.data.builtin_count} 条识别规则，部分制作组有多种写法），自定义 ${catalog.data.custom_count} 条。`
              : "正在读取词表……"}
            自定义规则每行一条正则，不区分大小写；会拒绝可能导致性能问题的写法。
          </p>
          <label class="field">
            <span>自定义制作组规则</span>
            <textarea rows={4} value={customText} placeholder={"例如：MyGroup(?:HD|WEB)?"} onInput={(event) => setCustomText((event.target as HTMLTextAreaElement).value)} />
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
        </fieldset>

        <div class="settings-actions">
          <button class="btn btn-primary btn-large" type="submit" disabled={busy || !orderValid || !profilesValid}>
            {busy ? "处理中…" : "保存入馆标准"}
          </button>
        </div>
      </form>

      <form class="card settings-group" onSubmit={runTrial}>
        <h3 class="settings-legend">标题试算</h3>
        <p class="muted settings-note">用当前页面上（包括未保存）的规则判断一个发布名能否入馆。</p>
        <div class="toolbar">
          <label class="field field-grow">
            <span>发布名</span>
            <input value={trial.title} placeholder="Casablanca 1942 1080p BluRay x265 10bit-CHD" onInput={(event) => setTrial({ ...trial, title: (event.target as HTMLInputElement).value })} />
          </label>
          <label class="field">
            <span>做种数</span>
            <input type="number" value={trial.seeders} onInput={(event) => setTrial({ ...trial, seeders: (event.target as HTMLInputElement).value })} style={{ width: "96px" }} />
          </label>
          <label class="check" style={{ alignSelf: "flex-end", minHeight: "44px" }}>
            <input type="checkbox" checked={trial.free} onChange={(event) => setTrial({ ...trial, free: (event.target as HTMLInputElement).checked })} />
            <span>免费</span>
          </label>
          <button class="btn" type="submit" disabled={busy || !trial.title.trim()}>
            试算
          </button>
        </div>
        {preview ? (
          <div class={`notice${preview.eligible ? "" : " notice-bad"}`} role="status">
            <strong>{preview.eligible ? (preview.recommendation === "preferred" ? "可入馆 · 推荐" : "可入馆 · 保底") : "不能入馆"}</strong>
            <span>
              {preview.resolution} · {preview.codec} · {preview.group || "未识别制作组"}
              {preview.eligible ? ` · ${preview.profile_label}` : ` · ${preview.exclusion_reason}`}
            </span>
          </div>
        ) : null}
      </form>
    </div>
  );
}
