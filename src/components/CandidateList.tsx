import { ChevronDown, ExternalLink, Scale, Star } from 'lucide-react';
import { useMemo, useState } from 'react';

import type { ApiCandidate, ApiCandidatePage } from '../api/contracts';
import CandidateCompare from './CandidateCompare';

type SortKey = 'rank' | 'total_score' | 'audience' | 'content' | 'engagement' | 'budget' | 'platform_score' | 'risk' | 'platform' | 'followers' | 'price';

interface CandidateListProps {
  page?: ApiCandidatePage;
  favoriteKolIds?: ReadonlySet<string>;
  onFavorite: (candidate: ApiCandidate) => void | Promise<void>;
}

const columns: Array<{ key: SortKey; label: string }> = [
  { key: 'total_score', label: '总分' },
  { key: 'audience', label: '受众匹配' },
  { key: 'content', label: '内容契合' },
  { key: 'engagement', label: '互动率' },
  { key: 'budget', label: '预算匹配' },
  { key: 'platform_score', label: '平台表现' },
  { key: 'risk', label: '风险控制' },
];

function metric(candidate: ApiCandidate, key: SortKey): number | string | null {
  if (key === 'rank') return candidate.rank;
  if (key === 'total_score') return candidate.total_score;
  if (key === 'platform') return candidate.platform;
  if (key === 'platform_score') return candidate.scores.platform ?? null;
  if (key === 'followers') return candidate.metrics?.followers ?? null;
  if (key === 'price') return candidate.metrics?.quoted_price_cny ?? null;
  return candidate.scores[key] ?? null;
}

function formatScore(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : value.toFixed(0);
}

function formatMetric(value: number | null | undefined, prefix = '') {
  return value === null || value === undefined ? '—' : `${prefix}${value.toLocaleString('zh-CN')}`;
}

function freshnessLabel(items: readonly ApiCandidate[]) {
  const latest = items
    .map(item => item.metrics?.collected_at)
    .filter((value): value is string => Boolean(value))
    .sort()
    .at(-1);
  return latest ? latest.slice(0, 10) : '待补充';
}

function completenessLabel(items: readonly ApiCandidate[]) {
  const values = items
    .map(item => item.metrics?.data_completeness)
    .filter((value): value is number => value !== null && value !== undefined);
  return values.length ? `${Math.round(values.reduce((sum, value) => sum + value, 0) / values.length)}%` : '待补充';
}

