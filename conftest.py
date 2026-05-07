"""
conftest.py — Project-wide pytest configuration.

Calls django.setup() once before any test module is imported so that
multiple test files calling django.setup() at module level don't trigger
the "populate() isn't reentrant" error.
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
