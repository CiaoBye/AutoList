/**
 * 候选的操作条：只在用得上的时候出现。
 * 清除候选——已经选定了资源后，把其余没选的清掉（影片回到缺片，已选定的保留）；
 * 忽略提交失败——影片最近一次提交失败的标记。重新寻片放在影片详情里。
 */
export function CandidateTools({ canClear, failed, busy, onClear, onDismiss }: {
  canClear: boolean;
  failed: boolean;
  busy: boolean;
  onClear: () => void;
  onDismiss: () => void;
}) {
  if (!canClear && !failed) return null;
  return (
    <div class="candidate-tools">
      {failed ? (
        <button class="btn btn-small" type="button" disabled={busy} title="提交记录保留，只是不再标记这部影片提交失败" onClick={onDismiss}>
          忽略提交失败
        </button>
      ) : null}
      {canClear ? (
        <button class="btn btn-small" type="button" disabled={busy} title="清除没选定的候选，影片回到缺片，下一批寻片重新搜" onClick={onClear}>
          清除其余候选
        </button>
      ) : null}
    </div>
  );
}