export default function CandidateList({ page, favoriteKolIds = new Set(), onFavorite }: CandidateListProps) {
  const [sort, setSort] = useState<{ key: SortKey; direction: 'asc' | 'desc' }>({ key: 'rank', direction: 'asc' });
  const [platformFilter, setPlatformFilter] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [showCompare, setShowCompare] = useState(false);
  const [favoriteError, setFavoriteError] = useState<string>();
  const items = page?.items ?? [];

  const platforms = useMemo(() => [...new Set(items.map(item => item.platform))].sort((left, right) => left.localeCompare(right, 'zh-CN')), [items]);
  const sorted = useMemo(() => items
    .filter(candidate => !platformFilter || candidate.platform === platformFilter)
    .sort((left, right) => {
      const a = metric(left, sort.key);
      const b = metric(right, sort.key);
      if (a === b) return left.rank - right.rank;
      if (a === null) return 1;
      if (b === null) return -1;
      const result = typeof a === 'string' && typeof b === 'string'
        ? a.localeCompare(b, 'zh-CN')
        : Number(a) - Number(b);
      return sort.direction === 'asc' ? result : -result;
    }), [items, platformFilter, sort]);
  const selected = items.filter(item => selectedIds.includes(item.id));

  const setSortKey = (key: SortKey) => {
    setSort(current => current.key === key
      ? { key, direction: current.direction === 'desc' ? 'asc' : 'desc' }
      : { key, direction: key === 'rank' || key === 'platform' ? 'asc' : 'desc' });
  };
  const toggleSelected = (candidate: ApiCandidate) => {
    setSelectedIds(current => {
      if (current.includes(candidate.id)) return current.filter(id => id !== candidate.id);
      return current.length === 4 ? current : [...current, candidate.id];
    });
  };
  const toggleFavorite = async (candidate: ApiCandidate) => {
    setFavoriteError(undefined);
    try {
      await onFavorite(candidate);
    } catch {
      setFavoriteError('收藏操作失败，请稍后重试');
    }
  };

  if (!page) {
    return <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">候选清单将在分析完成后展示</div>;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-slate-50">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
        <div>
          <h2 className="text-xs font-bold tracking-tight text-slate-800">KOL 候选清单</h2>
          <p className="mt-0.5 text-[10px] text-slate-400">版本 {page.version} · {page.total} 位候选 · 数据完整度 {completenessLabel(items)} · 更新于 {freshnessLabel(items)}</p>
        </div>
        <div className="flex items-center gap-2">
          <select
            aria-label="筛选平台"
            value={platformFilter}
            onChange={event => setPlatformFilter(event.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-[11px] font-medium text-slate-600 outline-none transition focus:border-indigo-300"
          >
            <option value="">全部平台</option>
            {platforms.map(platform => <option key={platform} value={platform}>{platform}</option>)}
          </select>
          <button
            type="button"
            disabled={selected.length < 2}
            onClick={() => setShowCompare(true)}
            className={selected.length >= 2
              ? 'flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700'
              : 'flex items-center gap-1.5 rounded-lg bg-slate-100 px-3 py-1.5 text-[11px] font-semibold text-slate-400'}
          >
            <Scale className="h-3.5 w-3.5" />对比 {selected.length} 位达人
          </button>
        </div>
      </div>

      {showCompare && <CandidateCompare candidates={selected} />}
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="min-w-[990px] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="w-full text-left">
            <thead className="bg-slate-50 text-[10px] font-semibold text-slate-500">
              <tr>
                <th className="w-10 px-3 py-2.5"><span className="sr-only">选择</span></th>
                <th className="px-3 py-2.5"><button type="button" onClick={() => setSortKey('rank')} className="inline-flex items-center gap-1 hover:text-indigo-600">排名<ChevronDown className="h-3 w-3" /></button></th>
                <th className="px-3 py-2.5"><button type="button" onClick={() => setSortKey('platform')} className="inline-flex items-center gap-1 hover:text-indigo-600">达人 / 平台<ChevronDown className="h-3 w-3" /></button></th>
                {columns.map(column => <th key={column.key} className="px-2 py-2.5 text-center"><button type="button" onClick={() => setSortKey(column.key)} className="inline-flex items-center gap-0.5 hover:text-indigo-600">{column.label}<ChevronDown className="h-3 w-3" /></button></th>)}
                <th className="px-3 py-2.5 text-center"><button type="button" onClick={() => setSortKey('followers')} className="inline-flex items-center gap-0.5 hover:text-indigo-600">粉丝<ChevronDown className="h-3 w-3" /></button></th>
                <th className="px-3 py-2.5 text-center"><button type="button" onClick={() => setSortKey('price')} className="inline-flex items-center gap-0.5 hover:text-indigo-600">价格<ChevronDown className="h-3 w-3" /></button></th>
                <th className="px-3 py-2.5 text-center">收藏</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[11px]">
              {sorted.map(candidate => {
                const isSelected = selectedIds.includes(candidate.id);
                const isFavorite = favoriteKolIds.has(candidate.kol_id);
                return <tr key={candidate.id} className={isSelected ? 'bg-indigo-50/50' : 'hover:bg-slate-50/70'}>
                  <td className="px-3 py-3"><input type="checkbox" aria-label={`选择${candidate.nickname ?? candidate.kol_id}`} checked={isSelected} onChange={() => toggleSelected(candidate)} className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500" /></td>
                  <td className="px-3 py-3 font-mono text-slate-400">#{candidate.rank}</td>
                  <td className="px-3 py-3"><div className="flex items-center gap-2"><div><div data-testid="candidate-name" className="font-semibold text-slate-800">{candidate.nickname ?? candidate.kol_id}</div><div className="mt-0.5 flex items-center gap-1 text-[10px] text-slate-400"><span>{candidate.platform}</span>{candidate.profile_url && <a href={candidate.profile_url} target="_blank" rel="noreferrer" aria-label={`查看${candidate.nickname ?? candidate.kol_id}证据`}><ExternalLink className="h-3 w-3" /></a>}</div></div></div></td>
                  {columns.map(column => <td key={column.key} className="px-2 py-3 text-center font-medium text-slate-600">{formatScore(metric(candidate, column.key) as number | null)}</td>)}
                  <td className="px-3 py-3 text-center text-slate-500">{formatMetric(candidate.metrics?.followers)}</td>
                  <td className="px-3 py-3 text-center text-slate-500">{formatMetric(candidate.metrics?.quoted_price_cny, '¥')}</td>
                  <td className="px-3 py-3 text-center"><button type="button" aria-label={isFavorite ? `取消收藏 ${candidate.nickname ?? candidate.kol_id}` : `收藏 ${candidate.nickname ?? candidate.kol_id}`} onClick={() => void toggleFavorite(candidate)} className={isFavorite ? 'rounded p-1 text-amber-500 transition hover:bg-amber-50' : 'rounded p-1 text-slate-300 transition hover:bg-slate-100 hover:text-amber-500'}><Star className={isFavorite ? 'h-3.5 w-3.5 fill-amber-400' : 'h-3.5 w-3.5'} /></button></td>
                </tr>;
              })}
            </tbody>
          </table>
        </div>
      </div>
      {favoriteError && <div role="alert" className="mx-4 mb-3 rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{favoriteError}</div>}
    </div>
  );
}
