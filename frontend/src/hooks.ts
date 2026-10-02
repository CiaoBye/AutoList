import { createContext } from "preact";
import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { ApiError } from "./api";

export interface Loadable<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * 读取数据并按需轮询：只有 ``pollMs`` 返回数字时才继续轮询，
 * 页面隐藏时暂停，避免没有进行中任务时持续请求。
 */
// 各页面上一次读到的数据：再次进入页面时先显示它，后台重新读取后替换，避免每次都从空白开始等接口。
const loaded = new Map<string, unknown>();

/**
 * 读取数据并可定时刷新。给出 ``cacheKey`` 时记住上次的结果：同一个 key 再次进入时立即显示旧数据（``loading`` 仍为真，
 * 新数据到达后替换）。key 要包含决定数据内容的参数（片单、筛选、页码等）。
 */
export function useLoad<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
  pollMs?: (data: T | null) => number | null,
  cacheKey?: string,
): Loadable<T> {
  const [data, setData] = useState<T | null>(() => (cacheKey !== undefined && loaded.has(cacheKey) ? (loaded.get(cacheKey) as T) : null));
  const keyRef = useRef(cacheKey);
  keyRef.current = cacheKey;
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const pollRef = useRef(pollMs);
  pollRef.current = pollMs;
  const controllerRef = useRef<AbortController | null>(null);
  const [tick, setTick] = useState(0);
  const failId = useRef(Symbol("load")).current;

  const reload = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    try {
      const next = await loaderRef.current(controller.signal);
      if (controller.signal.aborted) return;
      if (keyRef.current !== undefined) loaded.set(keyRef.current, next);
      setData(next);
      setError(null);
    } catch (reason) {
      if (controller.signal.aborted) return;
      setError(reason instanceof ApiError ? reason : new ApiError(String(reason), 0));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 条件变了（换片单、换筛选）：有这个条件上次的结果就先显示它。
    if (keyRef.current !== undefined && loaded.has(keyRef.current)) setData(loaded.get(keyRef.current) as T);
    void reload();
    return () => controllerRef.current?.abort();
  }, deps);

  useEffect(() => {
    markFailing(failId, error !== null && data !== null);
  }, [error, data]);
  useEffect(() => () => markFailing(failId, false), []);

  useEffect(() => {
    const interval = pollRef.current?.(data) ?? null;
    if (interval === null) return;
    const timer = window.setTimeout(() => {
      // 页面在后台时不请求，只重新计时；回到前台后继续轮询。
      if (document.visibilityState === "visible") void reload();
      else setTick((value) => value + 1);
    }, interval);
    return () => window.clearTimeout(timer);
  }, [data, error, reload, tick]); // 请求失败时 data 不变，靠 error 变化重新排程，网络恢复后继续刷新。

  return { data, error, loading, reload };
}

// 已有数据却刷新失败的读取：用来在顶栏下提示“数据可能不是最新的”。恢复或页面卸载后自动移除。
const failingLoads = new Set<symbol>();
const failingListeners = new Set<() => void>();
const markFailing = (id: symbol, failing: boolean) => {
  if (failingLoads.has(id) === failing) return;
  if (failing) failingLoads.add(id);
  else failingLoads.delete(id);
  failingListeners.forEach((listener) => listener());
};

/** 是否有页面正拿着旧数据、刷新却一直失败（如断网、服务暂时不可用）。 */
export function useStaleData(): boolean {
  const [stale, setStale] = useState(failingLoads.size > 0);
  useEffect(() => {
    const update = () => setStale(failingLoads.size > 0);
    failingListeners.add(update);
    update();
    return () => {
      failingListeners.delete(update);
    };
  }, []);
  return stale;
}

export interface Toaster {
  show: (message: string) => void;
}

export const ToastContext = createContext<Toaster>({ show: () => undefined });

export const useToast = (): Toaster => useContext(ToastContext);

/**
 * 一行能放下几张固定最小宽度的卡片：按容器宽度实时计算。
 * 窄屏（≤900px，即手机底部标签栏出现时）下海报架改为横向滑动，返回 null 表示全部渲染。
 */
export function useFitCount(minWidth: number, gap: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const narrow = window.matchMedia("(max-width: 900px)");
    const measure = () => {
      setCount(narrow.matches ? null : Math.max(1, Math.floor((element.clientWidth + gap) / (minWidth + gap))));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    narrow.addEventListener("change", measure);
    return () => {
      observer.disconnect();
      narrow.removeEventListener("change", measure);
    };
  }, [minWidth, gap]);
  return { ref, count };
}


/** 首页海报架的卡片宽度：按窗口剩余高度平分给各排海报（``lines`` 是每个海报架的排数），让各分辨率下都能一屏看全（最小 MIN、最大 MAX）。 */
export function useShelfCardWidth(lines: number[], deps: unknown[]) {
  const rows = lines.reduce((sum, value) => sum + value, 0);
  const shelfCount = lines.length;
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(SHELF_CARD_MAX);
  // 第一次按真实布局量过尺寸之前先不显示海报架，避免先画出一组偏大的海报再缩小的闪动。
  const [ready, setReady] = useState(false);
  useLayoutEffect(() => {
    const measure = (): boolean => {
      const element = ref.current;
      if (rows < 1) return true;
      if (!element) return false;
      const top = element.getBoundingClientRect().top + window.scrollY;
      // 窄屏底部固定的标签栏会挡住海报，按它的上沿计算可用高度。
      const tabbar = document.querySelector<HTMLElement>(".tabbar");
      const tabbarHeight = tabbar && getComputedStyle(tabbar).position === "fixed" ? tabbar.getBoundingClientRect().height : 0;
      const available = window.innerHeight - tabbarHeight - top - SHELF_BOTTOM_SPACE - (shelfCount - 1) * SHELF_ROW_SPACE;
      // 每个海报架除海报以外的高度（标题行、片名、排间距）按实际渲染测量，尚未渲染时用估计值。
      const overhead = lines.map((count, index) => {
        const shelf = element.children[index] as HTMLElement | undefined;
        const poster = shelf?.querySelector<HTMLElement>(".shelf-row img, .shelf-row .poster");
        if (!shelf || !poster) return SHELF_HEAD_HEIGHT + count * SHELF_CAPTION_HEIGHT + (count - 1) * SHELF_GAP;
        return shelf.getBoundingClientRect().height - count * poster.getBoundingClientRect().height;
      }).reduce((sum, value) => sum + value, 0);
      const posterHeight = (available - overhead) / rows;
      setWidth(Math.round(Math.min(SHELF_CARD_MAX, Math.max(SHELF_CARD_MIN, posterHeight / 1.5))));
      return true;
    };
    // 先按当前布局算一次；窗口变化后等下一帧布局稳定再算，避免读到变化前的尺寸。
    let frame = 0;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (measure()) setReady(true);
      });
    };
    if (measure()) setReady(true);
    schedule();
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
    };
  }, [lines.join(","), ...deps]);
  return { ref, width, ready };
}

// 尚未渲染海报时的估计值：每排的标题行、海报下的片名、排与排之间、页面底部留白。
const SHELF_HEAD_HEIGHT = 40;
const SHELF_CAPTION_HEIGHT = 70;
const SHELF_ROW_SPACE = 32;
const SHELF_GAP = 16;
const SHELF_BOTTOM_SPACE = 24;
const SHELF_CARD_MIN = 72;
const SHELF_CARD_MAX = 260;
