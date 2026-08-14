"""Pi POC 的最小安全诊断投影。

原始数据库异常可能包含 SQL、参数、DSN 或供应商凭证；POC 诊断只能记录可归因的
异常类型、MySQL errno 与约束名。
"""

import re
from typing import Any

_CONSTRAINT_PATTERN = re.compile(r"(?:for key|constraint) ['`]([^'`]+)['`]", re.IGNORECASE)


def safe_db_diagnostic(error: BaseException) -> dict[str, Any]:
    """投影数据库错误，不复制原始异常、SQL 或参数。"""
    diagnostic: dict[str, Any] = {"exception_type": type(error).__name__}
    original = getattr(error, "orig", None)
    errno = getattr(original, "errno", None)
    if isinstance(errno, int):
        diagnostic["mysql_errno"] = errno
    if original is not None:
        match = _CONSTRAINT_PATTERN.search(str(original))
        if match is not None:
            diagnostic["constraint"] = match.group(1)
    return diagnostic
