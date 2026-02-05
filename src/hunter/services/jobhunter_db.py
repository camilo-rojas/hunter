"""
JobHunter Database Manager

Handles database schema creation, initialization, and connection management
for the JobHunter service - automated job search and matching system.

Tables:
- companies: Target companies to scrape
- company_credentials: Authentication credentials (encrypted)
- job_postings: All scraped job listings
- job_matches: Jobs that passed filtering criteria
- scrape_history: Audit trail of scrape executions
- search_criteria: User-defined criteria (keywords, locations, salary, etc.)
- run_schedules: Multiple daily run times for scraping
"""

import sqlite3
import logging
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class JobHunterDatabaseManager:
    """
    JobHunter Database Manager

    Handles database schema creation, initialization, and connection management.
    Follows the same pattern as IBDatabaseManager for consistency.
    """

    def __init__(self, db_path: str = "data/jobhunter_service.db"):
        """Initialize the database manager."""
        self.db_path = db_path
        self.logger = logging.getLogger("robotray.service.jobhunter-db")

        # Ensure data directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.logger.debug(f"JobHunterDatabaseManager initialized with database: {db_path}")

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Return rows as dictionaries
        conn.execute("PRAGMA foreign_keys = ON")  # Enable foreign key constraints
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            # UNIQUE constraint errors are expected (duplicates), log at debug
            if 'UNIQUE constraint' in str(e):
                self.logger.debug(f"Duplicate entry skipped: {e}")
            else:
                self.logger.error(f"Database error: {e}")
            raise
        finally:
            conn.close()

    def initialize_database(self):
        """Create database tables if they don't exist."""
        try:
            with self._get_connection() as conn:
                # Table 1: Companies to scrape
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS companies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        careers_url TEXT NOT NULL,
                        scraper_type TEXT DEFAULT 'generic',  -- generic, greenhouse, lever, workday
                        active INTEGER DEFAULT 1,             -- 1=active, 0=inactive
                        notes TEXT,
                        added_date TEXT NOT NULL,             -- ISO format
                        last_scraped TEXT,                    -- ISO format
                        total_scrapes INTEGER DEFAULT 0,
                        created_by TEXT DEFAULT 'system'
                    )
                """)

                # Table 2: Authentication credentials (encrypted)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS company_credentials (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id INTEGER NOT NULL,
                        auth_type TEXT NOT NULL,              -- 'none', 'basic', 'form', 'oauth'
                        login_url TEXT,                       -- URL of login page (for form auth)
                        username TEXT,                        -- Username or email
                        password_encrypted TEXT,              -- Encrypted password
                        additional_fields TEXT,               -- JSON for extra form fields
                        session_cookie_name TEXT,             -- Cookie name to preserve
                        session_duration INTEGER DEFAULT 3600, -- How long session lasts (seconds)
                        last_auth_success TEXT,               -- Last successful authentication
                        auth_notes TEXT,                      -- Manual notes about auth process
                        created_date TEXT NOT NULL,
                        FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                        UNIQUE(company_id)                    -- One credential set per company
                    )
                """)

                # Table 3: Job postings (with freshness tracking)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS job_postings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id INTEGER NOT NULL,
                        job_hash TEXT NOT NULL,               -- SHA256 hash for deduplication
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        location TEXT,
                        salary_min REAL,                      -- Extracted minimum salary
                        salary_max REAL,                      -- Extracted maximum salary
                        salary_text TEXT,                     -- Raw salary text
                        description TEXT,                     -- Job description (truncated)
                        posted_date TEXT,                     -- Job posting date (if available)
                        scraped_date TEXT NOT NULL,           -- When we scraped it
                        is_active INTEGER DEFAULT 1,          -- 1=still on website, 0=removed

                        -- Freshness tracking (Phase 8)
                        is_new INTEGER DEFAULT 1,             -- 1=new this scrape, 0=seen before
                        first_seen_date TEXT,                 -- When job was first discovered
                        last_seen_date TEXT,                  -- Most recent scrape that found it
                        times_seen INTEGER DEFAULT 1,         -- Count of scrapes that saw it

                        FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                        UNIQUE(job_hash, company_id)          -- Prevent duplicates
                    )
                """)

                # Indexes for job_postings
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_job_postings_company_id
                    ON job_postings(company_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_job_postings_scraped_date
                    ON job_postings(scraped_date)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_job_postings_is_new
                    ON job_postings(is_new, is_active)
                """)

                # Table 4: Job matches (jobs that passed filtering)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS job_matches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id INTEGER NOT NULL,
                        match_score REAL NOT NULL,            -- 0-100 score
                        match_reasons TEXT,                   -- JSON array of why it matched
                        notified_date TEXT NOT NULL,          -- When user was notified
                        notification_status TEXT DEFAULT 'sent', -- sent, failed, queued
                        applied_date TEXT,                    -- When user applied (manual update)
                        application_status TEXT,              -- applied, interview, offer, rejected
                        notes TEXT,                           -- User notes
                        FOREIGN KEY (job_id) REFERENCES job_postings(id) ON DELETE CASCADE
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_job_matches_notified_date
                    ON job_matches(notified_date)
                """)

                # Table 5: Scrape history (audit trail)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS scrape_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id INTEGER NOT NULL,
                        scrape_date TEXT NOT NULL,            -- ISO format
                        jobs_found INTEGER DEFAULT 0,         -- Total jobs found
                        new_jobs INTEGER DEFAULT 0,           -- New jobs (first time seen)
                        success INTEGER DEFAULT 1,            -- 1=success, 0=failure
                        error_message TEXT,                   -- Error details if failed
                        duration_seconds REAL,                -- How long scrape took
                        FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
                    )
                """)

                # Table 6: Search criteria (user-defined filters)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS search_criteria (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        criterion_type TEXT NOT NULL,         -- keyword, location, min_salary, etc.
                        criterion_value TEXT NOT NULL,        -- The actual value
                        weight REAL DEFAULT 10,               -- Scoring weight (0-100)
                        active INTEGER DEFAULT 1,             -- 1=active, 0=disabled
                        created_date TEXT NOT NULL,
                        notes TEXT,
                        UNIQUE(criterion_type, criterion_value) -- Prevent duplicate criteria
                    )
                """)

                # Table 7: Run schedules (multiple daily run times)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS run_schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_time TEXT NOT NULL,               -- HH:MM format (24-hour)
                        timezone TEXT NOT NULL,               -- pytz timezone (America/New_York)
                        active INTEGER DEFAULT 1,             -- 1=active, 0=disabled
                        created_date TEXT NOT NULL,
                        notes TEXT,
                        UNIQUE(run_time, timezone)            -- Prevent duplicate schedules
                    )
                """)

                # Table 8: Criterion groups (Phase 3 - grouped/tiered criteria)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS criterion_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,            -- Human-readable name (departments, geography)
                        match_type TEXT NOT NULL,             -- 'any' (OR), 'all' (AND), 'tiered' (preferences)
                        weight REAL DEFAULT 0,                -- Base weight (for 'any' and 'all' types)
                        active INTEGER DEFAULT 1,             -- 1=active, 0=disabled
                        created_date TEXT NOT NULL,
                        notes TEXT
                    )
                """)

                # Table 9: Criterion group values (Phase 3 - values within groups)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS criterion_group_values (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id INTEGER NOT NULL,
                        value TEXT NOT NULL,                  -- The actual match value
                        tier_level TEXT,                      -- For tiered: 'preferred', 'acceptable', 'undesirable'
                        tier_weight REAL,                     -- Override weight for this tier (NULL uses group weight)
                        created_date TEXT NOT NULL,
                        FOREIGN KEY (group_id) REFERENCES criterion_groups(id) ON DELETE CASCADE,
                        UNIQUE(group_id, value)               -- Prevent duplicate values in same group
                    )
                """)

                # Index for criterion_group_values
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_criterion_group_values_group_id
                    ON criterion_group_values(group_id)
                """)

                self.logger.info("Database schema initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}")
            raise

    # ============================================================================
    # Company Management
    # ============================================================================

    def add_company(self, name: str, careers_url: str, scraper_type: str = 'generic',
                   notes: str = '', created_by: str = 'system') -> int:
        """Add a new company to scrape."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO companies (name, careers_url, scraper_type, notes,
                                         added_date, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (name, careers_url, scraper_type, notes,
                     datetime.now(timezone.utc).isoformat(), created_by))

                company_id = cursor.lastrowid
                self.logger.info(f"Added company: {name} (ID: {company_id})")
                return company_id

        except sqlite3.IntegrityError:
            self.logger.warning(f"Company already exists: {name}")
            raise ValueError(f"Company '{name}' already exists")

    def get_active_companies(self) -> List[Dict[str, Any]]:
        """Get all active companies."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM companies WHERE active = 1 ORDER BY name
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_company_by_id(self, company_id: int) -> Optional[Dict[str, Any]]:
        """Get company by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM companies WHERE id = ?
            """, (company_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def remove_company(self, company_id: int) -> bool:
        """Mark company as inactive (soft delete)."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE companies SET active = 0 WHERE id = ?
            """, (company_id,))
            return True

    def update_last_scraped(self, company_id: int, jobs_found: int):
        """Update company last scraped timestamp."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE companies
                SET last_scraped = ?, total_scrapes = total_scrapes + 1
                WHERE id = ?
            """, (datetime.now(timezone.utc).isoformat(), company_id))

    # ============================================================================
    # Job Posting Management (Phase 4)
    # ============================================================================

    def job_exists_by_hash(self, company_id: int, job_hash: str) -> bool:
        """
        Check if a job already exists in the database by hash.

        Args:
            company_id: Company ID
            job_hash: SHA256 hash of job (title + URL)

        Returns:
            True if job exists, False otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM job_postings
                WHERE company_id = ? AND job_hash = ?
            """, (company_id, job_hash))
            count = cursor.fetchone()[0]
            return count > 0

    def add_job_posting(self, company_id: int, job_hash: str, title: str,
                       url: str, location: str = None, description: str = None) -> int:
        """
        Add a job posting to the database.

        Args:
            company_id: Company ID
            job_hash: SHA256 hash for deduplication
            title: Job title
            url: Job URL
            location: Job location (optional)
            description: Job description (optional)

        Returns:
            Job posting ID
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO job_postings (
                    company_id, job_hash, title, url, location, description,
                    scraped_date, is_active, is_new, first_seen_date, last_seen_date, times_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, 1)
            """, (
                company_id, job_hash, title, url, location, description,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat()
            ))
            return cursor.lastrowid

    def update_job_details(self, job_id: int, description: str = None,
                          salary_text: str = None, salary_min: float = None,
                          salary_max: float = None) -> bool:
        """
        Update job posting with full details from deep scrape.

        Args:
            job_id: Job posting ID
            description: Full job description (optional)
            salary_text: Raw salary text extracted (optional)
            salary_min: Minimum salary if extracted (optional)
            salary_max: Maximum salary if extracted (optional)

        Returns:
            True if successful
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE job_postings
                SET description = COALESCE(?, description),
                    salary_text = COALESCE(?, salary_text),
                    salary_min = COALESCE(?, salary_min),
                    salary_max = COALESCE(?, salary_max)
                WHERE id = ?
            """, (description, salary_text, salary_min, salary_max, job_id))
            self.logger.debug(f"Updated job ID {job_id} with full details")
            return True

    def mark_job_as_seen(self, job_id: int) -> bool:
        """
        Mark a job as seen (no longer new) after notification (Phase 9).

        Args:
            job_id: Job posting ID

        Returns:
            True if successful
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE job_postings
                SET is_new = 0
                WHERE id = ?
            """, (job_id,))
            self.logger.debug(f"Marked job ID {job_id} as seen (is_new=0)")
            return True

    def get_job_by_id(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get a job posting by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM job_postings WHERE id = ?
            """, (job_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_jobs(self, company_id: int = None, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent job postings.

        Args:
            company_id: Filter by company (optional)
            limit: Maximum number of jobs to return

        Returns:
            List of job dictionaries
        """
        with self._get_connection() as conn:
            if company_id:
                cursor = conn.execute("""
                    SELECT * FROM job_postings
                    WHERE company_id = ?
                    ORDER BY scraped_date DESC
                    LIMIT ?
                """, (company_id, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM job_postings
                    ORDER BY scraped_date DESC
                    LIMIT ?
                """, (limit,))

            return [dict(row) for row in cursor.fetchall()]

    # ============================================================================
    # Search Criteria Management
    # ============================================================================

    def add_criterion(self, criterion_type: str, criterion_value: str,
                     weight: float = 10, active: int = 1, notes: str = '') -> int:
        """Add a search criterion."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO search_criteria (criterion_type, criterion_value,
                                                weight, active, created_date, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (criterion_type, criterion_value, weight, active,
                     datetime.now(timezone.utc).isoformat(), notes))

                criterion_id = cursor.lastrowid
                self.logger.info(f"Added criterion: {criterion_type}={criterion_value} (weight: {weight})")
                return criterion_id

        except sqlite3.IntegrityError:
            self.logger.warning(f"Criterion already exists: {criterion_type}={criterion_value}")
            raise ValueError(f"Criterion '{criterion_type}={criterion_value}' already exists")

    def get_active_criteria(self) -> List[Dict[str, Any]]:
        """Get all active search criteria."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM search_criteria WHERE active = 1
                ORDER BY criterion_type, weight DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_all_criteria(self) -> List[Dict[str, Any]]:
        """Get all search criteria (including inactive)."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM search_criteria
                ORDER BY active DESC, criterion_type, weight DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_criteria_by_id(self, criterion_id: int) -> Optional[Dict[str, Any]]:
        """Get a single criterion by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM search_criteria WHERE id = ?
            """, (criterion_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_criterion(self, criterion_id: int, criterion_value: str = None,
                        weight: float = None, active: int = None) -> bool:
        """Update an existing criterion (partial updates allowed)."""
        criterion = self.get_criteria_by_id(criterion_id)
        if not criterion:
            raise ValueError(f"Criterion ID {criterion_id} not found")

        # Build update query dynamically based on provided parameters
        updates = []
        params = []

        if criterion_value is not None:
            updates.append("criterion_value = ?")
            params.append(criterion_value)

        if weight is not None:
            updates.append("weight = ?")
            params.append(weight)

        if active is not None:
            updates.append("active = ?")
            params.append(active)

        if not updates:
            return False  # Nothing to update

        params.append(criterion_id)  # For WHERE clause

        with self._get_connection() as conn:
            query = f"UPDATE search_criteria SET {', '.join(updates)} WHERE id = ?"
            conn.execute(query, params)
            self.logger.info(f"Updated criterion ID {criterion_id}")
            return True

    def remove_criterion(self, criterion_id: int) -> bool:
        """Delete a criterion."""
        with self._get_connection() as conn:
            conn.execute("""
                DELETE FROM search_criteria WHERE id = ?
            """, (criterion_id,))
            self.logger.info(f"Removed criterion ID {criterion_id}")
            return True

    def get_criteria_summary(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get criteria grouped by type for formatted display."""
        criteria = self.get_active_criteria()

        # Group by criterion_type
        summary = {}
        for criterion in criteria:
            ctype = criterion['criterion_type']
            if ctype not in summary:
                summary[ctype] = []
            summary[ctype].append(criterion)

        return summary

    # ============================================================================
    # Grouped Criteria Management (Phase 3 - NEW)
    # ============================================================================

    def add_criterion_group(self, name: str, match_type: str, weight: float = 0,
                           active: int = 1, notes: str = '') -> int:
        """Add a criterion group."""
        valid_match_types = ['any', 'all', 'tiered']
        if match_type not in valid_match_types:
            raise ValueError(f"Invalid match_type '{match_type}'. Must be one of: {valid_match_types}")

        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO criterion_groups (name, match_type, weight, active,
                                                 created_date, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (name, match_type, weight, active,
                     datetime.now(timezone.utc).isoformat(), notes))

                group_id = cursor.lastrowid
                self.logger.info(f"Added criterion group: {name} ({match_type}, weight: {weight})")
                return group_id

        except sqlite3.IntegrityError:
            self.logger.warning(f"Criterion group already exists: {name}")
            raise ValueError(f"Criterion group '{name}' already exists")

    def add_group_value(self, group_id: int, value: str, tier_level: str = None,
                       tier_weight: float = None) -> int:
        """Add a value to a criterion group."""
        # Validate group exists
        group = self.get_group_by_id(group_id)
        if not group:
            raise ValueError(f"Criterion group ID {group_id} not found")

        # Validate tier_level if provided
        if tier_level:
            valid_tiers = ['preferred', 'acceptable', 'undesirable']
            if tier_level not in valid_tiers:
                raise ValueError(f"Invalid tier_level '{tier_level}'. Must be one of: {valid_tiers}")

        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO criterion_group_values (group_id, value, tier_level,
                                                       tier_weight, created_date)
                    VALUES (?, ?, ?, ?, ?)
                """, (group_id, value, tier_level, tier_weight,
                     datetime.now(timezone.utc).isoformat()))

                value_id = cursor.lastrowid
                tier_info = f" (tier: {tier_level}, weight: {tier_weight})" if tier_level else ""
                self.logger.info(f"Added value '{value}' to group ID {group_id}{tier_info}")
                return value_id

        except sqlite3.IntegrityError:
            self.logger.warning(f"Value '{value}' already exists in group ID {group_id}")
            raise ValueError(f"Value '{value}' already exists in this group")

    def get_active_groups(self) -> List[Dict[str, Any]]:
        """Get all active criterion groups with their values."""
        with self._get_connection() as conn:
            # Get groups
            cursor = conn.execute("""
                SELECT * FROM criterion_groups WHERE active = 1
                ORDER BY name
            """)
            groups = [dict(row) for row in cursor.fetchall()]

            # Get values for each group
            for group in groups:
                cursor = conn.execute("""
                    SELECT * FROM criterion_group_values
                    WHERE group_id = ?
                    ORDER BY
                        CASE tier_level
                            WHEN 'preferred' THEN 1
                            WHEN 'acceptable' THEN 2
                            WHEN 'undesirable' THEN 3
                            ELSE 4
                        END,
                        value
                """, (group['id'],))
                group['values'] = [dict(row) for row in cursor.fetchall()]

            return groups

    def get_all_groups(self) -> List[Dict[str, Any]]:
        """Get all criterion groups (including inactive) with their values."""
        with self._get_connection() as conn:
            # Get groups
            cursor = conn.execute("""
                SELECT * FROM criterion_groups
                ORDER BY active DESC, name
            """)
            groups = [dict(row) for row in cursor.fetchall()]

            # Get values for each group
            for group in groups:
                cursor = conn.execute("""
                    SELECT * FROM criterion_group_values
                    WHERE group_id = ?
                    ORDER BY
                        CASE tier_level
                            WHEN 'preferred' THEN 1
                            WHEN 'acceptable' THEN 2
                            WHEN 'undesirable' THEN 3
                            ELSE 4
                        END,
                        value
                """, (group['id'],))
                group['values'] = [dict(row) for row in cursor.fetchall()]

            return groups

    def get_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        """Get a single criterion group by ID with its values."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM criterion_groups WHERE id = ?
            """, (group_id,))
            row = cursor.fetchone()

            if not row:
                return None

            group = dict(row)

            # Get values
            cursor = conn.execute("""
                SELECT * FROM criterion_group_values
                WHERE group_id = ?
                ORDER BY
                    CASE tier_level
                        WHEN 'preferred' THEN 1
                        WHEN 'acceptable' THEN 2
                        WHEN 'undesirable' THEN 3
                        ELSE 4
                    END,
                    value
            """, (group_id,))
            group['values'] = [dict(row) for row in cursor.fetchall()]

            return group

    def get_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a criterion group by name with its values."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM criterion_groups WHERE name = ?
            """, (name,))
            row = cursor.fetchone()

            if not row:
                return None

            group = dict(row)

            # Get values
            cursor = conn.execute("""
                SELECT * FROM criterion_group_values
                WHERE group_id = ?
                ORDER BY
                    CASE tier_level
                        WHEN 'preferred' THEN 1
                        WHEN 'acceptable' THEN 2
                        WHEN 'undesirable' THEN 3
                        ELSE 4
                    END,
                    value
            """, (group['id'],))
            group['values'] = [dict(row) for row in cursor.fetchall()]

            return group

    def update_criterion_group(self, group_id: int, name: str = None,
                              match_type: str = None, weight: float = None,
                              active: int = None) -> bool:
        """Update an existing criterion group (partial updates allowed)."""
        group = self.get_group_by_id(group_id)
        if not group:
            raise ValueError(f"Criterion group ID {group_id} not found")

        # Validate match_type if provided
        if match_type:
            valid_match_types = ['any', 'all', 'tiered']
            if match_type not in valid_match_types:
                raise ValueError(f"Invalid match_type '{match_type}'. Must be one of: {valid_match_types}")

        # Build update query dynamically
        updates = []
        params = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)

        if match_type is not None:
            updates.append("match_type = ?")
            params.append(match_type)

        if weight is not None:
            updates.append("weight = ?")
            params.append(weight)

        if active is not None:
            updates.append("active = ?")
            params.append(active)

        if not updates:
            return False

        params.append(group_id)

        with self._get_connection() as conn:
            query = f"UPDATE criterion_groups SET {', '.join(updates)} WHERE id = ?"
            conn.execute(query, params)
            self.logger.info(f"Updated criterion group ID {group_id}")
            return True

    def update_group_value(self, value_id: int, value: str = None,
                          tier_level: str = None, tier_weight: float = None) -> bool:
        """Update an existing group value (partial updates allowed)."""
        # Validate tier_level if provided
        if tier_level:
            valid_tiers = ['preferred', 'acceptable', 'undesirable']
            if tier_level not in valid_tiers:
                raise ValueError(f"Invalid tier_level '{tier_level}'. Must be one of: {valid_tiers}")

        # Build update query dynamically
        updates = []
        params = []

        if value is not None:
            updates.append("value = ?")
            params.append(value)

        if tier_level is not None:
            updates.append("tier_level = ?")
            params.append(tier_level)

        if tier_weight is not None:
            updates.append("tier_weight = ?")
            params.append(tier_weight)

        if not updates:
            return False

        params.append(value_id)

        with self._get_connection() as conn:
            query = f"UPDATE criterion_group_values SET {', '.join(updates)} WHERE id = ?"
            conn.execute(query, params)
            self.logger.info(f"Updated group value ID {value_id}")
            return True

    def remove_criterion_group(self, group_id: int) -> bool:
        """Delete a criterion group (cascade deletes all values)."""
        with self._get_connection() as conn:
            conn.execute("""
                DELETE FROM criterion_groups WHERE id = ?
            """, (group_id,))
            self.logger.info(f"Removed criterion group ID {group_id} (cascade deleted values)")
            return True

    def remove_group_value(self, value_id: int) -> bool:
        """Remove a single value from a criterion group."""
        with self._get_connection() as conn:
            conn.execute("""
                DELETE FROM criterion_group_values WHERE id = ?
            """, (value_id,))
            self.logger.info(f"Removed group value ID {value_id}")
            return True

    def get_groups_summary(self) -> Dict[str, Any]:
        """Get formatted summary of all active groups for display."""
        groups = self.get_active_groups()

        summary = {
            'total_groups': len(groups),
            'groups_by_type': {
                'any': [],
                'all': [],
                'tiered': []
            }
        }

        for group in groups:
            match_type = group['match_type']
            summary['groups_by_type'][match_type].append(group)

        return summary

    def initialize_default_groups(self, default_groups: List[Dict[str, Any]]):
        """Initialize default criterion groups from config (only if table is empty)."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM criterion_groups")
            count = cursor.fetchone()[0]

            if count > 0:
                self.logger.debug("Criterion groups already exist, skipping defaults")
                return

        self.logger.info(f"Initializing {len(default_groups)} default criterion groups")
        for group_config in default_groups:
            try:
                # Create group
                group_id = self.add_criterion_group(
                    name=group_config['name'],
                    match_type=group_config['match_type'],
                    weight=group_config.get('weight', 0),
                    active=1,
                    notes='Default group'
                )

                # Add values
                for value_config in group_config.get('values', []):
                    if isinstance(value_config, str):
                        # Simple value (for 'any' or 'all' types)
                        self.add_group_value(
                            group_id=group_id,
                            value=value_config,
                            tier_level=None,
                            tier_weight=None
                        )
                    elif isinstance(value_config, dict):
                        # Tiered value with tier_level and weight
                        self.add_group_value(
                            group_id=group_id,
                            value=value_config['value'],
                            tier_level=value_config.get('tier'),
                            tier_weight=value_config.get('weight')
                        )

            except ValueError as e:
                self.logger.warning(f"Skipping duplicate group: {e}")

    # ============================================================================
    # Run Schedule Management
    # ============================================================================

    def add_run_time(self, run_time: str, tz: str = 'America/New_York',
                    active: int = 1, notes: str = '') -> int:
        """Add a scheduled run time."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO run_schedules (run_time, timezone, active,
                                             created_date, notes)
                    VALUES (?, ?, ?, ?, ?)
                """, (run_time, tz, active, datetime.now(timezone.utc).isoformat(), notes))

                schedule_id = cursor.lastrowid
                self.logger.info(f"Added schedule: {run_time} {tz}")
                return schedule_id

        except sqlite3.IntegrityError:
            self.logger.warning(f"Schedule already exists: {run_time} {tz}")
            raise ValueError(f"Schedule '{run_time} {tz}' already exists")

    def get_active_run_times(self) -> List[Dict[str, Any]]:
        """Get all active run schedules."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM run_schedules WHERE active = 1
                ORDER BY run_time
            """)
            return [dict(row) for row in cursor.fetchall()]

    def remove_run_time(self, schedule_id: int) -> bool:
        """Delete a schedule."""
        with self._get_connection() as conn:
            conn.execute("""
                DELETE FROM run_schedules WHERE id = ?
            """, (schedule_id,))
            return True

    # ============================================================================
    # Initialization Helpers
    # ============================================================================

    def initialize_default_criteria(self, default_criteria: List[Dict[str, Any]]):
        """Initialize default search criteria from config (only if table is empty)."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM search_criteria")
            count = cursor.fetchone()[0]

            if count > 0:
                self.logger.debug("Search criteria already exist, skipping defaults")
                return

        self.logger.info(f"Initializing {len(default_criteria)} default criteria")
        for criterion in default_criteria:
            try:
                self.add_criterion(
                    criterion_type=criterion['type'],
                    criterion_value=criterion['value'],
                    weight=criterion.get('weight', 10),
                    active=1,
                    notes='Default criterion'
                )
            except ValueError as e:
                self.logger.warning(f"Skipping duplicate criterion: {e}")

    def initialize_default_schedules(self, default_schedules: List[Dict[str, str]]):
        """Initialize default run schedules from config (only if table is empty)."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM run_schedules")
            count = cursor.fetchone()[0]

            if count > 0:
                self.logger.debug("Run schedules already exist, skipping defaults")
                return

        self.logger.info(f"Initializing {len(default_schedules)} default schedules")
        for schedule in default_schedules:
            try:
                self.add_run_time(
                    run_time=schedule['time'],
                    tz=schedule.get('timezone', 'America/New_York'),
                    active=1,
                    notes='Default schedule'
                )
            except ValueError as e:
                self.logger.warning(f"Skipping duplicate schedule: {e}")
