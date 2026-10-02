import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import type { ImportPreview, PlaylistImported } from "../types";

type Mode = "url" | "file" | "paste";

const fileToBase64 = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });

/** 导入片单：先预览再写入；支持网址（TMDB、Letterboxd、IMDb、MDBList）、XLSX/CSV/JSON 文件与粘贴内容。 */
export function ImportDialog({ onClose, onImported }: { onClose: () => void; onImported: (result: PlaylistImported) => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [mode, setMode] = useState<Mode>("url");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [pasted, setPasted] = useState("");
  const [pastedCsv, setPastedCsv] = useState(false);
  const [limit, setLimit] = useState("5000");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  const buildPayload = async (): Promise<Record<string, unknown>> => {
    const payload: Record<string, unknown> = { name: name.trim() || null, limit: Math.min(10000, Math.max(1, Number(limit) || 5000)) };
    if (mode === "url") {
      if (!url.trim()) throw new Error("请输入片单网址");
      payload.source_url = url.trim();
    } else if (mode === "paste") {
      if (!pasted.trim()) throw new Error("请粘贴 JSON 或 CSV 内容");
      if (pastedCsv) payload.csv_text = pasted.trim();
      else {
        try {
          payload.json_data = JSON.parse(pasted);
        } catch (reason) {
          throw new Error(`JSON 格式无效：${reason instanceof Error ? reason.message : ""}`);
        }
      }
    } else {
      if (!file) throw new Error("请选择 XLSX、CSV 或 JSON 文件");
      const lower = file.name.toLowerCase();
      if (lower.endsWith(".json")) {
        try {
          payload.json_data = JSON.parse(await file.text());
        } catch (reason) {
          throw new Error(`JSON 格式无效：${reason instanceof Error ? reason.message : ""}`);
        }
      } else if (lower.endsWith(".csv")) payload.csv_text = await file.text();
      else if (lower.endsWith(".xlsx")) payload.xlsx_base64 = await fileToBase64(file);
      else throw new Error("仅支持 XLSX、CSV 或 JSON 文件");
    }
    return payload;
  };

  const act = async (kind: "preview" | "import") => {
    setBusy(true);
    setError(null);
    try {
      const payload = await buildPayload();
      if (kind === "preview") {
        setPreview(await api<ImportPreview>("/api/playlists/import/preview", { method: "POST", body: payload, timeoutMs: 120000 }));
      } else {
        const result = await api<PlaylistImported>("/api/playlists/import", { method: "POST", body: payload, timeoutMs: 180000 });
        onImported(result);
        dialogRef.current?.close();
      }
    } catch (reason) {
      setError(reason instanceof ApiError || reason instanceof Error ? reason.message : "导入失败");
    } finally {
      setBusy(false);
    }
  };

  const switchMode = (next: Mode) => {
    setMode(next);
    setPreview(null);
    setError(null);
  };

  return (
    <dialog ref={dialogRef} class="dialog" aria-labelledby="import-title" onClose={onClose}>
      <form
        method="dialog"
        class="dialog-body"
        onSubmit={(event) => {
          event.preventDefault();
          void act(preview ? "import" : "preview");
        }}
      >
        <div class="drawer-head">
          <h2 id="import-title" class="section-title">
            导入片单
          </h2>
          <button class="btn icon-btn" type="button" aria-label="关闭" onClick={() => dialogRef.current?.close()}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>
        <div class="segmented" role="group" aria-label="导入方式">
          {(["url", "file", "paste"] as Mode[]).map((item) => (
            <button key={item} type="button" aria-pressed={mode === item} onClick={() => switchMode(item)}>
              {item === "url" ? "网址" : item === "file" ? "文件" : "粘贴"}
            </button>
          ))}
        </div>

        {mode === "url" ? (
          <label class="field">
            <span>片单网址</span>
            <input type="url" value={url} placeholder="https://letterboxd.com/…/list/… 或 TMDB、IMDb、MDBList 片单" onInput={(event) => { setUrl((event.target as HTMLInputElement).value); setPreview(null); }} />
          </label>
        ) : null}
        {mode === "file" ? (
          <label class="field">
            <span>文件（XLSX、CSV 或 JSON）</span>
            <input type="file" accept=".xlsx,.csv,.json" onChange={(event) => { setFile((event.target as HTMLInputElement).files?.[0] ?? null); setPreview(null); }} />
          </label>
        ) : null}
        {mode === "paste" ? (
          <>
            <label class="field">
              <span>内容</span>
              <textarea rows={6} value={pasted} placeholder={pastedCsv ? "rank,title,year,imdb" : '{"name": "片单", "films": [{"title": "…", "year": 1999}]}'} onInput={(event) => { setPasted((event.target as HTMLTextAreaElement).value); setPreview(null); }} />
            </label>
            <label class="check">
              <input type="checkbox" checked={pastedCsv} onChange={(event) => { setPastedCsv((event.target as HTMLInputElement).checked); setPreview(null); }} />
              <span>内容是 CSV</span>
            </label>
          </>
        ) : null}

        <div class="field-grid">
          <label class="field">
            <span>片单名称（可选）</span>
            <input value={name} placeholder="留空使用来源名称" onInput={(event) => setName((event.target as HTMLInputElement).value)} />
          </label>
          <label class="field">
            <span>最多导入</span>
            <input type="number" value={limit} onInput={(event) => setLimit((event.target as HTMLInputElement).value)} />
          </label>
        </div>

        {error ? <div class="notice notice-bad" role="alert">{error}</div> : null}
        {preview ? (
          <div class="panel" role="status">
            <strong>
              {preview.name || "未命名片单"} · {preview.count} 部
            </strong>
            <span class="muted">来源：{preview.source_type || "文件"}</span>
            <ol class="preview-list">
              {preview.sample.map((item) => (
                <li key={item.rank_no}>
                  {item.rank_no}. {item.chinese_title || item.original_title}
                  {item.year ? ` (${item.year})` : ""}
                </li>
              ))}
            </ol>
          </div>
        ) : null}

        <div class="settings-actions">
          {preview ? (
            <button class="btn btn-primary btn-large" type="submit" disabled={busy}>
              {busy ? "导入中…" : `导入 ${preview.count} 部`}
            </button>
          ) : (
            <button class="btn btn-primary btn-large" type="submit" disabled={busy}>
              {busy ? "读取中…" : "预览"}
            </button>
          )}
        </div>
      </form>
    </dialog>
  );
}
