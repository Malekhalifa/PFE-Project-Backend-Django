# QualityLab — Technical Architecture Document

> **Prepared for:** Academic Supervisor  
> **Project type:** End-of-Study Project (Projet de Fin d'Études)  
> **Last updated:** March 2026

---

## Table of Contents

1. [What is QualityLab?](#1-what-is-qualitylab)
2. [High-Level Architecture](#2-high-level-architecture)
3. [The Frontend — Angular](#3-the-frontend--angular)
4. [The Backend — Django](#4-the-backend--django)
5. [Why Celery? (Async Task Queue)](#5-why-celery-async-task-queue)
6. [Why a Separate Python Server? (AgentOS)](#6-why-a-separate-python-server-agentos)
7. [The AI Module — Multi-Agent Validation System](#7-the-ai-module--multi-agent-validation-system)
8. [The Databases](#8-the-databases)
9. [Complete Data Flow](#9-complete-data-flow)
10. [Project File Structure](#10-project-file-structure)
11. [How to Start the Project](#11-how-to-start-the-project)
12. [Technology Stack Summary](#12-technology-stack-summary)

---

## 1. What is QualityLab?

QualityLab is a **data quality analysis platform** with two distinct capabilities:

### 1.1 CSV Data Quality Analysis
Users upload a **CSV file** via the web interface. The platform automatically:
- Detects missing values, duplicates, and outliers (using `IsolationForest` from scikit-learn)
- Analyses column types and statistical distribution
- Produces a quality report
- Offers a **data cleaning** step and lets the user download the cleaned file

### 1.2 PostgreSQL Database Validation (AI-Powered)
Users provide a **PostgreSQL connection string**. A team of **four AI agents** (powered by Google Gemini) then:
- Inspect the database **schema** for structural issues (missing primary keys, unlinked foreign key columns, bad nullability)
- Run **cross-row SQL queries** to detect aggregate violations (quota overflows, duplicate phone numbers, referential gaps)
- Run **row-level SQL queries** to detect per-row business rule violations (impossible dates, blank required fields, format errors, etc.)
- Synthesise findings to generate a **structured JSON Cleaning Plan**, mapping out the exact transformation steps needed to fix the database automatically.

All results are stored in MongoDB. Users can review the full report and engage in a **human-in-the-loop chat** with the Agent OS memory directly on the platform.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          USER'S BROWSER                                  │
│                                                                           │
│                    Angular SPA  (port 3000)                              │
│         Upload · Analyze · Results · History · Login                     │
└────────────────────────────┬────────────────────────────────────────────┘
                             │  HTTP REST (JSON)
                             │  JWT Authentication (httpOnly cookies)
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Django Backend  (port 8000)                          │
│                                                                           │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │  Auth API   │  │  Jobs API    │  │  CSV Service │  │  PG Service │  │
│  │  (JWT)      │  │  (upload,    │  │  (analysis,  │  │  (bridge to │  │
│  │             │  │   status,    │  │   cleaning)  │  │   AI agents)│  │
│  │             │  │   results)   │  │              │  │             │  │
│  └─────────────┘  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  │
│                          │                  │                  │         │
│                          └─────────┬────────┘                  │         │
│                                    ▼                            │         │
│                    ┌──────────────────────────┐                │         │
│                    │   Celery Task Queue       │                │         │
│                    │   (async job executor)    │◄───────────────┘         │
│                    └──────────────────────────┘                          │
└──────────┬──────────────────┬──────────────────────────────────────────┘
           │                  │
           ▼                  ▼
   ┌───────────────┐  ┌──────────────┐
   │   MongoDB     │  │  PostgreSQL  │
   │  (jobs,       │  │  (users,     │
   │   results)    │  │   audit log) │
   └───────────────┘  └──────────────┘
           ▲
           │
   ┌───────────────────────────────────────────────┐
   │             AI Module  (port 7777)             │
   │                                                │
   │   AgentOS — Agno Framework + Gemini Flash      │
   │                                                │
   │   ┌───────────────────────────────────────┐   │
   │   │  Master Agent (Team leader)           │   │
   │   │    ├── Guideline Agent (Agent 0)      │   │
   │   │    ├── Schema Integrity Validator     │   │
   │   │    ├── Column Validator               │   │
   │   │    ├── Row-Level Logic Validator      │   │
   │   │    ├── Cross-Row Validator            │   │
   │   │    └── Data Cleaning Planner          │   │
   │   └───────────────────────────────────────┘   │
   └───────────────────────────────────────────────┘
```

> **Three processes must run simultaneously** when AI validation is active:
> 1. Django server (HTTP API)
> 2. Celery worker (async task runner)
> 3. AgentOS server OR embedded in-process agent call

---

## 3. The Frontend — Angular

### What it is
A **Single-Page Application (SPA)** built with **Angular 21**. It runs entirely in the browser and communicates with the Django backend via HTTP REST calls. The frontend has no business logic — it is purely a user interface.

### Key pages / components

| Page | Path | What it does |
|------|------|-------------|
| **Login / Register** | `/login` | Authenticates the user; JWT cookie is set by Django |
| **Upload** | `/upload` | Lets the user upload a CSV file or enter a PostgreSQL connection string |
| **Analyze** | `/analyze` | Triggers analysis on an uploaded file or DB connection |
| **Results** | `/results` | Displays the quality report (charts, tables, per-column stats) |
| **History** | `/history` | Lists all past analysis jobs with status and links to results |

### Key technical choices
- **Angular Signals** and **Standalone Components** (Angular 21's modern pattern — no NgModules)
- **RxJS** for reactive HTTP calls and state management
- **HTTP Interceptors** to automatically attach auth credentials to every request
- **Route Guards** to protect pages from unauthenticated access
- **Tailwind CSS** for styling

### How it communicates with Django
```
Angular HttpClient
  → interceptor attaches cookie credentials
  → Django REST API at port 8000
  → returns JSON
  → Angular displays data
```

---

## 4. The Backend — Django

### What it is
A **Django 5 + Django REST Framework (DRF)** application. It is the central hub of the whole platform: it handles authentication, stores data, manages jobs, runs CSV processing, and bridges to the AI agents.

### Django Apps

```
backend-django/
├── config/        Django project settings, Celery configuration, URL routing
├── users/         User model, registration, login, JWT authentication
├── api/           All business API endpoints (jobs, upload, analysis, cleaning, results)
├── services/      Pure Python business logic (no Django ORM dependency)
│   ├── analysis_service.py   CSV quality analysis (pandas, numpy, scikit-learn)
│   ├── cleaning_service.py   CSV data cleaning
│   └── postgres_service.py   Bridge layer → calls the AI agents
├── tasks/         Celery async task definitions
│   ├── csv_tasks.py          Celery tasks for CSV analysis and cleaning
│   └── postgres_tasks.py     Celery task for PostgreSQL AI validation
└── agents/        The AI multi-agent module (see Section 7)
```

### Authentication
Django uses **JWT (JSON Web Tokens)** stored as **httpOnly cookies** — the most secure approach for browser-based apps because JavaScript cannot read or steal the token.

### API Endpoints summary

**Auth:**
- `POST /api/login` — sign in, sets JWT cookie
- `POST /api/register` — create account
- `POST /api/logout` — clears JWT cookie
- `GET  /api/me` — returns the logged-in user's profile

**CSV Jobs:**
- `POST /api/upload` — upload a CSV, creates a job record in MongoDB
- `POST /api/analyze/:jobId` — starts async Celery analysis task
- `GET  /api/status/:jobId` — frontend polls this to know when job is ready
- `GET  /api/results/:jobId` — returns the full quality report
- `POST /api/clean/:jobId` — starts async Celery cleaning task
- `GET  /api/cleaned/:jobId` — download the cleaned CSV

**History:**
- `GET    /api/jobs` — list all jobs for the current user
- `DELETE /api/jobs` — bulk delete selected jobs

---

## 5. Why Celery? (Async Task Queue)

### The problem it solves
Analysing a large CSV file or running AI agents against a database can take **30 seconds to several minutes**. If Django ran that work directly inside an HTTP request, the browser would time out and the user would see an error.

### How Celery fixes this

```
1. Frontend sends POST /api/analyze/:jobId
        ↓
2. Django immediately responds 202 Accepted (job is "pending")
   and dispatches the work to Celery via Redis
        ↓
3. Celery worker picks up the task and runs it in the background
        ↓
4. Frontend polls GET /api/status/:jobId every few seconds
        ↓
5. When Celery finishes, it stores the result in MongoDB
   and sets the job status to "completed"
        ↓
6. Frontend detects "completed" and fetches the results
```

### Components involved

| Component | Role |
|-----------|------|
| **Redis** | The message broker — Django writes tasks to Redis, Celery reads from it |
| **Celery Worker** | A separate OS process that reads and executes tasks from Redis |
| **MongoDB** | Where task results (analysis reports) are stored after completion |

### Why Redis?
Redis is an in-memory data store that is extremely fast for passing messages between processes. It acts as a queue: Django puts tasks in, Celery takes tasks out.

### Why must Celery run as a separate process?
Celery needs its own process because it runs continuously in the background, independent of Django. Starting a task from Django merely sends a message — the actual execution happens in the Celery process. This allows the Django API server to remain fast and responsive at all times.

---

## 6. Why a Separate Python Server? (AgentOS)

### What AgentOS is
**Agno AgentOS** is a FastAPI-based server (port 7777) that exposes the AI agents via a REST API and provides a browser-based interactive playground at [os.agno.com](https://os.agno.com).

### Why it exists separately from Django

| Concern | Django (port 8000) | AgentOS (port 7777) |
|---------|-------------------|---------------------|
| Purpose | Production API for users | Developer playground for the AI agents |
| Users | End users (Angular frontend) | Developers / supervisors testing agents |
| Auth | JWT cookies | None (local dev tool) |
| Protocol | HTTP REST | FastAPI / WebSockets |

AgentOS is **not used by end users**. It is a developer tool for:
- Testing and interacting with the AI agents directly via a chat interface
- Debugging agent behaviour before it is called through Celery
- Demonstrating the agent system to supervisors

### In production
When a user triggers a PostgreSQL validation via the Angular frontend:
```
Angular → POST /api/analyze/:jobId
       → Django → Celery (async)
       → postgres_tasks.py → postgres_service.py
       → validation_team.py (runs agents directly, no AgentOS server needed)
```
The agents run **embedded inside the Celery worker process** — no HTTP call to AgentOS is made. AgentOS is only needed when you want to use the interactive chat playground.

---

## 7. The AI Module — Multi-Agent Validation System

### Overview

The AI module is located in `backend-django/agents/` and uses the **Agno** agent framework with **Google Gemini Flash** as the language model.

It implements a **multi-agent team** where a master agent coordinates four specialist sub-agents, each with a specific validation responsibility.

### Architecture

```
User prompt / Celery task
        ↓
  ┌─────────────────────────────────────────────────┐
  │           Master Agent (Team leader)             │
  │  - Receives the user's request                   │
  │  - Decides which agents to call and in what order│
  │  - Synthesises all outputs into one final report │
  └───────┬────────────────┬────────────────┬────────┬───────┘
          ↓                ↓                ↓        ↓
 ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
 │Schema Integrity│ │Cross-Row       │ │Row-Level Logic │ │Data Cleaning   │
 │Validator       │ │Validator       │ │Validator       │ │Planner         │
 │                │ │                │ │                │ │                │
 │Checks: missing │ │Checks: quota   │ │Checks: date    │ │Consumes all    │
 │PKs, unlinked   │ │math, duplicate │ │ranges, blank   │ │prior reports & │
 │FKs, bad        │ │msisdns,        │ │fields, formats.│ │generates a JSON│
 │nullability.    │ │orphaned refs.  │ │                │ │cleaning plan.  │
 └────────────────┘ └────────────────┘ └────────────────┘ └────────────────┘
          │                │                │
          └────────────────┴────────────────┘
                           ↓
                    PostgreSQL Database
                  (agents query via SQL tools)
```

### Key Design Decisions

#### 1. Sequential execution (coordinate mode)
Each agent **passes its findings to the next one** via the prompt context:
- Agent 2 (Cross-Row) receives Agent 1's schema findings to know real table/column names
- Agent 3 (Row-Level) receives both previous agents' findings

This avoids redundant schema discovery and ensures the agents build on each other's work.

#### 2. SQL-based validation (not in-memory)
The agents do not download the whole database. Instead they run **targeted SQL WHERE queries** against the live database:
```sql
-- Example: find campaigns that started before they were created
SELECT id, libelle, created_at, date_debut
FROM campagne
WHERE created_at > date_debut;
```
This is scalable to databases with millions of rows.

#### 3. Read-only access
The tools available to the agents only allow `SELECT` SQL queries. `DROP`, `DELETE`, `UPDATE`, `INSERT` etc. are blocked at the tool level. The agents can never modify the database.

#### 4. Shared memory
All agents share the same **SQLite session database** (`tmp/agentos_sessions.db`). This means:
- Conversation history is preserved across messages
- Sub-agents see what the master agent and other sub-agents have said

### Files

| File | Purpose |
|------|---------|
| `agent_config.py` | **Single source of truth** — all agent descriptions and instruction lists |
| `tools.py` | The two tools available to agents: `connect_to_database`, `execute_pg_query` |
| `validation_team.py` | Production pipeline called by Celery — runs agents sequentially |
| `agent_os.py` | Developer playground — wraps agents in a Team and exposes via AgentOS |

### Why `agent_config.py` is the single source of truth
Both `validation_team.py` (Celery pipeline) and `agent_os.py` (playground) define the same 3 agents. To avoid duplicating rules, all `description` and `instructions` are stored once in `agent_config.py` and imported by both files. Changing a rule in one place updates both contexts automatically.

---

## 8. The Databases

The project uses **two databases**, each chosen for a different purpose:

### MongoDB — Jobs, Results, and Chat History

| Stored data | Why MongoDB? |
|-------------|-------------|
| Job records (upload metadata, status, timestamps) | Flexible schema — jobs can be CSV jobs or PostgreSQL jobs with different fields |
| CSV analysis results (per-column stats, outliers, duplicates) | Large nested JSON documents — MongoDB handles these natively |
| AI agent reports & cleaning plans | Unstructured text and large JSON arrays — no rigid schema needed |
| Persistent Chat History | Every message between the user and the AI Agent is saved in the `chat_messages` collection |

### PostgreSQL — Users and Audit Log

| Stored data | Why PostgreSQL? |
|-------------|----------------|
| User accounts (id, email, hashed password) | Relational, consistent schema — perfect for SQL |
| Audit log (who did what, when, from which IP) | Requires strong consistency and indexing |

### Redis — Message Broker

| Purpose | Detail |
|---------|--------|
| Celery task queue | Django writes task payloads; Celery workers read them |
| Celery result backend | Task completion status before MongoDB storage |

---

## 9. Complete Data Flow

### 9.1 CSV Analysis Flow

```
1. User uploads CSV
   Angular POST /api/upload
   → Django saves file to uploads/, creates MongoDB job {status: "pending"}

2. User clicks "Analyze"
   Angular POST /api/analyze/:jobId
   → Django dispatches Celery task via Redis
   → Django responds 202 immediately

3. Celery worker executes csv_tasks.analyze_csv_task()
   → calls analysis_service.py (pandas, numpy, scikit-learn)
   → saves result to MongoDB
   → sets job status to "completed"

4. Angular polls GET /api/status/:jobId every 3 seconds
   → detects "completed"
   → fetches GET /api/results/:jobId
   → renders the report
```

### 9.2 PostgreSQL AI Validation Flow

```
1. User provides connection string
   Angular POST /api/analyze/:jobId (type=postgres)
   → Django dispatches Celery task via Redis (30 min time limit)
   → Django responds 202 immediately

2. Celery worker executes postgres_tasks.analyze_postgres_task()
   → calls postgres_service.analyze_postgres_database()
   → calls validation_team.orchestrator_workflow()

3. Agent 0 (Guideline Agent) runs
   → generates rules and orchestrates downstream validation behavior

4. Agent 1 (Schema Integrity Validator) runs
   → queries information_schema via execute_pg_query tool
   → returns markdown schema report

5. Agent 2 (Column Validator) receives Agent 1's report + context
   → runs data type, format, and statistical outlier queries
   → returns markdown column constraints violations report

6. Agent 3 (Row-Level Logic Validator) receives previous reports
   → runs targeted WHERE-clause queries for each business rule
   → returns markdown row-level violations report

7. Agent 4 (Cross-Row Validator) receives previous reports
   → runs aggregate GROUP BY / HAVING / JOIN SQL queries
   → returns markdown cross-row violations report

8. Agent 5 (Data Cleaning Planner) receives all preceding cascading reports
   → generates the deterministic JSON fix card list

9. postgres_service formats results into standard dict, including the cleaning plan
10. postgres_tasks saves to MongoDB, sets status "completed"
11. Angular detects "completed" and enables the AI Chat and "Download Plan" features

### 9.3 Persistent AI Chat Flow (Human-in-the-Loop)

```
1. Analysis Finishes
   → Angular crafts a Markdown summary of the 4 agents' results.
   → Angular POSTs this summary to Django to be saved as the first "System" chat message.
   → User is navigated to the /AI-chat/:jobId screen.

2. User Loads the AI Chat Page
   → Angular GET /api/chat-history/:jobId fetches the persistent conversation from MongoDB.
   → A unique AgentOS `session_id` is tied to the current Django `job_id`.

3. User Sends an Inquiry
   → Angular POST /api/agent-chat (includes message, role, job_id, and session_id)
   → Django saves the user message to MongoDB for permanent storage.
   → Django forwards the payload to AgentOS (port 7777).

4. AgentOS Replies
   → Agno framework uses its internal SQLite memory tied to `session_id` to maintain short-term context.
   → Agent executes SQL if required and constructs an answer.
   → Django intercepts the Agno markdown response, saves the Agent's message to MongoDB, and returns it to Angular.
   → The user receives the response with full Markdown rendering via Angular pipes.
```

---

## 10. Project File Structure

```
Projet_Fin_Etude/
│
├── frontend-angular/              Angular 21 SPA
│   └── src/app/
│       ├── components/
│       │   ├── login/             Login & registration page
│       │   ├── upload/            File upload + connection string input
│       │   ├── analyze/           Analysis trigger page
│       │   ├── results/           Quality report display
│       │   ├── history/           Job history dashboard
│       │   └── shared/            Shared UI components (navbar, etc.)
│       ├── services/              Angular HTTP services (calls Django API)
│       ├── guards/                Route guards (protect authenticated routes)
│       ├── interceptors/          HTTP interceptor (attaches auth cookies)
│       ├── models/                TypeScript interfaces (Job, Result, User...)
│       └── app.routes.ts          Application routing
│
└── backend-django/                Django 5 + DRF backend
    ├── config/                    Django settings, Celery config, main URL router
    ├── users/                     User model, register/login/logout views
    ├── api/
    │   ├── views.py               All API endpoint handlers
    │   ├── urls.py                URL patterns for the API
    │   ├── mongo.py               MongoDB helper functions (read/write jobs & results)
    │   ├── models.py              PostgreSQL models (audit log)
    │   └── serializers.py         DRF serializers
    ├── services/
    │   ├── analysis_service.py    CSV quality analysis (≈ 600 lines of data science)
    │   ├── cleaning_service.py    CSV data cleaning logic
    │   └── postgres_service.py    Bridge: formats rules, calls AI agents
    ├── tasks/
    │   ├── csv_tasks.py           Celery task: CSV analysis + cleaning
    │   └── postgres_tasks.py      Celery task: PostgreSQL AI validation
    └── agents/
        ├── agent_config.py        ★ Shared agent descriptions & instructions (single source)
        ├── tools.py               SQL tools: connect_to_database, execute_pg_query
        ├── validation_team.py     Production pipeline (called by Celery)
        └── agent_os.py            Developer playground (AgentOS / FastAPI server)
```

---

## 11. How to Start the Project

Five processes must run (in this order):

```bash
# Terminal 0 — Start infrastructure (databases + broker)
brew services start mongodb-community@8.0
brew services start postgresql
brew services start redis

# Launch postgres CLI (optional, for inspecting tables) : 
psql -d easybulk -P expanded=auto -P pager=off
# then for timing :   
# \timing

# Terminal 1 — Django backend (API server)
cd backend-django
source venv/bin/activate
python manage.py runserver 0.0.0.0:8000

# Terminal 2 — Celery worker (async task processor)
cd backend-django
source venv/bin/activate
celery -A config worker -l info

# Terminal 3 — Frontend
cd frontend-angular
npm run dev
# → http://localhost:3000

# Terminal 4 — AgentOS playground (optional, for developer use only)
cd backend-django
source venv/bin/activate
python agents/agent_os.py
# → http://localhost:7777  (connect via https://os.agno.com)
```

---

## 12. Technology Stack Summary

| Layer | Technology | Version | Purpose |
|-------|-----------|---------|---------|
| **Frontend** | Angular | 21 | SPA user interface |
| | TypeScript | 5 | Type-safe JavaScript |
| | Tailwind CSS | 3 | Styling |
| | RxJS | 7 | Reactive HTTP and state |
| **Backend** | Django | 5.1 | Web framework, ORM, auth |
| | Django REST Framework | 3.15 | REST API layer |
| | SimpleJWT | 5.3 | JWT authentication |
| | django-allauth | 0.61 | Social OAuth / passwordless login |
| **Async** | Celery | 5.4 | Distributed async task queue |
| | Redis | 7 | Message broker for Celery |
| **Data Science** | pandas | 2.2 | CSV parsing and manipulation |
| | numpy | 1.26 | Numerical operations |
| | scikit-learn | 1.4 | IsolationForest outlier detection |
| **AI / Agents** | Agno | 2.5 | Multi-agent orchestration framework |
| | Google Gemini Flash | 2.0 | LLM powering all agents |
| | FastAPI / Uvicorn | 0.115 | AgentOS HTTP server (dev playground) |
| **Databases** | MongoDB | 8.0 | Jobs, analysis results (document store) |
| | PostgreSQL | 16 | Users, audit log (relational) |
| | SQLite | — | Agno agent session memory |
