import type { PiRpcRecord } from "./protocol";

export type PiProbeRequest = {
  id: "poc-get-state" | "poc-get-commands" | "poc-abort";
  type: "get_state" | "get_commands" | "abort";
};

const EXPLICIT_EXTENSION = "poc-explicit-extension";
const EXPLICIT_SKILL = "skill:poc-explicit-skill";
const AUTOMATIC_EXTENSION = "poc-auto-extension";
const AUTOMATIC_SKILL = "skill:poc-auto-skill";

export function makeAuditRequests(): readonly PiProbeRequest[] {
  return [
    { id: "poc-get-state", type: "get_state" },
    { id: "poc-get-commands", type: "get_commands" },
    { id: "poc-abort", type: "abort" },
  ];
}

type PiCommand = { name: string; source: string };

function responseFor(
  records: readonly PiRpcRecord[],
  request: PiProbeRequest,
): PiRpcRecord {
  const response = records.find(
    (record) => record.type === "response" && record.id === request.id,
  );

  if (
    !response ||
    response.command !== request.type ||
    response.success !== true ||
    typeof response.id !== "string"
  ) {
    throw new Error("invalid_rpc_response");
  }

  return response;
}

function commands(response: PiRpcRecord): PiCommand[] {
  const data = response.data;
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new Error("invalid_rpc_response");
  }

  const commands = (data as { commands?: unknown }).commands;
  if (!Array.isArray(commands)) {
    throw new Error("invalid_rpc_response");
  }

  return commands.map((command) => {
    const value = command as { name?: unknown; source?: unknown };
    if (typeof value.name !== "string" || typeof value.source !== "string") {
      throw new Error("invalid_rpc_response");
    }
    return { name: value.name, source: value.source };
  });
}

export function assertAuditTranscript(
  requests: readonly PiProbeRequest[],
  records: readonly PiRpcRecord[],
): {
  stdoutJsonlOnly: true;
  responseIds: string[];
  explicitResources: string[];
} {
  const responses = requests.map((request) => responseFor(records, request));
  const availableCommands = commands(responses[1]);
  const names = availableCommands.map((command) => command.name);

  if (names.includes(AUTOMATIC_EXTENSION) || names.includes(AUTOMATIC_SKILL)) {
    throw new Error("automatic_resource_loaded");
  }

  const expectedResources: PiCommand[] = [
    { name: EXPLICIT_EXTENSION, source: "extension" },
    { name: EXPLICIT_SKILL, source: "skill" },
  ];
  if (
    !expectedResources.every((expected) =>
      availableCommands.some(
        (command) => command.name === expected.name && command.source === expected.source,
      ),
    )
  ) {
    throw new Error("explicit_resource_missing");
  }

  return {
    stdoutJsonlOnly: true,
    responseIds: responses.map((response) => response.id as string),
    explicitResources: expectedResources.map((resource) => resource.name),
  };
}

export const rpcProbeResources = {
  explicitExtension: EXPLICIT_EXTENSION,
  explicitSkill: EXPLICIT_SKILL,
  automaticExtension: AUTOMATIC_EXTENSION,
  automaticSkill: AUTOMATIC_SKILL,
} as const;
