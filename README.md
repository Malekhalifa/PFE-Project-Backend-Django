# QualityLab Backend (Django)

Last updated: May 2026

Powers authentication, PostgreSQL AI validation, chat history, guidelines, domain rules, human-in-the-loop SQL fix execution, admin dashboard, and report export.

## Tech Stack

- Python 3.14 + Django 5 + Django REST Framework
- PostgreSQL (application database)
- Redis (Celery broker + result backend)
- Celery (async pipeline execution)
- Agno multi-agent framework
- LLM backends: Gemini 2.0 Flash (default), configurable via `LLM_MODEL`

## High-Level Architecture

```
Browser (Angular SPA)
  → Django API (auth + jobs + pipeline + chat + guidelines + fixes + admin)
    → PostgreSQL (users, jobs, results, chat, guidelines, audit)
    → Redis (Celery broker/result backend)
    → Celery workers (async PostgreSQL AI pipeline)
      → Multi-agent pipeline (Profiler + 4 Validators + Fix Planner)
```

Request lifecycle:

1. Frontend sends authenticated requests to Django.
2. Django persists job state and dispatches long tasks to Celery.
3. Celery runs the multi-agent validation pipeline; writes results/status.
4. Frontend polls `/api/status/<jobId>` and renders results, chat, and fix cards.

## Multi-Agent Pipeline (6 Agents)

Defined in `agents/agent_config.py` and orchestrated in `agents/validation.py`.

| Agent | Role |
|-------|------|
| **Database Profiler** | Baseline pass — row counts, null rates, cardinality, FK/index map, column type inference |
| **Schema Integrity Validator** | PKs, FK constraints, nullable FKs, missing indexes, constraint coverage |
| **Statistical Validator** | Null rate anomalies, distribution drift, high-cardinality columns, outlier detection |
| **Structural Validator** | Row-level conformance — impossible dates, blank required fields, negative amounts |
| **Relational Validator** | Cross-table integrity — orphaned FKs, duplicates, functional dependency violations |
| **Data Fix Planner** | Synthesises ranked findings into human-reviewable SQL fix cards with dry-run impact |

### Pipeline Flow

```
Celery task starts
  → Profiler (schema + baseline stats)
  → Schema Integrity Validator  ┐
  → Statistical Validator       ├─ parallel via agno Team (coordinate mode)
  → Structural Validator        │
  → Relational Validator        ┘
  → Reducer (dedup + rank + dependency graph)
  → Fix Planner (SQL fix cards with pre-flight checks)
  → Results persisted to PostgreSQL
```

### Key Design Decisions

- **Domain rules**: injected into every agent at runtime; loaded once per pipeline run.
- **Schema snapshot**: compact per-agent view (stripped of FK/index lists) to reduce token cost ~25%.
- **Reducer**: deduplicates findings, scores severity, builds dependency graph before fix planner runs.
- **Fix pre-flight**: each fix card runs a `COUNT(*)` impact estimate; high-risk cards get `EXPLAIN` checks.
- **Migration script**: ordered DDL/DML bundle generated from the fix plan for one-shot execution.

## Project Structure

```
backend-django/
├── config/          Django settings, root URLs, Celery app
├── users/           User model, auth views, cookie JWT backend, social login
├── api/
│   ├── models.py    Job, AnalysisResult, ChatMessage, JobGuideline, AuditLog
│   ├── views.py     All API view classes
│   ├── urls.py      URL routing
│   └── db.py        PostgreSQL adapter (connect, query, schema introspection)
├── agents/
│   ├── agent_config.py   Per-agent instruction sets and output schemas
│   ├── team.py           Agent + Team construction (agno)
│   ├── validation.py     Full pipeline orchestration (Celery entry point)
│   ├── reducer.py        Finding dedup, ranking, dependency graph
│   ├── hooks.py          Pre/post hooks (schema priming, domain rules, logging)
│   ├── schemas.py        Pydantic output schemas (FixPlan, ValidatorReport, …)
│   └── tool_factories.py Per-agent DB tool sets
├── tasks/
│   └── postgres_tasks.py  Celery tasks for PostgreSQL pipeline
├── reports/         Generated report artifacts
├── manage.py
└── requirements.txt
```

## Data Models (`api/models.py`)

