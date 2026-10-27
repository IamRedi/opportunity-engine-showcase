"""Konektorët: kontrata, izolimi i gabimeve, orkestrimi me search_runs (spec §11, §30-F2)."""
import httpx

import connectors
import repository as repo
import search_apis


def _fixture_item(n, url=None):
    return {
        "source": "jobicy", "external_id": f"t{n}",
        "url": url or f"https://conn-test.example/{n}",
        "title": f"Connector Item {n}",
        "company": f"T{n}",
        "location": "Remote (remote)", "remote": True,
        "salary_text": "", "description": "d", "tags": [], "posted_at": "",
    }


def test_registry_has_four_sources_with_metadata():
    assert {"arbeitnow", "remotive", "jobicy", "adzuna"} <= set(connectors.REGISTRY)
    for c in connectors.REGISTRY.values():
        assert c.id and c.name and c.note
        h = c.health()
        assert set(h) >= {"id", "name", "ok", "detail", "requires_key"}


def test_adzuna_unconfigured_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(repo, "get_setting", lambda k, d="": "")
    res = connectors.REGISTRY["adzuna"].search(connectors.SearchQuery())
    assert "Adzuna" in res.error and res.items == []


def test_broken_connector_is_isolated(monkeypatch):
    def boom(**kw):
        raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(search_apis, "arbeitnow", boom)
    # edhe pse hedh exception brenda, run_source_search kthen gabim pa e lëshuar
    res = connectors.run_source_search("arbeitnow")
    assert res["ok"] is False
    assert "papërdorshëm" in res["error"]
    # run-i u regjistrua me status error
    runs = repo.recent_search_runs(limit=5)
    r = next(x for x in runs if x["source"] == "arbeitnow" and x["id"] == res["run_id"])
    assert r["status"] == "error" and r["finished_at"]


def test_unknown_source_records_error_run():
    res = connectors.run_source_search("nosuchsource")
    assert res["ok"] is False and res["error"] == "burim i panjohur"


def test_run_source_search_counts_new_vs_dup(monkeypatch):
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [_fixture_item(101), _fixture_item(102)])
    r1 = connectors.run_source_search("jobicy")
    assert r1["ok"] and r1["new"] == 2 and r1["dup"] == 0 and r1["discovered"] == 2
    r2 = connectors.run_source_search("jobicy")
    assert r2["ok"] and r2["new"] == 0 and r2["dup"] == 2
    # metadatën e run-it
    runs = {x["id"]: x for x in repo.recent_search_runs(limit=10)}
    assert runs[r1["run_id"]]["new_count"] == 2
    assert runs[r2["run_id"]]["duplicate_count"] == 2
    assert runs[r1["run_id"]]["finished_at"] and runs[r1["run_id"]]["started_at"]


def test_partial_failure_does_not_block_other_sources(monkeypatch):
    """Një burim i thyer s'bën dështuar kërkimin e profilit (DoD F2)."""
    def boom(**kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(search_apis, "arbeitnow", boom)
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [_fixture_item(201)])

    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["arbeitnow", "jobicy"]'})
    prof = repo.get_search_profile(prof["id"])

    results = connectors.run_profile_search(prof)
    by_src = {r["source"]: r for r in results}
    assert by_src["arbeitnow"]["ok"] is False
    assert by_src["jobicy"]["ok"] is True and by_src["jobicy"]["new"] >= 1
    # runs me profile_id
    run_ids = [r["run_id"] for r in results]
    runs = {x["id"]: x for x in repo.recent_search_runs(limit=10)}
    for rid in run_ids:
        assert runs[rid]["profile_id"] == prof["id"]


def test_profile_query_maps_keywords_location(monkeypatch):
    seen = {}
    def fake_remotive(**kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(search_apis, "remotive", fake_remotive)
    connectors.REGISTRY["remotive"].search(
        connectors.SearchQuery(what="python backend", where="Berlin", salary_min=50000))
    # remotive s'përdor parametrat (sjellje e pandryshuar) — por s'bën gabim
    connectors.run_source_search("remotive", connectors.SearchQuery(what="x"))
    runs = [r for r in repo.recent_search_runs(limit=5) if r["source"] == "remotive"]
    assert runs and runs[0]["status"] == "ok"
