import os


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_DATABASE", "kol_insight_test")
os.environ.setdefault("MYSQL_USER", "root")
os.environ.setdefault("MYSQL_PASSWORD", "test-only-password")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-at-least-32-characters")
