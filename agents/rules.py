"""
rules.py — Callable rule loaders for the QualityLab pipeline.

Usage in Workflow / Team:
    from agents.rules import common_rules, domain_rules
    workflow = Workflow(
        dependencies={
            "common_rules": common_rules,
            "domain_rules": domain_rules,
        },
        ...
    )

Each agent then templates {common_rules} etc. into its instructions string.
Per-agent slim domain_rules views ({schema_rules}, {statistical_rules}, etc.)
are injected into session_state by inject_domain_rules in hooks.py.
"""

import os
import yaml
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static rules — built once per process, cached forever.
# Use common_rules.cache_clear() on SIGHUP to hot-reload.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def common_rules() -> str:
    """Tool-usage rules shared by every pipeline agent."""
    return (
        "Use PostgresTools (show_tables, describe_table, run_query, inspect_query) "
        "for all database interaction — never call pg_catalog directly.\n"
        "run_query is read-only by design; never attempt writes through it.\n"
        "Respect user-provided domain_rules as strict business logic constraints."
    )


# ---------------------------------------------------------------------------
# Domain rules — reloaded fresh on every call (no cache), so config changes
# apply without restarting the process.
# ---------------------------------------------------------------------------

def domain_rules() -> dict:
    """
    Per-run, per-tenant business rules.

    Reads from the path in DOMAIN_RULES_PATH env var.
    Defaults to config/domain_rules.yaml relative to the Django project root.

    Returns an empty dict on any read/parse failure so the pipeline still runs.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_path = os.path.join(base_dir, "config", "domain_rules.yaml")
    path = os.environ.get("DOMAIN_RULES_PATH", default_path)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            rules = yaml.safe_load(fh) or {}
        if not isinstance(rules, dict):
            logger.warning("domain_rules.yaml did not parse to a dict — using empty rules.")
            return {}
        return rules
    except FileNotFoundError:
        logger.warning("domain_rules.yaml not found at %s — using empty rules.", path)
        return {}
    except Exception as exc:
        logger.error("Failed to load domain_rules.yaml: %s — using empty rules.", exc)
        return {}
