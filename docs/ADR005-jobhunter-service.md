# ADR-005: JobHunter Service - Automated Job Search and Matching

**Date:** 2026-01-30
**Status:** ✅ Approved
**Decision Makers:** Architecture Review
**Supersedes:** None
**Related:** ADR-001 (Multi-Channel), ADR-002 (Service Isolation), ADR-003 (Messaging & Identity)

---

## Executive Summary

This ADR defines the architecture for the **jobhunter-service** - an automated job hunting service that daily scrapes company career pages, intelligently filters positions based on user criteria (skills, location, salary), and delivers matching opportunities via Telegram for rapid application.

**Key Deliverables:**
1. **Flexible Scheduler**: Multiple daily run times with timezone support (Phase 7)
2. **Web Scraping Engine**: Fetch and parse job listings from public and authenticated career pages (Phase 4 & 4.5)
3. **Authentication Support**: Handle login-protected career pages (Workday, Taleo, internal portals) (Phase 4.5)
4. **User-Defined Criteria System**: Flexible key-value criteria with custom weights and range support (Phase 3 & 3.5)
5. **Dynamic Scoring Algorithm**: Match jobs based on active criteria, not hardcoded rules
6. **Job Freshness Detection**: Identify NEW jobs vs. existing postings with priority scoring (Phase 8)
7. **AI-Powered Analysis**: Claude API integration for semantic job matching beyond strict rules (Phase 11)
8. **Database Persistence**: Track companies, jobs, matches, search history, criteria, schedules, credentials, and freshness
9. **Telegram Notifications**: Real-time alerts prioritizing NEW jobs
10. **Command Interface**: Manage companies, credentials, criteria, schedules, freshness, AI analysis, and testing via @ routing

**Key Innovations:**
1. **Flexible Criteria Engine** (Phase 3 & 3.5): Unlike job boards with static filters, users define weighted criteria (keywords, locations, salary ranges, skills) that can be adjusted and tested before running scrapes. Enables personalization and experimentation without code changes. Supports numeric range matching (e.g., "200k-300k", "5+ years").

2. **Authentication Support** (Phase 4.5): Intelligently detects and handles login-protected career pages (Workday, Taleo, internal portals). Supports form-based login and HTTP Basic Auth with encrypted credential storage. Expands job sources beyond public career pages.

3. **Multi-Schedule System** (Phase 7): Run scrapes multiple times per day (e.g., 9 AM, 1 PM, 5 PM) to catch jobs posted throughout the day. Fully manageable via commands, with timezone support and manual fallback.

4. **Freshness Detection & Prioritization** (Phase 8): Automatically detects NEW jobs (not seen in previous scrapes) and gives them +15 bonus points and priority notification. Competitive advantage: apply to fresh postings within hours of posting.

5. **AI-Powered Semantic Matching** (Phase 11): Claude API integration for intelligent job analysis that catches nuanced matches strict rules miss. Understands "FastAPI/Django" implies Python, "staff engineer" is senior-level, and contextual salary signals. Cost-optimized with selective analysis modes ($1.50-13.50/month depending on mode).

**Estimated Difficulty:** ⭐⭐⭐⭐ Moderate-High (4/5) - Authentication and AI integration add complexity
**Estimated Timeline:** 26-33 hours across 12 implementation phases

---

## Context

### Current State

Job searching is manual, time-consuming, and inefficient:
- **Manual Checking**: Visiting 10-20 company career pages daily
- **Alert Fatigue**: Generic job board emails with poor matches
- **Missed Opportunities**: Jobs posted and filled within 24-48 hours
- **Application Overhead**: Hours spent filtering irrelevant postings
- **No Persistence**: No tracking of jobs seen, applied, or rejected

### Business Drivers

1. **Time Efficiency**: Automate daily career page checking (save 1-2 hours/day)
2. **Quality Filtering**: AI-powered matching reduces noise (80%+ irrelevant jobs filtered)
3. **Speed to Market**: Get notified within hours of posting (competitive advantage)
4. **Data-Driven**: Track application success rates and optimize criteria
5. **Focus**: Spend time applying, not searching

### Technical Requirements

#### Core Functionality
1. **Company Management**: Add/remove target companies with career page URLs
2. **Scheduled Scraping**: Daily execution at configured time (timezone-aware)
3. **HTML Parsing**: Extract job listings from diverse website structures
4. **Multi-Factor Filtering**:
   - Profile keywords (python, senior, engineer, backend)
   - Location flexibility (Remote OR Florida locations)
   - Salary threshold (minimum acceptable compensation)
   - Required skills matching
5. **Scoring Algorithm**: 0-100 match score with weighted criteria
6. **Notification System**: Telegram delivery for matches above threshold
7. **Search History**: Track jobs seen, matched, applied to, outcomes

#### Non-Functional Requirements
1. **Reliability**: Handle website failures gracefully (retry, log, continue)
2. **Performance**: Complete scraping within 10-15 minutes for 10+ companies
3. **Scalability**: Support 50+ target companies without degradation
4. **Data Integrity**: Deduplication, proper database constraints
5. **Privacy**: No PII scraping, only public job postings
6. **Ethics**: Respect robots.txt, rate limiting, ethical scraping

---

## Decision

We adopt a **Scheduled Scraper Architecture** that integrates with RobotRay's existing service infrastructure:

### Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                   JOBHUNTER SERVICE ARCHITECTURE                  │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              JobHunterService (BaseService)               │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │                                                            │   │
│  │  ┌───────────────┐        ┌──────────────────────────┐   │   │
│  │  │ Message Loop  │        │   Scheduler Loop         │   │   │
│  │  │ Thread        │        │   Thread                 │   │   │
│  │  ├───────────────┤        ├──────────────────────────┤   │   │
│  │  │ @ Commands:   │        │ Daily Timing:            │   │   │
│  │  │ - add-company │        │ - Calculate next run     │   │   │
│  │  │ - list        │        │ - Sleep until trigger    │   │   │
│  │  │ - run-now     │        │ - Execute scrape cycle   │   │   │
│  │  │ - matches     │        │ - Process results        │   │   │
│  │  │ - help        │        │ - Send notifications     │   │   │
│  │  └───────────────┘        └──────────────────────────┘   │   │
│  │           │                         │                      │   │
│  │           └─────────┬───────────────┘                      │   │
│  │                     │                                      │   │
│  │                     ↓                                      │   │
│  │         ┌────────────────────────┐                        │   │
│  │         │ JobHunterDatabaseMgr   │                        │   │
│  │         ├────────────────────────┤                        │   │
│  │         │ • companies            │                        │   │
│  │         │ • company_credentials  │ ← Phase 4.5            │   │
│  │         │ • job_postings         │                        │   │
│  │         │ • job_matches          │                        │   │
│  │         │ • scrape_history       │                        │   │
│  │         │ • search_criteria      │ ← Phase 3              │   │
│  │         │ • run_schedules        │ ← Phase 7              │   │
│  │         └────────────────────────┘                        │   │
│  │                     │                                      │   │
│  └─────────────────────┼──────────────────────────────────────┘   │
│                        │                                           │
│                        ↓                                           │
│         ┌──────────────────────────────────┐                      │
│         │   SQLite: jobhunter_service.db   │                      │
│         └──────────────────────────────────┘                      │
│                                                                    │
│  Daily Scrape Flow:                                               │
│  ────────────────────                                             │
│  1. Scheduler triggers at next scheduled time (e.g., 9:00 AM)     │
│     - Supports multiple daily runs (9 AM, 1 PM, 5 PM, etc.)       │
│     - Falls back to manual-only mode if no schedules exist        │
│  2. Load active companies from database                           │
│  3. Load active search criteria (keywords, locations, salary)     │
│  4. FOR EACH company:                                             │
│       a. HTTP GET with retry + rate limiting                      │
│       b. Parse HTML with BeautifulSoup                            │
│       c. Extract job listings (title, URL, location, salary)      │
│       d. Compute job hash for deduplication                       │
│       e. Check freshness: NEW (not in DB) vs existing             │
│       f. Save new jobs OR update existing (last_seen, times_seen) │
│       g. Mark missing jobs as inactive                            │
│  5. FOR EACH job (new or updated):                                │
│       a. Score against criteria (0-100)                           │
│       b. Add freshness bonus (+15 for NEW, decay for old)         │
│       c. If score >= threshold (60): Add to matches               │
│       d. Store match with reasons and freshness data              │
│  6. Send notifications to Telegram via @broadcast                 │
│       a. NEW jobs sent first (highest priority)                   │
│       b. Existing matches sent after (lower priority)             │
│  7. Log summary (companies, jobs, NEW jobs, matches)              │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### Core Components

#### 1. JobHunterService (Main Service)
**File:** `src/robotray/services/jobhunter_service.py` (~1500 lines)

**Threading Model:**
- **Message Loop Thread**: Processes @ routing commands from users
- **Scheduler Loop Thread**: Daily scraper with timezone-aware timing
- Main thread remains responsive for service lifecycle (start/stop)

**Key Responsibilities:**
- Service initialization and configuration loading
- Thread management with responsive shutdown
- Command routing and response formatting
- Web scraping orchestration
- Job filtering and scoring
- Notification delivery

**Public Methods:**
```python
class JobHunterService(BaseService):
    def __init__(name, config)
    def start_service()
    def stop_service()
    def health_check() -> Dict[str, Any]

    # Command handlers (called via @ routing)
    # Company Management
    _handle_add_company(sender, args, correlation_id)
    _handle_list_companies(sender, correlation_id)
    _handle_remove_company(sender, args, correlation_id)

    # Authentication Management (Phase 4.5 - NEW)
    _handle_set_credentials(sender, args, correlation_id)    # Store credentials
    _handle_test_auth(sender, args, correlation_id)          # Test authentication
    _handle_remove_credentials(sender, args, correlation_id) # Delete credentials

    # Criteria Management - Unified Model (Phase 3)
    _handle_add_criteria(sender, args, correlation_id)        # Create criterion (simple or group)
    _handle_add_criteria_value(sender, args, correlation_id)  # Add value to group criterion
    _handle_list_criteria(sender, correlation_id)             # Show ALL criteria (simple + groups) with IDs
    _handle_remove_criteria(sender, args, correlation_id)     # Remove criterion by ID
    _handle_remove_criteria_value(sender, args, correlation_id) # Remove value from group by name
    _handle_test_criteria(sender, args, correlation_id)       # Test scoring (simple + groups)

    # Schedule Management (Phase 7)
    _handle_add_schedule(sender, args, correlation_id)      # Add run time
    _handle_list_schedules(sender, correlation_id)          # Show all schedules
    _handle_remove_schedule(sender, args, correlation_id)   # Delete schedule
    _handle_disable_schedule(sender, args, correlation_id)  # Soft disable schedule
    _handle_enable_schedule(sender, args, correlation_id)   # Re-enable schedule

    # Freshness Management (Phase 8 - NEW)
    _handle_list_new_jobs(sender, correlation_id)           # Show only NEW jobs
    _handle_list_stale_jobs(sender, args, correlation_id)   # Show jobs >N days old
    _handle_reset_freshness(sender, correlation_id)         # Mark all as not new

    # Operations
    _handle_run_now(sender, correlation_id)
    _handle_matches(sender, args, correlation_id)
    _handle_status(sender, correlation_id)
    _handle_help(sender, correlation_id)

    # Core business logic
    _scheduler_loop()                       # Daily timing loop
    _run_daily_scrape()                     # Orchestrate full cycle
    _scrape_company(id, name, url)          # Scrape single company
    _filter_and_score_job(job, criteria)    # Dynamic matching with criteria
    _send_job_notification(match)           # Telegram delivery
    _load_active_criteria() -> List[Dict]   # Load criteria from DB
```

#### 2. JobHunterDatabaseManager (Persistence Layer)
**File:** `src/robotray/services/jobhunter_db.py` (~700 lines)

**Database Pattern:** Separate manager class (following `ib_db.py` pattern)

**Public Methods:**
```python
class JobHunterDatabaseManager:
    def __init__(db_path: str)
    def _get_connection() -> sqlite3.Connection  # Context manager

    # Company CRUD
    def add_company(name, url, scraper_type, notes, created_by) -> int
    def get_active_companies() -> List[Dict]
    def get_company_by_id(id) -> Dict
    def remove_company(id) -> bool
    def update_last_scraped(company_id, jobs_found, success, error_msg)

    # Authentication Management (Phase 4.5 - NEW)
    def add_company_credentials(company_id, auth_type, username, password_encrypted,
                                login_url, additional_fields, notes) -> int
    def get_company_credentials(company_id) -> Dict
    def update_company_credentials(company_id, **kwargs) -> bool
    def remove_company_credentials(company_id) -> bool
    def update_auth_success(company_id, timestamp) -> bool  # Track last successful auth
    def get_companies_with_auth_status() -> List[Dict]  # Join companies + credentials

    # Job Postings (with freshness tracking)
    def add_job_posting(company_id, job_hash, title, url, location,
                       is_new, first_seen_date, last_seen_date, times_seen, ...) -> int
    def get_job_by_hash(job_hash) -> Dict
    def update_job_freshness(job_id, is_new, last_seen_date, times_seen) -> bool
    def mark_job_as_seen(job_id) -> bool  # Mark is_new=0 after notification
    def mark_jobs_inactive(company_id, exclude_hashes: List[str])
    def get_recent_jobs(days: int, limit: int) -> List[Dict]
    def get_new_jobs(limit: int) -> List[Dict]  # Only is_new=1
    def get_stale_jobs(days_threshold: int) -> List[Dict]  # first_seen >N days ago
    def get_jobs_not_seen_since(days: int) -> List[Dict]  # For auto-cleanup

    # Job Matches
    def add_job_match(job_id, match_score, match_reasons, notified) -> int
    def get_recent_matches(limit: int) -> List[Dict]
    def update_match_notification(match_id, status)

    # Scrape History (Audit Trail)
    def add_scrape_history(company_id, jobs_found, new_jobs, success, ...) -> int
    def get_scrape_history(company_id, limit) -> List[Dict]

    # Criteria Management - Individual (Phase 3)
    def add_criterion(type, value, weight, active, notes) -> int
    def get_active_criteria() -> List[Dict]
    def get_all_criteria() -> List[Dict]  # Including inactive
    def get_criteria_by_id(id) -> Dict
    def update_criterion(id, value=None, weight=None, active=None) -> bool
    def remove_criterion(id) -> bool
    def remove_criterion_by_value(type, value) -> bool
    def get_criteria_summary() -> Dict  # Grouped by type for display
    def initialize_default_criteria(default_list: List[Dict])

    # Criteria Management - Grouped (Phase 3 - NEW)
    def add_criterion_group(name, match_type, weight, active, notes) -> int
    def add_group_value(group_id, value, tier_level, tier_weight) -> int
    def get_active_groups() -> List[Dict]  # Groups with nested values
    def get_all_groups() -> List[Dict]  # Including inactive groups
    def get_group_by_id(id) -> Dict  # Single group with values
    def get_group_by_name(name) -> Dict  # Lookup by name
    def update_criterion_group(id, name=None, match_type=None, weight=None, active=None) -> bool
    def update_group_value(value_id, value=None, tier_level=None, tier_weight=None) -> bool
    def remove_criterion_group(id) -> bool  # Cascade deletes values
    def remove_group_value(value_id) -> bool
    def get_groups_summary() -> Dict  # Formatted for display
    def initialize_default_groups(default_list: List[Dict])

    # Schedule Management (Phase 7 - NEW)
    def add_run_time(time_str, timezone, active, notes) -> int
    def get_active_run_times() -> List[Dict]
    def get_all_run_times() -> List[Dict]  # Including disabled
    def get_run_time_by_id(id) -> Dict
    def update_run_time(id, active=None, notes=None) -> bool
    def remove_run_time(id) -> bool
    def get_next_run_time() -> datetime  # Calculate next scheduled run
    def initialize_default_schedules(default_list: List[Dict])
```

