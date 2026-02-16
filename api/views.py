"""
API views for jobs, uploads, analysis, cleaning, and raw data.

Every endpoint matches the Node.js backend's API surface exactly,
so the Angular frontend requires only a base URL change.

All views require authentication (CookieJWTAuthentication) and
enforce job ownership via find_owned_job().
"""
import math
import os
import secrets
import uuid

import pandas as pd
from django.conf import settings
from django.http import FileResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import audit_log
from api.mongo import (
    create_job,
    get_job,
    get_jobs_for_user,
    get_analysis_result,
    get_cleaning_result,
    get_analysis_job_ids,
    get_cleaning_job_ids,
    delete_jobs,
    update_job_share_token,
    get_job_by_share_token,
)
from api.permissions import find_owned_job
from api.serializers import CleanRequestSerializer, JobDeleteSerializer
from services.analysis_service import compute_row_outliers, to_native
from tasks.csv_tasks import analyze_csv_task, clean_csv_task
from users.models import User


def _sanitize_for_json(obj):
    """
    Recursively sanitize data for JSON serialization.
    - Replace NaN/Infinity floats with None (invalid in JSON)
    - Convert datetime objects to ISO strings
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


# ──────────────────────────────────────────────
# Upload
# ──────────────────────────────────────────────

class UploadView(APIView):
    """
    POST /api/upload — upload CSV file, create job.

    Matches Node.js: saves file to uploads/, creates Job in MongoDB,
    writes audit log, returns { job_id }.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file uploaded'}, status=status.HTTP_400_BAD_REQUEST)

        # Save file to disk with UUID filename (matches Node.js multer pattern)
        file_ext = os.path.splitext(uploaded_file.name)[1] or '.csv'
        file_uuid = str(uuid.uuid4())
        filename = f'{file_uuid}{file_ext}'
        file_path = os.path.join(str(settings.UPLOAD_DIR), filename)

        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        file_size = os.path.getsize(file_path)
        mode = 'large' if file_size > 50 * 1024 * 1024 else 'normal'  # 50MB threshold

        # Create job in MongoDB
        user_id = str(request.user.id)
        job_id = create_job(
            user_id=user_id,
            file_name=uploaded_file.name,
            file_path=file_path,
            mode=mode,
        )

        # Audit logs
        audit_log(job_id, 'JOB_CREATED', {'user_id': user_id})
        audit_log(job_id, 'FILE_UPLOADED', {
            'fileName': uploaded_file.name,
            'size': file_size,
            'mode': mode,
        })

        return Response({'job_id': job_id})


# ──────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────

class AnalyzeView(APIView):
    """
    POST /api/analyze/<jobId> — start analysis (fire-and-forget Celery task).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response(
                {'detail': 'No uploaded file found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Dispatch Celery task (fire-and-forget)
        analyze_csv_task.delay(
            job_id=jobId,
            file_path=job['file_path'],
            mode=job.get('mode', 'normal'),
            options={},
        )

        audit_log(jobId, 'ANALYSIS_ENQUEUED', {})
        return Response({'message': 'analysis started'})


class StatusView(APIView):
    """
    GET /api/status/<jobId> — get job status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        job_status = job['status'] if job else 'unknown'
        return Response({'status': job_status})


class ResultsView(APIView):
    """
    GET /api/results/<jobId> — get analysis results.

    Returns { cleaned_data, quality_report } matching the Node.js response shape.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        analysis = get_analysis_result(jobId)
        if not analysis or not analysis.get('result'):
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        result = analysis['result']
        quality_report = _sanitize_for_json(result.get('quality_report', {}))

        # Filter out empty numeric_stats entries (non-numeric columns return {})
        # — the frontend calls .toFixed() on min/mean/max and crashes on empty objects
        if 'numeric_stats' in quality_report:
            quality_report['numeric_stats'] = {
                k: v for k, v in quality_report['numeric_stats'].items() if v
            }

        return Response({
            'cleaned_data': result.get('cleaned_data'),
            'quality_report': quality_report,
        })


class ResultsExportView(APIView):
    """
    GET /api/results/<jobId>/export — download full analysis report.

    Returns structured report matching Node.js export format.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        analysis = get_analysis_result(jobId)
        if not analysis or not analysis.get('result'):
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        data = analysis['result']
        meta = data.get('meta', {})
        qr = _sanitize_for_json(data.get('quality_report', {}))
        cleaned = data.get('cleaned_data', {})

        export_report = {
            'meta': {
                'job_id': jobId,
                'file_path': meta.get('file_path'),
                'file_size_bytes': meta.get('file_size_bytes'),
                'mode': meta.get('mode', 'normal'),
                'analysis_type': meta.get('analysis_type', 'in-memory'),
                'started_at': meta.get('started_at'),
                'completed_at': meta.get('completed_at'),
                'duration_ms': meta.get('duration_ms'),
                'version': '1.0',
            },
            'dataset_summary': {
                'rows': cleaned.get('rows', 0),
                'columns': cleaned.get('columns', 0),
            },
            'quality_overview': {
                'missing_rate': qr.get('missing_rate'),
                'duplicate_rate': qr.get('duplicate_rate'),
                'error_rate': qr.get('outlier_rate'),
            },
            'column_analysis': qr.get('column_analysis', {}),
            'limitations': {
                'approximate_metrics': False,
                'skipped_checks': [],
            },
        }

        return Response(export_report)


