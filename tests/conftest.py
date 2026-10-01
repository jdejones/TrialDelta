import os
import tempfile
from pathlib import Path

TEST_DIR = tempfile.TemporaryDirectory(prefix="trialdelta-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(TEST_DIR.name) / "test.db"
).replace("\\", "/")
os.environ["USE_LOCAL_MYSQL"] = "false"
os.environ["APP_MODE"] = "local"
os.environ["APP_ORIGIN"] = "http://127.0.0.1:8765"

import pytest
from fastapi.testclient import TestClient
from trialdelta.db import Base, engine, Session, Company, Study
from trialdelta.app import app


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        c.headers["X-TrialDelta-Client"] = "web"
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db(client):
    with Session() as s:
        yield s


@pytest.fixture
def raw():
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT01234567",
                "briefTitle": "Test trial",
            },
            "statusModule": {
                "overallStatus": "RECRUITING",
                "primaryCompletionDateStruct": {"date": "2028-06", "type": "ESTIMATED"},
                "lastUpdatePostDateStruct": {"date": "2026-09-01"},
            },
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Arvinas, Inc."}},
            "designModule": {
                "phases": ["PHASE2"],
                "enrollmentInfo": {"count": 100, "type": "ESTIMATED"},
                "designInfo": {"allocation": "RANDOMIZED"},
            },
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": "TEST-1"}]
            },
            "conditionsModule": {"conditions": ["Solid Tumor"]},
            "outcomesModule": {
                "primaryOutcomes": [{"measure": "Response", "timeFrame": "12 months"}]
            },
        }
    }


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    TEST_DIR.cleanup()
