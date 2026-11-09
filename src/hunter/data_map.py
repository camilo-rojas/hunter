"""
hunter's data, declared to RayServer (`app_data:` in the deployment's
config/rayserver.yaml; RayServer ADR-057 part 3). jobhunter is the owner's
personal workspace: no table holds rows per person, so none is exported or
purged per user; the CV files ride the backups.
"""

DB_PATHS = {"hunter": "data/jobhunter.db"}

TABLE_MAP = [
    ("hunter", t, "shared", None)
    for t in ("companies", "company_credentials", "job_postings", "job_matches", "scrape_history",
              "search_criteria", "run_schedules", "criterion_groups", "criterion_group_values",
              "ai_analysis_log", "run_history")
]

BACKUP_PATHS = ["data/cv/"]
