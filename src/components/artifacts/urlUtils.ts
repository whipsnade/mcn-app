/**
 * http/https URL 白名单（设计 §6.4/§16）。
 *
 * 模型生成的快照/报告数据里的 URL 只允许 http(s) 协议；javascript:、data: 等
 * 其他协议或非法串一律返回 null，由调用方渲染「不可用」态而非链接，防止
 * `href={url}` 注入可执行协议。后端强类型校验是最终边界，这里是前端防线。
 */
export function safeHttpUrl(url?: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return parsed.href;
  } catch {
    return null;
  }
}
