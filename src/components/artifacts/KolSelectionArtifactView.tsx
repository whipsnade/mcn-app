import { Activity, ListChecks, Star } from 'lucide-react';
import { Fragment } from 'react';

import type {
  AgentArtifactDistributionItem,
  KolSelectionItem,
  KolSelectionPayload,
} from '../../api/agentArtifacts';
import { Card, formatExposure, formatNumber, restrictedCount, restrictedRatio, restrictedScore } from '../reportPrimitives';

const PLATFORM_NAMES: Record<string, string> = {
  xiaohongshu: '小红书', douyin: '抖音', bilibili: 'B站', kuaishou: '快手', weibo: '微博',
};

function platformName(platform: string): string {
  return PLATFORM_NAMES[platform] ?? platform;
}

/** kol_score_v2 八个维度的展示顺序与中文标签（权重来自 payload.data.scoring.weights）。 */
const SCORE_DIMENSION_LABELS = [
  { key: 'industry_interest', label: '行业兴趣' },
  { key: 'target_region', label: '目标地区' },
  { key: 'target_age', label: '目标年龄' },
  { key: 'engagement', label: '互动表现' },
  { key: 'active_follower', label: '活跃粉丝' },
  { key: 'content', label: '内容质量' },
  { key: 'followers', label: '粉丝规模' },
  { key: 'engagement_follower_ratio', label: '互动粉丝比' },
];

function KolCard({ item, onOpenDetail }: { item: KolSelectionItem; onOpenDetail?: (item: KolSelectionItem) => void }) {
  const nickname = item.nickname || '未知达人';
  const snapshot = item.score_snapshot;
  const isValueScore = snapshot.version === 'kol_value_score_v3';
  // kol_score_v2 专属字段：历史只读降级，不为缺失的价格维度伪造数值。
  const legacyStars = snapshot.version === 'kol_score_v2' ? snapshot.stars : undefined;
  const legacyTotal = snapshot.version === 'kol_score_v2' ? snapshot.total : undefined;
  const followers = item.followers != null ? formatExposure(item.followers) : '数据受限';
  return (
    <section className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <button
        type="button"
        aria-label={`查看${nickname}详情`}
        onClick={() => onOpenDetail?.(item)}
        className="flex w-full items-center gap-2.5 text-left"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[13px] font-bold text-indigo-600">
          {nickname.slice(0, 1)}
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-1.5">
            <span className="truncate text-[12px] font-semibold text-slate-800">{nickname}</span>
            {legacyStars && <span className="shrink-0 text-[10px] text-amber-500">{legacyStars}</span>}
          </p>
          <p className="mt-0.5 truncate text-[10px] text-slate-400">
            {platformName(item.platform)} · 粉丝 {followers}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="text-[12px] font-bold text-slate-800">
            <span className="mr-1 text-[10px] font-normal text-slate-400">
              {isValueScore ? '投放性价比指数' : '综合评分'}
            </span>{isValueScore ? formatNumber(snapshot.value_score) : legacyTotal != null ? formatNumber(legacyTotal) : '—'}
          </p>
          {snapshot.rating && (
            <span className="mt-1 inline-block rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-600">
              {snapshot.rating}
            </span>
          )}
        </div>
      </button>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className="rounded-lg bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-600">
          数据完整度 {Math.round(snapshot.data_completeness)}%
        </span>
        {item.quoted_price != null && (
          <span className="rounded-lg bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
            报价 ¥{formatNumber(item.quoted_price)}
          </span>
        )}
        {isValueScore && (
          <span className="rounded-lg bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-600">
            效果 {formatNumber(snapshot.effect_score)} · 价格效率 {formatNumber(snapshot.price_efficiency_score)}
          </span>
        )}
        {item.reasons.length > 0 && (
          <span className="rounded-lg bg-indigo-50 px-2 py-0.5 text-[10px] font-medium text-indigo-600">{item.reasons[0]}</span>
        )}
      </div>
    </section>
  );
}

