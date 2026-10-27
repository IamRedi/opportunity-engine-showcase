# Opportunity Engine

**Find a relevant opportunity. Understand the match. Decide your next step.**

A personal workspace for jobs and freelance projects: reviewed profile facts become explainable matches and editable application drafts. Nothing is automatically sent to an employer.

[![CI](https://github.com/IamRedi/opportunity-engine-showcase/actions/workflows/ci.yml/badge.svg)](https://github.com/IamRedi/opportunity-engine-showcase/actions/workflows/ci.yml)

**Python 3.14 · FastAPI · SQLite / optional libSQL · SQ / EN / DE**

![Real local application with fictional listings and calculated matching scores](docs/assets/screenshots/workspace.webp)

**[Deutsch](README.md)** · **[Run locally](#run-locally)** · **[Verification record](EVIDENCE.md)** · **[Architecture](docs/ARCHITECTURE.md)**


> **Evidence and limitations:** Developed prototype with an offline demonstration using fictional data only. Public sign-up, production recruitment services and automatic job-application submission are not part of this release; documented tests do not use real applicant data. [Verification record](EVIDENCE.md).

## The problem

A list of vacancies, a CV and scattered notes do not explain which opportunity fits, what is still unknown, or what to do next.

- **A reviewed profile:** CV import, manual details, skills, experience and projects. Suggestions do not silently become confirmed qualifications.
- **Explainable relevance:** skills, role, location, work arrangement, salary, languages and freshness. Missing information stays unknown. A match score is not a probability of being hired.
- **An application you control:** save a listing, edit a draft, compare versions and approve a final version. Actual submission stays with the person.
- **Separate workflows:** job applications and freelance leads have their own views and status transitions. Product-lead and local-work modules are also present in the code.

## A short tour

Profile → opportunities → matching factors → draft → personal approval → CV preview.

[![Preview of the walkthrough, made from real application screenshots](docs/assets/video/walkthrough-preview.webp)](docs/assets/video/walkthrough-en.mp4)

**[English video · MP4](docs/assets/video/walkthrough-en.mp4)** · **[German video · MP4](docs/assets/video/walkthrough-de.mp4)** · **[Transcript](docs/assets/video/transcript.md)**

This silent, captioned tour is edited from real screenshots of the local application. All people and listings are fictional. No live AI, job collection or application submission is shown. [Media provenance](docs/MEDIA.md).

## Inside the application

### Why does this role match?

![Matching factors distinguish positive evidence, a partial language match and unknown seniority](docs/assets/screenshots/matching.webp)

### From profile facts to an editable draft

| Reviewed profile | Editable application |
|---|---|
| ![Alex Beispiel's fictional profile with skills, languages and work experience](docs/assets/screenshots/profile.webp) | ![Authored English demonstration draft, explicitly without live AI](docs/assets/screenshots/draft.webp) |

<details>
<summary>See approval, CV preview and freelance</summary>

![An approved local final version; the separate mark-as-sent action was not used](docs/assets/screenshots/approved.webp)

| CV from profile facts | Freelance project |
|---|---|
| ![CV preview based on fictional structured profile facts](docs/assets/screenshots/resume.webp) | ![A fictional API project with its own freelance status](docs/assets/screenshots/freelance.webp) |

</details>

## Run locally

**Requires Python 3.14. No API key is needed.** Installation downloads Python packages; the running demo blocks external connections.

Linux / macOS:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints/python314-linux.txt
python scripts/serve_demo.py
```

<details>
<summary>Windows / PowerShell</summary>

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints/python314-linux.txt
.\.venv\Scripts\python.exe scripts/serve_demo.py
```

</details>

Open **http://127.0.0.1:8765/?lang=en** or **http://127.0.0.1:8765/?lang=de**.

The launcher creates a new `.demo/` database with Alex Beispiel and eight fictional listings. It never reads an existing database or personal `.env` file. Demo edits survive restart. To reset, stop the server and remove only the generated `.demo/` directory.

Docker alternative:

```bash
docker compose up --build
```

The container is non-root and publishes its port only to loopback. This evaluation environment is not a publicly hosted service.

## Engineering and quality

- **FastAPI + Jinja + JavaScript:** server-rendered views, progressive interactions and a details drawer.
- **SQLite:** versioned migrations, normalized listings and deduplication; an optional libSQL adapter is independently testable.
- **Explicit boundaries:** profile facts, search signals, matching and application drafts remain distinct. Cloud modules include actor/tenant scoping, a usage ledger and durable background work.
- **CI:** the complete Python regression suite, local libSQL tests, offline demo smoke, tracked-publication inspection, Gitleaks and container smoke. See [verification scope and results](EVIDENCE.md).

The full test suite also needs **Node.js 22** for frontend contract checks.

```bash
python -m pip install -r requirements-ci.txt -c constraints/python314-linux.txt
python -m pytest -q
python scripts/serve_demo.py --check
python scripts/check_public_snapshot.py
```

The full cloud-adapter suite is checked on Linux. The normal SQLite demo does not need libSQL or the Vercel SDKs. Neither tests nor the demo use real provider accounts.

## Product boundaries

This is a developed prototype with reviewable core workflows, not a finished recruiting SaaS. Search and AI adapters are included but disabled in the showcase. Public signup, billing, automatic application submission and an openly accessible live pilot are outside this presentation. AI language quality and additional operational acceptance remain separate work. [Scope and known limits](docs/PUBLIC_SCOPE.md) · [Security](SECURITY.md).

## Author and use

Built by **Redion Cekrezi / RediON**. Product decisions and acceptance remain human; AI tools support development and verification.

You may read, clone and run the code locally for evaluation. Production, commercial use and redistribution require written permission. This is **source-available**, not Open Source. [License](LICENSE) · [Contact](https://github.com/IamRedi).
