"""FastAPI dashboard endpoint smoke tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arbitrage.web.app import create_app


@pytest.fixture
def client(_tmp_arb_env):
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_index_renders(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "arbitrage" in r.text
    assert "paper" in r.text


def test_pnl_fragment_renders(client) -> None:
    r = client.get("/fragments/pnl")
    assert r.status_code == 200
    assert "paper pnl" in r.text


def test_opportunities_fragment_empty_state(client) -> None:
    r = client.get("/fragments/opportunities")
    assert r.status_code == 200
    assert "no opportunities yet" in r.text


def test_baskets_fragment_empty_state(client) -> None:
    r = client.get("/fragments/baskets")
    assert r.status_code == 200
    assert "no baskets yet" in r.text


def test_kill_toggle_round_trip(client, _tmp_arb_env) -> None:
    import arbitrage.config as cfg

    assert not cfg.settings.kill_switch_file.exists()
    r = client.post("/kill")
    assert r.status_code == 200
    assert "KILL SWITCH ACTIVE" in r.text
    assert cfg.settings.kill_switch_file.exists()
    r = client.post("/unkill")
    assert r.status_code == 200
    assert "KILL SWITCH ACTIVE" not in r.text
    assert not cfg.settings.kill_switch_file.exists()
