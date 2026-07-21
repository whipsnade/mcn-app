import { useEffect, useState } from 'react';

export type LoadingStage = readonly [thresholdMs: number, text: string];

export const DEFAULT_LOADING_STAGES: readonly LoadingStage[] = [
  [0, '正在连接数据服务…'],
  [8000, '上游响应较慢，请稍候…'],
  [25000, '仍在等待上游返回，请耐心稍候…'],
];

/**
 * 加载中的分阶段提示文案：随等待时长推进阶段，避免用户误以为页面卡死。
 */
export function useLoadingMessage(
  active: boolean,
  stages: readonly LoadingStage[] = DEFAULT_LOADING_STAGES,
): string {
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    if (!active) {
      setElapsedMs(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAt);
    }, 300);
    return () => window.clearInterval(timer);
  }, [active]);

  let message = stages[0]?.[1] ?? '正在加载…';
  for (const [threshold, text] of stages) {
    if (elapsedMs >= threshold) message = text;
  }
  return message;
}
