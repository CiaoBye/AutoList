import type { ComponentChildren } from "preact";
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../../api";
import { useToast } from "../../hooks";
import type { ProviderStatus, RuntimeSettings } from "../../types";
import { dotClass, serviceDetail, type SettingsHealth } from "./health";
import { DirtyNote, SectionHead, SecretField, TextField, useAction } from "./shared";
import { useSettingsDraft, type SettingsDraft } from "./useSettingsForm";

const saved = (settings: RuntimeSettings, name: string) => (settings[`${name}_configured`] ? "已保存" : "未设置");

interface ServiceSpec {
  key: string;
  label: string;
  icon: string;
  /** 检测接口里的名称；AI 辅助识别没有检测，按是否配置显示。 */
  provider?: string;
  description: string;
  fields: string[];
  summary: (settings: RuntimeSettings) => string;
  body: (draft: SettingsDraft) => ComponentChildren;
}

const text = (draft: SettingsDraft, name: string) => String(draft.form[name] ?? "");

const secret = (draft: SettingsDraft, name: string, label: string, hint?: string) => (
  <SecretField
    label={label}
    value={text(draft, name)}
    onInput={(value) => draft.set(name, value)}
    configured={draft.configured(name)}
    cleared={draft.cleared.includes(name)}
    onToggleClear={() => draft.toggleClear(name)}
    hint={hint}
  />
);

const SERVICES: ServiceSpec[] = [
  {
    key: "moviepilot",
    label: "MoviePilot",
    icon: "/assets/service-moviepilot.svg",
    provider: "moviepilot",
    description: "负责按分类规则把选定资源提交到 Transmission，是提交下载的必需服务。",
    fields: ["mp_base_url", "mp_api_key", "mp_timeout_seconds"],
    summary: (s) => `${s.mp_base_url || "未填写地址"} · API Key ${saved(s, "mp_api_key")} · 超时 ${s.mp_timeout_seconds} 秒`,
    body: (draft) => (
      <>
        <TextField label="地址" value={text(draft, "mp_base_url")} onInput={(value) => draft.set("mp_base_url", value)} placeholder="http://192.168.x.x:3000" inputMode="url" />
        {secret(draft, "mp_api_key", "API Key")}
        <TextField label="请求超时（秒）" type="number" value={text(draft, "mp_timeout_seconds")} onInput={(value) => draft.set("mp_timeout_seconds", value)} hint="同时用于 TMDB、Emby、Transmission 请求，3–300 秒" />
      </>
    ),
  },
  {
    key: "tmdb",
    label: "TMDB",
    icon: "/assets/service-tmdb.svg",
    provider: "tmdb",
    description: "识别影片、生成搜索关键词与海报。支持 v3 API Key 或 v4 读取令牌。",
    fields: ["tmdb_api_key", "tmdb_language", "mdblist_api_key"],
    summary: (s) => `API Key ${saved(s, "tmdb_api_key")} · 语言 ${s.tmdb_language || "zh-CN"} · MDBList ${saved(s, "mdblist_api_key")}`,
    body: (draft) => (
      <>
        {secret(draft, "tmdb_api_key", "API Key / 读取令牌")}
        <TextField label="语言" value={text(draft, "tmdb_language")} onInput={(value) => draft.set("tmdb_language", value)} placeholder="zh-CN" />
        {secret(draft, "mdblist_api_key", "MDBList API Key（可选）", "公开的 MDBList 片单无需填写")}
      </>
    ),
  },
  {
    key: "fanart",
    label: "Fanart.tv",
    icon: "/assets/service-fanart.svg",
    provider: "fanart",
    description: "配置后海报优先取自 fanart.tv：按 TMDB 语言、英文、无字版的顺序挑选，同语言取点赞最多的一张；fanart.tv 没有的影片仍用 Emby 或 TMDB 海报。与 TMDB 共用代理开关。",
    fields: ["fanart_api_key"],
    summary: (s) => `海报优先来源 · API Key ${saved(s, "fanart_api_key")}`,
    body: (draft) => secret(draft, "fanart_api_key", "Project API Key", "在 fanart.tv 登录后申请"),
  },
  {
    key: "emby",
    label: "Emby",
    icon: "/assets/service-emby.svg",
    provider: "emby",
    description: "判断影片是否已入馆：只有实体媒体文件算入馆，.strm 视为未完成。",
    fields: ["emby_base_url", "emby_api_key"],
    summary: (s) => `${s.emby_base_url || "未填写地址"} · API Key ${saved(s, "emby_api_key")}`,
    body: (draft) => (
      <>
        <TextField label="地址" value={text(draft, "emby_base_url")} onInput={(value) => draft.set("emby_base_url", value)} placeholder="http://192.168.x.x:8096" inputMode="url" />
        {secret(draft, "emby_api_key", "API Key")}
      </>
    ),
  },
  {
    key: "transmission",
    label: "Transmission",
    icon: "/assets/service-transmission.svg",
    provider: "transmission",
    description: "用于确认下载进度与避免重复下载；下载本身经 MoviePilot 提交。",
    fields: ["tr_base_url", "tr_username", "tr_password"],
    summary: (s) => `${s.tr_base_url || "未填写地址"} · 用户名${saved(s, "tr_username")} · 密码${saved(s, "tr_password")}`,
    body: (draft) => (
      <>
        <TextField label="地址" value={text(draft, "tr_base_url")} onInput={(value) => draft.set("tr_base_url", value)} placeholder="http://192.168.x.x:9091" inputMode="url" />
        {secret(draft, "tr_username", "用户名")}
        {secret(draft, "tr_password", "密码")}
      </>
    ),
  },
  {
    key: "ai",
    label: "AI 辅助识别",
    icon: "/assets/service-ai.svg",
    description: "TMDB 找不到时，用兼容 OpenAI 接口的模型纠正片名后再识别一次。可选。",
    fields: ["ai_base_url", "ai_api_key", "ai_model"],
    summary: (s) => (s.ai_base_url ? `${s.ai_base_url} · 模型 ${s.ai_model || "未填写"} · API Key ${saved(s, "ai_api_key")}` : "TMDB 找不到时用兼容 OpenAI 接口的模型纠正片名"),
    body: (draft) => (
      <>
        <TextField label="接口地址" value={text(draft, "ai_base_url")} onInput={(value) => draft.set("ai_base_url", value)} placeholder="https://api.example.com/v1" inputMode="url" />
        {secret(draft, "ai_api_key", "API Key")}
        <TextField label="模型" value={text(draft, "ai_model")} onInput={(value) => draft.set("ai_model", value)} />
      </>
    ),
  },
];