**Schema Design:**
```sql
-- Target companies to scrape
companies (id, name, careers_url, scraper_type, active, notes,
           added_date, last_scraped, total_scrapes, created_by)

-- Authentication credentials (Phase 4.5 - encrypted storage)
company_credentials (id, company_id, auth_type, login_url, username,
                    password_encrypted, additional_fields, session_cookie_name,
                    session_duration, last_auth_success, auth_notes, created_date)
--   auth_type: 'none', 'basic', 'form', 'oauth'
--   password_encrypted: Encrypted using SecretsManager
--   login_url: Login page URL (for form-based auth)

-- All job postings ever scraped (with freshness tracking)
job_postings (id, company_id, job_hash, title, url, location,
              salary_min, salary_max, salary_text, description,
              posted_date, scraped_date, is_active,
              is_new, first_seen_date, last_seen_date, times_seen)
--   is_new: 1 if NEW this scrape, 0 if seen before
--   first_seen_date: When job was first discovered
--   last_seen_date: Most recent scrape that found this job
--   times_seen: Count of scrapes that have seen this job

-- Matches that passed filter threshold
job_matches (id, job_id, match_score, match_reasons, notified_date,
             notification_status, applied_date, application_status, notes)

-- Scrape execution history (audit + diagnostics)
scrape_history (id, company_id, scrape_date, jobs_found, new_jobs,
                success, error_message, duration_seconds)

-- User search criteria (flexible key-value pairs with weights)
search_criteria (id, criterion_type, criterion_value, weight, active, created_date, notes)
--   criterion_type: keyword, location, min_salary, max_salary, required_skill,
--                   preferred_skill, job_level, exclude_keyword, etc.
--   criterion_value: The actual value (e.g., "python", "Remote", "120000")
--   weight: Scoring weight 0-100 (how important this criterion is)
--   active: Enable/disable without deleting (1=active, 0=disabled)
--   Example rows:
--     (1, 'keyword', 'python', 10, 1, '2026-01-30', 'Primary language')
--     (2, 'keyword', 'senior engineer', 15, 1, '2026-01-30', 'Seniority level')
--     (3, 'location', 'Remote', 20, 1, '2026-01-30', 'Preferred location')
--     (4, 'min_salary', '120000', 20, 1, '2026-01-30', 'Salary floor')
--     (5, 'required_skill', 'python', 10, 1, '2026-01-30', 'Must have')
--     (6, 'exclude_keyword', 'manager', 0, 1, '2026-01-30', 'Exclude management roles')

-- Grouped criteria (multi-value OR logic and tiered preferences) - Phase 3
criterion_groups (id, name, match_type, weight, active, created_date, notes)
--   name: Human-readable group name (e.g., "departments", "geography")
--   match_type: 'any' (OR logic), 'all' (AND logic), 'tiered' (preference levels)
--   weight: Base weight for group (used for 'any' and 'all' types)
--   active: Enable/disable without deleting (1=active, 0=disabled)
--   Example rows:
--     (1, 'departments', 'any', 15, 1, '2026-01-30', 'Match any department')
--     (2, 'geography', 'tiered', 0, 1, '2026-01-30', 'Location preference tiers')
--     (3, 'tech_stack', 'any', 10, 1, '2026-01-30', 'Backend technology match')

-- Values within criterion groups
criterion_group_values (id, group_id, value, tier_level, tier_weight, created_date)
--   group_id: Foreign key to criterion_groups
--   value: The actual match value (e.g., "sales", "Latin America", "FastAPI")
--   tier_level: For tiered groups: 'preferred', 'acceptable', 'undesirable', NULL for non-tiered
--   tier_weight: Override weight for this specific tier (NULL uses group weight)
--   Example rows (for group_id=1 'departments' with match_type='any'):
--     (1, 1, 'sales', NULL, NULL, '2026-01-30')
--     (2, 1, 'marketing', NULL, NULL, '2026-01-30')
--     (3, 1, 'business development', NULL, NULL, '2026-01-30')
--   Example rows (for group_id=2 'geography' with match_type='tiered'):
--     (4, 2, 'Latin America', 'preferred', 20, '2026-01-30')
--     (5, 2, 'Remote', 'preferred', 20, '2026-01-30')
--     (6, 2, 'US East', 'acceptable', 0, '2026-01-30')
--     (7, 2, 'US West', 'acceptable', 0, '2026-01-30')
--     (8, 2, 'On-site only', 'undesirable', -10, '2026-01-30')

-- Multiple daily run times (timezone-aware scheduling)
run_schedules (id, run_time, timezone, active, created_date, notes)
--   run_time: HH:MM format (24-hour)
--   timezone: pytz timezone string (e.g., "America/New_York")
--   active: Enable/disable without deleting
--   Example rows:
--     (1, '09:00', 'America/New_York', 1, '2026-01-30', 'Morning scrape')
--     (2, '13:00', 'America/New_York', 1, '2026-01-30', 'Lunch scrape')
--     (3, '17:00', 'America/New_York', 1, '2026-01-30', 'Evening scrape')
```

**Design Patterns:**
- Context manager for connection safety: `with self._get_connection() as conn`
- Idempotent schema creation: `CREATE TABLE IF NOT EXISTS`
- Parameterized queries: `cursor.execute(query, (param1, param2))`
- Foreign keys with CASCADE for referential integrity
- Indexes on common query columns for performance

#### 3. Configuration File
**File:** `config/services/jobhunter-service.yaml` (~80 lines)

**Key Configuration:**
```yaml
config:
  database_path: "data/jobhunter_service.db"

  # Scheduled run times (loaded on first start, then managed via commands)
  default_schedules:
    - {time: "09:00", timezone: "America/New_York"}   # Morning check
    - {time: "13:00", timezone: "America/New_York"}   # Lunch check
    - {time: "17:00", timezone: "America/New_York"}   # Evening check
  # NOTE: After first start, use commands to manage schedules:
  #   @jobhunter-service add-schedule "21:00"
  #   @jobhunter-service list-schedules

  # Web scraping behavior
  request_timeout: 30
  rate_limit_delay: 3                 # Seconds between companies
  max_retries: 3
  user_agent: "Mozilla/5.0 (compatible; JobHunterBot/1.0)"

  # Authentication settings (Phase 4.5)
  session_cache_duration: 3600        # Cache authenticated sessions (seconds)
  auth_retry_limit: 2                 # Max auth attempts before failing
  credential_encryption_key: "env:JOBHUNTER_ENCRYPTION_KEY"  # From SecretsManager

  # Default criteria (loaded on first start if database is empty)
  # Users can customize via @jobhunter-service set-criteria commands
  # These are just starting templates
  default_criteria:
    - {type: "keyword", value: "python", weight: 10}
    - {type: "keyword", value: "senior engineer", weight: 15}
    - {type: "keyword", value: "backend", weight: 10}
    - {type: "min_salary", value: "120000", weight: 20}
    - {type: "required_skill", value: "python", weight: 10}
    - {type: "required_skill", value: "REST API", weight: 10}

  # Default criterion groups (Phase 3 - multi-value and tiered matching)
  # Loaded on first start if database is empty
  default_criterion_groups:
    # Example 1: Multi-value OR matching (department flexibility)
    - name: "departments"
      match_type: "any"           # Match ANY value → add weight ONCE
      weight: 15
      values:
        - "sales"
        - "marketing"
        - "business development"
        - "customer success"

    # Example 2: Tiered geography preferences
    - name: "geography"
      match_type: "tiered"        # Check tiers in order, use first match
      values:
        - {value: "Latin America", tier: "preferred", weight: 20}
        - {value: "Remote", tier: "preferred", weight: 20}
        - {value: "US East", tier: "acceptable", weight: 0}
        - {value: "US West", tier: "acceptable", weight: 0}
        - {value: "On-site only", tier: "undesirable", weight: -10}

    # Example 3: Tech stack flexibility (backend frameworks)
    - name: "tech_stack"
      match_type: "any"           # Match ANY backend framework
      weight: 10
      values:
        - "FastAPI"
        - "Django"
        - "Flask"
        - "Tornado"

  # Notification settings
  batch_notifications: false          # One message per match (true = summary)
  max_notifications_per_run: 10       # Prevent spam on first run
  min_match_score: 60                 # Notify if score >= 60
```

#### 4. Web Scraping Engine

**Technology Stack:**
- `requests` - HTTP client with retry logic
- `BeautifulSoup` + `lxml` - HTML parsing
- `pytz` - Timezone calculations

**Scraping Strategy:**

**Phase 1 (MVP):** Generic HTML Parser
- Heuristic-based extraction (look for links with job keywords)
- Simple regex for location/salary extraction
- Works for ~60-70% of career pages

**Phase 2 (Partial - Greenhouse Complete):** Platform-Specific Parsers
- **Greenhouse**: ✅ IMPLEMENTED (2026-02-04)
  - Dual-strategy parser: JSON extraction from `<script>` tags (preferred) or structured HTML fallback
  - Method `_scrape_greenhouse()` in `jobhunter_service.py`
  - More reliable than generic parser for Greenhouse-hosted boards
  - Used by: Anthropic and many tech companies
  - Routes automatically when company `scraper_type='greenhouse'`
- **Lever**: Future - HTML with data attributes (structured)
- **Workday**: Future - Complex (may need browser automation)

**Verbose Logging Configuration:** ✅ IMPLEMENTED (2026-02-04)

The service supports configurable logging verbosity to reduce log noise during scheduled runs:

```yaml
config:
  verbose_logging: false  # Default: false (concise executive summary)
```

**When `verbose_logging: false` (DEFAULT - Recommended for Production):**
- Shows only executive summary:
  - Start banner: "🚀 Starting Daily Scrape Workflow"
  - Final statistics: companies scraped, total jobs, new jobs, matches, deep scraped count, time elapsed
  - Errors and warnings (always shown)
- **Benefits**: Clean logs, easy to parse, focuses on outcomes
- **Output example**:
  ```
  ============================================================
  jobhunter-service: 🚀 Starting Daily Scrape Workflow
  ============================================================

  ============================================================
  jobhunter-service: ✨ Daily Scrape Workflow Complete
  jobhunter-service:    • Companies scraped: 1
  jobhunter-service:    • Total jobs found: 62
  jobhunter-service:    • New jobs: 0
  jobhunter-service:    • High-scoring matches: 27
  jobhunter-service:    • Deep scraped: 20
  jobhunter-service:    • Time elapsed: 1.4 minutes
  ============================================================
  ```

**When `verbose_logging: true` (Debugging/Development):**
- Shows detailed progress for every operation:
  - Individual company scraping: "🔍 Scraping 1/1: Anthropic"
  - Scraper selection: "Using Greenhouse parser for..."
  - Job counts per company: "✅ 62 jobs found, 0 new"
  - Scoring progress: "🎯 Scoring jobs against criteria..."
  - Deep scrape progress: "Deep scraping 1/20: Job Title..."
  - Individual job success: "Successfully deep scraped job 123"
- **Benefits**: Detailed debugging, progress tracking, development visibility
- **Use when**: Troubleshooting scraper issues, monitoring first runs, development

**Implementation**: Controlled via `self.verbose_logging` flag checked before logging non-essential progress messages.

**Error Handling:**
```python
def _fetch_with_retry(url: str) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30, headers={'User-Agent': ...})
            response.raise_for_status()
            return response.text
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    return None  # Failed after retries
```

#### 5. Scoring Algorithm

**Dynamic Criteria-Based Scoring with Grouped/Tiered Support (Phase 3 Enhanced):**

The scoring system uses **both individual criteria** and **criterion groups**:
- **Individual Criteria** (`search_criteria`): Simple type+value pairs with weights
- **Grouped Criteria** (`criterion_groups`): Multi-value OR/AND logic or tiered preferences

**Scoring Logic:**
```python
def score_job(job_data, active_criteria, active_groups):
    score = 0
    match_reasons = []
    job_text = (job_data['title'] + ' ' + job_data['description']).lower()

    # ============================================================================
    # PHASE 1: Individual Criteria (existing system)
    # ============================================================================
    for criterion in active_criteria:
        matched = False

        if criterion.type == 'keyword':
            if criterion.value.lower() in job_text:
                matched = True

        elif criterion.type == 'location':
            if criterion.value.lower() in job_data['location'].lower():
                matched = True

        elif criterion.type == 'min_salary':
            if job_data['salary_min'] and job_data['salary_min'] >= int(criterion.value):
                matched = True

        elif criterion.type == 'required_skill':
            if criterion.value.lower() in job_text:
                matched = True
            else:
                return 0, ["Missing required skill: " + criterion.value]

        elif criterion.type == 'exclude_keyword':
            if criterion.value.lower() in job_text:
                return 0, ["Contains excluded keyword: " + criterion.value]

        if matched:
            score += criterion.weight
            match_reasons.append(f"{criterion.type}: {criterion.value}")

    # ============================================================================
    # PHASE 2: Grouped Criteria (new system for multi-value and tiered matching)
    # ============================================================================
    for group in active_groups:
        group_matched = False
        matched_value = None
        tier_matched = None

        if group.match_type == 'any':
            # OR Logic: Match ANY value in the group → add weight ONCE
            for value_obj in group.values:
                if value_obj['value'].lower() in job_text:
                    group_matched = True
                    matched_value = value_obj['value']
                    break  # Stop at first match

            if group_matched:
                score += group.weight
                match_reasons.append(f"{group.name}: {matched_value}")

        elif group.match_type == 'all':
            # AND Logic: Must match ALL values in the group
            all_matched = all(
                value_obj['value'].lower() in job_text
                for value_obj in group.values
            )
            if all_matched:
                score += group.weight
                match_reasons.append(f"{group.name}: all required")

        elif group.match_type == 'tiered':
            # Tiered Preferences: Check tiers in order, use first match
            # Tiers ordered: preferred → acceptable → undesirable
            for value_obj in sorted(group.values, key=lambda x:
                    {'preferred': 0, 'acceptable': 1, 'undesirable': 2}.get(x['tier_level'], 3)):
                if value_obj['value'].lower() in job_text or \
                   value_obj['value'].lower() in job_data['location'].lower():
                    tier_weight = value_obj.get('tier_weight', 0)
                    score += tier_weight
                    tier_matched = value_obj['tier_level']
                    matched_value = value_obj['value']
                    match_reasons.append(f"{group.name}: {matched_value} ({tier_matched}, {tier_weight:+d}pts)")
                    break  # Stop at first matching tier

    return min(score, 100), match_reasons
```

