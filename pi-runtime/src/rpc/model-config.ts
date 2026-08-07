export type RuntimeModelSettings = {
  baseUrl: string;
  model: string;
  thinking: string;
  apiKeyPresent: boolean;
};

export function buildPiModelConfig(settings: RuntimeModelSettings) {
  if (!settings.baseUrl || !settings.model || !settings.thinking || !settings.apiKeyPresent) {
    throw new Error("missing_runtime_model_settings");
  }

  return {
    providers: {
      kol_insight_pi_poc: {
        baseUrl: settings.baseUrl,
        apiKey: "$TENCENT_PLAN_API_KEY",
        authHeader: true,
        api: "openai-completions",
        models: [
          {
            id: settings.model,
            reasoning: true,
            thinkingLevelMap: { [settings.thinking]: settings.thinking },
            input: ["text"],
            compat: {
              supportsReasoningEffort: true,
              thinkingFormat: "reasoning_effort",
            },
          },
        ],
      },
    },
  };
}
