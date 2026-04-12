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

## Multi-Agent Validation System Architecture (7 Agents)

The backend agent architecture includes seven distinct agent roles.

### Agent List (All 7)

1. Database Guideline Generator Agent
2. Schema Integrity Validator
3. Column Validator
4. Row-Level Logic Validator
5. Cross-Row Validator
6. Data Fix Planner
7. Query Executor Agent

### How They Work Together

```text
Automated analysis pipeline (Celery):

1) Database Guideline Generator Agent
	-> generates database-specific instructions per stage

2) Schema Integrity Validator
	-> structure constraints, PK/FK, nullability, schema quality

3) Column Validator
	-> column-level quality, semantics, null/cardinality/distribution checks

4) Row-Level Logic Validator
	-> per-row business-rule and format violations

5) Cross-Row Validator
	-> temporal, relational, and multi-row anomaly checks

6) Data Fix Planner
	-> synthesizes findings into human-reviewable SQL fix cards

Human-in-the-loop execution path:

7) Query Executor Agent
	-> executes approved fix-card SQL in one safe transaction
```

### Pipeline Notes

- Guideline Generator runs before each validator/planner stage.
- Validators return structured JSON reports and pass prior context forward.
- Fix Planner generates proposed fixes; it does not directly execute SQL.
- Query Executor Agent executes only user-approved queries from fix cards.

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
- GET /api/share/<sharedToken>
- GET /api/powerbi-config/<jobId>

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

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Ensure PostgreSQL and Redis are running.

4. Apply migrations:

```bash
python manage.py migrate
```

5. Start API server:

```bash
python manage.py runserver 0.0.0.0:8000
```

6. Start Celery worker (new terminal):

```bash
cd backend-django
source venv/bin/activate
celery -A config worker -l info
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
