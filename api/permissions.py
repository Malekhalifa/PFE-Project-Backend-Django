"""
Custom DRF permissions and job ownership helpers.

Matches the Node.js findOwnedJob() pattern:
  - Admins can access any job
  - Regular users can only access their own jobs
"""
from api.mongo import get_job


def find_owned_job(job_id: str, user) -> dict | None:
    """
    Find a job by ID and verify ownership.
    Admins can access any job; regular users can only access their own.

    Returns the job document or None.
    """
    job = get_job(job_id)
    if job is None:
        return None
    if user.role == 'admin':
        return job
    if job.get('user_id') != str(user.id):
        return None
    return job
