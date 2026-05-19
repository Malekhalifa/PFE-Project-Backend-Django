"""
DRF serializers for request validation.

These only validate incoming request data — responses are plain dicts
(matching the Node.js JSON shapes) returned via Response().
"""
import re

from rest_framework import serializers


_SUGGESTED_LLM_MODELS = (
    'gemini-2.0-flash-lite',
    'gemini-2.0-flash',
    'gemini-1.5-flash',
    'gemini-1.5-flash-8b',
    'gemini-1.5-pro',
    'gemini-2.5-flash',
    'gemini-2.5-pro',
)
_LLM_MODEL_HINT = f"Suggested models: {', '.join(_SUGGESTED_LLM_MODELS)}"


class JobDeleteSerializer(serializers.Serializer):
    job_ids = serializers.ListField(
        child=serializers.CharField(),
        min_length=1,
    )


class AnalyzePostgresSerializer(serializers.Serializer):
    connection_string = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    domain_rules = serializers.ListField(required=False, default=list)
    llm_model = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=128,
        default='gemini-2.0-flash',
        help_text=_LLM_MODEL_HINT,
    )
    baseline_job_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get('connection_string') and not attrs.get('baseline_job_id'):
            raise serializers.ValidationError("Either connection_string or baseline_job_id must be provided.")
        return attrs

    def validate_llm_model(self, value: str) -> str:
        model_id = (value or '').strip()
        if not model_id:
            raise serializers.ValidationError(f"llm_model cannot be empty. {_LLM_MODEL_HINT}")

        if not re.match(r'^[A-Za-z0-9._:-]+$', model_id):
            raise serializers.ValidationError(
                f"llm_model contains invalid characters. Use letters, numbers, '.', '_', '-', ':' only. {_LLM_MODEL_HINT}"
            )

        return model_id


class ChatMessageCreateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=['user', 'agent'])
    content = serializers.CharField(required=True, allow_blank=False, max_length=60000)

    def validate_content(self, value: str) -> str:
        normalized = (value or '').strip()
        if not normalized:
            raise serializers.ValidationError('content cannot be empty')
        return normalized
