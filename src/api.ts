import { AnalyzePayload, AnalyzeResult, analyzeMock } from './mockAnalyst';

// Calls the analyze backend when one is available (e.g. `npm run dev` with the
// Express server), and transparently falls back to the offline mock analyst when
// there is no backend — which is the case on static hosting like GitHub Pages.
export async function analyze(payload: AnalyzePayload): Promise<AnalyzeResult> {
  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    // On GitHub Pages the request resolves to index.html (HTML, not JSON) or 404,
    // so guard on both the status and the content type before trusting it.
    const contentType = response.headers.get('content-type') || '';
    if (response.ok && contentType.includes('application/json')) {
      return (await response.json()) as AnalyzeResult;
    }
  } catch {
    // Network error / no backend — fall through to the offline mock.
  }

  return analyzeMock(payload);
}
