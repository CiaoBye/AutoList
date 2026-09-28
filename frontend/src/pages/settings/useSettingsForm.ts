import { useEffect, useState } from "preact/hooks";
import { api } from "../../api";
import { useLoad } from "../../hooks";
import { buildSettingsPayload, SECRET_FIELDS, type SettingsForm } from "../../settingsPayload";
import type { RuntimeSettings } from "../../types";

/**
 * 读取运行时设置并跟踪本分区的编辑。
 * 只发送用户改过的字段和明确要清除的字段，其余保持服务端原值。
 */
export function useSettingsForm(fields: readonly string[]) {
  const settings = useLoad<RuntimeSettings>((signal) => api<RuntimeSettings>("/api/settings", { signal }), []);
  const [form, setForm] = useState<SettingsForm>({});
  const [touched, setTouched] = useState<string[]>([]);
  const [cleared, setCleared] = useState<string[]>([]);

  useEffect(() => {
    if (!settings.data) return;
    const initial: SettingsForm = {};
    for (const name of fields) {
      initial[name] = (SECRET_FIELDS as readonly string[]).includes(name) ? "" : (settings.data[name] ?? "");
    }
    setForm(initial);
    setTouched([]);
    setCleared([]);
  }, [settings.data]);

  const set = (name: string, value: string | boolean | number) => {
    setForm((current) => ({ ...current, [name]: value }));
    setTouched((current) => (current.includes(name) ? current : [...current, name]));
  };

  const toggleClear = (name: string) => {
    setCleared((current) => (current.includes(name) ? current.filter((item) => item !== name) : [...current, name]));
    setForm((current) => ({ ...current, [name]: "" }));
  };

  const configured = (name: string): boolean => Boolean(settings.data?.[`${name}_configured`]);
  const dirty = touched.length > 0 || cleared.length > 0;

  const save = async () => {
    const names = [...new Set([...touched, ...cleared])].filter((name) => fields.includes(name));
    if (!names.length) return null;
    const payload = buildSettingsPayload(form, names, cleared);
    const result = await api<RuntimeSettings>("/api/settings", { method: "PUT", body: payload });
    await settings.reload();
    return result;
  };

  return { settings, form, set, cleared, toggleClear, configured, dirty, save };
}
