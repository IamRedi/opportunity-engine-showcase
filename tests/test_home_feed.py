"""OE-007D — hierarkia e faqes kryesore: Top Matches, porta e sugjerimeve,
qetësimi i status-strip-it, filtri ≥70. Prova në kufijtë ekzistues HTTP/UI.
"""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

import app
import profile_intelligence
import repository as repo

repo.init()


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def _ids_in_list(html: str, list_id: str) -> list[str]:
    """Vetëm <li data-id> brenda <ul id=list_id> — butoni i ruajjes ka
    data-id të njëjtë dhe duhet injoruar."""
    chunk = html.split(f'id="{list_id}"', 1)[1]
    chunk = chunk.split("</ul>", 1)[0]
    return [a["data-id"] for t, a in Elements(chunk).nodes
            if t == "li" and "data-id" in a]


def _seed(url: str, title: str, kind: str = "job", score: int | None = None,
          description: str = "python react"):
    status, oid = repo.upsert_opportunity_full({
        "source": "home-feed", "url": url, "title": title, "company": f"C-{url[-3:]}",
        "location": "Berlin", "remote": False, "description": description,
        "tags": [], "kind": kind})
    assert status == "new", status
    if score is not None:
        with repo.db() as con:
            con.execute("UPDATE opportunities SET rule_score=?, final_score=? WHERE id=?",
                        (score, score, oid))
            got = con.execute("SELECT rule_score FROM opportunities WHERE id=?",
                              (oid,)).fetchone()[0]
        assert got == score
    return str(oid)


@pytest.fixture()
def client():
    return TestClient(app.app)


def _eligible_top_ids(limit=4, kind="job"):
    """Pritshmëria e pavarur: 4 më të mirat e mënyrës 'jobs' sipas renditjes
    së repository, me rregullin e përjashtimit të seksionit Top (pa
    arkivuar/refuzuar/të përjashtuar/parapamje web)."""
    out = []
    for j in repo.list_opportunities(kind=kind, limit=10000):
        if j.get("status") in ("archived", "rejected") or j.get("excluded"):
            continue
        if (j.get("kd") or {}).get("verification_status") == "search_preview":
            continue
        out.append(str(j["id"]))
        if len(out) == limit:
            break
    return out


def test_top_matches_respect_ranking_and_no_duplication(client):
    ids = {s: _seed(f"https://home-feed.example/rank-{s}", f"Rank role {s}", score=s)
           for s in (90, 80, 70, 60, 50, 40)}
    html = client.get("/").text
    top = _ids_in_list(html, "top-matches")
    feed = _ids_in_list(html, "opportunity-list")
    assert top == _eligible_top_ids()
    assert not set(top) & set(feed), "karta e njëjtë në dy seksione"
    # çdo rresht i implantuar duket ekzakt një herë nëpër seksione
    all_ids = top + feed
    for i in ids.values():
        assert all_ids.count(i) == 1, f"id {i} dublikohet ose mungon"
    assert html.index('id="top-matches"') < html.index('id="opportunity-list"')


def test_top_matches_exclude_archived_excluded_and_previews(client):
    # Pikët dominuese mbi çdo rresht të mbetur nga testet e mëparshme, që
    # pritshmëria e seksionit të mbetet e përcaktuar vetëm nga këto rreshta.
    archived = _seed("https://home-feed.example/arch", "Archived role", score=100)
    excluded = _seed("https://home-feed.example/excl", "Excluded role", score=99)
    preview = _seed("https://home-feed.example/prev", "Preview role", score=98)
    legit = [_seed(f"https://home-feed.example/legit-{s}", f"Legit role {s}", score=s)
             for s in (97, 96, 95, 94, 93)]
    with repo.db() as con:
        con.execute("UPDATE opportunities SET status='archived' WHERE id=?", (archived,))
        con.execute("UPDATE opportunities SET excluded=1 WHERE id=?", (excluded,))
        con.execute("UPDATE opportunities SET kind_data='{\"verification_status\":\"search_preview\"}' WHERE id=?", (preview,))
    html = client.get("/").text
    top = _ids_in_list(html, "top-matches")
    feed = _ids_in_list(html, "opportunity-list")
    assert archived not in top and preview not in top and excluded not in top
    # pritshmëria nga renditja reale me rregullin e seksionit Top — në suite
    # ekzistojnë rreshta të tjerë me pikë të larta, prandaj s'kërkojmë
    # dominim të seancave tona, vetëm zbatimin e rregullit.
    assert top == _eligible_top_ids()
    # të përshtatshmit e implantuar duhet të ndodhin diku në faqe
    assert set(legit) & (set(top) | set(feed)) or not legit
    # feed-i i plotë i mban (s'i fshehim — vetëm s'i ngritmë në krye)
    assert archived in feed and preview in feed and excluded in feed


