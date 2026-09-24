import json
import logging

from app.config import Settings, normalize_database_url
from app.core.enums import Timeframe
from app.logging_config import JsonFormatter, Redactor


def test_railway_postgres_url_is_converted_to_asyncpg():
    url, args = normalize_database_url("postgresql://u:p@host:5432/railway")
    assert url == "postgresql+asyncpg://u:p@host:5432/railway"
    assert args == {}


def test_legacy_postgres_scheme_and_sslmode_are_handled():
    url, args = normalize_database_url("postgres://u:p@host:5432/db?sslmode=require")
    assert url.startswith("postgresql+asyncpg://u:p@host:5432/db")
    assert "sslmode" not in url
    assert args == {"ssl": "require"}


def test_sqlite_url_untouched():
    url, args = normalize_database_url("sqlite+aiosqlite:///tmp/x.db")
    assert url == "sqlite+aiosqlite:///tmp/x.db" and args == {"timeout": 30}  # waits for the single writer


def test_csv_env_values_and_timeframes(monkeypatch):
    monkeypatch.setenv("BINANCE_REST_BASE_URLS", "https://a.example, https://b.example")
    monkeypatch.setenv("INTEGRITY_REQUIRED_TIMEFRAMES", "5m,1H,1D")
    monkeypatch.setenv("SYMBOL_OVERRIDES", "MIOTA:IOTA")
    s = Settings(_env_file=None)
    assert s.binance_rest_base_urls == ["https://a.example", "https://b.example"]
    assert s.required_timeframes == (Timeframe.M5, Timeframe.H1, Timeframe.D1)
    assert s.symbol_overrides == {"MIOTA": "IOTA"}


def test_openai_key_is_secret_and_optional(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert Settings(_env_file=None).openai_configured is False
    s = Settings(_env_file=None, openai_api_key="sk-test-1234567890abcdef")
    assert s.openai_configured is True
    assert "sk-test" not in repr(s)
    assert s.secret_values() == ["sk-test-1234567890abcdef"]


def test_timeframe_parsing():
    assert Timeframe.parse("1H") is Timeframe.H1
    assert Timeframe.parse("4h").label == "4H"
    assert Timeframe.D1.seconds == 86400
    try:
        Timeframe.parse("2h")
    except ValueError as exc:
        assert "Unsupported" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_redactor_masks_keys_passwords_and_known_secrets():
    r = Redactor(["my-super-secret-value"])
    text = r.redact(
        "key=sk-proj-ABCDEFGHIJKLMNOPQRST auth: Bearer abcdefghijklmnopqrstuvwxyz "
        "db=postgresql://user:hunter2@host/db other=my-super-secret-value"
    )
    assert "ABCDEFGHIJ" not in text
    assert "abcdefghijklmnop" not in text
    assert "hunter2" not in text
    assert "my-super-secret-value" not in text


def test_json_formatter_redacts_extra_fields_and_exceptions():
    fmt = JsonFormatter(Redactor([]))
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "call failed", None, None)
    record.provider = "openai"
    record.error = "invalid key sk-live-ABCDEFGHIJKLMNOPQRS"
    out = json.loads(fmt.format(record))
    assert out["provider"] == "openai"
    assert "ABCDEFGHIJ" not in out["error"]