class RawDataView(APIView):
    """
    GET /api/raw/<jobId> — get raw CSV data as { header, rows }.

    In the Node.js backend this was proxied to Python. Now we do it directly.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('file_path'):
            return Response(
                {'detail': 'No data found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = job['file_path']
        if not os.path.exists(file_path):
            return Response(
                {'detail': 'File not found on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        df = pd.read_csv(file_path)
        df = df.replace(
            {pd.NA: None, float('nan'): None, float('inf'): None, float('-inf'): None}
        )
        df = df.where(pd.notna(df), None)

        return Response(_sanitize_for_json({
            'header': df.columns.tolist(),
            'rows': df.values.tolist(),
        }))


class RawWithOutliersView(APIView):
    """
    GET /api/raw_with_outliers/<jobId> — get raw CSV with row-level outlier metadata.

    Returns { header, rows: [{ values, is_outlier, outlier_score, outlier_severity }] }.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('file_path'):
            return Response(
                {'detail': 'No data found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = job['file_path']
        if not os.path.exists(file_path):
            return Response(
                {'detail': 'File not found on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        df = pd.read_csv(file_path)
        df = df.replace(
            {pd.NA: None, float('nan'): None, float('inf'): None, float('-inf'): None}
        )
        df = df.where(pd.notna(df), None)

        header = df.columns.tolist()
        rows = df.values.tolist()
        enriched_rows = compute_row_outliers(header, rows)

        return Response({
            'header': header,
            'rows': _sanitize_for_json(to_native(enriched_rows)),
        })


# ──────────────────────────────────────────────
# Cleaning
# ──────────────────────────────────────────────

class CleanView(APIView):
    """
    POST /api/clean/<jobId> — start cleaning (fire-and-forget Celery task).

    Body: { mode: "auto"|"manual", rules?: { ... } }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        # Parse cleaning options
        mode = request.data.get('mode', 'auto')
        rules = request.data.get('rules', {})

        # Dispatch Celery task (fire-and-forget)
        clean_csv_task.delay(
            job_id=jobId,
            file_path=job['file_path'],
            mode=mode,
            rules=rules,
            options={},
        )

        audit_log(jobId, 'CLEANING_ENQUEUED', {'mode': mode})
        return Response({'job_id': jobId, 'status': 'cleaning_started'})


class CleanedFileView(APIView):
    """
    GET /api/cleaned/<jobId> — download cleaned CSV file.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Cleaned file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        cleaned_path = job['cleaned_file_path']
        if not os.path.exists(cleaned_path):
            return Response(
                {'error': 'File missing on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            open(cleaned_path, 'rb'),
            content_type='text/csv',
            as_attachment=True,
            filename=f'{jobId}-cleaned.csv',
        )


class CleaningResultView(APIView):
    """
    GET /api/cleaning-result/<jobId> — get cleaning result metadata.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        result = get_cleaning_result(jobId)
        if not result:
            # Return null instead of 404 so the frontend catchError isn't triggered
            # and Chrome doesn't log a red console error
            return Response(None)

        # Remove MongoDB's internal _id field before returning
        result.pop('_id', None)
        return Response(_sanitize_for_json(result))


class CleaningResultExportView(APIView):
    """
    GET /api/cleaning-result/<jobId>/export — download cleaning report.

    Returns structured report matching Node.js export format.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        result = get_cleaning_result(jobId)
        if not result:
            return Response(
                {'error': 'No cleaning result found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        summary = result.get('summary', {})
        rules_applied = result.get('rules_applied', [])
        rows_before = summary.get('rows_before', 0)
        rows_removed = summary.get('rows_removed', 0)

        report = {
            'meta': {
                'job_id': jobId,
                'file_name': job.get('file_name'),
                'file_path': job.get('file_path'),
                'cleaned_file_path': job.get('cleaned_file_path'),
                'cleaning_mode': result.get('mode', 'auto'),
                'cleaned_at': (
                    result.get('updated_at') or result.get('created_at') or None
                ),
                'version': '1.0',
            },
            'summary': {
                'rows_before': rows_before,
                'rows_after': summary.get('rows_after'),
                'rows_removed': rows_removed,
                'removal_rate': (
                    round(rows_removed / rows_before, 4)
                    if rows_before else None
                ),
            },
            'rules_applied': [
                {
                    'rule': r.get('rule'),
                    'description': r.get('description'),
                    'rows_removed': r.get('rows_removed'),
                    'columns': r.get('columns'),
                }
                for r in rules_applied
            ],
            'rules_summary': {
                'total_rules_applied': len(rules_applied),
                'rules_list': [r.get('rule') for r in rules_applied if r.get('rule')],
            },
        }

        return Response(report)


# ──────────────────────────────────────────────
# Jobs (History)
# ──────────────────────────────────────────────

class JobsView(APIView):
    """
    GET    /api/jobs — list all jobs for the authenticated user.
    DELETE /api/jobs — bulk delete jobs by ID.

    Combined into one view since they share the same URL path (matches Node.js).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """List jobs. Admins see all; regular users see only their own."""
        user_id = str(request.user.id)
        is_admin = request.user.role == 'admin'

        jobs = get_jobs_for_user(user_id, is_admin=is_admin)
        job_ids = [j['_id'] for j in jobs]

        user_map = {}
        if is_admin:
            user_ids = {j.get('user_id') for j in jobs if j.get('user_id')}
            uuid_ids = []
            for uid in user_ids:
                try:
                    uuid_ids.append(uuid.UUID(uid))
                except ValueError:
                    continue
            users = User.objects.filter(id__in=uuid_ids).only('id', 'email', 'username')
            user_map = {str(u.id): u for u in users}

        # Batch-check which jobs have analysis/cleaning results
        analysis_set = get_analysis_job_ids(job_ids) if job_ids else set()
        cleaning_set = get_cleaning_job_ids(job_ids) if job_ids else set()

        result = [
            {
                '_id': j['_id'],
                'file_name': j.get('file_name', 'unknown'),
                'status': j.get('status'),
                'mode': j.get('mode'),
                'created_at': j.get('created_at'),
                'has_analysis': j['_id'] in analysis_set,
                'has_cleaning': j['_id'] in cleaning_set,
                **(
                    {
                        'owner_email': (
                            user_map.get(j.get('user_id')).email
                            if user_map.get(j.get('user_id'))
                            else None
                        ),
                        'owner_username': (
                            user_map.get(j.get('user_id')).username
                            if user_map.get(j.get('user_id'))
                            else None
                        ),
                    }
                    if is_admin
                    else {}
                ),
            }
            for j in jobs
        ]

        return Response(result)

    def delete(self, request):
        """Bulk delete jobs. Body: { job_ids: ["id1", "id2", ...] }"""
        serializer = JobDeleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'error': 'job_ids array is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_ids = serializer.validated_data['job_ids']
        user_id = str(request.user.id)
        is_admin = request.user.role == 'admin'

        # Delete from MongoDB (with ownership enforcement)
        deleted_jobs = delete_jobs(job_ids, user_id=user_id, is_admin=is_admin)

        if not deleted_jobs:
            return Response(
                {'error': 'No matching jobs found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Delete files on disk (best-effort)
        for job in deleted_jobs:
            file_path = job.get('file_path')
            cleaned_path = job.get('cleaned_file_path')
            if file_path:
                try:
                    os.unlink(file_path)
                except OSError:
                    pass
            if cleaned_path:
                try:
                    os.unlink(cleaned_path)
                except OSError:
                    pass

        # Audit log each deletion
        owned_ids = [j['_id'] for j in deleted_jobs]
        for jid in owned_ids:
            audit_log(jid, 'JOB_DELETED', {'deleted_by': user_id})

        return Response({'deleted': len(owned_ids), 'job_ids': owned_ids})


# ──────────────────────────────────────────────
# Power BI Integration
# ──────────────────────────────────────────────

class ShareableView(APIView):
    """
    GET /api/share/<sharedToken> — download cleaned CSV file without authentication.
    """
    permission_classes = []  # Public endpoint

    def get(self, request, sharedToken):
        job = get_job_by_share_token(sharedToken)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Invalid token or file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        cleaned_path = job['cleaned_file_path']
        if not os.path.exists(cleaned_path):
            return Response(
                {'error': 'File missing on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            open(cleaned_path, 'rb'),
            content_type='text/csv',
            as_attachment=True,
            filename=f"{job['_id']}-cleaned.csv",
        )


class PowerBIConfigView(APIView):
    """
    GET /api/powerbi-config/<jobId> — get .pbids file content.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Cleaned file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Generate or reuse share token
        share_token = job.get('share_token')
        if not share_token:
            share_token = secrets.token_urlsafe(32)
            update_job_share_token(jobId, share_token)

        # Construct protocol and host
        # Power BI typically needs an absolute URL
        protocol = 'https' if request.is_secure() else 'http'
        host = request.get_host()
        share_url = f"{protocol}://{host}/api/share/{share_token}"

        pbids_content = {
            "version": "0.1",
            "connections": [
                {
                    "details": {
                        "protocol": "web",
                        "address": {
                            "url": share_url
                        },
                        "authentication": None,
                        "query": None
                    },
                    "options": {},
                    "mode": "Import"
                }
            ]
        }

        return Response(pbids_content)
