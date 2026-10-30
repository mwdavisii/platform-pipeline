"""The portal keeps its own copy of the builder's reserved env names and prefixes (portal/env_names.py: it can't import the
builder); this holds the copy equal to builder.spec, so a binding secret the portal accepts is never one deploy-verify refuses."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))

from builder import spec  # noqa: E402

from portal import env_names  # noqa: E402


def test_the_reserved_names_and_prefixes_are_the_builder_s():
    assert env_names.RESERVED == set(spec.RESERVED_ENV)
    assert set(env_names.RESERVED_PREFIXES) == set(spec.RESERVED_ENV_PREFIXES)
    assert set(spec.BINDING_ENV_PREFIXES) <= set(env_names.RESERVED_PREFIXES)


@pytest.mark.parametrize("name", ["PORT", "HOME", "LD_PRELOAD", "PYTHONPATH", "NODE_X", "AZURE_CLIENT_ID", "DATABRICKS_HOST",
                                  "SECRETSERVER_TOKEN", "NOTES_DB_PASSWORD", "_X", "A1"])
def test_the_portal_refuses_exactly_what_the_builder_reserves(name):
    assert (env_names.refused(name) is not None) == (spec.reserved_reason(name) is not None)
