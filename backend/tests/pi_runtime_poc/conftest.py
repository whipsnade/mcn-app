"""Pi POC SQLite 测试的最小启动配置（只含非敏感占位值）。

pytest 会在收集测试模块前加载本文件，避免导入运行时工具时意外读取本机 `.env`。
所有测试仍显式构造 SQLite 内存库与随机 Settings，不连接任何 MySQL 数据库。
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MYSQL_DATABASE", "kol_insight_pi_poc")
os.environ.setdefault("MYSQL_USER", "pi")
os.environ.setdefault("MYSQL_PASSWORD", "pi")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-characters-12345678")
os.environ.setdefault("TENCENT_PLAN_API_KEY", "placeholder")
os.environ.setdefault("DATATAP_MCP_TOKEN", "placeholder")
