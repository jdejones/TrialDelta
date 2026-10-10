"""Read-only source selection for a self-contained portfolio snapshot."""

import ipaddress
import os
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import create_engine, inspect


def safe_source_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlparse(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port
        hostname = parsed.hostname.lower().rstrip(".")
        if "%" in hostname:
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if "." not in hostname or hostname.endswith(
                (".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid")
            ):
                return None
        else:
            if not address.is_global:
                return None
        if any(hostname == domain or hostname.endswith("." + domain) for domain in (
            "drive.google.com", "docs.google.com", "dropbox.com", "onedrive.live.com", "1drv.ms", "box.com",
        )):
            return None
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
        if query_keys.intersection({
            "token", "access_token", "api_key", "apikey", "password", "secret", "signature", "x-amz-signature",
        }):
            return None
    except ValueError:
        return None
    return value


def source_engine(destination):
    # Hosted feeds never reach a workstation or use its source credentials.
    if os.getenv("APP_MODE", "local") == "hosted":
        return destination if destination.dialect.name == "sqlite" else None
    if destination.dialect.name == "sqlite" and any(
        inspect(destination).has_table(table)
        for table in ("td_portfolio_news", "td_portfolio_filings")
    ):
        destination._trialdelta_portfolio_snapshot = True
        return destination
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if source_url:
        return create_engine(source_url, pool_pre_ping=True)
    if os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
        return destination
    return None


def source_table(source, name):
    portfolio = {
        "news": "td_portfolio_news",
        "filings": "td_portfolio_filings",
        "symbols": "td_portfolio_symbols",
    }
    if source.dialect.name == "sqlite" and (
        os.getenv("APP_MODE", "local") == "hosted"
        or getattr(source, "_trialdelta_portfolio_snapshot", False)
    ):
        return portfolio[name]
    return {"news": "news.stock_news", "filings": "filings.biotech_filings",
            "symbols": "stocks.symbol_sector_industry"}[name]


def sqlite_public_urls(connection):
    connection.connection.driver_connection.create_function(
        "td_public_url", 1, safe_source_url, deterministic=True
    )
