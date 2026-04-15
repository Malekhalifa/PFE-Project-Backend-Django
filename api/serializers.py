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


class CleaningRulesSerializer(serializers.Serializer):
    remove_duplicates = serializers.BooleanField(default=True)
    handle_missing = serializers.ChoiceField(
        choices=['drop', 'mean', 'median', 'mode'],
        default='drop',
    )
    outliers = serializers.ChoiceField(
        choices=['remove', 'cap', 'ignore'],
        default='ignore',
    )
    normalize = serializers.BooleanField(default=False)


class CleanRequestSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=['auto', 'manual'], default='auto')
    rules = CleaningRulesSerializer(required=False)


class JobDeleteSerializer(serializers.Serializer):
    job_ids = serializers.ListField(
        child=serializers.CharField(),
        min_length=1,
    )


class AnalyzePostgresSerializer(serializers.Serializer):
    connection_string = serializers.CharField(required=True, allow_blank=False)
    domain_rules = serializers.ListField(required=False, default=list)
    llm_model = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=128,
        default='gemini-2.0-flash',
        help_text=_LLM_MODEL_HINT,
    )

    def validate_llm_model(self, value: str) -> str:
        model_id = (value or '').strip()
        if not model_id:
            raise serializers.ValidationError(f"llm_model cannot be empty. {_LLM_MODEL_HINT}")

        if not re.match(r'^[A-Za-z0-9._:-]+$', model_id):
            raise serializers.ValidationError(
                f"llm_model contains invalid characters. Use letters, numbers, '.', '_', '-', ':' only. {_LLM_MODEL_HINT}"
            )

        return model_id
