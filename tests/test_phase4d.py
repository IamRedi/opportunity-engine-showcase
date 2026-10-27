"""Faza 4D: CV extraction + parsing + merge i sigurt + ProfileContext."""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cv_merge
import cv_parser
import matching
import profile_context
import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def _make_pdf_bytes(text: str) -> bytes:
    """PDF minimal me tekst ekstraktues (pypdf e lexon)."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, TextStringObject, DictionaryObject
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # vendos përmbajtje teksti direkt në stream-in e faqes
    page = writer.pages[0]
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    stream = io.BytesIO()
    stream.write(content.encode("latin-1", errors="replace"))
    data = stream.getvalue()
    from pypdf.generic import DecodedStreamObject
    content_stream = DecodedStreamObject()
    content_stream.set_data(data)
    page[NameObject("/Contents")] = content_stream
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(text: str) -> bytes:
    import docx
    doc = docx.Document()
    for para in text.split("\n"):
        if para.strip():
            doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


CV_TXT = """Example Dëshmi
Software Developer

Skills: Python, SQL, Docker, Kubernetes, Git
Languages: Gjermanisht – B2, Anglisht – C1, Shqip – native

Experience:
Backend Developer @ TechCorp | 2021–tani
Intern @ StartupY | 2020

Education: BSC Informatik | Universität Duisburg
Certifications: AWS Certified Developer
Portfolio: https://example.invalid/portfolio

