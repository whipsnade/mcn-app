import { describe, expect, it } from "vitest";

import { buildSecretEnv, clearSecretEnv } from "../src/secret-env.js";

describe("worker secret environment", () => {
  it("builds a private child env without mutating the parent process", () => {
    const parent = { PATH: "/usr/bin", LANG: "C" };
    Object.assign(parent, {
      TENCENT_PLAN_API_KEY: "host-secret",
      DATATAP_MCP_TOKEN: "host-secret",
      MYSQL_PASSWORD: "host-secret",
      JWT_SECRET: "host-secret",
      PI_WORKER_TEMP_DIR: "/tmp/caller-owned",
    });
    const env = buildSecretEnv({
      modelBaseUrl: "https://model.invalid",
      modelApiKey: "model-secret",
      datatapToken: "datatap-secret",
      datatapUrls: { insightCube: "https://cube.invalid" },
    }, parent);

    expect(parent).toMatchObject({
      PATH: "/usr/bin",
      LANG: "C",
      TENCENT_PLAN_API_KEY: "host-secret",
      DATATAP_MCP_TOKEN: "host-secret",
      MYSQL_PASSWORD: "host-secret",
      JWT_SECRET: "host-secret",
      PI_WORKER_TEMP_DIR: "/tmp/caller-owned",
    });
    expect(env).toMatchObject({ PATH: "/usr/bin", LANG: "C", PI_MODEL_BASE_URL: "https://model.invalid", PI_MODEL_API_KEY: "model-secret", PI_DATATAP_TOKEN: "datatap-secret" });
    expect(env).not.toHaveProperty("HOME");
    expect(env).not.toHaveProperty("TENCENT_PLAN_API_KEY");
    expect(env).not.toHaveProperty("DATATAP_MCP_TOKEN");
    expect(env).not.toHaveProperty("MYSQL_PASSWORD");
    expect(env).not.toHaveProperty("JWT_SECRET");
    expect(env).not.toHaveProperty("PI_WORKER_TEMP_DIR");
    clearSecretEnv(env);
    expect(env.PI_MODEL_API_KEY).toBeUndefined();
    expect(env.PI_DATATAP_TOKEN).toBeUndefined();
  });

  it("canonicalizes backend DataTap secret keys to the approved Pi service names", () => {
    const env = buildSecretEnv({
      modelBaseUrl: "https://model.invalid",
      modelApiKey: "model-secret",
      datatapToken: "datatap-secret",
      datatapUrls: {
        "insight-cube-mcp": "https://cube.invalid",
        "social-grow-mcp": "https://grow.invalid",
        "social-grow-content-mcp": "https://content.invalid",
        "aktools-mcp": "https://aktools.invalid",
      },
    }, {});

    expect(env).toMatchObject({
      PI_DATATAP_URL_INSIGHT_CUBE: "https://cube.invalid",
      PI_DATATAP_URL_SOCIAL_GROW: "https://grow.invalid",
      PI_DATATAP_URL_SOCIAL_GROW_CONTENT: "https://content.invalid",
      PI_DATATAP_URL_AKTOOLS: "https://aktools.invalid",
    });
  });
});