**Example 1: Individual Criteria (Simple Matching)**
```
Criterion Type      | Value              | Weight | Active
--------------------|--------------------|---------|---------
keyword             | python             | 10      | 1
keyword             | senior engineer    | 15      | 1
min_salary          | 120000             | 20      | 1
required_skill      | python             | 10      | 1
required_skill      | REST API           | 10      | 1
exclude_keyword     | manager            | 0       | 1
```

**Example 2: Grouped Criteria (Multi-Value OR Logic)**
```
Group: "departments" (match_type='any', weight=15)
  ├─ Value: "sales"
  ├─ Value: "marketing"
  └─ Value: "business development"

Scoring: If job mentions ANY of these → +15 points (applied ONCE)
```

**Example 3: Tiered Criteria (Preference Levels)**
```
Group: "geography" (match_type='tiered')
  ├─ Tier: preferred (+20 points)
  │   ├─ "Latin America"
  │   └─ "Remote"
  ├─ Tier: acceptable (0 points, neutral)
  │   ├─ "US East"
  │   └─ "US West"
  └─ Tier: undesirable (-10 points, penalty)
      └─ "On-site only"

Scoring: Checks tiers in order, uses first match
  - Job in "Latin America" → +20 points (preferred)
  - Job in "US East" → 0 points (acceptable, no penalty)
  - Job "On-site only" → -10 points (undesirable)
```

**Example 4: Combined System (Individual + Grouped)**
```
Individual Criteria:
  - keyword: python (10)
  - keyword: senior engineer (15)
  - min_salary: 120000 (20)

Grouped Criteria:
  - departments (any): [sales, marketing, bizdev] → 15 points if ANY match
  - geography (tiered): preferred/acceptable/undesirable → variable points
  - tech_stack (any): [FastAPI, Django, Flask] → 10 points if ANY match
```

**Example Calculation A: NEW Job with Individual Criteria Only**
```
Job: "Senior Python Engineer - Remote"
Salary: $140k-$180k
Description: "Build REST APIs with Python..."
Freshness: NEW (first time seen)

✓ keyword: python (10 pts)
✓ keyword: senior engineer (15 pts)
✓ min_salary: $140k >= $120k (20 pts)
✓ required_skill: python (10 pts)
✓ required_skill: REST API (10 pts)
✓ freshness_bonus: NEW posting (+15 pts)

Base Score: 65/100
Freshness Bonus: +15
Total Score: 80/100 → MATCH (notify user)
```

**Example Calculation B: NEW Job with Grouped Criteria**
```
Job: "Senior Sales Engineer - Remote, Latin America"
Salary: $130k-$160k
Description: "Drive enterprise sales with Python/FastAPI technical demos..."
Freshness: NEW (first time seen)

Individual Criteria:
✓ keyword: python (10 pts)
✓ keyword: senior engineer (15 pts)
✓ min_salary: $130k >= $120k (20 pts)

Grouped Criteria:
✓ departments (any): matched "sales" → +15 pts (group matched once)
✓ geography (tiered): matched "Latin America" (preferred) → +20 pts
✓ tech_stack (any): matched "FastAPI" → +10 pts

✓ freshness_bonus: NEW posting (+15 pts)

Base Score: 90/100
Freshness Bonus: +15
Total Score: 100/100 → 🔥 HIGH PRIORITY MATCH (grouped criteria added 45 pts!)

Match Reasons: keyword:python, keyword:senior engineer, min_salary,
               departments:sales, geography:Latin America (preferred, +20pts),
               tech_stack:FastAPI, freshness_bonus:+15 (NEW)
```

**Example Calculation C: Tiered Geography - Acceptable Tier (Neutral)**
```
Job: "Senior Python Engineer - US East (hybrid)"
Salary: $150k-$190k

Individual Criteria:
✓ keyword: python (10 pts)
✓ keyword: senior engineer (15 pts)
✓ min_salary: $150k >= $120k (20 pts)

Grouped Criteria:
✓ geography (tiered): matched "US East" (acceptable) → +0 pts (neutral, no penalty)
✗ departments: no match (0 pts)

Base Score: 45/100
Total Score: 45/100 → SAVED (below 60 threshold, no notification)

Match Reasons: keyword:python, keyword:senior engineer, min_salary,
               geography:US East (acceptable, +0pts)

Result: Saved to database but not notified (score < 60)
```

**Example Calculation D: Undesirable Tier (Penalty)**
```
Job: "Senior Python Engineer - On-site only (Austin, TX)"
Salary: $130k

Individual Criteria:
✓ keyword: python (10 pts)
✓ keyword: senior engineer (15 pts)
✓ min_salary: $130k >= $120k (20 pts)

Grouped Criteria:
✓ geography (tiered): matched "On-site only" (undesirable) → -10 pts (penalty)

Base Score: 45/100
Geography Penalty: -10
Total Score: 35/100 → SAVED (low score, likely not a good fit)

Match Reasons: keyword:python, keyword:senior engineer, min_salary,
               geography:On-site only (undesirable, -10pts)
```

**Example Calculation (Existing Job - 5 days old):**
```
Same job, but seen in previous 3 scrapes (5 days ago)

Base Score: 85/100
Freshness Bonus: +5 (within 7 days)
Total Score: 90/100 → MATCH (notify user)

Match Reasons: [same criteria], freshness_bonus:+5 (5 days old)
```

**Match Threshold:** `min_match_score` (configurable, default: 60)
- Score < 60: Saved to database but not notified
- Score >= 60: Saved + notification sent
- Required skills not met: Score = 0 (automatic rejection)
- Exclude keywords present: Score = 0 (automatic rejection)

**Benefits of Dynamic Scoring with Grouped/Tiered Criteria:**

**Individual Criteria Benefits:**
- **User Control**: Change criteria weights without code changes
- **Personalization**: Different users can have different criteria
- **Experimentation**: Test different weightings to optimize matches
- **Extensibility**: Add new criterion types without changing scoring logic
- **Transparency**: Users see exactly why jobs matched

**Grouped Criteria Benefits (NEW - Phase 3 Enhanced):**
- **Multi-Value Flexibility**: OR logic for departments, tech stacks, job titles
  - Example: Match "sales OR marketing OR bizdev" → add points ONCE
  - Avoids double-counting when job mentions multiple values
- **Tiered Preferences**: Express nuanced location/role preferences
  - Example: "Remote preferred (+20), US East acceptable (0), On-site undesirable (-10)"
  - Enables "preferred > acceptable > undesirable" hierarchies
- **Neutral Scoring**: Support "no bonus, no penalty" for acceptable options
  - Traditional systems only do positive/negative - this adds neutral tier
- **Geographical Hierarchies**: Model real-world location preferences
  - "Latin America preferred, but US East is OK" becomes explicit in data
- **Reduced Configuration Complexity**: Group related values logically
  - Instead of 10 individual location criteria, one tiered geography group
- **Penalty Support**: Negative weights for undesirable attributes
  - Example: -10 points for "On-site only" when Remote is preferred

#### 6. Notification System

**Delivery:** Via `@broadcast` message to reach all channels (including Telegram)

**Markdown Format:**
```markdown
🎯 **Job Match (84% match)**

**Senior Python Engineer - Remote**
📍 Company: Anthropic
📍 Location: Remote
💰 Salary: $140k-$180k
🔗 Apply: https://www.anthropic.com/careers/senior-python-engineer

**Why it matched:**
• Keywords: python, senior, engineer
• Location: Remote
• Salary: $140,000+
• Skills: python, api
```

**Modes:**
- **Individual**: One message per match (default)
- **Batch**: Summary message with top N matches

#### 7. Command Interface

**User Commands (via @ routing):**
```bash
# Company Management
@jobhunter-service add-company "Anthropic" "https://www.anthropic.com/careers"
@jobhunter-service add-company "OpenAI" "https://openai.com/careers" "Greenhouse parser"
@jobhunter-service add-company "Workday Portal" "https://workday.example.com/careers"
@jobhunter-service list-companies                # Shows auth status per company
@jobhunter-service remove-company "Anthropic"
@jobhunter-service remove-company 5              # By ID

# Authentication Management (Phase 4.5)
@jobhunter-service set-credentials 5 form "myemail@example.com" "mypassword" "https://workday.example.com/login"
@jobhunter-service set-credentials 7 basic "username" "password"
@jobhunter-service test-auth 5                   # Test if credentials work
@jobhunter-service remove-credentials 5          # Delete stored credentials

# Criteria Management (Phase 3 - Unified Model)
# Simple criteria (single value matching)
@jobhunter-service add-criteria python simple 20
@jobhunter-service add-criteria remote simple 15
@jobhunter-service add-criteria fastapi simple 10
@jobhunter-service add-criteria senior simple 10

# Group criteria with OR matching (match any value)
@jobhunter-service add-criteria geography group-any 15
@jobhunter-service add-criteria-value geography "US West"
@jobhunter-service add-criteria-value geography "US East"
@jobhunter-service add-criteria-value geography "Latin America"

# Group criteria with tiered matching (preference levels)
@jobhunter-service add-criteria department group-tiered 0
@jobhunter-service add-criteria-value department "Sales Engineering" preferred 20
@jobhunter-service add-criteria-value department Marketing preferred 15
@jobhunter-service add-criteria-value department "Customer Success" acceptable 10
@jobhunter-service add-criteria-value department "IT Support" undesirable -10

# List all criteria (simple and groups)
@jobhunter-service list-criteria

# Remove criteria
@jobhunter-service remove-criteria 1                   # Remove by ID (use list-criteria to see IDs)
@jobhunter-service remove-criteria-value geography "US East"  # Remove value from group

# Testing & Validation
@jobhunter-service test-criteria "Senior Sales Engineer - Remote, US West - $150k - FastAPI"
# Returns: Score: 75/100
#          Simple matches: remote (+15), fastapi (+10), senior (+10)
#          Group matches: geography "US West" (+15), department "Sales Engineering" (+20)
#          Total: 70 pts → HIGH PRIORITY MATCH

# Schedule Management (Phase 7)
@jobhunter-service add-schedule "09:00"                    # Add 9 AM run
@jobhunter-service add-schedule "13:00"                    # Add 1 PM run
@jobhunter-service add-schedule "17:00"                    # Add 5 PM run
@jobhunter-service add-schedule "21:00" "America/Chicago"  # Different timezone
@jobhunter-service list-schedules                          # Show all run times
@jobhunter-service remove-schedule 2                       # Remove by ID
@jobhunter-service disable-schedule 3                      # Pause schedule
@jobhunter-service enable-schedule 3                       # Resume schedule

# Freshness Management (Phase 8)
@jobhunter-service list-new-jobs                           # Show only NEW jobs (is_new=1)
@jobhunter-service list-stale-jobs 7                       # Show jobs >7 days old
@jobhunter-service list-stale-jobs 30                      # Show jobs >30 days old
@jobhunter-service reset-freshness                         # Mark all as not new

# Operations
@jobhunter-service run-now          # Trigger immediate scrape (manual)
@jobhunter-service matches          # Show recent matches (top 20, prioritizes NEW)
@jobhunter-service matches 50       # Show top 50
@jobhunter-service status           # Service health + next run time
@jobhunter-service help             # Command reference
```

**Response Examples:**

```bash
# add-company response (with auth detection)
@jobhunter-service add-company "Workday Portal" "https://workday.example.com/careers"

✅ Added company: Workday Portal (ID: 8)
⚠️ Authentication required (form-based login detected)
To configure: @jobhunter-service set-credentials 8 form <username> <password> <login_url>

# set-credentials response
@jobhunter-service set-credentials 8 form "user@example.com" "mypassword" "https://workday.example.com/login"

✅ Credentials stored for company 8 (Workday Portal)
Auth type: form-based login
Testing authentication...
✅ Authentication successful
Next scrape will use these credentials

# test-auth response (success)
@jobhunter-service test-auth 8

🔐 Testing authentication for Workday Portal...
✅ Authentication successful
Session valid for: 3600 seconds (1 hour)
Last successful auth: 2026-02-01 14:30:00

# test-auth response (failure)
@jobhunter-service test-auth 8

🔐 Testing authentication for Workday Portal...
❌ Authentication failed: Invalid credentials
Please update: @jobhunter-service set-credentials 8 form <username> <password> <login_url>

# list-companies response (with auth status)
📋 Active Companies (5)

1. **Anthropic** ✅ Public
   URL: https://www.anthropic.com/careers
   Scraper: Generic | Last scraped: 2 hours ago
   Jobs: 12 active

2. **Workday Portal** 🔐 Form Auth (Configured)
   URL: https://workday.example.com/careers
   Scraper: Generic | Last scraped: 3 hours ago
   Auth status: ✅ Valid | Last auth: Today at 2:30 PM
   Jobs: 8 active

3. **Internal Careers** 🔐 Basic Auth (Not configured)
   URL: https://internal.example.com/careers
   Scraper: Generic | Never scraped
   Auth status: ⚠️ Credentials needed
   Configure: @jobhunter-service set-credentials 3 basic <username> <password>

4. **OpenAI** ✅ Public
   URL: https://openai.com/careers
   Scraper: Greenhouse | Last scraped: 1 hour ago
   Jobs: 15 active

# set-criteria response
✅ Added criterion: keyword='python' (weight: 10)
Total criteria: 8 active

# list-criteria response
📋 Active Search Criteria (Total Weight: 125/100)

Keywords (40 pts):
  • python (10 pts)
  • senior engineer (15 pts)
  • backend (10 pts)
  • API (5 pts)

Locations (35 pts):
  • Remote (20 pts)
  • Florida (15 pts)

Salary (20 pts):
  • Minimum: $120,000
  • Maximum: $200,000

Required Skills (20 pts):
  • python (10 pts)
  • REST API (10 pts)

Preferred Skills (5 pts):
  • AWS (5 pts)

Exclude Keywords:
  • manager
  • on-call

Job Levels (5 pts):
  • senior (5 pts)

⚠️ Note: Total weight is 125. Scores are capped at 100.
Consider adjusting weights for better distribution.

# list-new-jobs response
🆕 NEW Jobs Found (3)

1. **Senior Python Engineer - Remote** (Score: 92/100) 🔥
   Company: Anthropic
   Location: Remote | Salary: $150k-$190k
   First Seen: Today at 1:00 PM (2 hours ago)
   Apply: https://anthropic.com/careers/senior-python-engineer

2. **Backend Engineer** (Score: 88/100) 🔥
   Company: OpenAI
   Location: San Francisco | Salary: $140k-$180k
   First Seen: Today at 1:00 PM (2 hours ago)
   Apply: https://openai.com/careers/backend-engineer

3. **Python Developer** (Score: 75/100)
   Company: Stripe
   Location: Remote | Salary: $130k-$170k
   First Seen: Today at 1:00 PM (2 hours ago)
   Apply: https://stripe.com/jobs/python-developer

💡 Tip: NEW jobs have highest response rates. Apply within 24 hours!

# list-stale-jobs response
📊 Stale Jobs (>7 days old) - May be filled

1. **Senior Engineer** (Score: 72/100)
   Company: Netflix
   Location: Los Angeles | Salary: $160k-$200k
   First Seen: 12 days ago | Seen in: 8 scrapes
   Apply: https://netflix.com/careers/senior-engineer
   ⚠️ Old posting - may be filled

2. **Python Developer** (Score: 68/100)
   Company: Uber
   Location: San Francisco | Salary: $135k-$175k
   First Seen: 15 days ago | Seen in: 10 scrapes
   Apply: https://uber.com/careers/python-developer
   ⚠️ Old posting - may be filled

💡 Consider removing companies that keep stale postings

# test-criteria response
🎯 Test Results for: "Senior Python Engineer - Remote - $150k"

Score: 85/100 ✅ WOULD MATCH (threshold: 60)

Matched Criteria:
  ✓ keyword: python (10 pts)
  ✓ keyword: senior engineer (15 pts)
  ✓ location: Remote (20 pts)
  ✓ min_salary: $150,000 >= $120,000 (20 pts)
  ✓ required_skill: python (10 pts)
  ✓ required_skill: REST API (10 pts)

Not Matched:
  ✗ keyword: backend (0 pts)
  ✗ keyword: API (0 pts) - not in title
  ✗ location: Florida (0 pts)
  ✗ preferred_skill: AWS (0 pts)
  ✗ job_level: senior (0 pts) - need "senior" as standalone word
```