def test_top_matches_respect_selected_mode(client):
    job_ids = [_seed(f"https://home-feed.example/mode-job-{s}", f"Job role {s}",
                     kind="job", score=s) for s in (85, 75, 65, 55, 45)]
    fr_ids = [_seed(f"https://home-feed.example/mode-fr-{s}", f"Freelance role {s}",
                    kind="freelance", score=s + 20) for s in (85, 75, 65, 55, 45)]
    html = client.get("/", params={"mode": "freelance"}).text
    top = _ids_in_list(html, "top-matches")
    feed = _ids_in_list(html, "opportunity-list")
    listed = set(top) | set(feed)
    assert listed and not (listed & set(job_ids)), "rrjedhje job në freelance"
    assert set(fr_ids) <= listed


def test_adjacent_section_hidden_when_empty(client):
    html = client.get("/").text
    assert 'id="adjacent-opportunities"' in html
    tag = next(a for t, a in Elements(html).nodes
               if t == "details" and a.get("id") == "adjacent-opportunities")
    assert "hidden" in tag
    # API pa profil të analizuar (stale) → zero kandidatë → UI e mban të fshehur
    data = client.get("/api/profile/adjacent-opportunities", params={"mode": "jobs"}).json()
    assert data["items"] == []


def test_adjacent_relevance_gate_and_mode_leakage():
    rows = [
        {"id": 1, "kind": "job", "final_score": 30},    # nën prag → jashtë
        {"id": 2, "kind": "job", "final_score": 60},    # job brenda jobs
        {"id": 3, "kind": "freelance", "final_score": 70},  # kontradikon jobs
        {"id": 4, "kind": "job", "final_score": 55},    # job me lentë freelance
    ]
    lenses = {4: ["freelance"]}
    allowed = profile_intelligence.ADJACENT_MODE_KINDS["jobs"]
    kept = [r["id"] for r in rows
            if r["final_score"] >= profile_intelligence.ADJACENT_MIN_SCORE
            and profile_intelligence.adjacent_kind_allowed(r["kind"], lenses.get(r["id"], []), allowed)]
    assert kept == [2, 4]
    # në mode freelance: job pa lentë s'kalon, freelance po
    allowed_fr = profile_intelligence.ADJACENT_MODE_KINDS["freelance"]
    kept_fr = [r["id"] for r in rows
               if r["final_score"] >= profile_intelligence.ADJACENT_MIN_SCORE
               and profile_intelligence.adjacent_kind_allowed(r["kind"], lenses.get(r["id"], []), allowed_fr)]
    assert 3 in kept_fr and 2 not in kept_fr and 4 in kept_fr
    # mode 'all' — çdo lloj kalon
    assert profile_intelligence.adjacent_kind_allowed("local_job", [], None) is True


def test_min_score_seventy_filter_works(client):
    seeded = {s: _seed(f"https://home-feed.example/mins-{s}", f"Threshold role {s}", score=s)
              for s in (85, 72, 70, 69, 45, 20)}
    html = client.get("/", params={"mins": 70}).text
    shown = set(_ids_in_list(html, "top-matches")) | set(_ids_in_list(html, "opportunity-list"))
    assert shown
    for s in (85, 72, 70):
        assert seeded[s] in shown, f"{s}% duhet të duket me filtri ≥70"
    for s in (69, 45, 20):
        assert seeded[s] not in shown, f"{s}% s'duhet të duket me filtri ≥70"


def test_status_strip_quiets_redundant_and_empty_counts(client):
    first = _seed("https://home-feed.example/strip-1", "Strip role one", score=80)
    second = _seed("https://home-feed.example/strip-2", "Strip role two", score=60)
    # Gjendje e kontrolluar: çdo rresht job → new, pra të rejat == të gjitha
    # → chip-i "Të reja" s'shton informacion dhe duhet të qetësohet.
    with repo.db() as con:
        con.execute("UPDATE opportunities SET status='new' WHERE kind='job'")
    html = client.get("/").text
    strip = html.split('class="statusstrip"', 1)[1].split("</nav>", 1)[0]
    assert 'data-status="new"' not in strip, "të rejat == të gjitha → chip i tepërt"
    assert 'data-status="saved"' not in strip, "numërues 0 → fsheh"
    # status i përzier → "Të reja" rikthehet; zero-count mbeten të fshehur
    with repo.db() as con:
        con.execute("UPDATE opportunities SET status='saved' WHERE id=?", (second,))
    html = client.get("/").text
    strip = html.split('class="statusstrip"', 1)[1].split("</nav>", 1)[0]
    assert 'data-status="new"' in strip and 'data-status="saved"' in strip
    assert 'data-status="applied"' not in strip


def test_drawer_and_feed_rows_unchanged_contract(client):
    oid = _seed("https://home-feed.example/drawer-1", "Drawer contract role", score=77)
    html = client.get("/").text
    rows = [a for t, a in Elements(html).nodes if t == "li" and a.get("data-id") == oid]
    assert len(rows) == 1, "s'i dyfishohet rreshti asnjë mundësie"
    panel = client.get(f"/api/jobs/{oid}/panel")
    assert panel.status_code == 200 and "Përse përputhet" in panel.text
