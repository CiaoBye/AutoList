import type { ComponentChildren } from "preact";
import { createPortal } from "preact/compat";
import { useLayoutEffect, useState } from "preact/hooks";

/**
 * 抽屉与遮罩挂到 body：不受所在页面版心宽度、层叠顺序的影响，始终盖住整个窗口。
 * 抽屉是模态的：打开期间页面其余部分不可用键盘或鼠标操作（inert），关闭后焦点回到打开前的元素。
 */
export function DrawerLayer({ children }: { children: ComponentChildren }) {
  const [host] = useState(() => document.createElement("div"));
  // 布局阶段就挂上：抽屉里的 useEffect 要聚焦关闭按钮，此时容器必须已在页面里。
  useLayoutEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.append(host);
    const background = Array.from(document.body.children).filter(
      (element): element is HTMLElement => element instanceof HTMLElement && element !== host && !element.inert,
    );
    for (const element of background) element.inert = true;
    return () => {
      for (const element of background) element.inert = false;
      host.remove();
      if (opener?.isConnected) opener.focus();
    };
  }, [host]);
  return createPortal(<>{children}</>, host);
}
