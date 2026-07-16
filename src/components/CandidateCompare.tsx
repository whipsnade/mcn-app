import type { ApiCandidate } from '../api/contracts';

const dimensions = [
  ['audience', '受众匹配'],
  ['content', '内容契合'],
  ['engagement', '互动率'],
  ['budget', '预算匹配'],
  ['platform', '平台表现'],
  ['risk', '风险控制'],
] as const;

function scoreLabel(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${value}`;
}

function candidateName(candidate: ApiCandidate): string {
  return candidate.nickname?.trim() || '未命名达人';
}

function platformName(platform: string): string {
  return ({ xiaohongshu: '小红书', douyin: '抖音', bilibili: '哔哩哔哩', weibo: '微博', wechat: '微信' } as Record<string, string>)[platform] ?? platform;
}

function riskLabel(candidate: ApiCandidate) {
  const texts = candidate.risks.flatMap(riskText).filter((value, index, values) => values.indexOf(value) === index);
  return texts.length ? texts.join('、') : '暂无';
}

function riskText(value: unknown): string[] {
  if (typeof value === 'string') return value.trim() ? [value] : [];
  if (Array.isArray(value)) return value.flatMap(riskText);
  if (!value || typeof value !== 'object') return [];

  const record = value as Record<string, unknown>;
  const prioritized = ['label', 'reason', 'message'].flatMap(key => riskText(record[key]));
  return prioritized.length ? prioritized : Object.values(record).flatMap(riskText);
}

export default function CandidateCompare({ candidates }: { candidates: ApiCandidate[] }) {
  if (candidates.length === 0) return null;

  return (
    <section aria-label="达人对比" className="mx-4 mb-4 overflow-x-auto rounded-xl border border-indigo-100 bg-indigo-50/30">
      <div className="min-w-[620px]">
        <div className="grid grid-cols-[120px_repeat(4,minmax(120px,1fr))] border-b border-indigo-100 bg-white text-[10px] font-semibold text-slate-500">
          <div className="px-3 py-2.5">对比维度</div>
          {candidates.map(candidate => <div key={candidate.id} className="px-3 py-2.5 text-slate-800">{candidateName(candidate)}</div>)}
        </div>
        {[
          ['总分', (candidate: ApiCandidate) => scoreLabel(candidate.total_score)],
          ['排名', (candidate: ApiCandidate) => `#${candidate.rank}`],
          ['平台', (candidate: ApiCandidate) => platformName(candidate.platform)],
          ['粉丝', (candidate: ApiCandidate) => scoreLabel(candidate.metrics?.followers)],
          ['价格', (candidate: ApiCandidate) => candidate.metrics?.quoted_price_cny === null || candidate.metrics?.quoted_price_cny === undefined ? '—' : `¥${candidate.metrics.quoted_price_cny.toLocaleString('zh-CN')}`],
        ].map(([label, value]) => (
          <div key={String(label)} className="grid grid-cols-[120px_repeat(4,minmax(120px,1fr))] border-b border-indigo-100/60 text-[11px]">
            <div className="bg-white/70 px-3 py-2 font-medium text-slate-500">{label}</div>
            {candidates.map(candidate => <div key={candidate.id} className="px-3 py-2 text-slate-700">{(value as (item: ApiCandidate) => string)(candidate)}</div>)}
          </div>
        ))}
        {dimensions.map(([key, label]) => (
          <div key={key} className="grid grid-cols-[120px_repeat(4,minmax(120px,1fr))] border-b border-indigo-100/60 text-[11px] last:border-0">
            <div className="bg-white/70 px-3 py-2 font-medium text-slate-500">{label}</div>
            {candidates.map(candidate => <div key={candidate.id} className="px-3 py-2 text-slate-700">{scoreLabel(candidate.scores[key])}</div>)}
          </div>
        ))}
        <div className="grid grid-cols-[120px_repeat(4,minmax(120px,1fr))] text-[11px]">
          <div className="bg-white/70 px-3 py-2 font-medium text-slate-500">风险提示</div>
          {candidates.map(candidate => <div key={candidate.id} className="px-3 py-2 text-slate-700">{riskLabel(candidate)}</div>)}
        </div>
      </div>
    </section>
  );
}
