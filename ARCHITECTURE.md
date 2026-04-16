# QualityLab Backend Architecture

Last updated: April 2026

## 1. Overview

The backend is a Django 5 + Django REST Framework application that powers:

- Authentication (cookie-based JWT)
- CSV upload, analysis, and cleaning workflows
- PostgreSQL agentic validation workflows
- Human-in-the-loop SQL fix execution
- Persistent chat, guidelines, and audit events

This backend is PostgreSQL-first for application persistence. MongoDB is not part of the active runtime data path.

## 2. Runtime Topology

```text
Angular SPA (:3000)
   -> Django API (:8000)
      -> PostgreSQL (application data)
      -> Redis (Celery broker/result backend)
      -> Celery worker (async analysis/cleaning jobs)
      -> Optional AgentOS playground (:7777) for developer testing
```

## 3. Backend Module Structure

```text
backend-django/
├── config/      Django settings, Celery config, root URLs
├── users/       User model + auth backends + auth endpoints
├── api/         DRF views, URLs, serializers, permissions, DB adapter
├── services/    Analysis and domain services
├── tasks/       Celery async task entrypoints
├── agents/      Agno orchestration and standalone QueryExecutorAgent
├── uploads/     Uploaded CSV files
└── reports/     Persisted final pipeline reports
```

### 3.1 Core Responsibilities by Package

- `api/views.py`: HTTP contract layer
- `api/db.py`: PostgreSQL persistence adapter for jobs/results/chat/guidelines
- `tasks/postgres_tasks.py`: async orchestration lifecycle and status updates
- `agents/validation_team.py`: sequential multi-agent validation pipeline
- `agents/query_executor_agent.py`: isolated transactional execution of accepted fix queries

## 4. Persistence and Data Model

Application data is stored in PostgreSQL using Django ORM models in `api/models.py`:

- `Job`
- `AnalysisResult`
- `CleaningResult`
- `ChatMessage`
- `JobGuideline`
- `AuditLog`

Redis is used only for Celery message transport and task result backend behavior.

## 5. API Surface

Defined in `api/urls.py` under `/api`.

### 5.1 Authentication

- `POST /api/register`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/me`
- `POST /api/social-login`
- `GET /api/github/login`

### 5.2 Jobs, Analysis, Cleaning

- `POST /api/upload`
- `POST /api/analyze/<jobId>`
- `POST /api/test-postgres`
- `POST /api/analyze-postgres`
- `GET /api/status/<jobId>`
- `GET /api/results/<jobId>`
- `GET /api/results/<jobId>/export`
- `GET /api/raw/<jobId>`
- `GET /api/raw_with_outliers/<jobId>`
- `POST /api/clean/<jobId>`
- `GET /api/cleaned/<jobId>`
- `GET /api/cleaning-result/<jobId>`
- `GET /api/cleaning-result/<jobId>/export`
- `GET /api/jobs`
- `DELETE /api/jobs`

### 5.3 Agent Chat, Guidelines, Fixes

- `POST /api/agent-chat`
- `GET /api/chat-history/<jobId>`
- `POST /api/chat-history/<jobId>`
- `GET /api/guidelines/<jobId>`
- `GET /api/fixes/<job_id>/`
- `POST /api/execute-fix/<job_id>/`

## 6. Async Processing (Celery)

Long-running operations are asynchronous:

- CSV analysis and cleaning
- PostgreSQL agentic analysis
- Legacy cleaning-plan execution compatibility path

Job lifecycle pattern:

1. API endpoint validates input and creates/updates job metadata.
2. API enqueues Celery task.
3. Worker updates job status (`running`, phase-specific states, terminal state).
4. Worker persists results and audit events to PostgreSQL.

## 7. PostgreSQL Agentic Validation Pipeline

Production orchestrator: `agents/validation_team.py`.

The runtime pipeline is sequential and guideline-driven:

1. `running_guideline_gen_schema`
2. `running_schema`
3. `running_guideline_gen_column`
4. `running_column`
5. `running_guideline_gen_row_level`
6. `running_row_level`
7. `running_guideline_gen_cross_row`
8. `running_cross_row`
9. `running_guideline_gen_fix_planner`
10. `running_fix_planner`

Key implementation facts:

- Each agent receives prior report context (`prior_reports` chain).
- Guideline text is persisted per job in `JobGuideline`.
- Final analysis payload is saved in `AnalysisResult`.
- Final pipeline report is also persisted in `reports/`.

## 8. Human-in-the-Loop Fix Execution

### 8.1 Fix Card Retrieval

`GET /api/fixes/<job_id>/`

Returns AI-generated fix cards from `AnalysisResult.result.fixes`.

### 8.2 Query Execution Flow

`POST /api/execute-fix/<job_id>/`

`ExecuteFixView` delegates execution to `QueryExecutorAgent`.

`QueryExecutorAgent` characteristics:

- Standalone and stateless
- One invocation executes one fix card payload
- Input query list is executed sequentially in one transaction
- No retries
- Commit only if all queries succeed
- Immediate rollback on first failure
- Connection and cursor are always closed

### 8.3 Request Contract

```json
{
  "queries": [
    "<sql statement 1>",
    "<sql statement 2>"
  ]
}
```

### 8.4 Response Contract (Strict Execution Log)

```json
{
  "status": "success | failure",
  "query": "<all queries joined as text>",
  "queries": ["<sql statement 1>", "<sql statement 2>"],
  "database_id": "<host_port_dbname>",
  "executed_at": "<ISO timestamp>",
  "duration_ms": 123,
  "rows_affected": 10,
  "transaction": "committed | rolled_back",
  "error": {
    "code": "<postgres code or null>",
    "message": "<full db error message>",
    "detail": "<postgres detail or null>"
  },
  "query_logs": [
    {
      "index": 1,
      "query": "<sql>",
      "status": "success | failure",
      "duration_ms": 25,
      "rows_affected": 3,
      "error": null
    }
  ]
}
```

Audit events:

- Success: `FIX_EXECUTED`
- Failure: `FIX_EXECUTION_FAILED`

## 9. Security and Access Boundaries

- Endpoints are authenticated (`IsAuthenticated`).
- Ownership checks are enforced via `find_owned_job`.
- JWT is stored in httpOnly cookies.
- QueryExecutorAgent executes exactly what is accepted from fix cards and returns raw DB error details for operator visibility.

## 10. Local Run Sequence

```bash
# 1) Infrastructure
brew services start postgresql
brew services start redis

# 2) Backend API
cd backend-django
source venv/bin/activate
python manage.py runserver 0.0.0.0:8000

# 3) Celery worker
cd backend-django
source venv/bin/activate
celery -A config worker -l info
```

Optional developer playground:

```bash
cd backend-django/agents
source ../venv/bin/activate
python agent_os.py
```
