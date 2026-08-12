/**
 * Per-Run model request budget, enforced at the provider dispatch boundary.
 *
 * The gate runs synchronously inside the worker's streamSimple wrapper —
 * before any HTTP request can leave the process.  Both successful and failed
 * provider attempts count.  When the budget is exhausted the next call throws
 * the stable ``pi_decision_limit`` error before touching the network; the
 * worker turns that into a business-failed terminal (never an infrastructure
 * retry).
 */

export const DECISION_LIMIT_CODE = "pi_decision_limit";

export class PiDecisionLimitError extends Error {
  readonly code = DECISION_LIMIT_CODE;

  constructor(readonly maxDecisions: number) {
    super(DECISION_LIMIT_CODE);
    this.name = "PiDecisionLimitError";
  }
}

/** 稳定业务终态码：provider 流式调用终局失败（重试已关闭，错误即终态）。 */
export const PROVIDER_FAILURE_CODE = "pi_model_provider_error";

export class PiModelProviderError extends Error {
  readonly code = PROVIDER_FAILURE_CODE;

  constructor() {
    super(PROVIDER_FAILURE_CODE);
    this.name = "PiModelProviderError";
  }
}

export function isDecisionLimitError(error: unknown): boolean {
  if (error instanceof PiDecisionLimitError) return true;
  if (!error || typeof error !== "object") return false;
  const code = (error as { code?: unknown }).code;
  if (code === DECISION_LIMIT_CODE) return true;
  return error instanceof Error && error.message === DECISION_LIMIT_CODE;
}

export function isProviderFailureError(error: unknown): boolean {
  if (error instanceof PiModelProviderError) return true;
  if (!error || typeof error !== "object") return false;
  const code = (error as { code?: unknown }).code;
  if (code === PROVIDER_FAILURE_CODE) return true;
  return error instanceof Error && error.message === PROVIDER_FAILURE_CODE;
}

export class ModelRequestBudget {
  private used = 0;
  private exceeded = false;

  constructor(readonly maxDecisions: number) {
    if (
      typeof maxDecisions !== "number" ||
      !Number.isInteger(maxDecisions) ||
      maxDecisions < 1 ||
      maxDecisions > 100
    ) {
      throw new Error("pi_gateway_runtime_snapshot_invalid");
    }
  }

  /** Provider dispatch attempts consumed so far (success and failure alike). */
  get usedCount(): number {
    return this.used;
  }

  /** True once a call has been rejected by the limit. */
  get limitExceeded(): boolean {
    return this.exceeded;
  }

  /**
   * Synchronously check and consume one provider dispatch.  Throws
   * ``pi_decision_limit`` before any network work when the budget is spent.
   */
  assertAndConsume(): void {
    if (this.used >= this.maxDecisions) {
      this.exceeded = true;
      throw new PiDecisionLimitError(this.maxDecisions);
    }
    this.used += 1;
  }
}

interface StreamDelegate {
  (model: unknown, context: unknown, options?: Record<string, unknown>): unknown;
}

/**
 * Bind a budget gate to a provider stream delegate.  The delegate is captured
 * by the caller (static import of the built-in provider implementation, or the
 * offline fake factory) — never resolved through the mutable api registry, so
 * the wrapper can neither recurse into nor unregister itself.  Every real
 * provider call is forced to ``maxRetries: 0``: retry policy belongs to the
 * explicit budget/settings layer, not the HTTP client.
 */
export function createBoundedStreamSimple(
  delegate: StreamDelegate,
  budget: ModelRequestBudget,
): StreamDelegate {
  return (model, context, options) => {
    budget.assertAndConsume();
    return delegate(model, context, { ...(options ?? {}), maxRetries: 0 });
  };
}
