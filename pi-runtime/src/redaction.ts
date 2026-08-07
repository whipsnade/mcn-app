/**
 * 递归脱敏：只用于审计与事件，绝不修改返回 Pi 的 MCP 业务 payload。
 *
 * - key 名匹配 `authorization|api[_-]?key|token|secret|password|cookie` 时值替换为 `***`；
 * - 任意字符串中出现当前进程内已知 secret 子串时替换为 `***`；
 * - 返回深拷贝，绝不修改原值。
 */

const SECRET_KEY_PATTERN = /authorization|api[_-]?key|token|secret|password|cookie/i;
const REDACTED = "***";

export function redact(value: unknown, knownSecrets: readonly string[]): unknown {
  if (typeof value === "string") {
    return redactKnownSecrets(value, knownSecrets);
  }
  if (Array.isArray(value)) {
    return value.map((item) => redact(item, knownSecrets));
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value)) {
      out[key] = SECRET_KEY_PATTERN.test(key) ? REDACTED : redact(child, knownSecrets);
    }
    return out;
  }
  return value;
}

function redactKnownSecrets(value: string, knownSecrets: readonly string[]): string {
  let out = value;
  for (const secret of knownSecrets) {
    if (secret && out.includes(secret)) {
      out = out.split(secret).join(REDACTED);
    }
  }
  return out;
}
