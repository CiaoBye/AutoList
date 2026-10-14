import { useState } from "preact/hooks";
import { api } from "../../api";
import { formatSize, formatTime } from "../../format";
import { useLoad } from "../../hooks";
import type { BackupRecord, BackupRestored, BackupsDeleted } from "../../types";
import { CardHead, SectionHead, useAction } from "./shared";
import { confirmDialog } from "../../components/ConfirmDialog";

const KIND_LABEL: Record<BackupRecord["kind"], string> = { manual: "手动", auto: "自动", deploy: "部署前" };

export function Backups() {
  const list = useLoad<BackupRecord[]>((signal) => api<BackupRecord[]>("/api/backups", { signal }), []);
  const { busy, run } = useAction();
  const [picked, setPicked] = useState<string[]>([]);
  const rows = list.data ?? [];
  // 列表刷新后丢掉已不存在的勾选。
  const selected = picked.filter((name) => rows.some((row) => row.name === name));
  const allSelected = rows.length > 0 && selected.length === rows.length;

  const toggle = (name: string) => setPicked(selected.includes(name) ? selected.filter((item) => item !== name) : [...selected, name]);
  const toggleAll = () => setPicked(allSelected ? [] : rows.map((row) => row.name));

  const create = () => run(
    () => api("/api/backups", { method: "POST" }),
    "已备份当前数据库",
    list.reload,
  );

  const restore = async (row: BackupRecord) => {
    if (!(await confirmDialog({ title: `用 ${formatTime(row.created_at)} 的备份覆盖当前数据库？`, message: "恢复前会先自动备份一份当前数据，可以随时再恢复回来。", confirmText: "恢复", danger: true }))) return;
    void run(
      () => api<BackupRestored>(`/api/backups/${encodeURIComponent(row.name)}/restore`, { method: "POST", timeoutMs: 120_000 }),
      (result) => `已恢复，恢复前的数据另存为 ${result.safety_backup}`,
      list.reload,
    );
  };

  const remove = async (names: string[]) => {
    const text = names.length === 1 ? "这份备份" : `这 ${names.length} 份备份`;
    if (!(await confirmDialog({ title: `删除${text}？`, message: "删除后无法恢复。", confirmText: "删除", danger: true }))) return;
    void run(
      () => api<BackupsDeleted>("/api/backups/delete", { method: "POST", body: { names } }),
      (result) => `已删除 ${result.deleted} 份备份`,
      async () => {
        setPicked([]);
        await list.reload();
      },
    );
  };

  return (
    <div class="settings-form">
      <SectionHead
        title="备份"
        actions={<button class="btn btn-small btn-primary" type="button" disabled={busy} onClick={create}>立即备份</button>}
      >
        每天自动备份一次（留 7 份），手动备份留 10 份，部署前的备份留 3 份。
      </SectionHead>

      <section class="card setting-card" aria-labelledby="backup-list">
        <CardHead id="backup-list" title="备份列表" note={`${rows.length} 份`}>
          {rows.length ? (
            <>
              <label class="backup-check">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                <span>全选</span>
              </label>
              <button class="btn btn-small btn-danger" type="button" disabled={busy || !selected.length} onClick={() => void remove(selected)}>
                删除所选{selected.length ? `（${selected.length}）` : ""}
              </button>
            </>
          ) : null}
        </CardHead>
        <div class="setting-card-body">
          {list.loading && !list.data ? <p class="muted">读取中…</p> : null}
          {list.data && !rows.length ? <p class="muted">还没有备份。</p> : null}
          {rows.length ? (
            <ul class="backup-list">
              {rows.map((row) => (
                <li key={row.name} class="backup-row">
                  <label class="backup-main">
                    <input type="checkbox" checked={selected.includes(row.name)} onChange={() => toggle(row.name)} aria-label={`选择 ${formatTime(row.created_at)} 的备份`} />
                    <strong>{formatTime(row.created_at)}</strong>
                    <span class="badge">{KIND_LABEL[row.kind]}</span>
                    <span class="muted">{formatSize(row.size)}</span>
                  </label>
                  <span class="backup-actions">
                    <button class="btn btn-small" type="button" disabled={busy || !row.restorable} title={row.restorable ? undefined : "备份来自更新的版本，无法恢复"} onClick={() => void restore(row)}>恢复</button>
                    <button class="btn btn-small" type="button" disabled={busy} onClick={() => void remove([row.name])}>删除</button>
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </section>
    </div>
  );
}
