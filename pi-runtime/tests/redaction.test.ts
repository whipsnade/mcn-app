import { describe, expect, it } from "vitest";
import { redact } from "../src/redaction";

describe("redact", () => {
  it("把匹配敏感正则的 key 值替换为 ***（递归嵌套）", () => {
    const input = {
      authorization: "Bearer top-secret",
      api_key: "k-123",
      token: "tok-abc",
      nested: {
        secret: "s-1",
        password: "pw",
        cookie: "session=1",
      },
      safe: "keep-me",
    };
    const out = redact(input, []);
    expect(out).toEqual({
      authorization: "***",
      api_key: "***",
      token: "***",
      nested: {
        secret: "***",
        password: "***",
        cookie: "***",
      },
      safe: "keep-me",
    });
  });

  it("替换数组元素与任意字符串中的已知 secret 字符串", () => {
    const input = {
      list: ["header: Bearer abc123", "plain"],
      message: "token is abc123 and stays",
    };
    const out = redact(input, ["abc123"]);
    expect(out).toEqual({
      list: ["header: Bearer ***", "plain"],
      message: "token is *** and stays",
    });
    expect(JSON.stringify(out)).not.toContain("abc123");
  });

  it("不修改原值（返回副本）", () => {
    const input = { api_key: "secret", data: { x: 1 } };
    const snapshot = JSON.stringify(input);
    redact(input, []);
    expect(JSON.stringify(input)).toBe(snapshot);
  });

  it("标量值原样返回", () => {
    expect(redact("hello", [])).toBe("hello");
    expect(redact(42, [])).toBe(42);
    expect(redact(null, [])).toBeNull();
  });
});
