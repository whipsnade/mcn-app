// 快捷功能支持的五平台常量与中文 label（与后端 KOL_SEARCH_TOOLS 一致）。
// 现有 ApiQuickPlatform 仅覆盖爆贴两平台，达人推荐多选以这里的全集为准。
export const QUICK_PLATFORMS = ['xiaohongshu', 'douyin', 'bilibili', 'weibo', 'wechat'] as const;

export type QuickPlatform = (typeof QUICK_PLATFORMS)[number];

export const QUICK_PLATFORM_LABELS: Record<QuickPlatform, string> = {
  xiaohongshu: '小红书',
  douyin: '抖音',
  bilibili: 'B站',
  weibo: '微博',
  wechat: '微信',
};

export function quickPlatformLabel(platform: string): string {
  return QUICK_PLATFORM_LABELS[platform as QuickPlatform] ?? platform;
}
