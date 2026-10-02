import { api, ApiError } from "../api";
import { useToast } from "../hooks";
import type { SiteCheck } from "../types";

/**
 * 站点需要搜索人机验证时，在新标签页打开它的种子搜索页让用户完成验证。
 * 用户回到本页后自动重新检测该站点，状态随之更新，不必再手动点检测。
 */
export function VerifyLink({ url, siteId, onChecked }: { url: string; siteId: number | null; onChecked?: () => void }) {
  const toast = useToast();
  const recheckOnReturn = () => {
    if (siteId === null) return;
    const recheck = async () => {
      window.removeEventListener("focus", recheck);
      try {
        const result = await api<SiteCheck>(`/api/sites/${siteId}/test`, { method: "POST", timeoutMs: 60000 });
        toast.show(result.ok ? `${result.name} 验证通过，已恢复` : `${result.name} 仍未通过：${result.message}`);
      } catch (error) {
        toast.show(error instanceof ApiError ? error.message : "站点检测失败");
      }
      onChecked?.();
    };
    window.addEventListener("focus", recheck);
  };
  return (
    <a
      class="btn btn-small verify-link"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title="在浏览器完成验证后回到这里，会自动重新检测该站点；进行中的寻片会自动继续，已结束的任务可点“重试失败的站点”"
      onClick={(event) => {
        event.stopPropagation();
        recheckOnReturn();
      }}
    >
      去验证
    </a>
  );
}
