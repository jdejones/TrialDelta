"""Check startup credential selection without connecting to a database."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIGURATION_KEYS = {
    "DATABASE_URL", "DATABASE_PASSWORD", "USE_LOCAL_MYSQL", "MYSQL_USER",
    "MYSQL_HOST", "MYSQL_PORT", "GPTDB",
}
INSPECT_CONFIGURATION = """
import json
import dotenv
import pymysql

dotenv.load_dotenv = lambda *args, **kwargs: None

def forbid_connection(*args, **kwargs):
    raise AssertionError("Credential inspection must not connect to MySQL")

pymysql.connect = forbid_connection
try:
    from trialdelta.db import engine
except Exception as exc:
    print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}))
else:
    print(json.dumps({
        "driver": engine.url.drivername,
        "username": engine.url.username,
        "password": engine.url.password,
        "database": engine.url.database,
    }))
    engine.dispose()
"""


def inspect_configuration(tmp_path, settings):
    # Do not inherit real database credentials or discover the checkout's .env.
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() not in CONFIGURATION_KEYS
    }
    environment.update(settings)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", INSPECT_CONFIGURATION],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_local_mysql_uses_gptdb_credential_even_when_root_credential_exists(tmp_path):
    result = inspect_configuration(tmp_path, {
        "USE_LOCAL_MYSQL": "true",
        "gptdb": "gptdb-test-password",
        "DATABASE_PASSWORD": "unused-root-password",
        "DATABASE_URL": "sqlite:///must-not-be-used.db",
    })
    assert result == {
        "driver": "mysql+pymysql", "username": "gptdb",
        "password": "gptdb-test-password", "database": "trialdelta",
    }


@pytest.mark.parametrize("gptdb_password", [None, ""])
def test_local_mysql_rejects_missing_gptdb_credential_despite_root_credential(tmp_path, gptdb_password):
    settings = {"USE_LOCAL_MYSQL": "true", "DATABASE_PASSWORD": "unused-root-password"}
    if gptdb_password is not None:
        settings["gptdb"] = gptdb_password
    result = inspect_configuration(tmp_path, settings)
    assert result["error_type"] == "ValueError"
    assert "gptdb" in result["error"]
    assert "unused-root-password" not in result["error"]


def test_explicit_database_url_keeps_its_own_credentials(tmp_path):
    result = inspect_configuration(tmp_path, {
        "USE_LOCAL_MYSQL": "false",
        "DATABASE_URL": "mysql+pymysql://url-user:url-password@db-host/trialdelta",
    })
    assert result == {
        "driver": "mysql+pymysql", "username": "url-user",
        "password": "url-password", "database": "trialdelta",
    }
