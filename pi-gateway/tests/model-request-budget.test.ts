import { describe, expect, it, vi } from "vitest";

import {
  createBoundedStreamSimple,
  isDecisionLimitError,
  ModelRequestBudget,
  PiDecisionLimitError,
} from "../src/model-request-budget.js";

describe("ModelRequestBudget", () => {
  it.each([1, 2, 50, 100])("accepts integer budget %s", (value) => {
    expect(new ModelRequestBudget(value).maxDecisions).toBe(value);
  });

  it.each([0, -1, 101, 2.5, Number.NaN, "2", true, undefined, null])(
    "rejects invalid budget %s",
    (value) => {
      expect(() => new ModelRequestBudget(value as number)).toThrow(
        "pi_gateway_runtime_snapshot_invalid",
      );
    },
  );

  it("counts success and failure attempts and throws pi_decision_limit before the next dispatch", () => {
    const budget = new ModelRequestBudget(2);
    budget.assertAndConsume();
    budget.assertAndConsume();
    expect(budget.usedCount).toBe(2);
    expect(budget.limitExceeded).toBe(false);
    expect(() => budget.assertAndConsume()).toThrow(PiDecisionLimitError);
    expect(budget.limitExceeded).toBe(true);
    expect(budget.usedCount).toBe(2);
    try {
      budget.assertAndConsume();
      expect.unreachable();
    } catch (error) {
      expect(isDecisionLimitError(error)).toBe(true);
      expect((error as Error).message).toBe("pi_decision_limit");
    }
  });

  it("is scoped per instance (no cross-run global counting)", () => {
    const a = new ModelRequestBudget(1);
    const b = new ModelRequestBudget(1);
    a.assertAndConsume();
    expect(() => a.assertAndConsume()).toThrow("pi_decision_limit");
    expect(() => b.assertAndConsume()).not.toThrow();
  });
});

describe("createBoundedStreamSimple", () => {
  it("checks and consumes the budget synchronously before the delegate runs", () => {
    const order: string[] = [];
    const budget = new ModelRequestBudget(1);
    const delegate = vi.fn(() => {
      order.push("delegate");
      return "stream";
    });
    const stream = createBoundedStreamSimple(delegate, budget);
    order.push("before");
    expect(stream({}, {}, {})).toBe("stream");
    expect(order).toEqual(["before", "delegate"]);
    expect(delegate).toHaveBeenCalledTimes(1);
  });

  it("forces maxRetries=0 on the delegate options without mutating the caller's object", () => {
    const budget = new ModelRequestBudget(3);
    const delegate = vi.fn(() => "stream");
    const stream = createBoundedStreamSimple(delegate, budget);
    const options = { maxRetries: 5, timeoutMs: 1000 };
    stream({}, {}, options);
    expect(delegate).toHaveBeenCalledWith({}, {}, { maxRetries: 0, timeoutMs: 1000 });
    expect(options.maxRetries).toBe(5);
    stream({}, {}, undefined);
    expect(delegate).toHaveBeenLastCalledWith({}, {}, { maxRetries: 0 });
  });

  it("never reaches the delegate once the budget is spent (0 HTTP dispatch)", () => {
    const budget = new ModelRequestBudget(1);
    const delegate = vi.fn(() => "stream");
    const stream = createBoundedStreamSimple(delegate, budget);
    stream({}, {}, {});
    expect(() => stream({}, {}, {})).toThrow("pi_decision_limit");
    expect(delegate).toHaveBeenCalledTimes(1);
  });

  it("counts failed provider attempts as consumed budget", () => {
    const budget = new ModelRequestBudget(2);
    const delegate = vi.fn(() => {
      throw new Error("http_500");
    });
    const stream = createBoundedStreamSimple(delegate, budget);
    expect(() => stream({}, {}, {})).toThrow("http_500");
    expect(() => stream({}, {}, {})).toThrow("http_500");
    expect(budget.usedCount).toBe(2);
    expect(() => stream({}, {}, {})).toThrow("pi_decision_limit");
    expect(delegate).toHaveBeenCalledTimes(2);
  });
});