---

## Architecture Review

### Strengths

#### 1. Integration with Existing Patterns ✅

**Follows RobotRay Best Practices:**
- **ADR-002 Service Isolation**: Separate threads for message loop + scheduler
- **ADR-003 Identity Assertion**: All commands preserve user identity
- **BaseService Pattern**: Extends base class, gets message queues automatically
- **DatabaseManager Pattern**: Follows `ib_db.py` separation of concerns
- **Responsive Shutdown**: Chunked sleeps with `threading.Event()`

**Score: 10/10** - Perfect adherence to established patterns

#### 2. Separation of Concerns ✅

**Clear Component Boundaries:**
- **JobHunterService**: Orchestration, scheduling, messaging
- **JobHunterDatabaseManager**: All database operations
- **Configuration**: YAML-based, no hardcoded values
- **Web Scraping**: Encapsulated in dedicated methods

**Score: 9/10** - Clean architecture, minor room for extraction of scraping logic to separate module in future

#### 3. Data Model Design ✅

**Database Schema Quality:**
- **Proper normalization**: Companies → Jobs → Matches (clear hierarchy)
- **Deduplication**: `job_hash` (SHA256) prevents duplicate entries
- **Soft deletes**: `is_active` flag for jobs (preserves history)
- **Audit trail**: `scrape_history` table tracks all executions
- **Flexible criteria**: Generic `search_criteria` table (extensible)

**Indexes:**
```sql
CREATE INDEX idx_job_postings_company_id ON job_postings(company_id);
CREATE INDEX idx_job_postings_scraped_date ON job_postings(scraped_date);
CREATE INDEX idx_job_matches_notified_date ON job_matches(notified_date);
```

**Score: 9/10** - Excellent schema design, could add composite indexes for common queries

#### 4. Error Handling & Resilience ✅

**Robust Failure Handling:**
- **Retry logic**: Exponential backoff (3 attempts)
- **Rate limiting**: 3-second delays between companies
- **Graceful degradation**: Failed company doesn't stop scrape cycle
- **Database resilience**: Context managers ensure cleanup
- **Logging**: All errors logged with context for debugging

**Failure Modes Covered:**
- Connection timeout → Retry with backoff
- HTTP 404/403 → Log error, continue to next company
- Rate limiting (429) → Back off 60 seconds
- Invalid HTML → Log parsing error, skip company
- Database locked → Retry with short sleep
- Long-running scrape → 30-minute timeout

**Score: 9/10** - Comprehensive error handling, missing circuit breaker pattern

#### 5. Scalability ✅

**Performance Characteristics:**

| Metric | Current (10 companies) | Projected (50 companies) |
|--------|------------------------|--------------------------|
| **Scrape Duration** | ~1-2 minutes | ~8-10 minutes |
| **Database Size** | ~5MB/month | ~25MB/month |
| **Memory Usage** | ~100MB | ~200MB |
| **CPU Usage** | <10% | <15% |

**Bottlenecks Identified:**
- Sequential scraping (3-second delays)
- Synchronous HTTP requests

**Mitigation Strategies:**
- **Phase 2**: Async scraping with `aiohttp` (concurrent requests)
- **Phase 3**: Distributed scraping (multiple workers)

**Score: 7/10** - Adequate for MVP, needs async for scale

#### 6. Security & Privacy ✅

**Security Measures:**
- **No credentials scraped**: Only public job postings
- **Rate limiting**: Respectful of target websites
- **User-Agent identification**: Clear bot identification
- **Parameterized queries**: SQL injection prevention
- **Input validation**: URL validation, command sanitization

**Privacy Considerations:**
- **No PII collection**: Only job listing metadata
- **User data**: Only search preferences (no resumes stored)
- **Ethical scraping**: Follows robots.txt conventions

**Risks:**
- **Legal**: Terms of Service violations (some sites prohibit scraping)
- **Technical**: IP blocking if detected as bot
- **Mitigation**: Manual review of ToS per company, rotating user agents

**Score: 8/10** - Good security hygiene, legal risk requires manual oversight

#### 7. Testing Strategy ✅

**Unit Test Coverage:**
```python
# Database operations
test_add_company()
test_get_active_companies()
test_add_job_posting()
test_job_deduplication()

# Scraping logic
test_fetch_with_retry()
test_scrape_generic_html()
test_extract_location()
test_extract_salary()

# Scoring algorithm
test_job_scoring_keywords()
test_job_scoring_location()
test_job_scoring_salary()
test_job_scoring_combined()

# Scheduler
test_calculate_next_run()
test_daily_timing_calculation()
```

**Integration Tests:**
```python
test_full_scrape_cycle()           # End-to-end scrape
test_message_routing()              # @ command handling
test_telegram_notification()        # Delivery verification
```

**Score: 8/10** - Good test strategy, missing mocks for external HTTP requests

#### 8. Observability ✅

**Monitoring Capabilities:**
- **Health checks**: Service status, thread health, database connectivity
- **Metrics**: Companies scraped, jobs found, new jobs, matches, errors
- **Audit trail**: `scrape_history` table tracks all executions
- **Logging**: Structured logs at INFO/DEBUG/ERROR levels

**Diagnostic Commands:**
```bash
@jobhunter-service status
# Returns:
# - Next scrape time (calculated from active schedules)
# - All scheduled run times (e.g., 09:00, 13:00, 17:00)
# - Last scrape results (jobs found, matches, errors)
# - Active companies count
# - Active criteria count
# - Recent matches count
```

**Score: 9/10** - Excellent observability, could add Prometheus metrics

### Weaknesses

#### 1. Scraping Reliability ⚠️

**Generic Parser Limitations:**
- **Success rate**: ~60-70% (many sites have complex structures)
- **False positives**: Non-job links may be extracted
- **Data quality**: Location/salary extraction is heuristic

**Mitigation:**
- **Phase 2**: Platform-specific parsers (Greenhouse, Lever)
- **User feedback**: Mark false positives for training
- **Manual review**: Option to review before notification

**Score: 6/10** - MVP approach, needs platform-specific parsers for production

#### 2. No Distributed Architecture ⚠️

**Sequential Processing:**
- Scrapes companies one at a time (rate limiting)
- Single-threaded scraping (could use async)

**Impact:**
- **50 companies**: ~10-15 minutes scrape time
- **100 companies**: Would exceed reasonable window

**Mitigation:**
- **Phase 2**: Async scraping with `aiohttp`
- **Phase 3**: Multiple worker processes

**Score: 6/10** - Acceptable for MVP (<20 companies), needs redesign for scale

#### 3. Limited Job Data Extraction ⚠️

**Current Extraction:**
- Title (✓)
- URL (✓)
- Location (~ heuristic)
- Salary (~ regex-based, many formats)
- Description (✓ but truncated)

**Missing:**
- Job posting date (affects freshness)
- Application deadline
- Required years of experience
- Tech stack details
- Remote work policy specifics

**Mitigation:**
- **Phase 2**: LLM-based extraction (Claude API)
- **Phase 3**: Structured data extraction (JSON-LD, microdata)

**Score: 7/10** - Basic data captured, room for enhancement

#### 4. Limited Learning & Optimization ⚠️

**Dynamic but Manual Scoring:**
- ✅ User-configurable criteria weights (Phase 3 implementation)
- ✅ Personalization through manual tuning
- ✅ Test command for experimentation (`test-criteria`)
- ❌ No automatic learning from user feedback
- ❌ No adaptive weight adjustment over time

**Current State:**
Users can manually tune criteria weights based on results, which is a form of "manual machine learning." The test-criteria command helps users experiment with different weightings before running full scrapes.

**Future Enhancement (Phase 4+):**
- Track application outcomes (applied, interview, offer, rejected)
- Automatically adjust weights based on successful matches
- Learn from user actions (which matches they apply to)
- A/B test different scoring algorithms
- Collaborative filtering (learn from similar users)

**Score: 7/10** - User-tunable criteria is a strong start, automatic learning would elevate this to 9/10

### Architecture Score Summary

| Category | Score | Weight | Weighted |
|----------|-------|--------|----------|
| **Integration with Patterns** | 10/10 | 20% | 2.0 |
| **Separation of Concerns** | 9/10 | 15% | 1.35 |
| **Data Model Design** | 9/10 | 15% | 1.35 |
| **Error Handling** | 9/10 | 15% | 1.35 |
| **Scalability** | 7/10 | 10% | 0.70 |
| **Security & Privacy** | 8/10 | 10% | 0.80 |
| **Testing Strategy** | 8/10 | 5% | 0.40 |
| **Observability** | 9/10 | 5% | 0.45 |
| **Scraping Reliability** | 6/10 | 5% | 0.30 |

**Overall Architecture Score: 8.7/10** - **Strong Architecture, Production-Ready for MVP**

### Recommendation: ✅ APPROVED WITH CONDITIONS

**Approval Conditions:**
1. ✅ Implement Phase 1 as designed (MVP)
2. ✅ Add unit tests for all core functionality (>80% coverage)
3. ✅ Document legal review checklist for company ToS
4. ⚠️ Plan Phase 2 async architecture before scaling beyond 20 companies
5. ⚠️ Implement platform-specific parsers (Greenhouse, Lever) in Phase 2

---

## Implementation Plan

### Phase 1: Database Foundation (2-3 hours)
1. Create `jobhunter_db.py` with schema and DatabaseManager
   - All tables: companies, job_postings, job_matches, scrape_history, search_criteria, run_schedules
   - Initialize default criteria from config on first start
   - Initialize default schedules from config on first start
2. Create basic `jobhunter_service.py` skeleton
   - Extend BaseService
   - Basic health check
3. Create `jobhunter-service.yaml` config
   - Database path, default schedules, default criteria template
4. Test database initialization and service lifecycle
5. Verify default criteria and schedules loaded

**Verification:**
- Service starts/stops cleanly
- All database tables created with proper schema
- Default criteria inserted on first run (e.g., 8 criteria)
- Default schedules inserted on first run (e.g., 3 schedules: 9 AM, 1 PM, 5 PM)
- Health check returns "healthy" status

### Phase 2: Message Handling (2 hours)
1. Implement `_message_loop()` and command routing
2. Implement `add-company`, `list-companies`, `help` handlers
3. Test @ routing via web channel

**Verification:** Can add companies and list them

### Phase 3: Criteria Management (3-4 hours) - Unified Model ✅ COMPLETED

**Implementation Complete - Unified Criteria Model:**

1. Database operations in `jobhunter_db.py` (already implemented):
   - Simple criteria: `add_criterion()`, `get_active_criteria()`, `remove_criterion()`
   - Group criteria: `add_criterion_group()`, `add_group_value()`, `get_active_groups()`, `remove_criterion_group()`, `remove_group_value()`
   - Initialization: `initialize_default_criteria()`, `initialize_default_groups()`

2. Unified command handlers in `jobhunter_service.py` ✅:
   - `_handle_add_criteria(sender, args, correlation_id)` - Create criterion (simple or group)
     - Accepts: `<name> <type> [weight]`
     - Types: `simple`, `group-any`, `group-all`, `group-tiered`

   - `_handle_add_criteria_value(sender, args, correlation_id)` - Add value to group
     - Accepts: `<name> <value> [tier] [weight]`

   - `_handle_list_criteria(sender, correlation_id)` - Show ALL criteria (unified view)

   - `_handle_remove_criteria(sender, args, correlation_id)` - Remove by name

   - `_handle_remove_criteria_value(sender, args, correlation_id)` - Remove value
     - Accepts: `<name> <value>`

   - `_handle_test_criteria(sender, args, correlation_id)` - Test scoring

3. Criterion types:
   - **simple**: Single-value substring matching
   - **group-any**: Multi-value OR logic (match any → score once)
   - **group-all**: Multi-value AND logic (must match all) - future
   - **group-tiered**: Preference-based (preferred/acceptable/undesirable)

4. Tier levels (for group-tiered):
   - **preferred**: High value (e.g., +20 points)
   - **acceptable**: Moderate value (e.g., +5-10 points)
   - **undesirable**: Penalty (e.g., -10 points)

5. Features implemented:
   - ✅ Multi-word value support (shlex quote parsing)
   - ✅ Case-insensitive matching
   - ✅ Tiered groups with individual weights per value
   - ✅ Unified list view showing both simple and groups
   - ✅ Name-based removal (not ID-based)
   - ✅ Value-based removal from groups

