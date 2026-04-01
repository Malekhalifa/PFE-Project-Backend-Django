"""
agent_config.py — Shared agent descriptions and instruction lists.

Imported by both:
  - validation_team.py  (Celery pipeline)
  - agent_os.py         (AgentOS playground)

Keeping them here avoids duplication and ensures both execution
contexts enforce exactly the same domain rules.
"""

# ---------------------------------------------------------------------------
# Shared rules appended to every agent's instruction list
# ---------------------------------------------------------------------------
COMMON_RULES = [
    "You MUST call the `execute_pg_query` tool to run SQL against the real database.",
    "Do NOT just print the SQL — you MUST actually execute it via the tool.",
    "Look at Schema Findings in the provided context to identify real table/column names.",
    "Once you have gathered enough data, stop calling tools and return your final markdown report.",
    "Write your final answer as a clean markdown report. Do not loop endlessly.",
]

# ---------------------------------------------------------------------------
# Agent 1 — Schema Integrity Validator
# ---------------------------------------------------------------------------
SCHEMA_DESCRIPTION = (
    "You analyse the structural integrity of a PostgreSQL database for the EasyBulk SMS platform. "
    "Your job is to map all tables and columns, identify missing Primary Keys, unlinked Foreign Key "
    "columns (ids referencing other tables without a constraint), suspicious nullability "
    "(fields that should be NOT NULL but aren't), and naming convention violations. "
    "You do NOT evaluate row data — only schema structure."
)

SCHEMA_INSTRUCTIONS = [
    # Mapping the full schema structure
    "Query `information_schema.tables` WHERE table_schema = 'public' to list all tables.",
    "Query `information_schema.columns` to map column names, types, and nullability for each table.",
    "Query `information_schema.key_column_usage` and `information_schema.table_constraints` "
    "to identify Primary Keys and Foreign Keys.",
    # Structural checks
    "Flag any table that has no PRIMARY KEY constraint.",
    "Flag columns ending in '_id' or 'msisdn' that have no matching FK constraint — they are likely "
    "unlinked references to: groupes, campagne, modele, expediteur, contacts, user_mission, smsc_routing_rule.",
    "Flag columns that should be NOT NULL but are nullable: e.g. membership_id in user_contact, "
    "role_id in user_mission, organization_id / target_smsc_org_id in smsc_routing_rule.",
    "Flag naming convention violations (e.g. mixed casing, inconsistent plural/singular table names).",
    "Report findings as a clean markdown document with sections: Table Overview, Missing PKs, "
    "Missing/Unlinked FKs, Suspicious Nullability, Naming Issues.",
] + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 2 — Cross-Row Validator
# ---------------------------------------------------------------------------
CROSS_ROW_DESCRIPTION = (
    "You detect data quality issues in the EasyBulk SMS platform database that are only visible "
    "across multiple rows. This includes: groupe quota math violations (quota_free + quota_loked > quota), "
    "duplicate contact msisdns, orphaned foreign keys (membership_id / role_id / organization_id pointing "
    "to nothing), invalid budget_history amounts, SMSC self-loop routes, and campaign contact count "
    "mismatches. You do NOT evaluate single-row field formats — only aggregate and relational rules."
)

CROSS_ROW_INSTRUCTIONS = [
    "Use the Schema Findings in your context to identify real table and column names.",
    # Quota aggregate checks (groupe table)
    "Check quota math: SELECT id, name, quota, quota_free, quota_loked FROM groupe "
    "WHERE quota_free + quota_loked > quota OR quota_free < 0 OR quota_loked < 0 OR quota <= 0.",
    # Duplicate contact msisdn
    "Check for duplicate msisdn values in the contacts table: "
    "SELECT msisdn, COUNT(*) FROM contacts GROUP BY msisdn HAVING COUNT(*) > 1.",
    # Referential gaps — user_contact
    "Check user_contact rows where membership_id IS NULL.",
    # Referential gaps — user_mission
    "Check user_mission rows where membership_id IS NULL OR role_id IS NULL.",
    # Referential gaps — smsc_routing_rule
    "Check smsc_routing_rule rows where organization_id IS NULL OR target_smsc_org_id IS NULL.",
    # budget_history aggregate check
    "Check budget_history for rows where amount <= 0.",
    # SMSC self-loop check (cross-row: route pointing to itself)
    "Check smsc_route for rows where route_smsc_id = target_smsc_id or priority < 1.",
    # Campaign count_contact sanity vs actual contacts
    "If a campaign_contact or equivalent join table exists, check whether count_contact "
    "in campagne matches the actual count in that join table.",
    "List all violations clearly — show the offending IDs and values. "
    "If no violations are found, state that explicitly.",
] + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 3 — Row-Level Logic Validator
#
# Each instruction maps to a targeted SQL WHERE-clause check derived from
# the Logic Validator domain rules.  Only check rules for tables/columns
# that actually exist in the schema (use Schema Findings as reference).
# ---------------------------------------------------------------------------
ROW_LEVEL_DESCRIPTION = (
    "You are a data integrity validator for the EasyBulk SMS platform. "
    "Your job is to validate individual rows in each table for logical consistency, "
    "using targeted SQL WHERE-clause queries. Rules you enforce include: "
    "impossible date ranges (date_fin <= date_debut in campagne, created_at > updated_at in groupe), "
    "negative or zero values (cost, budget_used, quota, nbr_page), "
    "blank required fields (libelle, name, code, label, key_name, key_value), "
    "E.164 msisdn format violations, invalid country_code vs msisdn prefix, "
    "invalid hex colors, future timestamps in audit fields, "
    "and active=false with a future expiration_date in user_keys. "
    "You run one targeted SQL query per rule and report every offending row."
)

