import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { api, ApiError } from "../../api";
import { useToast } from "../../hooks";
import type { ProviderStatus } from "../../types";

/** 设置分区的标题与说明。 */
export function SectionHead({ title, children, actions }: { title: string; children?: ComponentChildren; actions?: ComponentChildren }) {
  return (
    <div class="section-head">
      <div>
        <h2 class="section-title">{title}</h2>
        {children ? <p class="muted section-lead">{children}</p> : null}
      </div>
      {actions ? <div class="actions">{actions}</div> : null}
    </div>
  );
}

export function TextField({
  label, value, onInput, placeholder, type = "text", hint, autocomplete = "off", inputMode,
}: {
  label: string;
  value: string | number;
  onInput: (value: string) => void;
  placeholder?: string;
  type?: string;
  hint?: string;
  autocomplete?: string;
  inputMode?: "numeric" | "url" | "text";
}) {
  return (
    <label class="field">
      <span>{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        autocomplete={autocomplete}
        inputMode={inputMode}
        onInput={(event) => onInput((event.target as HTMLInputElement).value)}
      />
      {hint ? <small class="field-hint">{hint}</small> : null}
    </label>
  );
}

/**
 * 密钥输入：服务端从不返回明文，只显示“已保存”。
 * 留空保存即保留原值；点“清除”后保存才会删除已保存的值。
 */
export function SecretField({
  label, value, onInput, configured, cleared, onToggleClear, multiline = false, hint,
}: {
  label: string;
  value: string;
  onInput: (value: string) => void;
  configured: boolean;
  cleared: boolean;
  onToggleClear?: () => void;
  multiline?: boolean;
  hint?: string;
}) {
  const placeholder = cleared ? "保存后将清除已保存的值" : configured ? "已保存；留空保留，输入新值替换" : "未设置";
  return (
    <label class="field">
      <span class="field-label-row">
        {label}
        {configured && !cleared ? <span class="badge st-in_library">已保存</span> : null}
        {cleared ? <span class="badge st-issue">将清除</span> : null}
      </span>
      <span class="secret-wrap">
        {multiline ? (
          <textarea rows={3} value={value} placeholder={placeholder} onInput={(event) => onInput((event.target as HTMLTextAreaElement).value)} />
        ) : (
          <input type="password" autocomplete="new-password" value={value} placeholder={placeholder} onInput={(event) => onInput((event.target as HTMLInputElement).value)} />
        )}
        {configured && onToggleClear ? (
          <button class="btn btn-small" type="button" onClick={onToggleClear} aria-pressed={cleared}>
            {cleared ? "撤销" : "清除"}
          </button>
        ) : null}
      </span>
      {hint ? <small class="field-hint">{hint}</small> : null}
    </label>
  );
}

/** 行式设置卡片的标题栏：可带序号或图标，右侧放说明或操作；下面接若干 setting-row。 */
export function CardHead({ id, title, step, icon, note, children }: {
  id: string;
  title: string;
  step?: number;
  icon?: string;
  note?: ComponentChildren;
  children?: ComponentChildren;
}) {
  return (
    <header class="setting-card-head">
      {step ? <span class="setting-card-step" aria-hidden="true">{step}</span> : null}
      {icon ? <img src={icon} alt="" width="20" height="20" /> : null}
      <h3 id={id}>{title}</h3>
      {note ? <span class="muted setting-card-note">{note}</span> : null}
      {children ? <span class="setting-card-actions">{children}</span> : null}
    </header>
  );
}

/** 未保存修改的提示与“放弃修改”，放在保存按钮前。 */
export function DirtyNote({ dirty, onReset }: { dirty: boolean; onReset: () => void }) {
  return (
    <>
      {dirty ? (
        <span class="service-row-dirty">
          <span class="dot dot-warn" aria-hidden="true" />
          有未保存的修改
        </span>
      ) : null}
      <span class="grow" />
      {dirty ? (
        <button class="btn" type="button" onClick={onReset}>
          放弃修改
        </button>
      ) : null}
    </>
  );
}

/** 行式设置：左边名称与说明，右边开关。 */
export function SwitchRow({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label class="setting-row">
      <span class="setting-row-text">
        <strong>{label}</strong>
        {hint ? <small>{hint}</small> : null}
      </span>
      <input class="switch" type="checkbox" role="switch" checked={checked} onChange={(event) => onChange((event.target as HTMLInputElement).checked)} />
    </label>
  );
}

export function ProviderBadge({ status }: { status: ProviderStatus | undefined }) {
  if (!status) return <span class="badge st-unchecked">未检测</span>;
  if (status.ok) return <span class="badge st-in_library">连接正常</span>;
  if (status.configured === false) return <span class="badge st-missing">未配置</span>;
  return <span class="badge st-issue">连接失败</span>;
}

/** 检测单个服务（使用已保存的配置）。 */
export function useProviderTest() {
  const toast = useToast();
  const [results, setResults] = useState<Record<string, ProviderStatus>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const test = async (provider?: string) => {
    setTesting(provider || "all");
    try {
      const response = await api<Record<string, ProviderStatus>>(
        `/api/settings/test${provider ? `?provider=${provider}` : ""}`,
        { method: "POST", timeoutMs: 45000 },
      );
      setResults((current) => ({ ...current, ...response }));
      if (provider) {
        const item = response[provider];
        toast.show(item?.ok ? `${provider}：连接正常` : `${provider}：${item?.message || "连接失败"}`);
      }
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "检测失败");
    } finally {
      setTesting(null);
    }
  };
  return { results, testing, test };
}

/** 统一的“执行操作 → 提示 → 刷新”流程。 */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async <T,>(action: () => Promise<T>, success: string | ((result: T) => string), after?: () => Promise<unknown> | void) => {
    setBusy(true);
    try {
      const result = await action();
      toast.show(typeof success === "function" ? success(result) : success);
      await after?.();
      return true;
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : "操作失败");
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, run };
}
