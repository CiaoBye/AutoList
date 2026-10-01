import { useEffect, useState } from "preact/hooks";
import { api } from "../../api";
import { useLoad, type Loadable } from "../../hooks";
import { buildSettingsPayload, SECRET_FIELDS, type SettingsForm } from "../../settingsPayload";
import type { RuntimeSettings } from "../../types";

const initialValue = (data: RuntimeSettings, name: string) =>
  (SECRET_FIELDS as readonly string[]).includes(name) ? "" : (data[name] ?? "");

/**
 * 一组设置字段的编辑草稿，基于已读取的运行时设置。
 * 只发送用户改过的字段和明确要清除的字段，其余保持服务端原值；设置重新读取后，
 * 草稿里没改过的字段跟着更新，改过的保留，所以同一页上别的草稿保存时不会冲掉这里的修改。
 */
export function useSettingsDraft(fields: readonly string[], settings: Loadable<RuntimeSettings>) {
  const [form, setForm] = useState<SettingsForm>({});
  const [touched, setTouched] = useState<string[]>([]);
  const [cleared, setCleared] = useState<string[]>([]);

  useEffect(() => {
    const data = settings.data;
    if (!data) return;
    setForm((current) => {
      const next: SettingsForm = {};
      for (const name of fields) {
        next[name] = touched.includes(name) || cleared.includes(name) ? current[name] : initialValue(data, name);
      }
      return next;
    });
  }, [settings.data]);

  const set = (name: string, value: string | boolean | number) => {
    setForm((current) => ({ ...current, [name]: value }));
    setTouched((current) => (current.includes(name) ? current : [...current, name]));
  };

  const toggleClear = (name: string) => {
    setCleared((current) => (current.includes(name) ? current.filter((item) => item !== name) : [...current, name]));
    setForm((current) => ({ ...current, [name]: "" }));
  };

  /** 放弃修改：回到已保存的值。 */
  const reset = () => {
    const data = settings.data;
    if (!data) return;
    const next: SettingsForm = {};
    for (const name of fields) next[name] = initialValue(data, name);
    setForm(next);
    setTouched([]);
    setCleared([]);
  };

  const configured = (name: string): boolean => Boolean(settings.data?.[`${name}_configured`]);
  const dirty = touched.length > 0 || cleared.length > 0;

  const save = async () => {
    const names = [...new Set([...touched, ...cleared])].filter((name) => fields.includes(name));
    if (!names.length) return null;
    const payload = buildSettingsPayload(form, names, cleared);
    const result = await api<RuntimeSettings>("/api/settings", { method: "PUT", body: payload });
    setTouched([]);
    setCleared([]);
    await settings.reload();
    return result;
  };

  return { settings, form, set, cleared, toggleClear, configured, dirty, save, reset };
}

export type SettingsDraft = ReturnType<typeof useSettingsDraft>;

/** 单独读取运行时设置并编辑其中一组字段。 */
export function useSettingsForm(fields: readonly string[]) {
  const settings = useLoad<RuntimeSettings>((signal) => api<RuntimeSettings>("/api/settings", { signal }), []);
  return useSettingsDraft(fields, settings);
}