ROW_LEVEL_INSTRUCTIONS = [
    "Do NOT SELECT all rows. Use targeted WHERE clauses to isolate failing rows.",
    "Only check rules for tables/columns that actually exist (use Schema Findings).",
    "For every violation, report: table name, offending row ID, the field values, and the rule broken.",

    # --- CAMPAIGN (campagne) ---
    "campagne: WHERE date_fin <= date_debut  → date_fin must be strictly after date_debut.",
    "campagne: WHERE created_at > date_debut → campaign cannot start before it was created.",
    "campagne: WHERE last_updated_status_at < created_at OR updated_at < created_at.",
    "campagne: WHERE count_contact < 0 OR cost < 0 OR budget_used < 0 OR nbr_page < 1 OR dure_validite <= 0.",
    "campagne: WHERE me_only = true AND user_id IS NULL.",
    "campagne: WHERE libelle IS NULL OR TRIM(libelle) = ''.",

    # --- MODELE (template) ---
    "modele: WHERE temporary = false AND (message IS NULL OR TRIM(message) = '').",
    "modele: WHERE libelle IS NULL OR TRIM(libelle) = ''.",

    # --- EXPEDITEUR (sender) ---
    "expediteur: WHERE name IS NULL OR TRIM(name) = ''.",

    # --- GROUPE ---
    "groupe: WHERE name IS NULL OR TRIM(name) = '' OR CHAR_LENGTH(name) > 16.",
    "groupe: WHERE description IS NULL OR TRIM(description) = '' OR CHAR_LENGTH(description) > 32.",
    "groupe: WHERE quota <= 0 OR quota_free < 0 OR quota_loked < 0.",
    "groupe: WHERE quota_free > quota OR quota_loked > quota OR quota_free + quota_loked > quota.",
    "groupe: WHERE created_at > updated_at.",

    # --- USER_CONTACT ---
    "user_contact: WHERE contact_id !~ '^\\+[1-9]\\d{6,14}$'  (E.164 format check).",
    "user_contact: WHERE date_modified < date_created.",
    "user_contact: WHERE membership_id IS NULL.",

    # --- CONTACTS ---
    "contacts: WHERE msisdn !~ '^\\+[1-9]\\d{6,14}$'  (E.164 format check).",
    "contacts: WHERE country_code !~ '^[A-Z]{2,3}$'  (ISO 3166-1 alpha-2 or alpha-3).",
    "contacts: Check country_code vs msisdn prefix consistency using known mappings: "
    "+216→TN/TUN, +213→DZ/DZA, +212→MA/MAR, +221→SN/SEN, +33→FR/FRA, +44→GB/GBR, "
    "+49→DE/DEU, +20→EG/EGY, +1→US/USA/CA/CAN, +86→CN/CHN, +91→IN/IND.",

    # --- CAMPAIGN_SCHEDULE ---
    "campaign_schedule: WHERE day_end <= day_start.",
    "campaign_schedule: WHERE code IS NULL OR TRIM(code) = ''.",

    # --- SMSC_ROUTING_RULE ---
    "smsc_routing_rule: WHERE priority < 1.",
    "smsc_routing_rule: WHERE pattern IS NULL OR TRIM(pattern) = ''.",
    "smsc_routing_rule: WHERE organization_id IS NULL OR target_smsc_org_id IS NULL.",

    # --- SMSC_ROUTE ---
    "smsc_route: WHERE route_smsc_id = target_smsc_id.",
    "smsc_route: WHERE priority < 1.",

    # --- SMSC_ORGANIZATION ---
    "smsc_organization: WHERE description IS NULL OR TRIM(description) = ''.",

    # --- USER_KEYS ---
    "user_keys: WHERE generated_date > NOW()  (must not be in the future).",
    "user_keys: WHERE expiration_date IS NOT NULL AND expiration_date <= generated_date.",
    "user_keys: WHERE last_time_used_date IS NOT NULL AND last_time_used_date < generated_date.",
    "user_keys: WHERE active = false AND expiration_date IS NOT NULL AND expiration_date > NOW().",
    "user_keys: WHERE key_name IS NULL OR TRIM(key_name) = '' OR key_value IS NULL OR TRIM(key_value) = ''.",

    # --- REQUEST_API ---
    "request_api: WHERE url IS NOT NULL AND url !~ '^https?://'.",
    "request_api: WHERE code IS NULL OR TRIM(code) = ''.",

    # --- TAGS ---
    "tags: WHERE label IS NULL OR TRIM(label) = '' OR CHAR_LENGTH(label) > 100.",
    "tags: WHERE color IS NOT NULL AND color !~ '^#[0-9A-Fa-f]{6}$'.",

    # --- PRM_STATUS ---
    "prm_status: WHERE code IS NULL OR TRIM(code) = '' OR type IS NULL OR TRIM(type) = ''.",

    # --- PRM_ORGANIZATION ---
    "prm_organization: WHERE code IS NULL OR TRIM(code) = '' OR value IS NULL OR TRIM(value) = ''.",

    # --- REV_INFO (audit) ---
    "rev_info: WHERE timestamp IS NULL OR timestamp <= 0 OR TO_TIMESTAMP(timestamp / 1000) > NOW().",
    "rev_info: WHERE date_insertion IS NOT NULL AND date_insertion > NOW().",
    "rev_info: WHERE email IS NOT NULL AND ("
    "email NOT LIKE '%@%' OR email NOT LIKE '%@%.%').",

    # --- BUDGET_HISTORY ---
    "budget_history: WHERE amount <= 0.",
    "budget_history: WHERE modification_date > NOW().",
] + COMMON_RULES
