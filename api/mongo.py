"""
MongoDB connection and helper functions.

Uses pymongo directly (NOT djongo/mongoengine) to mirror the current
Mongoose usage pattern. Provides CRUD helpers for:
  - jobs collection
  - analysis_results collection
  - cleaning_results collection

Document shapes match the existing Mongoose schemas exactly.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient
from django.conf import settings


# ──────────────────────────────────────────────
# Connection singleton
# ──────────────────────────────────────────────

_client: MongoClient | None = None


def get_db():
    """Return the default MongoDB database from the MONGO_URI."""
    global _client
    if _client is None:
        _client = MongoClient(settings.MONGO_URI)
    return _client.get_default_database()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# Jobs collection
# ──────────────────────────────────────────────

def create_job(
    user_id: str,
    file_name: str,
    file_path: str,
    mode: str = 'normal',
) -> str:
    """Create a new job document. Returns the job_id (UUID string)."""
    db = get_db()
    job_id = str(uuid.uuid4())
    now = _now()

    db.jobs.insert_one({
        '_id': job_id,
        'status': 'starting',
        'mode': mode,
        'user_id': user_id,
        'file_name': file_name,
        'file_path': file_path,
        'cleaned_file_path': None,
        'created_at': now,
        'updated_at': now,
    })
    return job_id


def get_job(job_id: str) -> dict | None:
    """Find a job by ID."""
    db = get_db()
    return db.jobs.find_one({'_id': job_id})


def get_jobs_for_user(user_id: str, is_admin: bool = False) -> list[dict]:
    """
    Return jobs for the authenticated user.
    Admins see all jobs; regular users see only their own.
    Sorted by created_at descending.
    """
    db = get_db()
    query = {} if is_admin else {'user_id': user_id}
    return list(
        db.jobs.find(
            query,
            {
                '_id': 1,
                'file_name': 1,
                'status': 1,
                'mode': 1,
                'created_at': 1,
                'user_id': 1,
            },
        ).sort('created_at', -1)
    )


def update_job_status(job_id: str, status: str) -> None:
    """Update a job's status."""
    db = get_db()
    db.jobs.update_one(
        {'_id': job_id},
        {'$set': {'status': status, 'updated_at': _now()}},
    )


def update_job_cleaned_path(job_id: str, cleaned_file_path: str) -> None:
    """Set the cleaned file path on a job."""
    db = get_db()
    db.jobs.update_one(
        {'_id': job_id},
        {'$set': {
            'cleaned_file_path': cleaned_file_path,
            'updated_at': _now(),
        }},
    )


def update_job_share_token(job_id: str, share_token: str | None) -> None:
    """Set or clear the share token for a job."""
    db = get_db()
    db.jobs.update_one(
        {'_id': job_id},
        {'$set': {
            'share_token': share_token,
            'updated_at': _now(),
        }},
    )


def get_job_by_share_token(share_token: str) -> dict | None:
    """Find a job by its share token."""
    db = get_db()
    return db.jobs.find_one({'share_token': share_token})


def delete_jobs(job_ids: list[str], user_id: str = None, is_admin: bool = False) -> list[dict]:
    """
    Delete jobs by IDs, enforcing ownership.
    Returns the list of deleted job documents.
    """
    db = get_db()

    query = {'_id': {'$in': job_ids}}
    if not is_admin:
        query['user_id'] = user_id

    jobs = list(db.jobs.find(query))
    owned_ids = [j['_id'] for j in jobs]

    if owned_ids:
        db.jobs.delete_many({'_id': {'$in': owned_ids}})
        db.analysis_results.delete_many({'job_id': {'$in': owned_ids}})
        db.cleaning_results.delete_many({'job_id': {'$in': owned_ids}})

    return jobs


# ──────────────────────────────────────────────
# Analysis results collection
# ──────────────────────────────────────────────

def save_analysis_result(
    job_id: str,
    result: dict[str, Any],
    analysis_type: str = 'in-memory',
) -> None:
    """Upsert an analysis result for a job."""
    db = get_db()
    now = _now()
    db.analysis_results.update_one(
        {'job_id': job_id},
        {'$set': {
            'job_id': job_id,
            'result': result,
            'analysis_type': analysis_type,
            'updated_at': now,
        }, '$setOnInsert': {
            'created_at': now,
        }},
        upsert=True,
    )


def get_analysis_result(job_id: str) -> dict | None:
    """Find an analysis result by job_id."""
    db = get_db()
    return db.analysis_results.find_one({'job_id': job_id})


def get_analysis_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have analysis results."""
    db = get_db()
    docs = db.analysis_results.find(
        {'job_id': {'$in': job_ids}},
        {'job_id': 1},
    )
    return {d['job_id'] for d in docs}


# ──────────────────────────────────────────────
# Cleaning results collection
# ──────────────────────────────────────────────

def save_cleaning_result(
    job_id: str,
    mode: str,
    rules_applied: list[dict],
    summary: dict,
) -> None:
    """Upsert a cleaning result for a job."""
    db = get_db()
    now = _now()
    db.cleaning_results.update_one(
        {'job_id': job_id},
        {'$set': {
            'job_id': job_id,
            'mode': mode,
            'rules_applied': rules_applied,
            'summary': summary,
            'updated_at': now,
        }, '$setOnInsert': {
            'created_at': now,
        }},
        upsert=True,
    )


def get_cleaning_result(job_id: str) -> dict | None:
    """Find a cleaning result by job_id."""
    db = get_db()
    return db.cleaning_results.find_one({'job_id': job_id})


def get_cleaning_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have cleaning results."""
    db = get_db()
    docs = db.cleaning_results.find(
        {'job_id': {'$in': job_ids}},
        {'job_id': 1},
    )
    return {d['job_id'] for d in docs}


# ──────────────────────────────────────────────
# Chat messages collection
# ──────────────────────────────────────────────

def save_chat_message(
    job_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    """Append a single chat message (user or agent) for a job."""
    db = get_db()
    db.chat_messages.insert_one({
        'job_id': job_id,
        'user_id': user_id,
        'role': role,
        'content': content,
        'created_at': _now(),
    })


def get_chat_messages(job_id: str) -> list[dict]:
    """Return all chat messages for a job, sorted by timestamp."""
    db = get_db()
    return list(
        db.chat_messages.find(
            {'job_id': job_id},
            {'_id': 0, 'role': 1, 'content': 1, 'created_at': 1},
        ).sort('created_at', 1)
    )


def get_chat_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have chat messages."""
    db = get_db()
    pipeline = [
        {'$match': {'job_id': {'$in': job_ids}}},
        {'$group': {'_id': '$job_id'}},
    ]
    return {d['_id'] for d in db.chat_messages.aggregate(pipeline)}

