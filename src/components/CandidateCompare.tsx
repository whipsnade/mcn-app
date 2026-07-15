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

function riskLabel(candidate: ApiCandidate) {
  const labels = candidate.risks
    .map(risk => typeof risk.label === 'string' ? risk.label : null)
    .filter((label): label is string => label !== null);
  return labels.length ? labels.join('、') : '暂无';
}

export default function CandidateCompare({ candidates }: { candidates: ApiCandidate[] }) {
  if (candidates.length === 0) return null;

  return (
    <section aria-label="达人对比" className="mx-4 mb-4 overflow-x-auto rounded-xl border border-indigo-100 bg-indigo-50/30">
      <div className="min-w-[620px]">
        <div className="grid grid-cols-[120px_repeat(4,minmax(120px,1fr))] border-b border-indigo-100 bg-white text-[10px] font-semibold text-slate-500">
          <div className="px-3 py-2.5">对比维度</div>
          {candidates.map(candidate => <div key={candidate.id} className="px-3 py-2.5 text-slate-800">{candidate.nickname ?? candidate.kol_id}</div>)}
        </div>
        {[
          ['总分', (candidate: ApiCandidate) => scoreLabel(candidate.total_score)],
          ['排名', (candidate: ApiCandidate) => `#${candidate.rank}`],
          ['平台', (candidate: ApiCandidate) => candidate.platform],
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
