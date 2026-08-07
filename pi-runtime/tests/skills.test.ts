import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const skillsRoot = resolve(process.cwd(), "skills");

const skills = [
  "social-marketing-analyst",
  "brand-research-report",
  "campaign-evaluation-report",
  "kol-selection-report",
  "artifact-drilldown",
  "marketing-strategy",
] as const;

const reportSkills = [
  "brand-research-report",
  "campaign-evaluation-report",
  "kol-selection-report",
] as const;

function skillPath(name: string): string {
  return resolve(skillsRoot, name, "SKILL.md");
}

function skillText(name: string): string {
  return readFileSync(skillPath(name), "utf8");
}

describe("Pi 营销 Skills", () => {
  it("每个 Skill 的 frontmatter 名称与目录一致且含 description", () => {
    for (const name of skills) {
      expect(existsSync(skillPath(name))).toBe(true);
      const text = skillText(name);
      expect(text).toMatch(new RegExp(`^---\\nname: ${name}\\n`, "m"));
      expect(text).toMatch(/^description: .+/m);
    }
  });

  it("品牌报告 Skill 引用脱敏的成功案例", () => {
    expect(
      existsSync(
        resolve(skillsRoot, "brand-research-report", "references", "chatgpt-datatap-success.md"),
      ),
    ).toBe(true);
  });

  it("每个报告 Skill 都要求 Evidence、partial、Builder feedback、澄清与完成条件", () => {
    for (const name of reportSkills) {
      const text = skillText(name);
      for (const required of ["Evidence", "partial", "Builder feedback", "禁止编造", "澄清", "完成条件"]) {
        expect(text).toContain(required);
      }
    }
  });

  it("营销总则把信息不足显式收口到受控澄清工具", () => {
    expect(skillText("social-marketing-analyst")).toContain("request_clarification");
  });

  it("Skill 与案例引用不含密钥、来源案例固定值或固定工具顺序", () => {
    const documents = [
      ...skills.map(skillText),
      readFileSync(
        resolve(skillsRoot, "brand-research-report", "references", "chatgpt-datatap-success.md"),
        "utf8",
      ),
    ];
    const combined = documents.join("\n");

    expect(combined).not.toMatch(/sk-[A-Za-z0-9_-]{12,}/);
    expect(combined).not.toMatch(/瑞幸咖啡|2026-07-\d{2}|425457/);
    expect(combined).not.toMatch(/先(?:调用|使用).{0,100}(?:再|然后)(?:调用|使用)/);
  });
});
