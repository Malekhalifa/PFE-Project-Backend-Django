# QualityLab Backend (Django)

Last updated: April 2026

This backend powers authentication, CSV analysis, PostgreSQL AI validation, chat history, guidelines, and human-in-the-loop SQL fix execution.

## Architecture Snapshot

Current runtime stack:

- Django 5 + Django REST Framework
- PostgreSQL (main application database)
- Redis (Celery broker/result backend)
- Celery worker (async processing)
- Optional AgentOS playground for developer testing

Important:

- Runtime persistence is PostgreSQL-first.

## High-Level Architecture

```text
Browser (Angular SPA)
   -> Django API (auth + jobs + reports + chat + guidelines + fixes)
 -> PostgreSQL (users, jobs, analysis, chat, guidelines, audit)
 -> Redis (Celery broker/result backend)
 -> Celery workers (async CSV/PG pipelines)
    -> Agents pipeline (guidelines + validators + fix planner)
  -> Report artifacts (backend-django/reports)

Optional developer path:
AgentOS playground (port 7777) for interactive agent testing
```

Request lifecycle summary:

1. Frontend sends authenticated API calls to Django.
2. Django persists job state to PostgreSQL and dispatches long tasks to Celery.
3. Celery executes analysis pipelines and writes results/status updates.
4. Frontend polls status and renders results, chat, guidelines, and fix cards.

## Multi-Agent Validation System Architecture (6 Agents)

The backend agent architecture includes six distinct agent roles.

### Agent List (All 6)

1. Schema Integrity Validator
2. Column Validator
3. Row-Level Logic Validator
4. Cross-Row Validator
5. Data Fix Planner
6. Query Executor Agent

### How They Work Together

```text
Automated analysis pipeline (Celery):

1) Schema Integrity Validator
 -> structure constraints, PK/FK, nullability, schema quality

2) Column Validator
 -> column-level quality, semantics, null/cardinality/distribution checks

3) Row-Level Logic Validator
 -> per-row business-rule and format violations

4) Cross-Row Validator
 -> temporal, relational, and multi-row anomaly checks

5) Data Fix Planner
 -> synthesizes findings into human-reviewable SQL fix cards

Human-in-the-loop execution path:

6) Query Executor Agent
 -> executes approved fix-card SQL in one safe transaction
```

### Pipeline Notes & Hybrid Signal-Based Architecture

The pipeline uses a **Hybrid Signal-Based Architecture** to minimize LLM token costs and ensure deterministic fixes:
- **Signals over Reports:** Inter-agent communication is strictly limited to compact `ValidationSignal` objects containing math-based `metrics` and exact SQL `predicate` clauses.
- **Minimal Context Sampling:** Tools run aggregations rather than returning raw rows. When semantic clarity is required, agents append a maximum of 3 `context_samples` to a signal.
- **Execution Safety:** The Data Fix Planner inherently acts on upstream signal predicates, but strictly dry-runs them (`EXPLAIN` or read-only `COUNT`) to verify safety before proposing the final `UPDATE` or `DELETE` SQL.
- Query Executor Agent executes only user-approved queries from fix cards in a standalone phase.

## Project Structure

```text
backend-django/
├── config/       Django settings, root URLs, Celery app
├── users/        User model, auth views, cookie JWT backend
├── api/          API views, URL routes, ORM models, DB adapter
├── services/     Analysis and domain services
├── tasks/        Celery tasks (CSV + PostgreSQL workflows)
├── agents/       Multi-agent orchestration and query executor
├── uploads/      Uploaded CSV files
├── reports/      Generated report artifacts
├── manage.py
└── requirements.txt
```

## Main Data Models

Defined in api/models.py:

- Job
- AnalysisResult
- CleaningResult
- ChatMessage
- JobGuideline
- AuditLog

## API Overview

All endpoints are mounted under /api.

Auth (users/urls.py):

- POST /api/register
- POST /api/login
- POST /api/logout
- GET /api/me
- POST /api/social-login
- GET /api/github/login
- GET /api/github/callback
- POST /api/password/reset
- POST /api/password/reset/confirm
- POST /api/password/change

Core data workflows (api/urls.py):

- POST /api/upload
- POST /api/analyze/<jobId>
- POST /api/test-postgres
- POST /api/analyze-postgres
- GET /api/status/<jobId>
- GET /api/results/<jobId>
- GET /api/results/<jobId>/export
- GET /api/raw/<jobId>
- GET /api/raw_with_outliers/<jobId>
- POST /api/clean/<jobId>
- GET /api/cleaned/<jobId>
- GET /api/cleaning-result/<jobId>
- GET /api/cleaning-result/<jobId>/export
- GET /api/jobs
- DELETE /api/jobs
- POST /api/agent-chat
- GET /api/chat-history/<jobId>
- POST /api/chat-history/<jobId>
- GET /api/guidelines/<jobId>
- GET /api/fixes/<job_id>/
- POST /api/execute-fix/<job_id>/

Health endpoint:

- GET /health

## Async Processing

Celery tasks handle long-running operations:

- CSV analysis and cleaning
- PostgreSQL AI validation pipeline
- Legacy PostgreSQL cleaning-plan execution
- Stuck-job monitoring task

Main task modules:

- tasks/csv_tasks.py
- tasks/postgres_tasks.py

## Authentication Model

- JWT cookie name: token
- Cookie transport: httpOnly, SameSite=Lax
- Frontend sends credentials with every request (withCredentials)
- DRF auth class: users.backends.CookieJWTAuthentication

## Local Setup

1. Create and activate virtual environment:

```bash
cd backend-django
python3 -m venv venv
source venv/bin/activate
```

1. Install dependencies:

```bash
pip install -r requirements.txt
```

1. Ensure PostgreSQL and Redis are running.

2. Apply migrations:

```bash
python manage.py migrate
```

1. Start API server:

```bash
python manage.py runserver 0.0.0.0:8000
```

1. Start Celery worker (new terminal):

```bash
cd backend-django
source venv/bin/activate
celery -A config worker -l info --concurrency=1 --prefetch-multiplier=1
```

Optional: start celery beat if you use scheduled jobs.

## Useful Commands

```bash
# Django checks
python manage.py check

# Unit tests (example auth test module)
python -m unittest users.tests.test_register_cookie_flow

# Health check (from another terminal)
curl http://localhost:8000/health
```

## Notes

- Use localhost consistently (frontend and backend) to avoid cookie-origin mismatches.
- If backend behavior changes, update ARCHITECTURE.md and this README together.
