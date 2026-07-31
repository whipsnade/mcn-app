import { authorizedFetch, request } from './client';
import type {
  ApiArtifactsSummary,
  ApiSessionReportItem,
  ArtifactModuleKey,
} from './contracts';


export type SessionReportType = 'brand_analysis' | 'campaign_analysis' | 'kol_analysis';

export function listSessionReports(
  sessionId: string,
  reportType: SessionReportType,
): Promise<ApiSessionReportItem[]> {
  return request<ApiSessionReportItem[]>(
    `/api/v1/sessions/${sessionId}/reports?report_type=${reportType}`,
  );
}

export function getArtifactsSummary(sessionId: string): Promise<ApiArtifactsSummary> {
  return request<ApiArtifactsSummary>(`/api/v1/sessions/${sessionId}/artifacts/summary`);
}

export function markArtifactRead(
  sessionId: string,
  moduleKey: ArtifactModuleKey,
  artifactId: string,
): Promise<void> {
  return request<void>(`/api/v1/sessions/${sessionId}/artifact-read-state`, {
    method: 'PUT',
    body: JSON.stringify({ module_key: moduleKey, artifact_id: artifactId }),
  });
}

// xlsx 是二进制下载，不能走 request 的 JSON 路径（模式同 kolSelection.ts 的 downloadKolSelection）。
export async function downloadBrandReport(sessionId: string, reportId: string): Promise<void> {
  const response = await authorizedFetch(`/api/v1/sessions/${sessionId}/reports/${reportId}/export`);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `HTTP_${response.status}`);
  }
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : '品牌分析报告.xlsx';
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  // Safari/Firefox 对未挂载的 <a> 及同步 revoke 会取消下载，挂到 DOM 并延迟回收。
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
