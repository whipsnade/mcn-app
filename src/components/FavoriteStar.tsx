import { Star } from 'lucide-react';

interface FavoriteStarProps {
  active: boolean;
  busy: boolean;
  onToggle: () => void;
}

// 达人卡片收藏星标：active 实心 amber；busy 防连击。
export default function FavoriteStar({ active, busy, onToggle }: FavoriteStarProps) {
  return (
    <button
      type="button"
      aria-label={active ? '取消收藏' : '收藏'}
      aria-pressed={active}
      disabled={busy}
      onClick={event => {
        event.stopPropagation();
        onToggle();
      }}
      className={`shrink-0 rounded-lg p-1.5 transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-50 ${
        active ? 'text-amber-500 hover:bg-amber-50' : 'text-slate-300 hover:bg-slate-100 hover:text-amber-400'
      }`}
    >
      <Star className={`h-3.5 w-3.5 ${active ? 'fill-amber-400' : ''}`} />
    </button>
  );
}

export { FavoriteStar };