function statusBadge(spec: ServiceSpec, status: ProviderStatus | undefined, settings: RuntimeSettings) {
  if (!spec.provider) {
    return settings.ai_base_url && settings.ai_api_key_configured
      ? <span class="badge st-in_library">已配置</span>
      : <span class="badge st-missing">未配置 · 可选</span>;
  }
  if (!status) return <span class="badge st-unchecked">检测中</span>;
  if (status.ok) {
    const detail = serviceDetail(spec.key, status);
    return <span class="badge st-in_library">{detail === "连接正常" ? "正常" : `正常 · ${detail}`}</span>;
  }
  if (status.configured === false) return <span class="badge st-missing">未配置</span>;
  return <span class="badge st-issue">连接失败</span>;
}

function ServiceRow({
  spec, health, status, testing, onTest, onDirty,
}: {
  spec: ServiceSpec;
  health: SettingsHealth;
  status: ProviderStatus | undefined;
  testing: boolean;
  onTest: () => Promise<void>;
  onDirty: (key: string, dirty: boolean) => void;
}) {
  const draft = useSettingsDraft(spec.fields, health.settings);
  const { busy, run } = useAction();
  const [open, setOpen] = useState(false);
  const settings = health.settings.data;
  useEffect(() => onDirty(spec.key, draft.dirty), [draft.dirty]);
  if (!settings) return null;
  const failed = Boolean(spec.provider && status && !status.ok && status.configured !== false);
  const dot = spec.provider ? dotClass(status) : settings.ai_base_url && settings.ai_api_key_configured ? "dot-ok" : "dot-off";
  const panelId = `service-${spec.key}`;
  return (
    <div class={`service-row${open ? " is-open" : ""}`}>
      <button class="service-row-head" type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
        <span class="service-row-name">
          <span class={`dot ${dot}`} aria-hidden="true" />
          <img src={spec.icon} alt="" width="20" height="20" />
          {spec.label}
        </span>
        <span class="service-row-summary">{spec.summary(settings)}</span>
        {draft.dirty ? <span class="badge st-candidates">未保存</span> : statusBadge(spec, status, settings)}
        <span class="service-row-toggle">{open ? "收起" : "编辑"}</span>
      </button>
      {failed && !open ? <p class="service-row-error">{String(status?.message || "连接失败")}</p> : null}
      {open ? (
        <form
          id={panelId}
          class="service-row-body"
          onSubmit={(event) => {
            event.preventDefault();
            void run(draft.save, `${spec.label} 已保存`, () => (spec.provider ? onTest() : undefined));
          }}
        >
          <p class="muted settings-note">{spec.description}</p>
          {failed ? <p class="notice notice-bad service-row-notice">{String(status?.message || "连接失败")}</p> : null}
          <div class="field-grid">{spec.body(draft)}</div>
          <div class="service-row-actions">
            <DirtyNote dirty={draft.dirty} onReset={draft.reset} />
            {spec.provider ? (
              <button class="btn" type="button" disabled={testing} title={draft.dirty ? "检测使用已保存的配置" : undefined} onClick={() => void onTest()}>
                {testing ? "检测中…" : "检测"}
              </button>
            ) : null}
            <button class="btn btn-primary" type="submit" disabled={busy || !draft.dirty}>
              {busy ? "保存中…" : `保存 ${spec.label}`}
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

/** 服务连接：每个服务一行，点开编辑、单独检测与保存；检测结果沿用进入设置时的那次检测。 */
export function Services({ health }: { health: SettingsHealth }) {
  const toast = useToast();
  const [overrides, setOverrides] = useState<Record<string, ProviderStatus>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [dirtyRows, setDirtyRows] = useState<string[]>([]);
  const statuses = { ...(health.services.data || {}), ...overrides };

  useEffect(() => {
    if (!dirtyRows.length) return;
    // 有未保存的修改时，关闭或刷新页面前让浏览器提醒一次。
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyRows.length > 0]);

  const testOne = async (spec: ServiceSpec) => {
    if (!spec.provider) return;
    setTesting(spec.key);
    try {
      const response = await api<Record<string, ProviderStatus>>(`/api/settings/test?provider=${spec.provider}`, { method: "POST", timeoutMs: 45000 });
      setOverrides((current) => ({ ...current, ...response }));
      const item = response[spec.provider];
      toast.show(item?.ok ? `${spec.label}：连接正常` : `${spec.label}：${item?.message || "连接失败"}`);
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "检测失败");
    } finally {
      setTesting(null);
    }
  };

  const testAll = () => {
    setOverrides({});
    void health.services.reload();
  };

  if (health.settings.error && !health.settings.data) {
    return <div class="notice notice-bad" role="alert">设置读取失败：{health.settings.error.message}</div>;
  }
  if (!health.settings.data) return <p class="muted">正在读取设置……</p>;
  const checked = health.checkedAt;
  return (
    <div class="settings-form">
      <SectionHead
        title="服务连接"
        actions={
          <>
            {checked ? <span class="muted section-meta">检测于 {String(checked.getHours()).padStart(2, "0")}:{String(checked.getMinutes()).padStart(2, "0")}</span> : null}
            <button class="btn" type="button" disabled={health.services.loading} onClick={testAll}>
              {health.services.loading ? "检测中…" : "全部重新检测"}
            </button>
          </>
        }
      >
        每个服务单独保存、单独检测；检测使用已保存的配置。密钥只保存在服务端，这里只显示是否已设置。
      </SectionHead>
      <div class="card service-list">
        {SERVICES.map((spec) => (
          <ServiceRow
            key={spec.key}
            spec={spec}
            health={health}
            status={spec.provider ? statuses[spec.provider] : undefined}
            testing={testing === spec.key}
            onTest={() => testOne(spec)}
            onDirty={(key, dirty) =>
              setDirtyRows((current) => (dirty ? (current.includes(key) ? current : [...current, key]) : current.filter((item) => item !== key)))
            }
          />
        ))}
      </div>
    </div>
  );
}
