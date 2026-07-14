from enum import StrEnum


class DataTapService(StrEnum):
    INSIGHT_CUBE = "insight-cube-mcp"
    SOCIAL_GROW = "social-grow-mcp"
    SOCIAL_GROW_CONTENT = "social-grow-content-mcp"
    AKTOOLS = "aktools-mcp"
    BILIBILI = "bilibili-mcp"


class McpCallStatus(StrEnum):
    PLANNED = "planned"
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SETTLED = "settled"
    RELEASED = "released"