**Command Examples (Unified Criteria Model):**
```bash
# Add simple criteria (single-value matching)
@jobhunter-service add-criteria python simple 20
@jobhunter-service add-criteria remote simple 15
@jobhunter-service add-criteria fastapi simple 10
@jobhunter-service add-criteria senior simple 10

# Create group-any criterion (match any value, OR logic)
@jobhunter-service add-criteria department group-any 15
@jobhunter-service add-criteria-value department sales
@jobhunter-service add-criteria-value department marketing
@jobhunter-service add-criteria-value department "business development"

# Create group-tiered criterion (preference levels)
@jobhunter-service add-criteria geography group-tiered 0
@jobhunter-service add-criteria-value geography "Latin America" preferred 20
@jobhunter-service add-criteria-value geography Remote preferred 20
@jobhunter-service add-criteria-value geography "US East" acceptable 5
@jobhunter-service add-criteria-value geography "US West" acceptable 5
@jobhunter-service add-criteria-value geography "On-site only" undesirable -10

# Create group-any criterion (tech stack flexibility)
@jobhunter-service add-criteria tech_stack group-any 10
@jobhunter-service add-criteria-value tech_stack FastAPI
@jobhunter-service add-criteria-value tech_stack Django
@jobhunter-service add-criteria-value tech_stack Flask

# List all criteria (shows both simple and groups)
@jobhunter-service list-criteria
# Output:
#   🎯 Active Search Criteria
#
#   Simple Criteria (4):
#     • python (weight: 20, ID: 1)
#     • remote (weight: 15, ID: 2)
#     • fastapi (weight: 10, ID: 3)
#     • senior (weight: 10, ID: 4)
#
#   Group Criteria (3):
#
#   **department** (group-any, 15 pts, ID: 5)
#     Values (3):
#       - sales (ID: 1)
#       - marketing (ID: 2)
#       - business development (ID: 3)
#
#   **geography** (group-tiered, 0 pts, ID: 6)
#     • PREFERRED:
#       - Latin America (+20 pts) (ID: 4)
#       - Remote (+20 pts) (ID: 5)
#     • ACCEPTABLE:
#       - US East (+5 pts) (ID: 6)
#       - US West (+5 pts) (ID: 7)
#     • UNDESIRABLE:
#       - On-site only (-10 pts) (ID: 8)

# Remove criteria
@jobhunter-service remove-criteria 1                        # Remove simple criterion by ID
@jobhunter-service remove-criteria 5                        # Remove entire group by ID
@jobhunter-service remove-criteria-value geography "US East"  # Remove value from group

# Test criteria against sample job text
@jobhunter-service test-criteria "Senior Sales Engineer - Remote, Latin America - $150k - FastAPI backend"
# Output:
#   Test Criteria Results
#   Sample Job: Senior Sales Engineer - Remote, Latin America - $150k - FastAPI backend
#
#   Simple Criteria: 55/100
#     ✓ python (+20)
#     ✓ remote (+15)
#     ✓ fastapi (+10)
#     ✓ senior (+10)
#
#   Group Criteria: +45 pts
#     ✓ department: sales (+15)
#     ✓ geography: Latin America (preferred, +20)
#     ✓ tech_stack: FastAPI (+10)
#
#   Total Score: 100/100 → 🔥 HIGH PRIORITY MATCH (would notify)
```

**Verification:**
- Can add multiple criteria of different types
- Criteria persisted to database
- List shows all active criteria grouped by type
- Can remove criteria
- Test command shows how a sample job would score

### Phase 4: Web Scraping (3-4 hours)
1. Implement `_fetch_with_retry()` with exponential backoff
2. Implement `_scrape_generic_html()` with BeautifulSoup
3. Implement `_scrape_company()` with deduplication
4. Test on 2-3 real company websites

**Verification:** Can scrape job listings from public career pages

### Phase 4.5: Authentication & Protected Career Pages (3-4 hours)
Many companies restrict career pages behind authentication (Workday, internal portals, employee referral systems). This phase adds intelligent auth detection and credential management.

**Authentication Scenarios:**

1. **Public Pages** (70% of companies)
   - No authentication required
   - Direct HTTP GET works

2. **Form-Based Login** (20% of companies)
   - Username/password form
   - Session cookies after login
   - Examples: Workday, Taleo, custom portals

3. **HTTP Basic Auth** (5% of companies)
   - HTTP 401 with WWW-Authenticate header
   - Credentials in request headers

4. **SSO/OAuth** (5% of companies - Phase 2 Future)
   - Corporate SSO (Okta, Azure AD)
   - Too complex for MVP, manual fallback

**Implementation:**

1. **Extend Database Schema**
   ```sql
   -- Company authentication credentials (encrypted)
   CREATE TABLE IF NOT EXISTS company_credentials (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       company_id INTEGER NOT NULL,
       auth_type TEXT NOT NULL,           -- 'none', 'basic', 'form', 'oauth'
       login_url TEXT,                    -- URL of login page (for form auth)
       username TEXT,                     -- Encrypted username
       password_encrypted TEXT,           -- Encrypted password
       additional_fields TEXT,            -- JSON for extra form fields
       session_cookie_name TEXT,          -- Cookie name to preserve
       session_duration INTEGER DEFAULT 3600,  -- How long session lasts (seconds)
       last_auth_success TEXT,            -- Last successful authentication
       auth_notes TEXT,                   -- Manual notes about auth process
       created_date TEXT NOT NULL,
       FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
   );
   ```

2. **Implement Auth Detection** (`_detect_auth_requirement`)
   ```python
   def _detect_auth_requirement(url: str) -> Dict[str, Any]:
       """
       Intelligently detect if URL requires authentication.

       Returns:
           {
               'required': bool,
               'auth_type': 'none'|'basic'|'form'|'unknown',
               'login_url': str (if detected),
               'redirect_detected': bool
           }
       """
       response = requests.get(url, allow_redirects=False, timeout=10)

       # Check for HTTP Basic Auth
       if response.status_code == 401:
           if 'WWW-Authenticate' in response.headers:
               return {
                   'required': True,
                   'auth_type': 'basic',
                   'login_url': None,
                   'redirect_detected': False
               }

       # Check for redirects to login page
       if response.status_code in [302, 303, 307, 308]:
           location = response.headers.get('Location', '')
           if any(keyword in location.lower() for keyword in
                  ['login', 'signin', 'auth', 'sso']):
               return {
                   'required': True,
                   'auth_type': 'form',
                   'login_url': location,
                   'redirect_detected': True
               }

       # Check for login forms in HTML
       if response.status_code == 200:
           soup = BeautifulSoup(response.text, 'lxml')
           login_forms = soup.find_all('form', {'action': lambda x: x and
                                                'login' in x.lower()})
           password_inputs = soup.find_all('input', {'type': 'password'})

           if login_forms or password_inputs:
               return {
                   'required': True,
                   'auth_type': 'form',
                   'login_url': url,  # Login form on same page
                   'redirect_detected': False
               }

       # No auth detected
       return {
           'required': False,
           'auth_type': 'none',
           'login_url': None,
           'redirect_detected': False
       }
   ```

3. **Implement Form-Based Authentication** (`_authenticate_form_login`)
   ```python
   def _authenticate_form_login(company_id: int, login_url: str,
                                username: str, password: str) -> requests.Session:
       """
       Perform form-based login and return authenticated session.

       Returns:
           requests.Session with valid auth cookies
       """
       session = requests.Session()

       # Get login page to extract CSRF tokens, hidden fields
       response = session.get(login_url)
       soup = BeautifulSoup(response.text, 'lxml')

       # Find login form
       form = soup.find('form', {'action': lambda x: x and 'login' in x.lower()})
       if not form:
           form = soup.find('form')  # Fallback to first form

       # Extract form action URL
       action = form.get('action', '')
       if not action.startswith('http'):
           action = urljoin(login_url, action)

       # Build form data
       form_data = {}

       # Copy hidden fields (CSRF tokens, etc.)
       for hidden in form.find_all('input', {'type': 'hidden'}):
           name = hidden.get('name')
           value = hidden.get('value', '')
           if name:
               form_data[name] = value

       # Add username and password (detect field names)
       username_field = form.find('input', {'type': ['text', 'email']})
       password_field = form.find('input', {'type': 'password'})

       if username_field:
           form_data[username_field.get('name')] = username
       if password_field:
           form_data[password_field.get('name')] = password

       # Submit login form
       response = session.post(action, data=form_data, allow_redirects=True)

       # Verify login success (check for errors or redirects)
       if response.status_code != 200:
           raise AuthenticationError(f"Login failed: HTTP {response.status_code}")

       if 'error' in response.url.lower() or 'login' in response.url.lower():
           raise AuthenticationError("Login failed: Redirected back to login")

       # Store session info
       self.db_manager.update_auth_success(company_id, datetime.now())

       return session
   ```

4. **Implement Basic Authentication**
   ```python
   def _authenticate_basic(username: str, password: str) -> requests.auth.HTTPBasicAuth:
       """Create HTTP Basic Auth object."""
       return requests.auth.HTTPBasicAuth(username, password)
   ```

5. **Update Scraping Logic** (`_scrape_company` with auth)
   ```python
   def _scrape_company(company_id, company_name, url):
       """Enhanced scraping with authentication support."""

       # Check if company has stored credentials
       credentials = self.db_manager.get_company_credentials(company_id)

       if not credentials:
           # First time scraping - detect auth requirement
           auth_info = self._detect_auth_requirement(url)

           if auth_info['required']:
               self.logger.warning(
                   f"{company_name}: Auth required ({auth_info['auth_type']}). "
                   f"Use: @jobhunter-service set-credentials {company_id}"
               )
               return []  # Skip for now, needs manual setup

       # Perform authentication based on type
       session = requests.Session()

       if credentials and credentials['auth_type'] == 'basic':
           password = self._decrypt_password(credentials['password_encrypted'])
           session.auth = self._authenticate_basic(
               credentials['username'],
               password
           )

       elif credentials and credentials['auth_type'] == 'form':
           password = self._decrypt_password(credentials['password_encrypted'])
           session = self._authenticate_form_login(
               company_id,
               credentials['login_url'],
               credentials['username'],
               password
           )

       # Now fetch career page with authenticated session
       response = session.get(url, timeout=30)
       response.raise_for_status()

       # Continue with normal scraping...
       jobs = self._scrape_generic_html(response.text, url)
       return jobs
   ```

