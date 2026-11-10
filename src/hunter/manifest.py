"""Command manifest of the jobhunter service."""

from rayserver.core.commands import Command, Param, Section, ServiceManifest


def manifest() -> ServiceManifest:
    return ServiceManifest(
        service="jobhunter",
        title="JobHunter",
        icon="🎯",
        color="cyan",
        order=46,
        telegram_command="jobhunter",
        telegram_description="Job search and AI-scored matches",
        summary="Job search: tracked companies, scoring criteria, AI analysis against your CV and goal.",
        sections=[
            Section("workflow", "Workflow"),
            Section("companies", "Companies"),
            Section("search_criteria", "Search Criteria"),
            Section("jobs_matches", "Jobs & Matches"),
            Section("ai_analysis", "AI Analysis"),
            Section("run_history", "Run History"),
            Section("profile", "Profile"),
            Section("scheduling", "Scheduling"),
        ],
        commands=[
            Command(
                "run-now",
                "Run the full workflow now: scrape, score, deep-match, AI analysis",
                label="Run Now",
            ),
            Command("top-10", "Top 10 AI-scored jobs from the last run", label="Top 10 Jobs"),
            Command(
                "status", "Service health, configuration and statistics", label="Service Status"
            ),
            Command(
                "list-companies",
                "All tracked companies",
                label="List Companies",
                section="companies",
            ),
            Command(
                "add-company",
                "Add a company to monitor (generic, greenhouse, lever, ashby)",
                label="Add Company",
                section="companies",
                params=[Param("args", kind="rest", hint="Usage: add-company <name> <url>")],
            ),
            Command(
                "remove-company",
                "Remove a company",
                label="Remove Company",
                section="companies",
                params=[Param("args", kind="rest", hint="Usage: remove-company <id>")],
                confirm="The company and its tracked jobs are removed.",
            ),
            Command(
                "list-criteria",
                "All criteria with their ids",
                label="List Criteria",
                section="search_criteria",
            ),
            Command(
                "add-criteria",
                "Create a criterion (simple, group-any, group-all, group-tiered)",
                label="Add Criteria",
                section="search_criteria",
                params=[
                    Param("args", kind="rest", hint="Usage: add-criteria <name> <type> [weight]")
                ],
            ),
            Command(
                "remove-criteria",
                "Remove a criterion by id",
                label="Remove Criteria",
                section="search_criteria",
                params=[Param("args", kind="rest", hint="Usage: remove-criteria <id>")],
                confirm="The criterion is removed from scoring.",
            ),
            Command(
                "test-criteria",
                "Score a sample job text against the criteria",
                label="Test Criteria",
                section="search_criteria",
                params=[Param("args", kind="rest", hint="Usage: test-criteria <job_text>")],
            ),
            Command(
                "score-jobs",
                "Score jobs against the criteria, or full details for one job id",
                label="Score Jobs",
                section="jobs_matches",
            ),
            Command(
                "deep-match",
                "Deep-scrape the matched jobs",
                label="Deep Match",
                section="jobs_matches",
            ),
            Command(
                "list-new-jobs",
                "Newly detected jobs",
                label="List New Jobs",
                section="jobs_matches",
            ),
            Command(
                "list-stale-jobs",
                "Jobs older than N days (default 14)",
                label="List Stale Jobs",
                section="jobs_matches",
            ),
            Command(
                "list-ai-approved",
                "Jobs the AI approved",
                label="List AI Approved",
                section="jobs_matches",
            ),
            Command(
                "analyze-job",
                "AI analysis of one job against your CV, criteria and goal",
                label="Analyze Job",
                section="ai_analysis",
                params=[Param("args", kind="rest", hint="Usage: analyze-job <job_id>")],
            ),
            Command(
                "ai-stats",
                "AI usage and cost statistics (default 7 days)",
                label="AI Statistics",
                section="ai_analysis",
            ),
            Command("history", "Recent runs (default 10)", label="History", section="run_history"),
            Command(
                "last-run",
                "Summary of the most recent completed run",
                label="Last Run",
                section="run_history",
            ),
            Command(
                "run-detail",
                "Full details of one run",
                label="Run Detail",
                section="run_history",
                params=[Param("args", kind="rest", hint="Usage: run-detail <run_id>")],
            ),
            Command(
                "goal",
                "Show the search goal, or set it (it guides the AI matching)",
                label="Goal",
                section="profile",
            ),
            Command(
                "load-cv",
                "Extract your CV from the PDF in data/cv/",
                label="Load CV",
                section="profile",
                params=[Param("args", kind="rest", hint="Usage: load-cv <file_path>")],
            ),
            Command(
                "list-schedules",
                "The scheduled run times",
                label="List Schedules",
                section="scheduling",
            ),
            Command(
                "add-schedule",
                "Add a daily run time",
                label="Add Schedule",
                section="scheduling",
                params=[Param("args", kind="rest", hint='Usage: add-schedule "HH:MM" [timezone]')],
            ),
            Command(
                "remove-schedule",
                "Remove a scheduled run",
                label="Remove Schedule",
                section="scheduling",
                params=[Param("args", kind="rest", hint="Usage: remove-schedule <id>")],
                confirm="The scheduled run is removed.",
            ),
            # not in RobotRay2's web menu
            Command("help", "Every command with examples", menu=False),
            Command(
                "scrape-company",
                "Scrape one company's career page now",
                section="companies",
                label="Scrape Company",
                params=[
                    Param(
                        "id", kind="number", label="Company ID", hint="IDs appear in list-companies"
                    )
                ],
            ),
            Command(
                "add-criteria-value",
                "Add a value to a group criterion",
                section="search_criteria",
                label="Add Criteria Value",
                params=[
                    Param(
                        "details",
                        kind="rest",
                        label="Value",
                        placeholder='salary "200k+" preferred 25',
                        hint="<name> <value> [preferred|acceptable|undesirable] [weight]",
                    )
                ],
            ),
            Command(
                "remove-criteria-value",
                "Remove a value from a group criterion",
                section="search_criteria",
                label="Remove Criteria Value",
                params=[
                    Param(
                        "details",
                        kind="rest",
                        label="Value",
                        placeholder='salary "200k+"',
                        hint="<name> <value>",
                    )
                ],
            ),
        ],
    )
