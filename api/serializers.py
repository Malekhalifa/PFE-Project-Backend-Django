"""
DRF serializers for request validation.

These only validate incoming request data — responses are plain dicts
(matching the Node.js JSON shapes) returned via Response().
"""
from rest_framework import serializers


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