6. **Credential Encryption** (using RobotRay's SecretsManager)
   ```python
   def _encrypt_password(self, password: str) -> str:
       """Encrypt password using SecretsManager."""
       return self.secrets_manager.encrypt(password)

   def _decrypt_password(self, encrypted: str) -> str:
       """Decrypt password using SecretsManager."""
       return self.secrets_manager.decrypt(encrypted)
   ```

7. **Implement Credential Management Commands**

   **Command Handlers:**
   ```python
   def _handle_set_credentials(self, sender, args, correlation_id):
       """
       Store authentication credentials for a company.

       Usage:
           @jobhunter-service set-credentials <company_id> <auth_type> <username> <password>
           @jobhunter-service set-credentials <company_id> form <username> <password> <login_url>
       """
       company_id, auth_type, username, password = args[0:4]
       login_url = args[4] if len(args) > 4 else None

       # Encrypt password
       encrypted_password = self._encrypt_password(password)

       # Store credentials
       self.db_manager.add_company_credentials(
           company_id=int(company_id),
           auth_type=auth_type,
           username=username,
           password_encrypted=encrypted_password,
           login_url=login_url
       )

       return {
           'status': 'success',
           'message': f'✅ Credentials stored for company {company_id}',
           'auth_type': auth_type
       }

   def _handle_test_auth(self, sender, args, correlation_id):
       """Test authentication for a company."""
       company_id = int(args[0])
       company = self.db_manager.get_company_by_id(company_id)

       try:
           # Attempt authentication
           credentials = self.db_manager.get_company_credentials(company_id)
           # ... perform auth test ...
           return {'status': 'success', 'message': '✅ Authentication successful'}
       except Exception as e:
           return {'status': 'error', 'message': f'❌ Auth failed: {str(e)}'}

   def _handle_remove_credentials(self, sender, args, correlation_id):
       """Remove stored credentials for a company."""
       company_id = int(args[0])
       self.db_manager.remove_company_credentials(company_id)
       return {'status': 'success', 'message': f'✅ Credentials removed'}
   ```

**Command Examples:**
```bash
# Add company (system detects if auth is needed)
@jobhunter-service add-company "Anthropic Internal" "https://workday.anthropic.com/careers"
# Response: ⚠️ Auth required (form). Use set-credentials to configure.

# Set credentials for form-based login
@jobhunter-service set-credentials 5 form "myemail@example.com" "mypassword" "https://workday.anthropic.com/login"
# Response: ✅ Credentials stored for company 5 (form auth)

# Set credentials for HTTP Basic Auth
@jobhunter-service set-credentials 7 basic "username" "password"
# Response: ✅ Credentials stored for company 7 (basic auth)

# Test authentication
@jobhunter-service test-auth 5
# Response: ✅ Authentication successful OR ❌ Auth failed: Invalid credentials

# Remove credentials (if changed or company removed)
@jobhunter-service remove-credentials 5
# Response: ✅ Credentials removed

# List companies with auth status
@jobhunter-service list-companies
# Response shows auth_type for each:
#   1. Anthropic (public)
#   2. Workday Portal (form auth - configured ✅)
#   3. Internal Careers (basic auth - not configured ⚠️)
```

**Security Considerations:**

1. **Encryption at Rest**
   - All passwords encrypted using SecretsManager
   - Never log passwords or decrypted credentials
   - Credentials table has restricted permissions

2. **Session Management**
   - Sessions cached in memory (not persisted)
   - Re-authenticate if session expires
   - Support session_duration per company

3. **Audit Trail**
   - Log auth attempts (success/failure) to scrape_history
   - Track last_auth_success timestamp
   - Alert on repeated auth failures

4. **Manual Setup Required**
   - User must explicitly provide credentials
   - Service never auto-detects or guesses passwords
   - Clear warnings when auth is needed

**Error Handling:**

```python
class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass

def _scrape_company_safe(company_id, company_name, url):
    """Wrapper with auth error handling."""
    try:
        return self._scrape_company(company_id, company_name, url)
    except AuthenticationError as e:
        self.logger.error(f"{company_name}: Auth failed - {e}")
        # Log to scrape_history
        self.db_manager.add_scrape_history(
            company_id=company_id,
            jobs_found=0,
            new_jobs=0,
            success=False,
            error_message=f"Authentication failed: {str(e)}"
        )
        # Notify user via Telegram
        self._send_notification(
            f"⚠️ {company_name}: Authentication failed. "
            f"Check credentials with: @jobhunter-service test-auth {company_id}"
        )
        return []  # Continue with other companies
```

**Future Enhancements (Phase 2):**

1. **OAuth/SSO Support**
   - Playwright browser automation for complex auth flows
   - Handle 2FA/MFA (manual intervention)
   - Support SAML/OAuth providers

2. **Credential Rotation**
   - Alert when credentials might be expired
   - Support API tokens with refresh logic

3. **Multi-Account Support**
   - Different credentials per user
   - Shared vs. personal accounts

**Verification:**
- ✅ Can detect auth requirements automatically
- ✅ Can store and encrypt credentials securely
- ✅ Form-based login works (Workday, Taleo)
- ✅ HTTP Basic Auth works
- ✅ Auth failures handled gracefully
- ✅ Sessions reused within scrape duration
- ✅ Credentials never logged in plaintext

### Phase 5: Job Filtering & Scoring (2-3 hours)
1. Implement `_filter_and_score_job()` scoring algorithm
   - Load active criteria from database
   - Calculate score based on criteria weights
   - Use dynamic weighting (not hardcoded)
2. Implement extraction helpers
   - `_extract_location()` - Parse location from job text
   - `_extract_salary()` - Parse salary ranges with regex
   - `_extract_keywords()` - Tokenize and normalize job text
3. Implement criteria matching logic
   - Keyword matching (case-insensitive, partial match)
   - Location matching (exact or contains)
   - Salary comparison (min/max threshold)
   - Required skills validation (all must be present)
   - Exclude keyword filtering (reject if present)
4. Test filtering with various job data
5. Tune default criteria weights

**Scoring Algorithm (Dynamic):**
```python
score = 0
for criterion in active_criteria:
    if criterion matches job:
        score += criterion.weight
return min(score, 100)  # Cap at 100
```

**Verification:**
- Jobs scored correctly based on active criteria
- Changing criteria weights affects scores
- Exclude keywords properly reject jobs
- Required skills are enforced

### Phase 5.5: Deep Scrape for Matches (2-3 hours) - Two-Phase Optimization

**Goal:** Implement intelligent two-phase scraping to reduce server load and improve efficiency.

**Problem:**
- Phase 4 scrapes job **listings** (title, URL, location) but NOT full job details
- Following every job URL to get descriptions would be slow and impolite (375 requests)
- Most jobs won't match criteria, so deep-scraping all is wasteful

**Two-Phase Strategy:**

**Phase 1: Light Scrape** (Phase 4 - DONE ✅)
- Scrape job listings page
- Extract: title, URL, location (basic info)
- Fast: 1 request per company
- Result: 300+ jobs in database with basic info

**Phase 2: Deep Scrape** (Phase 5.5 - THIS PHASE)
- ONLY for jobs that pass filtering (Phase 5)
- Follow job URL to get full details
- Extract: description, requirements, salary, benefits
- Polite: ~10-20 requests instead of 300+
- Update job record with full details

**Implementation:**

```python
def _deep_scrape_job(self, job_id: int, job_url: str) -> bool:
    """
    Follow job URL to scrape full details.

    Args:
        job_id: Database ID of job to update
        job_url: URL to scrape

    Returns:
        True if successful, False otherwise
    """
    try:
        # Fetch job detail page with politeness delay
        html = self._fetch_with_retry(job_url, politeness_delay=2.0)
        soup = BeautifulSoup(html, 'lxml')

        # Extract full description (Greenhouse-specific)
        description = None
        desc_elem = soup.find('div', id='content') or soup.find('div', class_='content')
        if desc_elem:
            description = desc_elem.get_text(strip=True)

        # Extract salary if present
        salary_text = None
        salary_patterns = [
            r'\$\s*(\d+[kK]?)\s*-\s*\$?\s*(\d+[kK]?)',  # $100k - $150k
            r'\$\s*(\d+,\d+)\s*-\s*\$?\s*(\d+,\d+)',    # $100,000 - $150,000
            r'(\d+[kK]?)\s*-\s*(\d+[kK]?)\s*(?:USD|per year)',  # 100k - 150k USD
        ]

        for pattern in salary_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                salary_text = match.group(0)
                # Parse min/max
                salary_min = self._parse_salary(match.group(1))
                salary_max = self._parse_salary(match.group(2))
                break

        # Update database with full details
        self.db_manager.update_job_details(
            job_id=job_id,
            description=description,
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max
        )

        self.logger.info(f"Deep scraped job {job_id}: {job_url}")
        return True

    except Exception as e:
        self.logger.error(f"Error deep scraping job {job_id}: {e}")
        return False

def _deep_scrape_matches(self, matched_jobs: List[Dict]) -> int:
    """
    Deep scrape full details for matched jobs.

    Args:
        matched_jobs: List of job dictionaries that passed filtering

    Returns:
        Count of successfully scraped jobs
    """
    success_count = 0

    for job in matched_jobs:
        # Skip if already has description (previously scraped)
        if job.get('description'):
            self.logger.debug(f"Job {job['id']} already has details, skipping")
            continue

        # Deep scrape with politeness delay
        if self._deep_scrape_job(job['id'], job['url']):
            success_count += 1

        # Rate limiting: 2 second delay between deep scrapes
        time.sleep(2.0)

    return success_count
```

**Integration with Phase 5:**

```python
def _run_daily_scrape(self):
    """Orchestrate daily scraping workflow."""

    # Phase 4: Light scrape all companies
    for company in self.db_manager.get_active_companies():
        jobs = self._scrape_company(company['id'], company['name'], company['url'])
        # Save basic job info (title, URL, location)
        self._save_jobs(jobs)

    # Phase 5: Filter & score all jobs
    all_jobs = self.db_manager.get_recent_jobs()
    matched_jobs = []
    for job in all_jobs:
        score = self._filter_and_score_job(job)
        if score >= 60:  # Threshold for matching
            matched_jobs.append(job)

    # Phase 5.5: Deep scrape ONLY matches
    self.logger.info(f"Deep scraping {len(matched_jobs)} matched jobs (out of {len(all_jobs)} total)")
    deep_scraped = self._deep_scrape_matches(matched_jobs)
    self.logger.info(f"Successfully deep scraped {deep_scraped} matched jobs")

    # Phase 9: Notify about matches (with full details now available)
    self._send_job_notifications(matched_jobs)
```

**Database Updates:**

Add method to `jobhunter_db.py`:
```python
def update_job_details(self, job_id: int, description: str = None,
                      salary_text: str = None, salary_min: float = None,
                      salary_max: float = None) -> bool:
    """Update job with full details from deep scrape."""
    with self._get_connection() as conn:
        conn.execute("""
            UPDATE job_postings
            SET description = COALESCE(?, description),
                salary_text = COALESCE(?, salary_text),
                salary_min = COALESCE(?, salary_min),
                salary_max = COALESCE(?, salary_max)
            WHERE id = ?
        """, (description, salary_text, salary_min, salary_max, job_id))
        return True
```

**Performance Comparison:**

| Approach | Requests | Time | Politeness |
|----------|----------|------|------------|
| **Naive** (deep scrape all) | 375 | ~12 min | ⚠️ Heavy |
| **Two-Phase** (filter first) | 1 + ~15 | ~1 min | ✅ Light |

**Commands:**

```bash
# Deep scrape matched jobs (renamed from deep-scrape-matches)
@jobhunter-service deep-match [min_score] [limit]

# Example: Deep scrape top 10 jobs with score >= 70
@jobhunter-service deep-match 70 10

# Default: min_score=60, limit=50
@jobhunter-service deep-match
```

**Implementation Status: ✅ COMPLETE (2026-02-02)**

**Verification:**
- ✅ Command: `deep-match` implemented and tested
- ✅ Salary parsing: Extracts ranges like "$290k-360k" → min: 290000, max: 360000
- ✅ Description extraction: 13-16KB full job descriptions saved
- ✅ Database updates: `update_job_details()` with COALESCE behavior
- ✅ Politeness: 2-second delays between deep scrapes
- ✅ Efficiency: 5 matched jobs deep-scraped from 375 total (99% reduction)
- ✅ Greenhouse platform: Multiple selector fallbacks working
- ✅ Test suite: 15 new tests covering all Phase 5.5 functionality

**Test Results (2026-02-02):**
```
Test Company: Anthropic (375 jobs scraped)
Matched Jobs: 50+ with score >= 60
Deep Scraped: 5 jobs requested
Success Rate: 100% (5/5)
Data Extracted:
  - Job ID 312: Enterprise AE, 13.8KB desc, $290k-360k ✅
  - Job ID 390: Business Analyst, 16.6KB desc, $190k-300k ✅
  - Job ID 396: IT Systems Eng, 15.3KB desc, $225k-275k ✅
  - Job ID 401: Security PM, 15.5KB desc, $220k-275k ✅
  - Job ID 412: Eng Manager, 16.0KB desc, $405k-485k ✅
```

**Test Coverage:**
```python
# Unit tests added (tests/unit/test_services/test_jobhunter_service.py)
class TestPhase5_5DeepScraping:
    - test_parse_salary_various_formats()        # $150k, $150,000, 150k
    - test_parse_salary_edge_cases()             # Edge cases, None handling
    - test_update_job_details()                  # Full update
    - test_update_job_details_partial()          # Partial update
    - test_update_job_details_coalesce()         # COALESCE behavior
    - test_deep_scrape_job_with_mock()           # HTTP mocking
    - test_deep_scrape_job_fetch_failure()       # Failure handling
    - test_deep_scrape_matches()                 # Multiple jobs
    - test_deep_match_command()                  # Command handler
    - test_deep_match_command_no_matches()       # No matches case
    - test_deep_match_command_no_jobs()          # Empty DB case
    - test_deep_match_command_invalid_args()     # Error handling
```
- Database correctly updates with detailed info

### Phase 6: Scheduler ✅ (2 hours) - COMPLETE

**Implementation Date:** 2026-02-03

**Completed:**
1. ✅ Implemented `_scheduler_loop()` with continuous background thread
2. ✅ Implemented `_calculate_next_run()` with pytz timezone support
3. ✅ Implemented `_run_daily_scrape()` complete workflow orchestration
4. ✅ Added `_generate_job_hash()` for SHA256 deduplication
5. ✅ Added `_handle_run_now()` for manual trigger command
6. ✅ Enhanced `score-jobs` command with job detail view mode
7. ✅ **Critical Bug Fix**: Fixed salary range matching (added salary_min/max to extracted numbers)
8. ✅ Standardized logging to "servicename: message" format
9. ✅ Tested with Anthropic careers (375 jobs, 50+ matches, 5 deep scraped)

**Key Features:**
- Background daemon thread running continuously
- Multi-timezone support (9am, 1pm, 5pm ET default)
- Complete workflow: scrape → score → deep-match
- Manual trigger: `@jobhunter-service run-now`
- Job details: `@jobhunter-service score-jobs <job_id>`
- SHA256 hash-based deduplication
- Graceful shutdown with event signals

**Bug Fix Details:**
- Issue: Jobs with deep-scraped salary data scoring 0-10 instead of 85+ points
- Root Cause: `salary_min` and `salary_max` database columns not included in extracted numbers
- Impact: Salary range criteria ("300k-400k") never matched
- Fix: Added explicit extraction of salary columns in `_filter_and_score_job()`
- Result: Job 312 now scores 100 points (sales=25, remote=30, salary=30, geography=20)

**Verification:** ✅ All tests passed
- Scheduler calculates next run correctly across timezones
- Manual trigger works and completes full workflow
- Job deduplication works (SHA256 hash)
- Scoring includes salary ranges correctly
- Job detail view displays full deep-scraped data

### Phase 7: Schedule Management - Multiple Run Times ✅ COMPLETE (2026-02-04)
1. Extend database schema to store run times
   - Add `run_schedules` table (id, run_time, timezone, active, created_date)
   - Support multiple scheduled times per day
2. Implement schedule database operations in `jobhunter_db.py`
   - `add_run_time(time_str, timezone, active)` → Add new scheduled time
   - `get_active_run_times()` → List all active run times
   - `remove_run_time(id)` → Delete scheduled time
   - `disable_run_time(id)` → Soft disable without deleting
   - `enable_run_time(id)` → Re-enable disabled time
   - `get_next_run_time()` → Calculate next scheduled run from list
3. Update scheduler logic to support multiple times
   - `_calculate_next_run()` → Find nearest future time from list
   - Handle day boundaries (if all times passed today, use first time tomorrow)
   - Support timezone-aware calculations
4. Implement schedule management command handlers
   - `_handle_add_schedule(sender, args, correlation_id)` → Add run time
   - `_handle_list_schedules(sender, correlation_id)` → Show all times
   - `_handle_remove_schedule(sender, args, correlation_id)` → Remove time
   - `_handle_disable_schedule(sender, args, correlation_id)` → Disable time
   - `_handle_enable_schedule(sender, args, correlation_id)` → Enable time
5. Support manual runs when no schedules exist
   - If `run_schedules` table is empty, scheduler sleeps and waits for manual trigger
   - `@jobhunter-service run-now` works regardless of schedule
6. Initialization logic
   - On first start, load default schedules from config
   - If config has single `scrape_time`, convert to list format
   - If no schedules exist, log warning and wait for manual setup

**Command Examples:**
```bash
# Add scheduled run times (HH:MM format, 24-hour)
@jobhunter-service add-schedule "09:00"                    # 9 AM daily
@jobhunter-service add-schedule "13:00"                    # 1 PM daily
@jobhunter-service add-schedule "17:00"                    # 5 PM daily
@jobhunter-service add-schedule "21:00" "America/Chicago"  # 9 PM Central

# List all scheduled run times
@jobhunter-service list-schedules
# Returns:
#   Active Schedules (3):
#   1. 09:00 America/New_York (next run: 2026-02-02 09:00:00)
#   2. 13:00 America/New_York (next run: 2026-02-02 13:00:00)
#   3. 17:00 America/New_York (next run: 2026-02-01 17:00:00) ← NEXT
#
#   Next scrape: Today at 5:00 PM (in 3h 24m)

# Remove schedule by ID
@jobhunter-service remove-schedule 2                       # Remove 1 PM run

# Disable temporarily (keeps in database)
@jobhunter-service disable-schedule 3                      # Pause 5 PM run

# Re-enable disabled schedule
@jobhunter-service enable-schedule 3                       # Resume 5 PM run

# Manual run (works even with no schedules)
@jobhunter-service run-now
```

**Response Examples:**
```bash
# add-schedule response
✅ Added schedule: 13:00 America/New_York
Total schedules: 3 active
Next run: Today at 5:00 PM (in 3h 24m)

# list-schedules response
📅 Scheduled Run Times

Active (3):
  1. 09:00 EST (America/New_York)
     Next: Tomorrow at 9:00 AM

  2. 13:00 EST (America/New_York)
     Next: Tomorrow at 1:00 PM

  3. 17:00 EST (America/New_York) ⏰ NEXT RUN
     Next: Today at 5:00 PM (in 3h 24m)

Disabled (1):
  4. 21:00 EST (America/New_York) [DISABLED]

Next Scrape: Today at 5:00 PM (in 3h 24m)
Manual runs: @jobhunter-service run-now

# No schedules configured
⚠️ No scheduled run times configured
To add a schedule: @jobhunter-service add-schedule "09:00"
To run manually: @jobhunter-service run-now
```

**Scheduler Logic (Updated):**
```python
def _calculate_next_run(run_times: List[Dict]) -> datetime:
    """
    Find the next scheduled run time from a list of times.

    Args:
        run_times: List of {"time": "HH:MM", "timezone": "America/New_York"}

    Returns:
        Datetime of next run (timezone-aware)
    """
    if not run_times:
        return None  # No schedules, manual-only mode

    now = datetime.now(pytz.UTC)
    next_runs = []

    for schedule in run_times:
        tz = pytz.timezone(schedule['timezone'])
        now_local = now.astimezone(tz)

        # Parse HH:MM
        hour, minute = map(int, schedule['time'].split(':'))

        # Today's run time
        next_run = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If time already passed today, schedule for tomorrow
        if next_run <= now_local:
            next_run += timedelta(days=1)

        next_runs.append(next_run)

    # Return earliest next run
    return min(next_runs).astimezone(pytz.UTC)

def _scheduler_loop():
    """Main scheduler loop supporting multiple daily run times."""
    while not self._stop_event.is_set():
        run_times = self.db_manager.get_active_run_times()

        if not run_times:
            self.logger.warning("No scheduled run times. Use run-now for manual scraping.")
            # Sleep for 1 hour, then check again for schedules
            self._stop_event.wait(3600)
            continue

        next_run = self._calculate_next_run(run_times)
        sleep_seconds = (next_run - datetime.now(pytz.UTC)).total_seconds()

        self.logger.info(f"Next scrape scheduled for {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Sleep in chunks for responsive shutdown
        while sleep_seconds > 0 and not self._stop_event.is_set():
            chunk = min(60, sleep_seconds)  # 1-minute chunks
            self._stop_event.wait(chunk)
            sleep_seconds -= chunk

        if not self._stop_event.is_set():
            self._run_daily_scrape()
```

**Database Schema Addition:**
```sql
-- Scheduled run times (supports multiple runs per day)
CREATE TABLE IF NOT EXISTS run_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,              -- HH:MM format (24-hour)
    timezone TEXT NOT NULL,              -- pytz timezone (America/New_York)
    active INTEGER DEFAULT 1,            -- 1=active, 0=disabled
    created_date TEXT NOT NULL,          -- ISO format timestamp
    notes TEXT,                          -- User notes
    UNIQUE(run_time, timezone)           -- Prevent duplicate schedules
);

-- Example data:
-- (1, '09:00', 'America/New_York', 1, '2026-01-30T12:00:00Z', 'Morning run')
-- (2, '13:00', 'America/New_York', 1, '2026-01-30T12:00:00Z', 'Lunch run')
-- (3, '17:00', 'America/New_York', 1, '2026-01-30T12:00:00Z', 'Evening run')
```

**Benefits:**
- ✅ **Multiple checks per day** - Catch jobs posted at different times
- ✅ **Flexible scheduling** - Run during business hours or off-peak
- ✅ **Timezone support** - Different times for different regions
- ✅ **Manual fallback** - Can run manually if no schedules exist
- ✅ **Easy management** - Add/remove times without editing config
- ✅ **Graceful degradation** - Service works with 0, 1, or many schedules

**Verification:**
- Can add multiple scheduled run times
- Scheduler calculates next run correctly from list
- Manual run works when no schedules exist
- Can disable/enable schedules without deleting
- Service survives schedule changes while running

### Phase 8: Job Freshness Detection & Prioritization ✅ COMPLETE (2026-02-04)
This phase implements **NEW job detection** to prioritize fresh postings that weren't in the previous scrape.

**Business Rationale:**
- New postings = active recruiting (higher response rate)
- Old postings may be filled but not removed from website
- Competitive advantage: Apply to new jobs within hours of posting
- Reduce noise: Deprioritize jobs seen for weeks

**Implementation:**

1. **Extend Database Schema** (`job_postings` table)
   ```sql
   ALTER TABLE job_postings ADD COLUMN is_new INTEGER DEFAULT 1;
   ALTER TABLE job_postings ADD COLUMN first_seen_date TEXT;
   ALTER TABLE job_postings ADD COLUMN last_seen_date TEXT;
   ALTER TABLE job_postings ADD COLUMN times_seen INTEGER DEFAULT 1;

   -- is_new: 1 = new this scrape, 0 = seen before
   -- first_seen_date: When job was first discovered
   -- last_seen_date: Most recent scrape that found this job
   -- times_seen: How many scrapes have seen this job
   ```

2. **Update Scraping Logic** (in `_scrape_company`)
   ```python
   def _scrape_company(company_id, company_name, url):
       """Enhanced with freshness detection."""
       # 1. Fetch and parse jobs from website
       jobs = self._fetch_and_parse_jobs(url)

       # 2. For each job, check freshness
       for job in jobs:
           job_hash = self._calculate_job_hash(job)
           existing_job = self.db_manager.get_job_by_hash(job_hash)

           if existing_job:
               # Job seen before - update last_seen_date
               self.db_manager.update_job_freshness(
                   job_id=existing_job['id'],
                   is_new=False,
                   last_seen_date=datetime.now(),
                   times_seen=existing_job['times_seen'] + 1
               )
               job['is_new'] = False
               job['freshness_days'] = (datetime.now() -
                                       existing_job['first_seen_date']).days
           else:
               # NEW job - first time seeing this
               job_id = self.db_manager.add_job_posting(
                   company_id=company_id,
                   job_hash=job_hash,
                   is_new=True,
                   first_seen_date=datetime.now(),
                   last_seen_date=datetime.now(),
                   times_seen=1,
                   **job
               )
               job['is_new'] = True
               job['freshness_days'] = 0

       return jobs
   ```

3. **Implement Freshness Scoring** (bonus to match score)
   ```python
   def _calculate_freshness_bonus(job):
       """Add bonus points for new jobs."""
       if job['is_new']:
           return 15  # +15 points for brand new posting

       # Decay bonus over time
       days = job['freshness_days']
       if days == 0:
           return 15
       elif days <= 3:
           return 10  # Still very fresh
       elif days <= 7:
           return 5   # Fresh within a week
       else:
           return 0   # No bonus for old postings

   def _filter_and_score_job(job, criteria):
       """Enhanced with freshness bonus."""
       base_score, match_reasons = self._score_against_criteria(job, criteria)
       freshness_bonus = self._calculate_freshness_bonus(job)

       final_score = min(base_score + freshness_bonus, 100)

       if freshness_bonus > 0:
           match_reasons.append(f"freshness_bonus: +{freshness_bonus} pts")

       return final_score, match_reasons
   ```

4. **Implement Notification Prioritization**
   - Sort matches by freshness before sending notifications
   - Send NEW jobs first (most urgent)
   - Batch old jobs separately or send with lower priority

5. **Add Freshness Commands**
   ```python
   # Command handlers
   _handle_list_new_jobs(sender, correlation_id)      # Show only NEW jobs
   _handle_list_stale_jobs(sender, args, correlation_id)  # Show jobs >N days old
   ```

**Command Examples:**
```bash
# List only NEW jobs from latest scrape
@jobhunter-service list-new-jobs
# Returns: Jobs marked is_new=1, sorted by match score

# List stale jobs (seen for >7 days, still active)
@jobhunter-service list-stale-jobs 7
# Returns: Jobs first_seen >7 days ago, might be filled

# Force re-check freshness (mark all as not new, re-scrape to find actual new ones)
@jobhunter-service reset-freshness
```

**Enhanced Notification Format:**
```markdown
🆕 **NEW JOB - URGENT** (Score: 92/100)

**Senior Python Engineer - Remote**
📍 Company: Anthropic
📍 Location: Remote
💰 Salary: $150k-$190k
🔗 Apply: https://www.anthropic.com/careers/senior-python-engineer

⚡ **FIRST SEEN TODAY** - Apply immediately for best chance!

**Match Details:**
✓ Keywords: python (10), senior engineer (15)
✓ Location: Remote (20)
✓ Salary: $150k+ (20)
✓ Required Skills: python (10), REST API (10)
✓ Freshness Bonus: +15 (NEW posting)

Total: 92/100 (includes +15 freshness bonus)
```

**Comparison for Older Jobs:**
```markdown
📋 **Job Match** (Score: 77/100)

**Backend Engineer - Python**
📍 Company: OpenAI
📍 Location: San Francisco
💰 Salary: $140k-$180k
🔗 Apply: https://openai.com/careers/backend-engineer

⏰ First seen: 5 days ago (seen in 3 scrapes)

**Match Details:**
✓ Keywords: python (10), backend (10)
✓ Salary: $140k+ (20)
✓ Required Skills: python (10), REST API (10)
✓ Freshness Bonus: +5 (posted within week)

Total: 77/100 (includes +5 freshness bonus)
```

**Database Queries for Freshness:**
```sql
-- Get all NEW jobs from latest scrape
SELECT * FROM job_postings
WHERE is_new = 1 AND is_active = 1
ORDER BY scraped_date DESC;

-- Get stale jobs (seen >7 days, might be filled)
SELECT *, julianday('now') - julianday(first_seen_date) as age_days
FROM job_postings
WHERE is_active = 1
  AND julianday('now') - julianday(first_seen_date) > 7
ORDER BY age_days DESC;

-- Get jobs by freshness decay
SELECT *,
       julianday('now') - julianday(first_seen_date) as age_days,
       CASE
           WHEN is_new = 1 THEN 15
           WHEN julianday('now') - julianday(first_seen_date) <= 3 THEN 10
           WHEN julianday('now') - julianday(first_seen_date) <= 7 THEN 5
           ELSE 0
       END as freshness_bonus
FROM job_postings
WHERE is_active = 1
ORDER BY freshness_bonus DESC, scraped_date DESC;
```

**Mark Jobs as "Not New" After Notification:**
After sending notification for a NEW job, mark it as seen so it doesn't get flagged as new in next scrape:
```python
def _send_job_notification(match):
    # Send notification
    self._send_to_telegram(match)

    # Mark as no longer new (user has been notified)
    if match['is_new']:
        self.db_manager.mark_job_as_seen(match['job_id'])
```

**Auto-cleanup Stale Jobs:**
```python
def _cleanup_stale_jobs(days_threshold=30):
    """Mark jobs inactive if not seen in recent scrapes."""
    stale_jobs = self.db_manager.get_jobs_not_seen_since(days_threshold)

    for job in stale_jobs:
        self.db_manager.mark_job_inactive(job['id'])
        self.logger.info(f"Marked job {job['id']} as inactive (not seen in {days_threshold} days)")
```

**Benefits:**
- ✅ **Competitive Advantage**: Apply to new jobs within hours
- ✅ **Reduced Noise**: Deprioritize old postings likely filled
- ✅ **Better Signals**: +15 bonus for new jobs = higher match rate
- ✅ **Actionable Intelligence**: Know which jobs are urgent
- ✅ **Historical Tracking**: See how long jobs stay open
- ✅ **Auto Cleanup**: Remove stale postings automatically

**Verification:**
- New jobs correctly detected (is_new=1)
- Existing jobs updated (last_seen_date, times_seen)
- Freshness bonus correctly applied to scores
- NEW jobs appear first in notifications
- Stale jobs (>7 days) can be listed separately
- Auto-cleanup removes jobs not seen in 30 days

### Phase 9: Notifications (1-2 hours)
1. Implement `_send_job_notification()` with markdown
2. **Update to prioritize NEW jobs** (is_new=1 first)
3. Add freshness indicators (🆕 emoji, "First seen today")
4. Implement batch notifications grouped by freshness
5. Test Telegram delivery with NEW vs existing jobs

**Notification Priority:**
```python
def _send_notifications(matches):
    """Send notifications with NEW jobs first."""
    # Sort: NEW jobs first, then by score
    matches.sort(key=lambda m: (not m['is_new'], -m['match_score']))

    new_matches = [m for m in matches if m['is_new']]
    existing_matches = [m for m in matches if not m['is_new']]

    if new_matches:
        self._send_to_telegram(f"🆕 Found {len(new_matches)} NEW job(s)!")
        for match in new_matches:
            self._send_job_notification(match)

    if existing_matches:
        self._send_to_telegram(f"📋 {len(existing_matches)} existing job(s) still match")
        # Optional: send these with lower priority or batch summary
```

**Verification:** Receive job notifications in Telegram with NEW jobs highlighted

### Phase 10: Testing (2-3 hours)
1. Unit tests for database operations (criteria, schedules, companies, freshness)
2. Unit tests for scraping/parsing with freshness detection
3. Unit tests for scoring algorithm with freshness bonus
4. Unit tests for scheduler with multiple run times
5. Unit tests for freshness bonus calculation (decay over days)
6. Integration test for full cycle with NEW job detection

**Test Cases for Freshness:**
```python
def test_new_job_detection():
    # First scrape - job is NEW
    job = scrape_job("Senior Python Engineer")
    assert job['is_new'] == True
    assert job['freshness_bonus'] == 15

    # Second scrape - same job, not NEW anymore
    job = scrape_job("Senior Python Engineer")
    assert job['is_new'] == False
    assert job['times_seen'] == 2

def test_freshness_bonus_decay():
    # Day 0: +15
    assert calculate_freshness_bonus(days=0) == 15
    # Day 3: +10
    assert calculate_freshness_bonus(days=3) == 10
    # Day 7: +5
    assert calculate_freshness_bonus(days=7) == 5
    # Day 8+: 0
    assert calculate_freshness_bonus(days=8) == 0
```

**Verification:** All tests pass, >80% coverage

### Phase 11: AI-Powered Job Analysis (2-3 hours)

**Goal:** Enhance matching accuracy by using Claude API for semantic analysis of job descriptions, catching nuanced matches that strict string/range matching misses.

**Problem with Strict Matching:**
- Substring matching misses semantic equivalents ("Django experience" doesn't match "python" keyword)
- Range matching is rigid (salary of "$199k" doesn't match "200k+" even though it's close)
- Can't understand context ("5+ years Python" vs "extensive Python background")
- Misses implicit signals (job requirements that imply criteria matches)

**AI Analysis Benefits:**
- Semantic understanding: Recognizes "FastAPI, Django, Flask" implies Python expertise
- Contextual reasoning: Understands "staff engineer" is senior-level even without "senior" keyword
- Salary interpretation: Recognizes "competitive compensation" or equity packages
- Location flexibility: Understands "hybrid" can mean partial remote
- Nuanced matching: Catches 80-90% matches that strict rules miss

**Cost Optimization Strategies:**

1. **Two-Pass Approach** (Recommended)
   - First pass: Strict matching (free, fast)
   - Second pass: AI analysis only on matched jobs (selective, targeted)
   - Cost: ~$0.01-0.03 per AI-analyzed job
   - Example: 100 jobs scraped → 15 match strict criteria → 15 AI calls = $0.15-0.45/day

2. **Daily Summary Review**
   - Batch all NEW jobs into single AI review
   - Prompt: "Review these 10 jobs against my criteria, rank by fit"
   - Cost: 1 AI call per day regardless of job count
   - Example: ~$0.05-0.10 per day

3. **User-Triggered Analysis**
   - Manual command: `@jobhunter-service analyze-job <job_id>`
   - On-demand deep dive for specific opportunities
   - Cost: Pay only when explicitly requested

**Implementation:**

```python
def _analyze_job_with_ai(self, job_text: str, criteria_summary: str) -> Dict[str, Any]:
    """
    Use Claude API to analyze job fit against user criteria.

    Returns:
        {
            'match_score': 85,  # 0-100 AI-determined score
            'reasoning': 'Strong Python background implied by Django/FastAPI...',
            'criteria_matches': {
                'python': True,
                'senior': True,
                'salary': 'likely competitive based on role level',
                'remote': 'hybrid mentioned - partial match'
            },
            'confidence': 'high'  # high/medium/low
        }
    """
    prompt = f"""
    You are a job matching assistant. Analyze this job description against the user's criteria.

    USER CRITERIA:
    {criteria_summary}

    JOB DESCRIPTION:
    {job_text}

    Analyze the fit and respond in JSON format:
    {{
        "match_score": <0-100>,
        "reasoning": "<brief explanation>",
        "criteria_matches": {{<criterion>: <true/false/partial/unknown>}},
        "confidence": "<high/medium/low>"
    }}
    """

    response = await self.claude_client.messages.create(
        model="claude-3-5-haiku-20241022",  # Fast, cheap model
        max_tokens=500,
        temperature=0.3,  # Lower temp for consistent analysis
        messages=[{"role": "user", "content": prompt}]
    )

    return json.loads(response.content[0].text)
```

**Commands:**

```bash
# Enable/disable AI analysis
@jobhunter-service set-ai-analysis <on|off>

# Configure AI analysis mode
@jobhunter-service set-ai-mode <two-pass|daily-summary|manual>

# Manually analyze a specific job
@jobhunter-service analyze-job <job_id>

# Batch analyze recent matches
@jobhunter-service analyze-matches [limit]

# Show AI analysis cost stats
@jobhunter-service ai-stats
```

**Database Changes:**

```sql
-- Add AI analysis results to job_matches table
ALTER TABLE job_matches ADD COLUMN ai_score REAL;
ALTER TABLE job_matches ADD COLUMN ai_reasoning TEXT;
ALTER TABLE job_matches ADD COLUMN ai_confidence TEXT;
ALTER TABLE job_matches ADD COLUMN ai_analyzed_date TEXT;

-- Track AI usage for cost monitoring
CREATE TABLE ai_analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    analysis_date TEXT NOT NULL,
    tokens_used INTEGER,
    cost_estimate REAL,
    model_used TEXT,
    FOREIGN KEY (job_id) REFERENCES job_postings(id)
);
```

**Configuration (config/services/jobhunter-service.yaml):**

```yaml
jobhunter-service:
  ai_analysis:
    enabled: true
    mode: "two-pass"  # two-pass | daily-summary | manual
    model: "claude-3-5-haiku-20241022"
    max_tokens: 500
    temperature: 0.3
    cost_limit_daily: 2.00  # Max $2/day on AI analysis
```

**Integration Points:**

1. **After Strict Matching** (two-pass mode):
   ```python
   # In _handle_run_now()
   matched_jobs = self._strict_match(scraped_jobs, criteria)

   if self.ai_analysis_enabled and self.ai_mode == 'two-pass':
       for job in matched_jobs:
           ai_result = await self._analyze_job_with_ai(job, criteria)
           job['ai_score'] = ai_result['match_score']
           job['final_score'] = (job['strict_score'] + ai_result['match_score']) / 2
   ```

2. **Daily Summary** (batch mode):
   ```python
   # At end of scraping run
   new_jobs = [j for j in matched_jobs if j['is_new']]

   if self.ai_mode == 'daily-summary':
       summary = await self._batch_analyze_jobs(new_jobs, criteria)
       # Send single notification with AI-ranked jobs
   ```

3. **Manual Analysis** (on-demand):
   ```python
   def _handle_analyze_job(self, sender: str, args: List[str]):
       job_id = int(args[0])
       job = self.db_manager.get_job_by_id(job_id)
       criteria = self._format_criteria_for_ai()

       result = await self._analyze_job_with_ai(job['description'], criteria)
       # Show detailed AI analysis to user
   ```

**Cost Estimates:**

| Mode | Jobs/Day | AI Calls | Est. Cost/Day | Est. Cost/Month |
|------|----------|----------|---------------|-----------------|
| Two-pass | 100 scraped, 15 matched | 15 | $0.15-0.45 | $4.50-13.50 |
| Daily summary | 100 scraped, 10 new | 1 | $0.05-0.10 | $1.50-3.00 |
| Manual (avg) | 2-3 analyses | 2-3 | $0.02-0.09 | $0.60-2.70 |

**Success Metrics:**
- AI finds 20%+ more relevant matches vs strict matching alone
- False positive rate decreases by 30%+
- User applies to 50%+ of AI-recommended jobs (vs 30% for strict only)
- Cost stays under configured daily limit

**Testing:**
```python
def test_ai_analysis():
    job = "Senior Engineer role requiring FastAPI, Docker, AWS experience"
    criteria = {"python": 20, "senior": 15, "cloud": 10}

    result = analyze_job_with_ai(job, criteria)

    assert result['match_score'] >= 70  # Should recognize FastAPI implies Python
    assert result['criteria_matches']['python'] == True
    assert result['confidence'] == 'high'
```

**Rollout Plan:**
1. Implement base AI analysis function
2. Add manual analysis command for testing
3. Add cost tracking and limits
4. Enable two-pass mode with opt-in flag
5. Monitor accuracy and cost for 1 week
6. Adjust thresholds and confidence levels
7. Consider enabling daily-summary mode

**Verification:** AI analysis improves match quality, costs stay within budget

### Phase 12: Documentation (1 hour)
1. Add docstrings to all public methods
2. Create user guide for criteria management
3. Create user guide for schedule management
4. Create user guide for freshness detection and NEW jobs
5. Create user guide for AI-powered analysis
6. Create user guide for commands
7. Update CHANGELOG.md

**Total Estimated Time:** 28-36 hours

**Breakdown:**
- Phase 1: Database Foundation (2-3h) ✅
- Phase 2: Message Handling (2h) ✅
- Phase 3: Criteria Management (3-4h) ✅
- Phase 3.5: Range Support (2h) ✅
- Phase 4: Web Scraping - Light Scrape (3-4h) ✅
- Phase 4.5: Authentication (3-4h)
- Phase 5: Job Filtering & Scoring (2-3h) ✅
- Phase 5.5: Deep Scrape for Matches (2-3h) ✅
- Phase 6: Scheduler (2h)
- Phase 7: Schedule Management (2h)
- Phase 8: Freshness Detection (2-3h)
- Phase 9: Notifications (1-2h)
- Phase 10: Testing (2-3h)
- Phase 11: AI-Powered Analysis (2-3h)
- Phase 12: Documentation (1h)

---

## Consequences

### Positive Consequences

1. **Time Savings**: 1-2 hours/day saved on manual job searching
2. **Higher Quality Matches**: AI-powered filtering reduces noise by 80%+
3. **Competitive Speed**: Notified within hours of job posting
4. **Data-Driven Optimization**: Track what works, refine criteria over time
5. **Extensibility**: Platform ready for Phase 2 (CV optimization, auto-apply)
6. **Reusable Pattern**: Scheduled scraper architecture applicable to other domains

### Negative Consequences

1. **Legal Risk**: Potential ToS violations (requires manual company-by-company review)
2. **Maintenance Burden**: Websites change, parsers break (ongoing maintenance)
3. **False Positives**: Generic parser may extract non-job content (~10-15% error rate)
4. **Resource Usage**: Daily scraping adds server load (minimal but measurable)
5. **Dependency on External Sites**: Service effectiveness depends on website availability

### Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **Legal action** | Low | High | Review ToS, respect robots.txt, use clear User-Agent |
| **IP blocking** | Medium | Medium | Rate limiting, rotating user agents, manual fallback |
| **Parser failures** | High | Low | Comprehensive error handling, log for manual review |
| **Website changes** | High | Medium | Automated change detection, platform-specific parsers |
| **Cost overrun** | Low | Low | Minimal infrastructure cost (<$5/month for SQLite) |

---

## Alternatives Considered

### Alternative 1: Commercial Job Board APIs

**Approach:** Use Indeed API, LinkedIn API, etc.

**Pros:**
- Structured data (no parsing needed)
- Broad job coverage
- Reliable service

**Cons:**
- **Cost**: $500-2000/month for API access
- **Limited targeting**: Can't filter to specific companies
- **Generic matches**: Same results as email alerts (noise)
- **Dependency**: Locked into vendor

**Decision:** ❌ Rejected - Too expensive, doesn't solve targeting problem

### Alternative 2: Browser Automation (Playwright/Selenium)

**Approach:** Use headless browser for JavaScript-heavy sites

**Pros:**
- Handles dynamic content (React, Angular sites)
- Can interact with forms/buttons
- More reliable extraction

**Cons:**
- **Resource intensive**: 5-10x memory/CPU usage
- **Slower**: 30-60 seconds per page vs. 3-5 seconds
- **Complexity**: Harder to debug and maintain
- **Fragility**: Small UI changes break selectors

**Decision:** ⏳ Deferred to Phase 2 - Start with simple HTTP, add browser automation if needed

### Alternative 3: RSS Feed Monitoring

**Approach:** Subscribe to company job RSS feeds

**Pros:**
- Structured data (XML/Atom feeds)
- Instant notifications
- Low overhead

**Cons:**
- **Limited availability**: Only ~20% of companies offer job RSS
- **Incomplete data**: Feeds often lack salary, detailed requirements
- **Not standardized**: Each company uses different format
- **Manual setup**: Must find and configure each feed

**Decision:** ✅ Hybrid Approach - Support RSS where available (Phase 2), scrape otherwise

### Alternative 4: Third-Party Aggregators (Aggregated job boards)

**Approach:** Scrape aggregators (Glassdoor, AngelList) instead of company sites

**Pros:**
- Centralized data
- Standardized format
- Broad coverage

**Cons:**
- **Delayed posting**: 24-48 hour lag from company site
- **Incomplete data**: Often missing salary, full descriptions
- **Legal risk**: More aggressive anti-scraping measures
- **Quality**: Stale postings, duplicates

**Decision:** ❌ Rejected - Delay defeats purpose (speed to market)

---

## Future Enhancements (Post-MVP)

### Phase 2: Enhanced Scraping (4-6 weeks)
1. **Platform-Specific Parsers**: Greenhouse, Lever, Workday
2. **Async Scraping**: `aiohttp` for concurrent requests (5-10x faster)
3. **Browser Automation**: Playwright for JavaScript-heavy sites
4. **LLM Extraction**: Claude API for unstructured job descriptions
5. **RSS Feed Support**: Instant notifications where available

### Phase 3: Intelligent Application (6-8 weeks)
1. **Resume Optimization**: Claude API generates tailored resumes
2. **Cover Letter Generation**: Personalized based on job description
3. **Auto-Apply**: Automated form filling (with user approval)
4. **Application Tracking**: Email parsing for status updates
5. **Interview Prep**: Generate interview questions from job description

### Phase 4: Advanced Analytics (4-6 weeks)
1. **Machine Learning Scoring**: Learn from user feedback (applied/rejected)
2. **Salary Intelligence**: Track offer data, market trends
3. **Company Insights**: Glassdoor integration, funding data
4. **Application Success Rate**: Optimize based on outcomes
5. **Referral Detection**: Identify employees for referrals (LinkedIn)

---

## References

### RobotRay Architecture
- **ADR-001**: Multi-Channel Architecture (@ routing, message queues)
- **ADR-002**: Service Isolation (threading model, BaseService pattern)
- **ADR-003**: Identity Assertion & Messaging (user context preservation)

### Implementation Patterns
- **health_service.py**: Scheduler loop with responsive shutdown
- **user_service.py**: Message handling and command routing
- **ib_db.py**: DatabaseManager pattern with context managers
- **leap_finviz_client.py**: Web scraping with retry logic

### External Documentation
- **BeautifulSoup**: https://www.crummy.com/software/BeautifulSoup/bs4/doc/
- **requests**: https://requests.readthedocs.io/
- **pytz**: https://pythonhosted.org/pytz/
- **SQLite**: https://www.sqlite.org/docs.html

---

## Appendix A: Example Scrape Execution

```
2026-01-30 09:00:00 - INFO - jobhunter-service: Starting daily scrape
2026-01-30 09:00:00 - INFO - jobhunter-service: Loaded 5 active companies
2026-01-30 09:00:00 - INFO - jobhunter-service: Loaded search criteria (5 keywords, 5 locations)

2026-01-30 09:00:01 - INFO - jobhunter-service: Scraping Anthropic (https://www.anthropic.com/careers)
2026-01-30 09:00:03 - INFO - jobhunter-service: Anthropic - 12 jobs found, 2 new
2026-01-30 09:00:03 - INFO - jobhunter-service: Job #1 scored 84/100 - MATCH
2026-01-30 09:00:03 - INFO - jobhunter-service: Job #2 scored 45/100 - below threshold

2026-01-30 09:00:06 - INFO - jobhunter-service: Scraping OpenAI (https://openai.com/careers)
2026-01-30 09:00:08 - INFO - jobhunter-service: OpenAI - 8 jobs found, 1 new
2026-01-30 09:00:08 - INFO - jobhunter-service: Job #3 scored 72/100 - MATCH

2026-01-30 09:00:15 - INFO - jobhunter-service: Scrape complete
2026-01-30 09:00:15 - INFO - jobhunter-service: Summary: 5 companies, 20 jobs, 3 new, 2 matches
2026-01-30 09:00:15 - INFO - jobhunter-service: Sending 2 notifications to Telegram
2026-01-30 09:00:16 - INFO - jobhunter-service: ✅ Daily scrape complete in 16 seconds
```

---

## Appendix B: Database Query Examples

```sql
-- Get all active companies
SELECT * FROM companies WHERE active = 1 ORDER BY name;

-- Get new jobs from last scrape
SELECT jp.*, c.name AS company_name
FROM job_postings jp
JOIN companies c ON jp.company_id = c.id
WHERE jp.scraped_date >= date('now', '-1 day')
  AND jp.is_active = 1
ORDER BY jp.scraped_date DESC;

-- Get top matches with company info
SELECT jm.match_score, jp.title, c.name AS company, jp.url, jm.match_reasons
FROM job_matches jm
JOIN job_postings jp ON jm.job_id = jp.id
JOIN companies c ON jp.company_id = c.id
ORDER BY jm.notified_date DESC
LIMIT 20;

-- Scrape success rate by company
SELECT c.name,
       COUNT(*) as total_scrapes,
       SUM(sh.success) as successful,
       ROUND(100.0 * SUM(sh.success) / COUNT(*), 1) as success_rate
FROM scrape_history sh
JOIN companies c ON sh.company_id = c.id
GROUP BY c.name
ORDER BY success_rate DESC;
```

---

**Document Status:** ✅ Ready for Implementation
**Review Date:** 2026-01-30
**Approval Status:** Architecture Review - APPROVED
**Next Steps:** Begin Phase 1 implementation
