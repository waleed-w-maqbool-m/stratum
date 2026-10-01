"""Shared fixtures.

The fixture database is built once per session and excavated once, because excavation
is the slow part and every detector test reads from the same result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stratum.connectors.executor import ReadOnlyExecutor
from stratum.connectors.registry import build
from stratum.excavate.compile_model import compile_model
from stratum.excavate.pipeline import excavate

from .fixtures.build_fixture import build as build_fixture


@pytest.fixture(scope="session")
def fixture_db() -> Path:
    return build_fixture()


@pytest.fixture(scope="session")
def connector(fixture_db: Path):
    conn = build(f"sqlite:///{fixture_db}")
    conn.connect()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def executor(connector):
    return ReadOnlyExecutor(connector, connector.profile)


@pytest.fixture(scope="session")
def excavated(connector):
    return excavate(connector)


@pytest.fixture(scope="session")
def schema(excavated):
    return excavated.schema


@pytest.fixture(scope="session")
def profiles(excavated):
    return excavated.profiles


@pytest.fixture(scope="session")
def findings(excavated):
    return excavated.findings


@pytest.fixture(scope="session")
def model(excavated):
    return compile_model(excavated, database="tiny", profile_name="sqlite3")


def find(findings, kind, target_contains: str = ""):
    """Every finding of one kind, optionally filtered to a target."""
    out = [f for f in findings if f.kind is kind]
    if target_contains:
        out = [f for f in out if target_contains.lower() in f.target.lower()]
    return out
