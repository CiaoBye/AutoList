import { useEffect, useRef, useState } from "preact/hooks";

export interface ConfirmOptions {
  title: string;
  /** 说明文字；换行保留。 */
  message?: string;
  /** 并排核对的几项（标签与内容），例如放行前的片单影片与资源标题。 */
  facts?: [string, string][];
  confirmText?: string;
  /** 不可恢复的操作（删除、清空）：确认按钮用警示色。 */
  danger?: boolean;
}

interface Pending extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

let open: ((pending: Pending) => void) | null = null;

/** 居中弹出的确认框，代替浏览器自带的 confirm；点确认得到 true，取消、按 Esc 或点遮罩得到 false。 */
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!open) {
      resolve(window.confirm([options.title, options.message].filter(Boolean).join("\n")));
      return;
    }
    open({ ...options, resolve });
  });
}

/** 放行前的确认内容：把片单里的影片与这条资源的标题并排写出，放行前一眼核对。 */
export const releaseConfirm = (
  film: { title: string; year: number | null },
  candidate: { title: string; exclusion_reason: string | null },
): ConfirmOptions => ({
  title: "人工放行这条资源？",
  facts: [
    ["片单影片", `${film.title}${film.year ? `（${film.year}）` : ""}`],
    ["资源", candidate.title],
    ["不符合标准", candidate.exclusion_reason ?? "未说明"],
  ],
  message: "确认是这部影片，并人工放行加入待入馆清单？",
  confirmText: "放行并选定",
});

/** 挂在应用根部，承接所有 ``confirmDialog`` 调用。 */
export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    open = (next) => setPending(next);
    return () => {
      open = null;
    };
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (pending && dialog && !dialog.open) {
      dialog.showModal();
      confirmRef.current?.focus();
    }
  }, [pending]);

  const finish = (value: boolean) => {
    if (!pending) return;
    const current = pending;
    setPending(null);
    dialogRef.current?.close();
    current.resolve(value);
  };

  if (!pending) return null;
  return (
    <dialog
      ref={dialogRef}
      class="dialog confirm-dialog"
      aria-labelledby="confirm-title"
      onCancel={(event) => {
        event.preventDefault();
        finish(false);
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) finish(false);
      }}
    >
      <div class="dialog-body">
        <h2 id="confirm-title" class="confirm-title">{pending.title}</h2>
        {pending.facts?.length ? (
          <dl class="confirm-facts">
            {pending.facts.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {pending.message ? <p class="confirm-message">{pending.message}</p> : null}
        <div class="confirm-actions">
          <button class="btn" type="button" onClick={() => finish(false)}>取消</button>
          <button ref={confirmRef} class={`btn ${pending.danger ? "btn-danger" : "btn-primary"}`} type="button" onClick={() => finish(true)}>
            {pending.confirmText ?? "确定"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