Contact: pilot@example.invalid, +49 170 1234567
"""


# ------------------------------------------------------- ekstraktimi

def test_txt_extraction(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text(CV_TXT, encoding="utf-8")
    res = cv_parser.extract_text(p)
    assert res["status"] == "ok" and "Dëshmi" in res["text"]
    assert res["method"] == "utf8-text"


def test_md_extraction(tmp_path):
    p = tmp_path / "cv.md"
    p.write_text("# CV\n\nSkills: Python\n", encoding="utf-8")
    res = cv_parser.extract_text(p)
    assert res["status"] == "ok" and "Python" in res["text"]


def test_pdf_extraction(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(_make_pdf_bytes("Python Developer CV Test"))
    res = cv_parser.extract_text(p)
    assert res["status"] == "ok" and res["method"] == "pypdf"
    assert "page 1" in res["text"]  # faqja u lexua (teksti varet nga PDF)


def test_docx_extraction(tmp_path):
    p = tmp_path / "cv.docx"
    p.write_bytes(_make_docx_bytes("Backend Developer\nSkills: Python, SQL"))
    res = cv_parser.extract_text(p)
    assert res["status"] == "ok" and "Python" in res["text"]
    assert res["method"] == "python-docx"


def test_unsupported_format_fails(tmp_path):
    p = tmp_path / "cv.exe"
    p.write_bytes(b"MZ fake")
    res = cv_parser.extract_text(p)
    assert res["status"] == "unsupported"


def test_corrupt_docx_fails_soft(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not a docx")
    res = cv_parser.extract_text(p)
    assert res["status"] == "failed" and res["error"]


# ------------------------------------------------- parse + dëshmitë

def test_parse_deterministic_and_evidence():
    s1 = cv_parser.parse_suggestions(CV_TXT)
    s2 = cv_parser.parse_suggestions(CV_TXT)
    assert s1 == s2
    skills = [x for x in s1 if x["section"] == "skills"]
    assert any(x["normalized_value"] == "python" for x in skills)
    assert all(x["evidence"] for x in s1)
    langs = [x for x in s1 if x["section"] == "languages"]
    assert any("gjermanisht" in x["normalized_value"] for x in langs)
    # dëshmia i referohet rreshtit burim
    assert any("Skills:" in x["evidence"] or "Gjermanisht" in x["evidence"]
               for x in langs)


def test_no_invented_skills():
    sugg = cv_parser.parse_suggestions(CV_TXT)
    names = {x["normalized_value"] for x in sugg if x["section"] == "skills"}
    assert "quantum computing" not in names
    assert "brain surgery" not in names


# ------------------------------------------------------- upload → parse

def test_upload_parse_suggestions_flow(client, tmp_path):
    # upload përmes API (multipart) — TXT për parse deterministik të plotë
    r = client.post("/api/profile/cv", files={"file": ("cv4d.txt", CV_TXT.encode(), "text/plain")})
    assert r.status_code == 200
    cv = client.get("/api/profile/cv").json()
    assert cv["parse_status"] == "pending", cv
    assert "id" in cv, cv
    cv_id = cv["id"]
    # kolonat e reja të ekstraktimit
    with repo.db() as con:
        row = dict(con.execute("SELECT * FROM profile_cv WHERE id=?", (cv_id,)).fetchone())
    assert row["extract_status"] == "pending"  # parse s'është bërë ende

    # parse → sugjerime pending
    res = client.post(f"/api/cv/{cv_id}/parse")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    counts = body["suggestion_counts"]
    assert counts.get("pending", 0) >= 1
    # parser_version i regjistruar
    with repo.db() as con:
        row = dict(con.execute("SELECT * FROM profile_cv WHERE id=?", (cv_id,)).fetchone())
    assert row["parser_version"] == cv_parser.PARSER_VERSION
    assert row["extract_status"] == "ok"

    # reparsim → pa dyfishim sugjerimesh
    res2 = client.post(f"/api/cv/{cv_id}/parse")
    assert res2.json()["created"] == 0
    client.delete("/api/profile/cv")


def test_unsupported_upload_fails(client, tmp_path):
    r = client.post("/api/profile/cv", files={"file": ("x.exe", b"MZ", "application/x-exe")})
    assert r.status_code == 400


# ----------------------------------------- pending NUK prek profilin

def test_pending_suggestions_do_not_modify_profile(client, tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text(CV_TXT, encoding="utf-8")
    cv_id = repo.save_cv("cv4d.txt", str(p), "text/plain", p.stat().st_size, "digest4d")
    before_sections = {k: v for k, v in repo.all_profile_sections().items()}
    before_profile = repo.get_profile()
    parse_res = cv_merge.parse_cv(cv_id)
    assert parse_res["ok"] is True
    pending = repo.cv_suggestions(cv_id, "pending")
    assert len(pending) >= 3
    # profili NUK u ndryshua nga parsing
    assert {k: v for k, v in repo.all_profile_sections().items()} == before_sections
    assert repo.get_profile()["skills"] == before_profile["skills"]
    # context s'përmban fakte cv pending
    ctx = profile_context.build_context()
    cv_facts = [f for f in ctx["facts"] if str(f["source"]).startswith("cv:")]
    assert cv_facts == []


# --------------------------------------------- accept / reject / idempotent

def test_accept_merges_with_provenance_and_idempotent(client, tmp_path):
    repo.set_profile_section('skills',[])
    p = tmp_path / "cv.txt"
    p.write_text(CV_TXT, encoding="utf-8")
    cv_id = repo.save_cv("cv.txt", str(p), "text/plain", 100, "d4a")
    cv_merge.parse_cv(cv_id)
    pending = repo.cv_suggestions(cv_id, "pending")
    skill_sugg = next(s for s in pending if s["section"] == "skills"
                      and s["normalized_value"] == "python")

    # profile skills bosh → accept shton
    before_skills, _ = repo.get_profile_section("skills")
    res = cv_merge.accept_suggestion(skill_sugg["id"])
    assert res["ok"] is True
    after_skills, _ = repo.get_profile_section("skills")
    assert len(after_skills) == len(before_skills) + 1
    added = [e for e in after_skills if _norm(e.get("name")) == "python"
             and e.get("source", "").startswith("cv:")]
    assert added  # provenanca cv:<cv_id>:<sid>
    # idempotent: aprovo sërish → pa dyfish
    res2 = cv_merge.accept_suggestion(skill_sugg["id"])
    assert res2["merged"] is False
    after2, _ = repo.get_profile_section("skills")
    assert len([e for e in after2 if _norm(e.get("name")) == "python"]) == 1

    # context tani përmban faktin me provenancë cv:
    ctx = profile_context.build_context()
    cv_facts = [f for f in ctx["facts"] if str(f["source"]).startswith("cv:")]
    assert cv_facts
    # gjeneratori 4B e përdor si dëshmi (fact_values)
    vals = profile_context.fact_values(ctx)
    assert "python" in vals


def test_reject_leaves_profile_unchanged(client, tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text(CV_TXT, encoding="utf-8")
    cv_id = repo.save_cv("cv.txt", str(p), "text/plain", 100, "d4b")
    cv_merge.parse_cv(cv_id)
    pending = repo.cv_suggestions(cv_id, "pending")
    target = next(s for s in pending if s["section"] == "skills"
                  and s["normalized_value"] == "docker")
    before_skills, _ = repo.get_profile_section("skills")
    res = cv_merge.reject_suggestion(target["id"])
    assert res["ok"] is True
    after_skills, _ = repo.get_profile_section("skills")
    assert before_skills == after_skills
    # statusi rejected mbetet i regjistruar (inaktiv)
    sugg = repo.get_cv_suggestion(target["id"])
    assert sugg["status"] == "rejected" and sugg["reviewed_at"]
    # ri-accept i refuzuarar → refuzuar mbetet (rejected stays inactive)
    r = cv_merge.accept_suggestion(target["id"])
    assert r.get("merged") is False
    after2, _ = repo.get_profile_section("skills")
    assert before_skills == after2


def _norm(v):
    import re
    return re.sub(r"\s+", " ", (v or "").lower()).strip()


def test_manual_data_not_overwritten(client, tmp_path):
    # profili tashmë ka 'python' manual → accept i sugjerimit identik → pa dyfish
    repo.save_profile({**repo.get_profile(), "skills": "python\nmanual-only-skill"})
    p = tmp_path / "cv.txt"
    p.write_text("Skills: Python, SQL\n", encoding="utf-8")
    cv_id = repo.save_cv("cv.txt", str(p), "text/plain", 50, "d4c")
    cv_merge.parse_cv(cv_id)
    pending = repo.cv_suggestions(cv_id, "pending")
    py = next(s for s in pending if s["normalized_value"] == "python")
    res = cv_merge.accept_suggestion(py["id"])
    assert res["ok"] is True
    skills, _ = repo.get_profile_section("skills")
    py_entries = [e for e in skills if _norm(e.get("name")) == "python"]
    assert len(py_entries) == 1                    # pa dyfish
    assert "manual-only-skill" in repo.get_profile()["skills"]  # manual i mbetur
    sql_sugg = next(s for s in repo.cv_suggestions(cv_id, "pending")
                    if s["normalized_value"] == "sql")
    cv_merge.accept_suggestion(sql_sugg["id"])
    skills, _ = repo.get_profile_section("skills")
    sql_entries = [e for e in skills if _norm(e.get("name")) == "sql"]
    assert sql_entries                             # i riu shtohet


# -------------------------------------------- gjeneratori me fakte CV

def test_generator_uses_accepted_cv_evidence(client, monkeypatch, tmp_path):
    # aftësi vetëm nga CV e aprovuar → garda e gjeneratorit e njeh
    p = tmp_path / "cv.txt"
    p.write_text("Skills: FastAPI, Kubernetes\n", encoding="utf-8")
    cv_id = repo.save_cv("cv.txt", str(p), "text/plain", 50, "d4e")
    cv_merge.parse_cv(cv_id)
    for s in repo.cv_suggestions(cv_id, "pending"):
        if s["normalized_value"] in ("fastapi", "kubernetes"):
            cv_merge.accept_suggestion(s["id"])
    ctx = profile_context.build_context()
    vals = profile_context.fact_values(ctx)
    assert "fastapi" in vals and "kubernetes" in vals
    # matching: score për punë FastAPI — aftësitë nga ProfileContext (CV e aprovuar)
    repo.upsert_opportunity({"source": "4d", "url": "https://4d.example/fast",
                             "title": "FastAPI Engineer", "company": "C",
                             "location": "B", "remote": False,
                             "description": "fastapi service", "tags": [],
                             "kind": "job"})
    with repo.db() as con:
        oid = con.execute(
            "SELECT id FROM opportunities WHERE source_url='https://4d.example/fast'"
        ).fetchone()[0]
    j = repo.get_opportunity(oid)
    profile = repo.get_profile() | {"skills": ["fastapi", "kubernetes"]}
    import matching
    r = matching.score(j, profile, repo.get_filters())
    sk = next(f for f in r.factors if f.id == "skills")
    assert sk.status == matching.MATCH  # evidence-backed CV fact → match


def test_no_score_or_scheduler_mutation(client, tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text("Skills: Rust\n", encoding="utf-8")
    cv_id = repo.save_cv("cv.txt", str(p), "text/plain", 50, "d4f")
    cv_merge.parse_cv(cv_id)
    for s in repo.cv_suggestions(cv_id, "pending")[:2]:
        cv_merge.accept_suggestion(s["id"])
    with repo.db() as con:
        before = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                  for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
    runs_before = repo.recent_search_runs(5)
    with repo.db() as con:
        after = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                 for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
    assert before == after
    assert repo.recent_search_runs(5) == runs_before
