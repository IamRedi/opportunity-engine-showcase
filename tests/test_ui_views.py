"""UI regressions at the existing HTTP/repository boundaries.

Browser interaction and responsive acceptance are documented separately.
"""
from html.parser import HTMLParser
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

import app
import applications
import classifier
import repository as repo


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def find(self, tag=None, **attrs):
        return [a for t, a in self.nodes
                if (tag is None or tag == t)
                and all(a.get(k) == v for k, v in attrs.items())]


@pytest.fixture
def client():
    return TestClient(app.app)


@pytest.mark.parametrize('remote', ['remote', 'onsite', ''])
def test_native_filter_form_accepts_empty_numeric_controls(client, monkeypatch, remote):
    captured = {}
    def listing(**kwargs):
        captured.update(kwargs)
        return []
    monkeypatch.setattr(repo, 'list_opportunities', listing)
    response = client.get('/', params={
        'mode': 'jobs', 'q': '', 'status': '', 'remote': remote,
        'mins': '', 'days': '', 'source': '', 'loc': '', 'salary': '', 'sort': 'match',
    })
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert captured['remote'] == remote
    assert (captured['mins'], captured['days'], captured['salary']) == (0, 0, 0)
    if remote:
        assert 'selected' in Elements(response.text).find('option', value=remote)[0]