function DistributionList({ title, items }: { title: string; items: AgentArtifactDistributionItem[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="mb-1.5 text-[11px] font-semibold text-slate-600">{title}</p>
      <ul className="space-y-1">
        {items.map(item => (
          <li key={item.key} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
            <span className="min-w-0 truncate">{item.label}</span>
            <b className="shrink-0 text-slate-700">{restrictedCount(item.count)} · {restrictedRatio(item.share)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface KolSelectionArtifactViewProps {
  payload: KolSelectionPayload;
  onOpenDetail?: (item: KolSelectionItem) => void;
}

export default function KolSelectionArtifactView({ payload, onOpenDetail }: KolSelectionArtifactViewProps) {
  const { data, narrative, data_status, limitations } = payload;
  const items = [...data.items].sort((a, b) => a.rank - b.rank).slice(0, 20);
  const topItem = items[0];
  const weights = data.scoring.weights;
  const isValueScore = data.scoring.version === 'kol_value_score_v3';

  return (
    <div className="space-y-3">
      {data_status === 'restricted' && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="text-[11px] font-semibold text-amber-700">数据受限</p>
          {limitations.map(limitation => (
            <p key={limitation.code} className="mt-1 text-[10px] leading-4 text-amber-600">{limitation.message}</p>
          ))}
        </div>
      )}

      <section data-chapter="items">
        <Card title="圈选达人" icon={<ListChecks className="h-4 w-4" />}>
          <p className="mb-2.5 text-[10px] text-slate-400">
            按{isValueScore ? '投放性价比指数' : '综合评分'}展示 Top 20 · 点击达人查看详情
          </p>
          <div className="space-y-2">
            {items.map(item => (
              <Fragment key={`${item.platform}-${item.kol_uid}`}>
                <KolCard item={item} onOpenDetail={onOpenDetail} />
              </Fragment>
            ))}
            {items.length === 0 && <p className="py-6 text-center text-[11px] text-slate-400">暂无圈选达人</p>}
          </div>
        </Card>
      </section>

      <section data-chapter="score_guide">
        <Card title="评分说明" icon={<Star className="h-4 w-4" />}>
          <p className="mb-2 text-[10px] text-slate-400">
            {isValueScore
              ? '投放性价比指数：效果与匹配度 70 + 价格效率 30；缺失维度记 0 分，不做估算'
              : '历史 kol_score_v2：仅只读展示既有八维评分，不伪造价格效率指标'}
          </p>
          {topItem ? (
            <div className="space-y-1.5">
              {SCORE_DIMENSION_LABELS.map(({ key, label }) => {
                const dim = topItem.score_snapshot.dimensions[key];
                if (!dim) return null;
                const missing = Boolean(dim.missing_reason);
                return (
                  <div
                    key={key}
                    data-testid={`score-dim-${key}`}
                    className="flex items-center gap-2 rounded-lg bg-slate-50 px-2.5 py-2 text-[11px]"
                  >
                    <span className="w-16 shrink-0 font-medium text-slate-600">{label}</span>
                    <span className="w-10 shrink-0 text-slate-400">{weights[key] ?? dim.weight}%</span>
                    <span className={`shrink-0 font-bold ${missing ? 'text-amber-600' : 'text-slate-800'}`}>
                      {missing ? '0分' : formatNumber(dim.raw_score)}
                    </span>
                    {missing && dim.missing_reason && (
                      <span className="min-w-0 truncate text-[10px] text-amber-600">{dim.missing_reason}</span>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="py-4 text-center text-[11px] text-slate-400">暂无评分数据</p>
          )}
        </Card>
      </section>

      <section data-chapter="summary">
        <Card title="趋势现状" icon={<Activity className="h-4 w-4" />}>
          <p className="mb-3 text-[11px] font-medium text-slate-600">
            候选达人 {restrictedScore(data.summary.candidate_count)} · 已选 {restrictedScore(data.summary.selected_count)}
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            <DistributionList title="平台分布" items={data.summary.platform_distribution} />
            <DistributionList title="评级分布" items={data.summary.rating_distribution} />
          </div>
          {narrative.selection_summary && (
            <p className="mt-3 whitespace-pre-wrap rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] leading-4 text-slate-500">
              {narrative.selection_summary}
            </p>
          )}
        </Card>
      </section>
    </div>
  );
}
