import { describe, expect, it } from "vitest";
import { parseRpcLine } from "../src/rpc/protocol";

describe("parseRpcLine", () => {
  it("只接受单条 LF JSON 记录并保留字符串内 U+2028", () => {
    const record = parseRpcLine('{"type":"message_update","text":"a b"}\r');

    expect(record.type).toBe("message_update");
    expect(record.text).toBe("a b");
  });

  it("拒绝空行和非对象 JSON", () => {
    expect(() => parseRpcLine(" ")).toThrow("empty_rpc_record");
    expect(() => parseRpcLine("[]")).toThrow("invalid_rpc_record");
  });
});