def test_numeric_filters_keep_values_and_normalize_whitespace(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(repo, 'list_opportunities', lambda **kw: captured.update(kw) or [])
    response = client.get('/', params={'remote': 'remote', 'mins': '70', 'days': '7', 'salary': '50000'})
    assert response.status_code == 200
    assert (captured['mins'], captured['days'], captured['salary']) == (70, 7, 50000)
    assert client.get('/', params={'mins': ' ', 'days': '\t', 'salary': ' '}).status_code == 200
    assert (captured['mins'], captured['days'], captured['salary']) == (0, 0, 0)


@pytest.mark.parametrize('field', ['mins', 'days', 'salary'])
def test_numeric_filters_still_reject_non_numbers(client, field):
    assert client.get('/', params={field: 'invalid'}).status_code == 422


@pytest.fixture
def opportunity():
    _, oid = repo.upsert_opportunity_full({
        "url": "https://ui-regression.example/escaped-job",
        "title": 'UI <img src=x onerror="alert(1)"> & developer',
        "company": "A long company " * 12,
        "kind": "job", "source": "ui-regression",
        "description": "python sql git", "tags": [],
    })
    with repo.db() as con:
        con.execute("UPDATE opportunities SET rule_score=95 WHERE id=?", (oid,))
    yield oid
    repo.delete_opportunity(oid)


def test_top_match_has_one_row_and_untrusted_title_is_text(client, opportunity):
    response = client.get("/", params={"source": "ui-regression"})
    assert response.status_code == 200
    dom = Elements(response.text)
    assert len(dom.find("li", **{"data-id": str(opportunity)})) == 1
    assert not dom.find("img", src="x")
    assert "&lt;img" in response.text
    assert len(dom.find("a", **{"data-drawer": str(opportunity)})) == 1
    assert dom.find("aside", id="drawer")[0].get("hidden") is None
    assert "hidden" in dom.find("aside", id="drawer")[0]


@pytest.mark.parametrize("kind", ["job", "freelance", "product_lead", "local_job"])
def test_manual_form_keeps_one_input_per_field_and_all_modes(client, kind):
    response = client.get("/jobs/add", params={"kind": kind})
    assert response.status_code == 200
    dom = Elements(response.text)
    radios = dom.find("input", type="radio", name="kind")
    assert {r["value"] for r in radios} == {"job", "freelance", "product_lead", "local_job"}
    assert [r["value"] for r in radios if "checked" in r] == [kind]
    for name in ("company", "salary_text", "postcode", "hourly_rate", "schedule"):
        assert len(dom.find("input", name=name)) == 1
    assert len(dom.find("div", id="local-extra")) == 1
    assert dom.find("option", value="gardening")


def test_search_profile_modes_and_arbitrary_match_threshold(client):
    for kind, mode in [("job", "jobs"), ("freelance", "freelance"),
                       ("product_lead", "products"), ("local_job", "local")]:
        pid = client.post("/api/profiles", json={
            "name": f"UI profile {kind}", "kinds": [kind], "min_match": 63,
        }).json()["id"]
        try:
            profile = repo.get_search_profile(pid)
            query = parse_qs(app._profile_apply_query(profile))
            assert query["mode"] == [mode]
            assert query["mins"] == ["63"]
            panel = client.get(f"/api/profiles/{pid}/panel")
            threshold = Elements(panel.text).find("input", name="min_match")[0]
            assert threshold["value"] == "63"
        finally:
            repo.delete_search_profile(pid)


def test_workspace_lens_counts_match_membership_without_changing_default():
    before = repo.status_counts("product_lead", include_lenses=True).get("new", 0)
    primary_before = repo.status_counts("product_lead").get("new", 0)
    _, oid = repo.upsert_opportunity_full({
        "url": "https://ui-regression.example/lens", "title": "UI lens count fixture",
        "kind": "freelance", "company": "UI lens", "description": "", "tags": [],
    })
    try:
        classifier.attach_lenses(oid, {"kind": "freelance", "description": "booking website"})
        assert repo.status_counts("product_lead").get("new", 0) == primary_before
        assert repo.status_counts("product_lead", include_lenses=True).get("new", 0) == before + 1
        assert oid in {j["id"] for j in repo.list_opportunities(kind="product_lead", limit=10000)}
    finally:
        repo.delete_opportunity(oid)


def test_html_cv_upload_redirects_to_review_and_preserves_validation(client, monkeypatch, tmp_path):
    import storage
    monkeypatch.setattr(storage, 'get_storage', lambda: storage.LocalStorage(tmp_path))
    response = client.post('/profile/cv', files={
        'file': ('ui-demo.txt', b'Skills: Python, SQL', 'text/plain'),
    }, follow_redirects=False)
    try:
        assert response.status_code == 303
        assert response.headers['location'] == '/profile#cv'
        assert client.get('/api/profile/cv').json()['parse_status'] == 'pending'
        invalid = client.post('/profile/cv', files={
            'file': ('invalid.exe', b'bad', 'application/octet-stream'),
        }, follow_redirects=False)
        assert invalid.status_code == 400
        assert client.get('/api/profile/cv').json()['filename'] == 'ui-demo.txt'
    finally:
        client.delete('/api/profile/cv')


def test_submitted_application_is_readonly_and_revision_endpoint_retains_text(client, opportunity):
    app_id = applications.create_draft(repo.get_opportunity(opportunity))
    text = "UI acceptance edit: Python and SQL."
    assert client.post(f"/api/applications/{app_id}/edit", json={"content": text}).status_code == 200
    assert client.post(f"/api/applications/{app_id}/approve").status_code == 200
    assert client.post(f"/api/applications/{app_id}/submit").status_code == 200
    response = client.get(f"/api/jobs/{opportunity}/panel")
    dom = Elements(response.text)
    content = dom.find("textarea", **{"aria-label": "Përmbajtja e aplikimit"})[0]
    assert "readonly" in content
    assert 'Version final i aprovuar' in response.text
    revisions = client.get(f"/api/applications/{app_id}").json()["versions"]
    assert [v["kind"] for v in revisions] == ["generated", "edited", "final"]
    assert revisions[-1]["content"] == revisions[-2]["content"] == text


def test_primary_product_lead_displays_fit_even_without_lens(client, monkeypatch):
    _, oid = repo.upsert_opportunity_full({
        "url": "https://ui-regression.example/product-primary", "title": "Booking website",
        "kind": "product_lead", "description": "Online booking", "tags": [],
    })
    monkeypatch.setattr("product_fit._catalog", lambda **kwargs: [])
    try:
        response = client.get(f"/api/jobs/{oid}/panel")
        assert response.status_code == 200
        assert "Shto një produkt aktiv" in response.text
        assert "Plotëso katalogun" in response.text
    finally:
        repo.delete_opportunity(oid)