| Model | Purpose |
|-------|---------|
| `Job` | Tracks pipeline job state, status, timestamps, DB name |
| `AnalysisResult` | Full pipeline results JSON blob per job |
| `ChatMessage` | Persisted chat history per job |
| `JobGuideline` | Domain rules / guidelines attached to a job |
| `AuditLog` | Admin audit trail for fix executions and sensitive ops |

## API Endpoints

All endpoints under `/api`.

### Auth (`users/urls.py`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/register` | Email + password registration |
| POST | `/api/login` | Cookie JWT login |
| POST | `/api/logout` | Clear auth cookie |
| GET | `/api/me` | Current user info |
| POST | `/api/social-login` | Google OAuth token exchange |
| GET | `/api/github/login` | GitHub OAuth redirect |
| GET | `/api/github/callback` | GitHub OAuth callback |
| POST | `/api/password/reset` | Request password reset email |
| POST | `/api/password/reset/confirm` | Confirm reset with token |
| POST | `/api/password/change` | Change password (authenticated) |
| POST | `/api/resend-verification` | Resend email verification link |

### Core Pipeline (`api/urls.py`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/test-postgres` | Test DSN connectivity |
| POST | `/api/analyze-postgres` | Start async validation pipeline |
| GET | `/api/status/<jobId>` | Poll pipeline status |
| GET | `/api/results/<jobId>` | Full results payload |
| GET | `/api/results/<jobId>/diff` | Results diff between two runs |
| GET | `/api/results/<jobId>/export` | Download results as JSON report |
| GET | `/api/jobs` | List all jobs for current user |
| DELETE | `/api/jobs` | Bulk delete jobs |
| GET/DELETE | `/api/jobs/<job_id>` | Job detail / delete single job |

### Chat

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agent-chat` | Send message to QualityLab team agent |
| GET | `/api/chat-history/<jobId>` | Load persisted chat history |
| POST | `/api/chat-history/<jobId>` | Save chat message |
| GET/POST | `/api/jobs/<job_id>/chat` | Alias for chat history |

### Fixes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/fixes/<job_id>/` | Get generated fix cards |
| POST | `/api/execute-fix/<job_id>/` | Execute approved fix SQL (human-in-the-loop) |

### Guidelines

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/guidelines/<jobId>` | Get domain rules/guidelines for job |

### Admin

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/stats/` | Platform-wide job + user stats |
| GET | `/api/admin/pipeline-performance/` | Agent timing + success metrics |
| GET | `/api/admin/audit-log/` | Fix execution audit trail |
| GET | `/api/admin/error-timeline/` | Pipeline error history |
| POST | `/api/admin/test-email/` | Send test email |
| GET | `/api/admin/users/` | List all users |
| GET/PATCH/DELETE | `/api/admin/users/<user_id>/` | User detail management |

### Health

| Method | Path |
|--------|------|
| GET | `/health` |

## Authentication

- Transport: httpOnly cookie named `token`
- Algorithm: JWT (HS256), expiry configurable
- DRF auth class: `users.backends.CookieJWTAuthentication`
- Social: Google OAuth (token exchange), GitHub OAuth (redirect flow)
- Email verification required before running analysis

## Async Processing

Celery tasks in `tasks/postgres_tasks.py`:

- `run_postgres_analysis_task` — full multi-agent pipeline
- Stuck-job monitor (periodic beat task)

Worker startup:

```bash
celery -A config worker -l info --concurrency=1 --prefetch-multiplier=1
```

## Local Setup

```bash
cd backend-django
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Ensure PostgreSQL and Redis are running
python manage.py migrate
python manage.py runserver 0.0.0.0:8000

# Second terminal — Celery worker
celery -A config worker -l info --concurrency=1 --prefetch-multiplier=1
```

## Useful Commands

```bash
python manage.py check
curl http://localhost:8000/health

# Run backend tests
python -m pytest agents/tests/ -v
```

## Notes

- Use `localhost` consistently — mixing `127.0.0.1` causes cookie-origin mismatches.
- Agent names in `agent_config.py` must match `id=` in `team.py`.
- Domain rules are injected via `inject_domain_rules` pre-hook; never hardcode table/column names in config.
- The fix planner schema snapshot strips FK/index metadata to reduce token cost; planner calls `describe_constraints()` directly when needed.
