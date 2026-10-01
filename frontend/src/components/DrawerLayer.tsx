import type { ComponentChildren } from "preact";
import { createPortal } from "preact/compat";

/** 抽屉与遮罩挂到 body：不受所在页面版心宽度、层叠顺序的影响，始终盖住整个窗口。 */
export function DrawerLayer({ children }: { children: ComponentChildren }) {
  return createPortal(<>{children}</>, document.body);
}
