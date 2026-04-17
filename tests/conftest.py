"""Pytest fixtures — temp SQLite per test, fresh Settings singleton."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_arb_env(monkeypatch, tmp_path: Path) -> Path:
    """Point every test at a throwaway DB + kill file."""
    db_path = tmp_path / "arb.db"
    kill_path = tmp_path / "KILL"
    monkeypatch.setenv("ARB_DB_PATH", str(db_path))
    monkeypatch.setenv("ARB_KILL_SWITCH_FILE", str(kill_path))
    monkeypatch.setenv("ARB_MODE", "paper")

    # Refresh the settings singleton and rebind any module-level imports.
    import arbitrage.config as cfg
    cfg.settings = cfg.Settings()
    import arbitrage.db as dbmod
    dbmod.settings = cfg.settings
    import arbitrage.clients.polymarket_rest as rest
    rest.settings = cfg.settings
    import arbitrage.engine.paper_fills as pf
    pf.settings = cfg.settings
    import arbitrage.web.app as web
    web.settings = cfg.settings
    return tmp_path


@pytest.fixture
async def db(_tmp_arb_env):
    from arbitrage.db import init_db
    await init_db()
    return _tmp_arb_env
