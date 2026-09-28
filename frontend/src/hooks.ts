import { createContext } from "preact";
import { useCallback, useContext, useEffect, useRef, useState } from "preact/hooks";
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
export function useLoad<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
  pollMs?: (data: T | null) => number | null,
): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const pollRef = useRef(pollMs);
  pollRef.current = pollMs;
  const controllerRef = useRef<AbortController | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    try {
      const next = await loaderRef.current(controller.signal);
      if (controller.signal.aborted) return;
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
    void reload();
    return () => controllerRef.current?.abort();
  }, deps);

  useEffect(() => {
    const interval = pollRef.current?.(data) ?? null;
    if (interval === null) return;
    const timer = window.setTimeout(() => {
      // 页面在后台时不请求，只重新计时；回到前台后继续轮询。
      if (document.visibilityState === "visible") void reload();
      else setTick((value) => value + 1);
    }, interval);
    return () => window.clearTimeout(timer);
  }, [data, reload, tick]);

  return { data, error, loading, reload };
}

export interface Toaster {
  show: (message: string) => void;
}

export const ToastContext = createContext<Toaster>({ show: () => undefined });

export const useToast = (): Toaster => useContext(ToastContext);

/**
 * 一行能放下几张固定最小宽度的卡片：按容器宽度实时计算。
 * 窄屏（≤1100px）下海报架改为横向滑动，返回 null 表示全部渲染。
 */
export function useFitCount(minWidth: number, gap: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const narrow = window.matchMedia("(max-width: 1100px)");
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
