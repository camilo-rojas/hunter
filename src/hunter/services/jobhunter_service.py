"""
JobHunter Service - Automated job search and matching service.

This service automatically scrapes company career pages, intelligently filters
positions based on user criteria (skills, location, salary), and delivers
matching opportunities via Telegram for rapid application.

ADR-005: JobHunter Service - Automated Job Search and Matching

Features:
- Daily/multi-schedule automated scraping of company career pages
- User-defined criteria with flexible weighting (keywords, location, salary, skills)
- Authentication support for login-protected career pages (Workday, Taleo, etc.)
- Job freshness detection (NEW jobs get +15 bonus and priority notification)
- Intelligent matching with multi-factor scoring algorithm
- Telegram notifications prioritizing NEW jobs
- Database persistence for companies, jobs, matches, criteria, and schedules

@ Routing Commands (to be implemented in phases):
- @jobhunter-service help
- @jobhunter-service add-company <name> <url> [scraper_type]
- @jobhunter-service list-companies
- @jobhunter-service set-criteria <type> <value> <weight>
- @jobhunter-service list-criteria
- @jobhunter-service add-schedule <time> [timezone]
- @jobhunter-service list-schedules
- @jobhunter-service run-now
- @jobhunter-service matches [limit]
- @jobhunter-service status
"""

import logging
import threading
import json
import asyncio
import shlex
import re
import os
import time
import hashlib
import pytz
from typing import Dict, Any, Optional, List, Tuple, Optional as Opt
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

from robotray.services.base import BaseService
from robotray.services.jobhunter_db import JobHunterDatabaseManager
from robotray.core.messaging import SecureMessage, MessagePriority

logger = logging.getLogger(__name__)


class JobHunterService(BaseService):
    """
    Automated job hunting service with intelligent matching.

    This is a BaseService (not ChannelService) - it provides backend
    functionality accessible from all channels via @ routing.
    """

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """
        Initialize JobHunter service.

        Args:
            name: Service name (usually "jobhunter-service")
            config: Service configuration
        """
        super().__init__(name, config)

        # Get configuration
        db_path = self.config.get('database_path', 'data/jobhunter_service.db')
        self.default_criteria = self.config.get('default_criteria', [])
        self.default_criterion_groups = self.config.get('default_criterion_groups', [])
        self.default_schedules = self.config.get('default_schedules', [])

        # Load scoring thresholds (user-configurable)
        thresholds = self.config.get('scoring_thresholds', {})
        self.threshold_high_priority = thresholds.get('high_priority', 70)
        self.threshold_good_match = thresholds.get('good_match', 60)
        self.threshold_review = thresholds.get('review', 40)
        self.threshold_low_priority = thresholds.get('low_priority', 0)

        # Initialize database manager (will be initialized during start_service)
        self.db_manager: Optional[JobHunterDatabaseManager] = None

        # Threading components
        self._message_thread: Optional[threading.Thread] = None
        self._scheduler_thread: Optional[threading.Thread] = None  # Phase 6
        self._running = False
        self._shutdown_event = threading.Event()

        self.logger.info(f"jobhunter-service: Initialized with database at {db_path}")

    def start_service(self):
        """
        Start the JobHunter service.

        This method:
        1. Initializes the database and creates tables
        2. Loads default criteria and schedules if first run
        3. Starts the scheduler thread (future phases)
        4. Starts the message loop (future phases)
        """
        try:
            self.logger.info("jobhunter-service: Starting JobHunter service...")

            # Initialize database
            db_path = self.config.get('database_path', 'data/jobhunter_service.db')

            # Check if database file exists (to determine if this is first run)
            is_first_run = not os.path.exists(db_path)

            self.db_manager = JobHunterDatabaseManager(db_path)
            self.db_manager.initialize_database()

            # Initialize default criteria, groups, and schedules ONLY on first run
            if is_first_run:
                self.logger.info("jobhunter-service: First run detected - initializing default criteria, groups, and schedules")

                if self.default_criteria:
                    self.db_manager.initialize_default_criteria(self.default_criteria)

                if self.default_criterion_groups:
                    self.db_manager.initialize_default_groups(self.default_criterion_groups)

                if self.default_schedules:
                    self.db_manager.initialize_default_schedules(self.default_schedules)
            else:
                self.logger.info("jobhunter-service: Existing database found - skipping default initialization")

            # Verify initialization
            criteria_count = len(self.db_manager.get_active_criteria())
            groups_count = len(self.db_manager.get_active_groups())
            schedule_count = len(self.db_manager.get_active_run_times())
            self.logger.info(f"jobhunter-service: Database initialized: {criteria_count} criteria, {groups_count} groups, {schedule_count} schedules")

            # Set service state
            self._running = True
            self._shutdown_event.clear()

            # Start message processing loop (Phase 2)
            self._message_thread = self.create_service_thread(
                target=self._message_loop,
                name="message-loop",
                daemon=True
            )
            self._message_thread.start()
            self.logger.info("jobhunter-service: Message loop started")

            # Start scheduler thread (Phase 6)
            self._scheduler_thread = self.create_service_thread(
                target=self._scheduler_loop,
                name="scheduler-loop",
                daemon=True
            )
            self._scheduler_thread.start()
            self.logger.info("jobhunter-service: Scheduler loop started")

            self.logger.info("jobhunter-service: JobHunter service started successfully")

        except Exception as e:
            self.logger.error(f"jobhunter-service: Failed to start JobHunter service: {e}")
            raise

    def stop_service(self):
        """
        Stop the JobHunter service.

        This method:
        1. Signals threads to stop
        2. Waits for threads to complete
        3. Cleans up resources
        """
        try:
            self.logger.info("jobhunter-service: Stopping JobHunter service...")

            # Signal threads to stop
            self._running = False
            self._shutdown_event.set()

            # Wait for threads to complete (with timeout)
            if self._message_thread and self._message_thread.is_alive():
                self._message_thread.join(timeout=5.0)
                if self._message_thread.is_alive():
                    self.logger.warning("jobhunter-service: Message loop thread did not stop gracefully")

            # TODO Phase 6: Stop scheduler thread

            self.logger.info("jobhunter-service: JobHunter service stopped successfully")

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error stopping JobHunter service: {e}")
            raise

    # ============================================================================
    # Range Support Helpers (Phase 3.5)
    # ============================================================================

    def _is_range_pattern(self, value: str) -> bool:
        """Check if value is a range pattern (200k+, 100k-, 100k-150k)."""
        # Match patterns: 200k+, 100k-, 100k-150k, 5+, 3-5
        pattern = r'^\d+[kK]?[+-]$|^\d+[kK]?-\d+[kK]?$'
        return bool(re.match(pattern, value))

    def _parse_range(self, value: str) -> Tuple[Opt[float], Opt[float]]:
        """
        Parse range pattern into min/max values.

        Returns (min, max) tuple:
        - "200k+" → (200000, None)
        - "100k-" → (None, 100000)
        - "100k-150k" → (100000, 150000)
        - "5+" → (5, None)
        - "3-5" → (3, 5)
        """
        value = value.strip()

        # Handle "200k+" (greater than or equal)
        if value.endswith('+'):
            num_str = value[:-1]
            min_val = self._parse_number(num_str)
            return (min_val, None)

        # Handle "100k-" (less than or equal)
        elif value.endswith('-'):
            num_str = value[:-1]
            max_val = self._parse_number(num_str)
            return (None, max_val)

        # Handle "100k-150k" or "3-5" (range)
        elif '-' in value:
            parts = value.split('-')
            if len(parts) == 2:
                min_val = self._parse_number(parts[0])
                max_val = self._parse_number(parts[1])
                return (min_val, max_val)

        return (None, None)

    def _parse_number(self, value: str) -> Opt[float]:
        """
        Parse number with optional 'k' suffix.

        Examples:
        - "200k" → 200000
        - "150K" → 150000
        - "5" → 5
        """
        value = value.strip().lower()

        if value.endswith('k'):
            try:
                return float(value[:-1]) * 1000
            except ValueError:
                return None
        else:
            try:
                return float(value)
            except ValueError:
                return None

    def _parse_salary(self, salary_str: str) -> Opt[float]:
        """
        Parse salary string to numeric value.

        Examples:
        - "$150k" → 150000
        - "$150,000" → 150000
        - "150k" → 150000
        - "150K" → 150000
        - "150000" → 150000

        Args:
            salary_str: Salary string to parse

        Returns:
            Numeric salary value or None if parsing fails
        """
        # Remove $ and commas
        cleaned = salary_str.replace('$', '').replace(',', '').strip()
        return self._parse_number(cleaned)

    def _generate_job_hash(self, title: str, url: str) -> str:
        """
        Generate SHA256 hash for job deduplication.

        Args:
            title: Job title
            url: Job URL

        Returns:
            SHA256 hash hex string
        """
        content = f"{title}|{url}".encode('utf-8')
        return hashlib.sha256(content).hexdigest()

    def _extract_numbers_from_text(self, text: str) -> List[float]:
        """
        Extract numeric values from job text.

        Matches:
        - $150k, $150,000, $150000
        - 150k, 150K
        - 5+ years, 3-5 years

        Returns list of normalized numbers (e.g., [150000, 5])
        """
        numbers = []

        # Pattern 1: $150k, $150,000, $150000
        pattern1 = r'\$[\d,]+[kK]?'
        for match in re.finditer(pattern1, text):
            val = match.group().replace('$', '').replace(',', '')
            num = self._parse_number(val)
            if num:
                numbers.append(num)

        # Pattern 2: Standalone numbers with k suffix (150k, 5k)
        pattern2 = r'\b\d+[kK]\b'
        for match in re.finditer(pattern2, text):
            val = match.group()
            num = self._parse_number(val)
            if num:
                numbers.append(num)

        # Pattern 3: Plain numbers in specific contexts (5+ years, 3-5 years)
        pattern3 = r'\b(\d+)\+?\s*(years?|yrs?)\b'
        for match in re.finditer(pattern3, text, re.IGNORECASE):
            try:
                num = float(match.group(1))
                numbers.append(num)
            except ValueError:
                pass

        return numbers

    def _matches_range(self, min_val: Opt[float], max_val: Opt[float], numbers: List[float]) -> bool:
        """
        Check if any number in the list matches the range.

        Args:
            min_val: Minimum value (None if no minimum)
            max_val: Maximum value (None if no maximum)
            numbers: List of numbers extracted from text

        Returns:
            True if any number falls within range
        """
        for num in numbers:
            if min_val is not None and max_val is not None:
                # Range: must be between min and max (inclusive)
                if min_val <= num <= max_val:
                    return True
            elif min_val is not None:
                # Greater than or equal
                if num >= min_val:
                    return True
            elif max_val is not None:
                # Less than or equal
                if num <= max_val:
                    return True

        return False

    def health_check(self) -> Dict[str, Any]:
        """
        Perform health check and return service status.

        Returns:
            Dict containing:
            - status: "healthy", "degraded", or "unhealthy"
            - message: Human-readable status message
            - details: Additional diagnostic information
        """
        try:
            # Check if database is accessible
            if not self.db_manager:
                return {
                    "status": "unhealthy",
                    "message": "Database manager not initialized",
                    "details": {}
                }

            # Try to query database
            criteria_count = len(self.db_manager.get_active_criteria())
            schedule_count = len(self.db_manager.get_active_run_times())
            company_count = len(self.db_manager.get_active_companies())

            return {
                "status": "healthy",
                "message": "JobHunter service is running normally",
                "details": {
                    "active_criteria": criteria_count,
                    "active_schedules": schedule_count,
                    "active_companies": company_count,
                    "database_path": self.config.get('database_path', 'data/jobhunter_service.db')
                }
            }

        except Exception as e:
            self.logger.error(f"jobhunter-service: Health check failed: {e}")
            return {
                "status": "unhealthy",
                "message": f"Health check failed: {str(e)}",
                "details": {"error": str(e)}
            }

    # ============================================================================
    # Message Handling (Phase 2)
    # ============================================================================

    def _message_loop(self):
        """Process incoming messages from message queue."""
        try:
            self.logger.info("jobhunter-service: Starting message processing loop")

            while self._running and not self._shutdown_event.is_set():
                try:
                    # Check for messages without blocking
                    message = self.message_queues.inbound_queue.get_nowait()
                    self._handle_message_sync(message)
                except asyncio.QueueEmpty:
                    # No messages, sleep briefly (asyncio.Queue raises QueueEmpty, not queue.Empty)
                    threading.Event().wait(0.1)
                except Exception as e:
                    # Log actual errors (not empty queue)
                    if str(e):  # Only log if there's an actual error message
                        self.logger.error(f"jobhunter-service: Error in message loop: {e}")
                    threading.Event().wait(0.1)

            self.logger.info("jobhunter-service: Message loop stopped")

        except Exception as e:
            self.logger.error(f"jobhunter-service: Critical error in message loop: {e}")
            import traceback
            self.logger.error(f"jobhunter-service: Traceback: {traceback.format_exc()}")

    def _handle_message_sync(self, message):
        """Handle incoming message synchronously."""
        try:
            content = message.payload.get("content", "")
            sender = message.metadata.sender_service
            correlation_id = message.metadata.correlation_id

            correlation_info = f" (correlation_id: {correlation_id[:8]}...)" if correlation_id else ""

            # Parse command (shlex handles quoted strings properly)
            try:
                parts = shlex.split(content.strip())
            except ValueError as e:
                # Unmatched quotes or other parsing errors
                self._send_error_response(
                    sender,
                    f"❌ Command parsing error: {str(e)}\n"
                    "Tip: Make sure quotes are properly matched.",
                    correlation_id=correlation_id
                )
                return

            if len(parts) < 1:
                self._send_error_response(sender, "Empty command", correlation_id=correlation_id)
                return

            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            self.logger.info(f"jobhunter-service: 📨 Processing '{command}' from {sender}{correlation_info}")
            self.logger.debug(f"jobhunter-service: Command '{command}' has {len(args)} argument(s)")

            # Route to appropriate handler
            if command == "help":
                self._handle_help(sender, correlation_id)
            elif command == "add-company":
                self._handle_add_company(sender, args, correlation_id)
            elif command == "list-companies":
                self._handle_list_companies(sender, correlation_id)
            elif command == "remove-company":
                self._handle_remove_company(sender, args, correlation_id)
            elif command == "status":
                self._handle_status(sender, correlation_id)
            # Unified criteria commands (Phase 3 - refactored)
            elif command == "add-criteria":
                self._handle_add_criteria(sender, args, correlation_id)
            elif command == "add-criteria-value":
                self._handle_add_criteria_value(sender, args, correlation_id)
            elif command == "list-criteria":
                self._handle_list_criteria(sender, correlation_id)
            elif command == "remove-criteria":
                self._handle_remove_criteria(sender, args, correlation_id)
            elif command == "remove-criteria-value":
                self._handle_remove_criteria_value(sender, args, correlation_id)
            elif command == "test-criteria":
                self._handle_test_criteria(sender, args, correlation_id)
            elif command == "scrape-company":
                self._handle_scrape_company(sender, args, correlation_id)
            elif command == "score-jobs":
                self._handle_score_jobs(sender, args, correlation_id)
            elif command == "deep-match":
                self._handle_deep_match(sender, args, correlation_id)
            elif command == "run-now":
                self._handle_run_now(sender, correlation_id)
            elif command == "add-schedule":
                self._handle_add_schedule(sender, args, correlation_id)
            elif command == "list-schedules":
                self._handle_list_schedules(sender, correlation_id)
            elif command == "remove-schedule":
                self._handle_remove_schedule(sender, args, correlation_id)
            elif command == "list-new-jobs":
                self._handle_list_new_jobs(sender, correlation_id)
            elif command == "list-stale-jobs":
                self._handle_list_stale_jobs(sender, args, correlation_id)
            else:
                self._send_error_response(
                    sender,
                    f"Unknown command: {command}. Try '@jobhunter-service help' for available commands.",
                    correlation_id=correlation_id
                )

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error handling message: {e}")
            import traceback
            self.logger.error(f"jobhunter-service: Traceback: {traceback.format_exc()}")

    # ============================================================================
    # Command Handlers
    # ============================================================================

    def _handle_help(self, sender: str, correlation_id: str = None):
        """Handle help command."""
        help_text = """
**🎯 JobHunter Service - Automated Job Search & Matching**

**Overview:**
Automatically scrape company career pages, intelligently filter positions based on your criteria (skills, location, salary), and receive matching opportunities via Telegram for rapid application.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📋 AVAILABLE COMMANDS**

**Company Management:**
• `add-company <name> <url> [scraper_type]`
  Add a company to monitor for job postings
  Example: @jobhunter-service add-company Anthropic https://www.anthropic.com/careers

• `list-companies` - Show all tracked companies
• `remove-company <id>` - Remove a company from tracking
• `status` - Service health and configuration summary
• `help` - Show this help message

**Search Criteria (Phase 3 ✅ - Unified Model):**
• `add-criteria <name> <type> [weight]`
  Create a search criterion
  Types: simple, group-any, group-all, group-tiered
  Examples:
    Simple: @jobhunter-service add-criteria remote simple 15
    Group:  @jobhunter-service add-criteria department group-tiered 0

• `add-criteria-value <name> <value> [tier] [weight]`
  Add value to a criterion (only for group types)
  For 'group-any/all': @jobhunter-service add-criteria-value department sales
  For 'group-tiered': @jobhunter-service add-criteria-value department marketing preferred 20
  Tiers: preferred, acceptable, undesirable

  **Range Support (Phase 3.5 ✅):**  Numeric ranges for salary, years, etc.
  - "200k+" = >= $200k     Example: add-criteria-value salary "200k+" preferred 20
  - "100k-" = <= $100k     Example: add-criteria-value salary "100k-" undesirable -10
  - "100k-150k" = between  Example: add-criteria-value salary "100k-150k" acceptable 10
  - "5+" = >= 5 years      Example: add-criteria-value experience "5+" preferred 15

• `list-criteria` - Show all criteria (simple and groups) with IDs
• `remove-criteria <id>` - Remove criterion by ID (see list-criteria for IDs)
• `remove-criteria-value <name> <value>` - Remove value from group criterion

**Testing:**
• `test-criteria <job_text>`
  Test scoring against sample job text (includes individual + grouped criteria)
  Example: @jobhunter-service test-criteria Senior Sales Engineer - Remote, Latin America - $150k - FastAPI

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Web Scraping (Phase 4 ✅):**
• `scrape-company <id>` - Manually scrape a specific company
  Example: @jobhunter-service scrape-company 1

**Job Filtering & Scoring (Phase 5 ✅):**
• `score-jobs [limit] [min_score]` - Score jobs against your criteria
  Example: @jobhunter-service score-jobs 50 60
  Default: Shows top 50 jobs with score >= 0

• `score-jobs <job_id>` - Show full details for specific job (with deep-scraped info)
  Example: @jobhunter-service score-jobs 412
  Shows: full description, salary, location, score breakdown

**Deep Scraping (Phase 5.5 ✅):**
• `deep-match [min_score] [limit]` - Deep scrape matched jobs for full details
  Example: @jobhunter-service deep-match 60 20
  Default: min_score=60, limit=50
  Note: Only scrapes jobs with score >= threshold (efficient two-phase strategy)

**Automated Workflow (Phase 6 ✅):**
• `run-now` - Manually trigger complete workflow
  Example: @jobhunter-service run-now
  Workflow: scrape all companies → score jobs → deep-match top results
  Note: Runs automatically on schedule, or trigger manually anytime

**🚧 COMING SOON (Phases 7-11)**

**Authentication (Phase 4.5):**
• `set-credentials <company_id> <username> <password>` - For login-protected pages

**Schedule Management (Phase 7):**
• `add-schedule <time> [timezone]` - Add daily scrape time (HH:MM)
• `list-schedules` - Show all scheduled run times
• `remove-schedule <id>` - Remove a schedule

**Job Management (Phase 8 & 9):**
• `matches [limit]` - Show recent job matches
• `list-new-jobs` - Show only NEW jobs (freshness detection)
• `list-stale-jobs <days>` - Show jobs older than N days

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**💡 QUICK START GUIDE**

**Step 1: Add Companies**
   @jobhunter-service add-company Anthropic https://www.anthropic.com/careers/jobs
   @jobhunter-service list-companies

**Step 2: Define Your Criteria**

   Simple keywords:
   @jobhunter-service add-criteria remote simple 15
   @jobhunter-service add-criteria python simple 20
   @jobhunter-service add-criteria fastapi simple 10

   Location preferences (group-any = match ANY):
   @jobhunter-service add-criteria geography group-any 15
   @jobhunter-service add-criteria-value geography "US West"
   @jobhunter-service add-criteria-value geography "Remote"
   @jobhunter-service add-criteria-value geography "Latin America"

   Salary ranges (group-tiered = match by preference):
   @jobhunter-service add-criteria salary group-tiered 0
   @jobhunter-service add-criteria-value salary "300k+" preferred 30
   @jobhunter-service add-criteria-value salary "200k-300k" preferred 25
   @jobhunter-service add-criteria-value salary "150k-200k" acceptable 10
   @jobhunter-service add-criteria-value salary "150k-" undesirable -15

**Step 3: Test Your Setup**
   @jobhunter-service test-criteria Engineering Manager - Remote - $350k - Python
   @jobhunter-service list-criteria

**Step 4: Run the Complete Workflow**

   a) Light scrape (fast - gets job listings):
      @jobhunter-service scrape-company 1
      Result: 300+ jobs with titles, URLs, locations

   b) Score and filter:
      @jobhunter-service score-jobs 50 60
      Result: See top matches (thresholds configurable in YAML)

   c) Deep scrape matches (slow - gets full details):
      @jobhunter-service deep-match 70 10
      Result: Full descriptions + salaries for top 10 jobs

   d) Review detailed matches:
      @jobhunter-service score-jobs 20 70

**📊 Two-Phase Scraping Strategy:**
- **Phase 1 (Light):** 1 request → 300+ jobs (title, URL, location)
- **Phase 2 (Deep):** ~10 requests → 10 matched jobs (full details)
- **Efficiency:** 96% fewer requests, polite to servers

**🎯 Current Status:**
✅ Company Management (Phase 1-2)
✅ Criteria Management with Ranges (Phase 3-3.5)
✅ Web Scraping - Light Scrape (Phase 4)
✅ Job Filtering & Scoring (Phase 5)
✅ Deep Scraping - Selective (Phase 5.5)
✅ Automated Scheduler (Phase 6)
🚧 Schedule Management (Phase 7)
🚧 Authentication (Phase 4.5)
🚧 Freshness Detection (Phase 8)
🚧 Notifications (Phase 9)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📚 Documentation:**
See `docs/ADR-005-jobhunter-service.md` for full architecture details

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📝 NOTES**

• **Command names:** Multi-word names use underscores (e.g., Y_Combinator, Meta-Platforms)
• **Quoted values:** Use quotes for multi-word criteria values (e.g., "US West", "Sales Engineering")
• **Scraper types:** generic (default, works for most sites), greenhouse, lever, workday
• **Score thresholds:** 70+ = high priority, 60-69 = good match, <60 = review carefully
• **Deep scrape timing:** ~2 seconds per job (be patient with large batches)
• **Database:** All jobs, criteria, and scores persist in SQLite
• **Salary extraction:** Automatically parses ranges like "$200k-300k" from job descriptions

**🔮 Coming Soon:**
• Automated scheduling (run daily at specific times)
• Telegram notifications for new matches
• Freshness detection (prioritize newly posted jobs)
• AI-powered semantic matching with Claude API

**🔗 Documentation:** See docs/ADR-005-jobhunter-service.md for full architecture details
        """.strip()

        self._send_response(sender, help_text, correlation_id=correlation_id)

    def _handle_add_company(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle add-company command."""
        try:
            if len(args) < 2:
                self._send_error_response(
                    sender,
                    "Usage: add-company <name> <url> [scraper_type]\nExample: add-company Anthropic https://www.anthropic.com/careers",
                    correlation_id=correlation_id
                )
                return

            # Parse arguments (no quotes needed - use underscores for multi-word names)
            name = args[0]
            url = args[1]
            scraper_type = args[2] if len(args) > 2 else "generic"

            # Validate URL
            if not url.startswith(('http://', 'https://')):
                self._send_error_response(
                    sender,
                    f"Invalid URL: {url}. Must start with http:// or https://",
                    correlation_id=correlation_id
                )
                return

            # Add to database
            company_id = self.db_manager.add_company(
                name=name,
                careers_url=url,
                scraper_type=scraper_type,
                notes=f"Added via @ command",
                created_by=sender
            )

            response = f"✅ Added company: **{name}** (ID: {company_id})\n"
            response += f"📍 URL: {url}\n"
            response += f"🔧 Scraper: {scraper_type}\n\n"
            response += f"Next: Run `@jobhunter-service run-now` to scrape immediately, or wait for next scheduled run."

            self._send_response(sender, response, correlation_id=correlation_id)
            self.logger.info(f"jobhunter-service: Added company '{name}' (ID: {company_id})")

        except ValueError as e:
            self._send_error_response(sender, str(e), correlation_id=correlation_id)
        except Exception as e:
            self.logger.error(f"jobhunter-service: Error adding company: {e}")
            self._send_error_response(
                sender,
                f"Failed to add company: {str(e)}",
                correlation_id=correlation_id
            )

    def _handle_list_companies(self, sender: str, correlation_id: str = None):
        """Handle list-companies command."""
        try:
            companies = self.db_manager.get_active_companies()

            if not companies:
                response = "📋 No companies configured.\n\n"
                response += "Add companies with: `@jobhunter-service add-company \"Company Name\" \"https://example.com/careers\"`"
                self._send_response(sender, response, correlation_id=correlation_id)
                return

            response = f"📋 **Active Companies ({len(companies)})**\n\n"

            for company in companies:
                response += f"**{company['id']}. {company['name']}**\n"
                response += f"   📍 URL: {company['careers_url']}\n"
                response += f"   🔧 Scraper: {company['scraper_type']}\n"

                if company['last_scraped']:
                    response += f"   🕒 Last scraped: {company['last_scraped']}\n"
                else:
                    response += f"   ⚠️ Never scraped\n"

                response += f"   📊 Total scrapes: {company['total_scrapes']}\n"

                if company['notes']:
                    response += f"   📝 Notes: {company['notes']}\n"

                response += "\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error listing companies: {e}")
            self._send_error_response(
                sender,
                f"Failed to list companies: {str(e)}",
                correlation_id=correlation_id
            )

    def _handle_remove_company(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle remove-company command."""
        try:
            if len(args) < 1:
                self._send_error_response(
                    sender,
                    "Usage: remove-company <id_or_name>",
                    correlation_id=correlation_id
                )
                return

            identifier = args[0]

            # Try to parse as ID
            try:
                company_id = int(identifier)
                company = self.db_manager.get_company_by_id(company_id)
                if not company:
                    self._send_error_response(
                        sender,
                        f"Company with ID {company_id} not found",
                        correlation_id=correlation_id
                    )
                    return
            except ValueError:
                # Not an ID, search by name
                self._send_error_response(
                    sender,
                    "Please specify company ID (use list-companies to see IDs)",
                    correlation_id=correlation_id
                )
                return

            # Remove company (soft delete)
            self.db_manager.remove_company(company_id)

            response = f"✅ Removed company: **{company['name']}** (ID: {company_id})"
            self._send_response(sender, response, correlation_id=correlation_id)
            self.logger.info(f"jobhunter-service: Removed company '{company['name']}' (ID: {company_id})")

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error removing company: {e}")
            self._send_error_response(
                sender,
                f"Failed to remove company: {str(e)}",
                correlation_id=correlation_id
            )

    def _handle_status(self, sender: str, correlation_id: str = None):
        """Handle status command."""
        try:
            health = self.health_check()
            companies = self.db_manager.get_active_companies()
            criteria = self.db_manager.get_active_criteria()
            schedules = self.db_manager.get_active_run_times()

            response = f"**JobHunter Service Status**\n\n"
            response += f"🟢 Status: {health['status']}\n"
            response += f"📊 Active companies: {len(companies)}\n"
            response += f"🎯 Active criteria: {len(criteria)}\n"
            response += f"⏰ Scheduled runs: {len(schedules)}\n\n"

            if schedules:
                response += "**Upcoming Runs:**\n"
                for schedule in schedules[:3]:
                    response += f"• {schedule['run_time']} {schedule['timezone']}\n"
            else:
                response += "⚠️ No schedules configured. Use `add-schedule` to set up automated runs.\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error getting status: {e}")
            self._send_error_response(
                sender,
                f"Failed to get status: {str(e)}",
                correlation_id=correlation_id
            )

    # ============================================================================
    # Response Helpers
    # ============================================================================

    def _send_response(self, recipient: str, content: str, correlation_id: str = None):
        """Send a response message."""
        try:
            from robotray.core.messaging import MessageMetadata

            metadata = MessageMetadata(
                sender_service=self.name,
                target_service=recipient,
                priority=MessagePriority.NORMAL,
                correlation_id=correlation_id
            )
            message = SecureMessage(
                payload={"content": content},
                metadata=metadata
            )
            self.message_queues.outbound_queue.put_nowait(message)
            self.logger.debug(f"jobhunter-service: Sent response to {recipient}")
        except Exception as e:
            self.logger.error(f"jobhunter-service: Failed to send response: {e}")

    def _send_error_response(self, recipient: str, error_message: str, correlation_id: str = None):
        """Send an error response message."""
        content = f"❌ Error: {error_message}"
        self._send_response(recipient, content, correlation_id=correlation_id)

    # ============================================================================
    # Unified Criteria Handlers (Phase 3 - Refactored)
    # ============================================================================

    def _handle_add_criteria(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle add-criteria command (unified for simple and groups)."""
        try:
            if len(args) < 2:
                self._send_error_response(
                    sender,
                    "❌ Usage: add-criteria <name> <type> [weight]\n"
                    "Types: simple, group-any, group-all, group-tiered\n"
                    "Examples:\n"
                    "  add-criteria remote simple 15\n"
                    "  add-criteria department group-tiered 0",
                    correlation_id=correlation_id
                )
                return

            name = args[0]
            criterion_type = args[1].lower()
            weight = 0

            # Parse optional weight
            if len(args) >= 3:
                try:
                    weight = float(args[2])
                except ValueError:
                    self._send_error_response(
                        sender,
                        f"❌ Invalid weight '{args[2]}'. Must be a number.",
                        correlation_id=correlation_id
                    )
                    return

            # Validate type
            valid_types = ['simple', 'group-any', 'group-all', 'group-tiered']
            if criterion_type not in valid_types:
                self._send_error_response(
                    sender,
                    f"❌ Invalid type '{criterion_type}'. Must be one of: {', '.join(valid_types)}",
                    correlation_id=correlation_id
                )
                return

            # Handle simple criteria
            if criterion_type == 'simple':
                # For simple criteria, the name IS the value
                criterion_id = self.db_manager.add_criterion(
                    criterion_type='keyword',  # Store as keyword type internally
                    criterion_value=name,
                    weight=weight,
                    active=1
                )
                response = f"✅ Added simple criterion: **{name}** (weight: {weight}, ID: {criterion_id})"
                self._send_response(sender, response, correlation_id=correlation_id)

            # Handle group criteria
            else:
                # Map group-* to internal match_type
                match_type_map = {
                    'group-any': 'any',
                    'group-all': 'all',
                    'group-tiered': 'tiered'
                }
                match_type = match_type_map[criterion_type]

                group_id = self.db_manager.add_criterion_group(
                    name=name,
                    match_type=match_type,
                    weight=weight,
                    active=1
                )

                response = f"✅ Added {criterion_type} criterion: **{name}** (weight: {weight}, ID: {group_id})\n"
                response += f"\n💡 Now add values with: add-criteria-value {name} <value>"

                if match_type == 'tiered':
                    response += f"\nFor tiered criteria, include tier and weight:\n"
                    response += f"  add-criteria-value {name} <value> <tier> <weight>\n"
                    response += "Tiers: preferred, acceptable, undesirable"

                self._send_response(sender, response, correlation_id=correlation_id)

        except ValueError as e:
            self._send_error_response(sender, f"❌ {str(e)}", correlation_id=correlation_id)
        except Exception as e:
            self.logger.error(f"jobhunter-service: Error adding criterion: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to add criterion: {str(e)}", correlation_id=correlation_id)

    def _handle_list_criteria(self, sender: str, correlation_id: str = None):
        """Handle list-criteria command (shows both simple and groups)."""
        try:
            simple_criteria = self.db_manager.get_active_criteria()
            groups = self.db_manager.get_active_groups()

            if not simple_criteria and not groups:
                response = "No criteria configured yet.\n\nUse `add-criteria` to add criteria:\n"
                response += "Examples:\n"
                response += "  Simple: add-criteria remote simple 15\n"
                response += "  Group:  add-criteria department group-tiered 0"
                self._send_response(sender, response, correlation_id=correlation_id)
                return

            response = "**🎯 Active Search Criteria**\n\n"

            # Show simple criteria
            if simple_criteria:
                response += f"**Simple Criteria ({len(simple_criteria)}):**\n"
                for criterion in simple_criteria:
                    response += f"  • **{criterion['criterion_value']}** (weight: {int(criterion['weight'])}, ID: {criterion['id']})\n"
                response += "\n"

            # Show groups
            if groups:
                response += f"**Group Criteria ({len(groups)}):**\n\n"
                for group in groups:
                    # Map internal match_type to display type
                    type_map = {'any': 'group-any', 'all': 'group-all', 'tiered': 'group-tiered'}
                    group_type = type_map.get(group['match_type'], group['match_type'])

                    response += f"**{group['name']}** ({group_type}, {int(group['weight'])} pts, ID: {group['id']})\n"

                    values = group.get('values', [])
                    if group['match_type'] == 'tiered':
                        # Group by tier for tiered groups
                        tiers = {'preferred': [], 'acceptable': [], 'undesirable': []}
                        for value in values:
                            tier = value.get('tier_level', 'unknown')
                            if tier in tiers:
                                tiers[tier].append(value)

                        for tier_name, tier_values in tiers.items():
                            if tier_values:
                                response += f"    • {tier_name.upper()}:\n"
                                for value in tier_values:
                                    value_weight = value.get('tier_weight') or 0
                                    weight_display = f"({int(value_weight):+d} pts)"
                                    response += f"      - {value['value']} {weight_display} (ID: {value['id']})\n"
                    else:
                        # Simple list for any/all groups
                        response += f"    Values ({len(values)}):\n"
                        for value in values:
                            response += f"      - {value['value']} (ID: {value['id']})\n"

                    response += "\n"

            response += "💡 Commands:\n"
            response += "  • remove-criteria <id> - Remove criterion by ID\n"
            response += "  • remove-criteria-value <name> <value> - Remove value from group"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error listing criteria: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list criteria: {str(e)}", correlation_id=correlation_id)

    def _handle_remove_criteria(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle remove-criteria command (uses ID for both simple and groups)."""
        try:
            if len(args) < 1:
                self._send_error_response(
                    sender,
                    "❌ Usage: remove-criteria <id>\n"
                    "Use list-criteria to see IDs.",
                    correlation_id=correlation_id
                )
                return

            try:
                criterion_id = int(args[0])
            except ValueError:
                self._send_error_response(
                    sender,
                    f"❌ Invalid ID '{args[0]}'. Must be a number.\n"
                    "Use list-criteria to see IDs.",
                    correlation_id=correlation_id
                )
                return

            # Try to find as simple criterion first
            simple_criteria = self.db_manager.get_active_criteria()
            found_simple = None
            for criterion in simple_criteria:
                if criterion['id'] == criterion_id:
                    found_simple = criterion
                    break

            # Try to find as group
            groups = self.db_manager.get_active_groups()
            found_group = None
            for group in groups:
                if group['id'] == criterion_id:
                    found_group = group
                    break

            # Remove based on what we found
            if found_simple:
                # Remove simple criterion
                self.db_manager.remove_criterion(criterion_id)
                response = f"✅ Removed simple criterion: **{found_simple['criterion_value']}** (ID: {criterion_id})"
                self._send_response(sender, response, correlation_id=correlation_id)
            elif found_group:
                # Remove group (cascade deletes values)
                self.db_manager.remove_criterion_group(criterion_id)
                response = f"✅ Removed group criterion: **{found_group['name']}** ({found_group['match_type']}, ID: {criterion_id})"
                self._send_response(sender, response, correlation_id=correlation_id)
            else:
                self._send_error_response(
                    sender,
                    f"❌ Criterion with ID {criterion_id} not found. Use list-criteria to see IDs.",
                    correlation_id=correlation_id
                )

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error removing criterion: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to remove criterion: {str(e)}", correlation_id=correlation_id)

    def _handle_add_criteria_value(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle add-criteria-value command."""
        try:
            if len(args) < 2:
                self._send_error_response(
                    sender,
                    "❌ Usage: add-criteria-value <name> <value> [tier] [weight]\n"
                    "Examples:\n"
                    "  add-criteria-value department sales\n"
                    "  add-criteria-value geography \"US West\" preferred 20",
                    correlation_id=correlation_id
                )
                return

            criterion_name = args[0]
            value = args[1]
            tier_level = None
            tier_weight = None

            # Get group by name
            group = self.db_manager.get_group_by_name(criterion_name)
            if not group:
                self._send_error_response(
                    sender,
                    f"❌ Criterion '{criterion_name}' not found or is not a group type. Use list-criteria to see available groups.",
                    correlation_id=correlation_id
                )
                return

            # Parse tier and weight if provided (for tiered groups)
            if len(args) >= 3:
                tier_level = args[2].lower()

            if len(args) >= 4:
                try:
                    tier_weight = float(args[3])
                except ValueError:
                    self._send_error_response(
                        sender,
                        f"❌ Invalid weight '{args[3]}'. Must be a number.",
                        correlation_id=correlation_id
                    )
                    return

            # Validate: for tiered groups, tier and weight should both be provided
            if group['match_type'] == 'tiered':
                if tier_level and tier_weight is None:
                    self._send_error_response(
                        sender,
                        f"⚠️ Warning: Adding tiered value without weight. Weight will default to 0.\n"
                        f"Usage: add-criteria-value {criterion_name} {value} {tier_level} <weight>\n"
                        f"Example: add-criteria-value {criterion_name} {value} {tier_level} 20",
                        correlation_id=correlation_id
                    )
                    return
                elif not tier_level:
                    self._send_error_response(
                        sender,
                        f"❌ This is a tiered criterion. You must specify tier and weight.\n"
                        f"Usage: add-criteria-value {criterion_name} {value} <tier> <weight>\n"
                        f"Tiers: preferred, acceptable, undesirable\n"
                        f"Example: add-criteria-value {criterion_name} {value} preferred 20",
                        correlation_id=correlation_id
                    )
                    return

            # Add value to group
            value_id = self.db_manager.add_group_value(
                group_id=group['id'],
                value=value,
                tier_level=tier_level,
                tier_weight=tier_weight
            )

            tier_info = f" (tier: {tier_level}, weight: {tier_weight})" if tier_level else ""
            response = f"✅ Added value '{value}' to criterion **{criterion_name}**{tier_info} (ID: {value_id})"
            self._send_response(sender, response, correlation_id=correlation_id)

        except ValueError as e:
            self._send_error_response(sender, f"❌ {str(e)}", correlation_id=correlation_id)
        except Exception as e:
            self.logger.error(f"jobhunter-service: Error adding criterion value: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to add criterion value: {str(e)}", correlation_id=correlation_id)

    def _handle_remove_criteria_value(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle remove-criteria-value command."""
        try:
            if len(args) < 2:
                self._send_error_response(
                    sender,
                    "❌ Usage: remove-criteria-value <name> <value>\n"
                    "Example: remove-criteria-value department marketing\n"
                    "Use list-criteria to see available values.",
                    correlation_id=correlation_id
                )
                return

            criterion_name = args[0]
            value_to_remove = args[1]

            # Get group by name
            group = self.db_manager.get_group_by_name(criterion_name)
            if not group:
                self._send_error_response(
                    sender,
                    f"❌ Criterion '{criterion_name}' not found or is not a group type.",
                    correlation_id=correlation_id
                )
                return

            # Find the value ID
            values = group.get('values', [])
            found_value = None
            for value_obj in values:
                if value_obj['value'].lower() == value_to_remove.lower():
                    found_value = value_obj
                    break

            if not found_value:
                self._send_error_response(
                    sender,
                    f"❌ Value '{value_to_remove}' not found in criterion '{criterion_name}'.",
                    correlation_id=correlation_id
                )
                return

            # Remove value
            self.db_manager.remove_group_value(found_value['id'])

            response = f"✅ Removed value '{value_to_remove}' from criterion **{criterion_name}** (ID: {found_value['id']})"
            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error removing criterion value: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to remove criterion value: {str(e)}", correlation_id=correlation_id)

    def _handle_test_criteria(self, sender: str, args: List[str], correlation_id: str = None):
        """Handle test-criteria command - test scoring against sample job text."""
        try:
            if len(args) < 1:
                self._send_error_response(
                    sender,
                    "❌ Usage: test-criteria <job_text>\n"
                    "Example: test-criteria Senior Sales Engineer - Remote, Latin America - $150k - FastAPI",
                    correlation_id=correlation_id
                )
                return

            # Join all args as job text
            job_text = ' '.join(args)
            job_text_lower = job_text.lower()

            # Get active criteria and groups
            criteria = self.db_manager.get_active_criteria()
            groups = self.db_manager.get_active_groups()

            response = f"**Test Criteria Results**\n\nSample Job: `{job_text}`\n\n"

            # Test individual criteria
            individual_score = 0
            individual_matches = []

            for criterion in criteria:
                matched = False

                if criterion['criterion_type'] == 'keyword':
                    if criterion['criterion_value'].lower() in job_text_lower:
                        matched = True
                elif criterion['criterion_type'] == 'location':
                    if criterion['criterion_value'].lower() in job_text_lower:
                        matched = True
                elif criterion['criterion_type'] in ['required_skill', 'preferred_skill']:
                    if criterion['criterion_value'].lower() in job_text_lower:
                        matched = True

                if matched:
                    individual_score += criterion['weight']
                    individual_matches.append(f"{criterion['criterion_type']}: {criterion['criterion_value']} (+{criterion['weight']})")

            # Test grouped criteria (with range support - Phase 3.5)
            group_score = 0
            group_matches = []

            # Extract numbers from job text once for efficiency
            extracted_numbers = self._extract_numbers_from_text(job_text)
            self.logger.debug(f"jobhunter-service: DEBUG: Extracted numbers from text: {extracted_numbers}")

            for group in groups:
                self.logger.debug(f"jobhunter-service: DEBUG: Checking group '{group['name']}' (match_type={group['match_type']}, {len(group.get('values', []))} values)")
                if group['match_type'] == 'any':
                    # OR logic - match any value
                    for value_obj in group['values']:
                        matched = False
                        value = value_obj['value']

                        # Check if value is a range pattern (Phase 3.5)
                        if self._is_range_pattern(value):
                            min_val, max_val = self._parse_range(value)
                            if self._matches_range(min_val, max_val, extracted_numbers):
                                matched = True
                                group_matches.append(f"{group['name']}: {value} [numeric match] (+{group['weight']})")
                        else:
                            # Fall back to substring matching
                            if value.lower() in job_text_lower:
                                matched = True
                                group_matches.append(f"{group['name']}: {value} (+{group['weight']})")

                        if matched:
                            group_score += group['weight']
                            break  # Stop at first match

                elif group['match_type'] == 'tiered':
                    # Tiered - check in order
                    for value_obj in sorted(group['values'], key=lambda x:
                            {'preferred': 0, 'acceptable': 1, 'undesirable': 2}.get(x.get('tier_level', ''), 3)):
                        matched = False
                        value = value_obj['value']
                        tier_weight = value_obj.get('tier_weight') or 0
                        tier = value_obj.get('tier_level', 'unknown')

                        # Check if value is a range pattern (Phase 3.5)
                        if self._is_range_pattern(value):
                            min_val, max_val = self._parse_range(value)
                            self.logger.debug(f"jobhunter-service: DEBUG:   '{value}' is range → ({min_val}, {max_val})")
                            if self._matches_range(min_val, max_val, extracted_numbers):
                                self.logger.debug(f"jobhunter-service: DEBUG:   MATCHED! Adding {int(tier_weight):+d} points")
                                matched = True
                                group_matches.append(f"{group['name']}: {value} [numeric match] ({tier}, {int(tier_weight):+d})")
                            else:
                                self.logger.debug(f"jobhunter-service: DEBUG:   No match")
                        else:
                            # Fall back to substring matching
                            self.logger.debug(f"jobhunter-service: DEBUG:   '{value}' is string, checking substring match")
                            if value.lower() in job_text_lower:
                                self.logger.debug(f"jobhunter-service: DEBUG:   MATCHED! Adding {int(tier_weight):+d} points")
                                matched = True
                                group_matches.append(f"{group['name']}: {value} ({tier}, {int(tier_weight):+d})")

                        if matched:
                            group_score += tier_weight
                            break

            # Build response
            response += f"**Individual Criteria: {int(individual_score)}/100**\n"
            if individual_matches:
                for match in individual_matches:
                    response += f"  ✓ {match}\n"
            else:
                response += "  (no matches)\n"

            response += f"\n**Grouped Criteria: {int(group_score):+d} pts**\n"
            if group_matches:
                for match in group_matches:
                    response += f"  ✓ {match}\n"
            else:
                response += "  (no matches)\n"

            total_score = individual_score + group_score
            response += f"\n**Total Score: {int(total_score)}** (unbounded)"

            if total_score >= self.threshold_good_match:
                response += " → 🔥 HIGH PRIORITY MATCH (would notify)"
            else:
                response += " → (saved but not notified)"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error testing criteria: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to test criteria: {str(e)}", correlation_id=correlation_id)

    def _handle_scrape_company(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle scrape-company command - manually trigger scraping for a company.

        Usage:
            @jobhunter-service scrape-company <company_id>
            @jobhunter-service scrape-company <company_name>
        """
        try:
            if not args:
                self._send_error_response(
                    sender,
                    "❌ Missing company ID or name\n\nUsage: @jobhunter-service scrape-company <company_id|name>",
                    correlation_id=correlation_id
                )
                return

            # Try to parse as ID first
            company = None
            try:
                company_id = int(args[0])
                company = self.db_manager.get_company_by_id(company_id)
            except ValueError:
                # Not an ID, try as name
                company_name = ' '.join(args)
                # TODO: Add get_company_by_name method
                self._send_error_response(
                    sender,
                    f"❌ Company lookup by name not yet implemented. Please use company ID.\n\nTip: Use '@jobhunter-service list-companies' to see IDs",
                    correlation_id=correlation_id
                )
                return

            if not company:
                self._send_error_response(
                    sender,
                    f"❌ Company not found: {args[0]}",
                    correlation_id=correlation_id
                )
                return

            # Scrape the company
            self._send_response(sender, f"🔄 Scraping {company['name']}...", correlation_id=correlation_id)

            jobs = self._scrape_company(
                company_id=company['id'],
                company_name=company['name'],
                careers_url=company['careers_url']
            )

            # Save jobs to database (skip duplicates gracefully)
            saved_count = 0
            duplicate_count = 0
            for job in jobs:
                try:
                    job_id = self.db_manager.add_job_posting(
                        company_id=job['company_id'],
                        job_hash=job['job_hash'],
                        title=job['title'],
                        url=job['url'],
                        location=job.get('location'),
                        description=job.get('description')
                    )
                    saved_count += 1
                    self.logger.debug(f"jobhunter-service: Saved job {job_id}: {job['title']}")
                except Exception as e:
                    if 'UNIQUE constraint' in str(e):
                        duplicate_count += 1
                        self.logger.debug(f"jobhunter-service: Skipping duplicate: {job['title']}")
                    else:
                        self.logger.error(f"jobhunter-service: Error saving job '{job['title']}': {e}")

            # Update company last_scraped timestamp
            self.db_manager.update_last_scraped(company['id'], len(jobs))

            # Send result
            response = f"✅ **Scrape Complete: {company['name']}**\n\n"
            response += f"📊 **Results:**\n"
            response += f"  • Total jobs found: {len(jobs)}\n"
            response += f"  • New jobs saved: {saved_count}\n"
            response += f"  • Duplicates skipped: {duplicate_count}\n"

            if jobs:
                response += f"\n**Sample Jobs:**\n"
                for job in jobs[:5]:  # Show first 5
                    location_str = f" ({job.get('location')})" if job.get('location') else ""
                    response += f"  • {job['title']}{location_str}\n"

                if len(jobs) > 5:
                    response += f"\n  ... and {len(jobs) - 5} more\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error scraping company: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to scrape company: {str(e)}", correlation_id=correlation_id)

    def _handle_score_jobs(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle score-jobs command - score recent jobs against criteria.

        Usage:
            @jobhunter-service score-jobs [limit] [min_score]
            @jobhunter-service score-jobs <job_id>  # Show details for specific job
        """
        try:
            # Check if first argument looks like a job ID (for detail view)
            if args and len(args) == 1:
                try:
                    job_id = int(args[0])
                    # Try to show details for this job_id
                    self._show_job_details(sender, job_id, correlation_id)
                    return
                except ValueError:
                    pass  # Not an integer, continue with list view

            # Parse arguments for list view
            limit = int(args[0]) if args and len(args) >= 1 else 50
            min_score = int(args[1]) if args and len(args) >= 2 else 0

            # Get recent jobs
            jobs = self.db_manager.get_recent_jobs(limit=limit)

            if not jobs:
                self._send_response(sender, "📭 No jobs found in database", correlation_id=correlation_id)
                return

            # Score all jobs
            scored_jobs = []
            for job in jobs:
                score = self._filter_and_score_job(job)
                if score >= min_score:
                    scored_jobs.append({
                        'job': job,
                        'score': score
                    })

            # Sort by score (highest first)
            scored_jobs.sort(key=lambda x: x['score'], reverse=True)

            # Build response
            response = f"🎯 **Job Scoring Results**\n\n"
            response += f"📊 Scored {len(jobs)} jobs, {len(scored_jobs)} match (>= {min_score} points)\n\n"

            if scored_jobs:
                # Show top matches
                response += f"**Top Matches:**\n"
                for item in scored_jobs[:10]:  # Show top 10
                    job = item['job']
                    score = item['score']

                    title = job['title'][:50] + "..." if len(job['title']) > 50 else job['title']
                    location = (job.get('location') or 'No location')[:30]

                    # Score indicator (thresholds configurable in YAML)
                    if score >= self.threshold_high_priority:
                        indicator = "🔥"
                    elif score >= self.threshold_good_match:
                        indicator = "✅"
                    elif score >= self.threshold_review:
                        indicator = "📌"
                    else:
                        indicator = "📋"

                    response += f"\n{indicator} **{score} pts** - {title}\n"
                    response += f"   📍 {location}\n"
                    response += f"   🔗 Job ID: {job['id']}\n"

                if len(scored_jobs) > 10:
                    response += f"\n... and {len(scored_jobs) - 10} more matches\n"

                # Summary stats (using configurable thresholds)
                high_matches = len([s for s in scored_jobs if s['score'] >= self.threshold_high_priority])
                medium_matches = len([s for s in scored_jobs if self.threshold_good_match <= s['score'] < self.threshold_high_priority])
                low_matches = len([s for s in scored_jobs if s['score'] < 60])

                response += f"\n**Score Distribution:**\n"
                response += f"  🔥 High Priority (70+): {high_matches}\n"
                response += f"  ✅ Good Match (60-69): {medium_matches}\n"
                response += f"  📌 Worth Reviewing (<60): {low_matches}\n"
            else:
                response += "No jobs matched your criteria. Try adjusting your criteria weights.\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error scoring jobs: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to score jobs: {str(e)}", correlation_id=correlation_id)

    def _show_job_details(self, sender: str, job_id: int, correlation_id: str = None):
        """
        Show detailed information for a specific job.

        Args:
            sender: Service that sent the command
            job_id: Job ID to display
            correlation_id: Message correlation ID
        """
        try:
            # Get job from database
            job = self.db_manager.get_job_by_id(job_id)

            if not job:
                self._send_error_response(
                    sender,
                    f"❌ Job ID {job_id} not found in database",
                    correlation_id=correlation_id
                )
                return

            # Calculate score
            score = self._filter_and_score_job(job)

            # Score indicator (thresholds configurable in YAML)
            if score >= self.threshold_high_priority:
                indicator = "🔥 HIGH PRIORITY"
            elif score >= self.threshold_good_match:
                indicator = "✅ GOOD MATCH"
            elif score >= self.threshold_review:
                indicator = "📌 REVIEW"
            else:
                indicator = "📋 LOW PRIORITY"

            # Build detailed response
            response = f"**🎯 Job Details - ID: {job_id}**\n\n"
            response += f"**{indicator}** - Score: **{score} points**\n\n"
            response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            # Basic info
            response += f"**📋 Title:**\n{job['title']}\n\n"

            if job.get('location'):
                response += f"**📍 Location:**\n{job['location']}\n\n"

            response += f"**🔗 URL:**\n{job['url']}\n\n"

            # Salary info (if available from deep scrape)
            if job.get('salary_text') or job.get('salary_min'):
                response += "**💰 Compensation:**\n"
                if job.get('salary_text'):
                    response += f"{job['salary_text']}\n"
                if job.get('salary_min') and job.get('salary_max'):
                    response += f"Range: ${job['salary_min']:,.0f} - ${job['salary_max']:,.0f}\n"
                elif job.get('salary_min'):
                    response += f"Minimum: ${job['salary_min']:,.0f}\n"
                response += "\n"

            # Metadata
            response += f"**📅 Scraped:** {job.get('scraped_date', 'Unknown')}\n"
            if job.get('last_seen_date'):
                response += f"**👁️ Last Seen:** {job['last_seen_date']}\n"
            response += f"**🔢 Times Seen:** {job.get('times_seen', 1)}\n"
            response += f"**✓ Active:** {'Yes' if job.get('is_active', 1) else 'No'}\n"
            response += f"**🆕 New:** {'Yes' if job.get('is_new', 0) else 'No'}\n\n"

            # Description (if available from deep scrape)
            if job.get('description') and len(job['description']) > 100:
                response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                response += "**📝 Full Description:**\n\n"

                # Truncate description if too long (to avoid overwhelming the display)
                description = job['description']
                if len(description) > 2000:
                    response += description[:2000] + "\n\n... (description truncated, full details at URL above)"
                else:
                    response += description
                response += "\n\n"
            elif not job.get('description') or len(job.get('description', '')) < 100:
                response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                response += "**ℹ️ Note:** Full description not yet scraped.\n"
                response += f"Run `deep-match {score}` to fetch complete details for high-scoring jobs.\n\n"

            response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            response += "**💡 Tip:** Use `score-jobs` to see all matches, or `score-jobs <id>` for details."

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error showing job details: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to show job details: {str(e)}", correlation_id=correlation_id)

    def _handle_deep_match(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle deep-match command - deep scrape jobs with score >= threshold (Phase 5.5).

        Usage:
          deep-match [min_score] [limit]

        Args:
          min_score: Minimum score threshold (default: 60)
          limit: Maximum number of jobs to scrape (default: 50)
        """
        try:
            # Parse arguments
            min_score = int(args[0]) if len(args) > 0 else 60
            limit = int(args[1]) if len(args) > 1 else 50

            response = f"**🔍 Deep Scraping Matches (score >= {min_score})**\n\n"

            # Get and score recent jobs
            jobs = self.db_manager.get_recent_jobs(limit=500)  # Get more to find matches
            if not jobs:
                response += "❌ No jobs in database. Run 'scrape-company' first.\n"
                self._send_response(sender, response, correlation_id=correlation_id)
                return

            # Filter and score jobs
            matched_jobs = []
            for job in jobs:
                score = self._filter_and_score_job(job)
                if score >= min_score:
                    matched_jobs.append({'job': job, 'score': score, 'id': job['id'], 'url': job['url'], 'title': job.get('title', 'Unknown')})

            if not matched_jobs:
                response += f"No jobs matched with score >= {min_score}.\n"
                response += "Try lowering the threshold or adjusting your criteria.\n"
                self._send_response(sender, response, correlation_id=correlation_id)
                return

            # Limit to requested number
            if len(matched_jobs) > limit:
                response += f"⚠️ Found {len(matched_jobs)} matches, limiting to top {limit} by score\n\n"
                matched_jobs = sorted(matched_jobs, key=lambda x: x['score'], reverse=True)[:limit]

            response += f"📊 Found {len(matched_jobs)} jobs to deep scrape\n"
            response += f"⏱️ Estimated time: {len(matched_jobs) * 2} seconds (2s per job)\n\n"

            self._send_response(sender, response, correlation_id=correlation_id)

            # Deep scrape the matches
            success_count = self._deep_scrape_matches(matched_jobs)

            # Send final result
            final_response = f"\n✅ **Deep Scrape Complete**\n"
            final_response += f"  • Scraped: {success_count}/{len(matched_jobs)} jobs\n"
            final_response += f"  • Failed: {len(matched_jobs) - success_count}\n\n"

            if success_count > 0:
                final_response += "✨ Updated jobs now have full descriptions and salary info.\n"
                final_response += "Use 'score-jobs' to see them with complete details.\n"

            self._send_response(sender, final_response, correlation_id=correlation_id)

        except ValueError:
            self._send_error_response(
                sender,
                "Usage: deep-match [min_score] [limit]\n"
                "Example: deep-match 60 20\n"
                "  min_score: Minimum score threshold (default: 60)\n"
                "  limit: Maximum jobs to scrape (default: 50)",
                correlation_id=correlation_id
            )
        except Exception as e:
            self.logger.error(f"jobhunter-service: Error deep scraping matches: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to deep scrape: {str(e)}", correlation_id=correlation_id)

    def _handle_run_now(self, sender: str, correlation_id: str = None):
        """
        Handle run-now command - manually trigger the complete workflow (Phase 6).

        Usage:
          run-now
        """
        try:
            response = "**🚀 Manual Workflow Trigger**\n\n"
            response += "Starting complete workflow: scrape → score → deep-match → notify\n"
            response += "This may take several minutes depending on the number of companies...\n"

            self._send_response(sender, response, correlation_id=correlation_id)

            # Run the workflow in the current thread (blocking)
            self._run_daily_scrape()

            # Send completion message
            final_response = "\n✅ **Workflow Complete!**\n"
            final_response += "Check the logs for detailed results or use 'score-jobs' to see matches."

            self._send_response(sender, final_response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error in manual run: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to run workflow: {str(e)}", correlation_id=correlation_id)

    # ============================================================================
    # Schedule Management (Phase 7)
    # ============================================================================

    def _handle_add_schedule(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle add-schedule command - add a scheduled run time.

        Usage:
          add-schedule <time> [timezone]
          add-schedule "09:00"
          add-schedule "13:00" "America/Chicago"
        """
        try:
            if not args or len(args) < 1:
                self._send_error_response(
                    sender,
                    "Usage: add-schedule <time> [timezone]\nExample: add-schedule \"09:00\" \"America/New_York\"",
                    correlation_id=correlation_id
                )
                return

            run_time = args[0]
            timezone = args[1] if len(args) > 1 else "America/New_York"

            # Validate time format (HH:MM)
            import re
            if not re.match(r'^\d{1,2}:\d{2}$', run_time):
                self._send_error_response(
                    sender,
                    f"Invalid time format: {run_time}. Use HH:MM format (e.g., 09:00, 13:30)",
                    correlation_id=correlation_id
                )
                return

            # Add to database
            schedule_id = self.db_manager.add_run_time(run_time, timezone)

            response = f"✅ **Schedule Added**\n\n"
            response += f"**Time:** {run_time}\n"
            response += f"**Timezone:** {timezone}\n"
            response += f"**Schedule ID:** {schedule_id}\n\n"
            response += f"Use `list-schedules` to see all scheduled times."

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error adding schedule: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to add schedule: {str(e)}", correlation_id=correlation_id)

    def _handle_list_schedules(self, sender: str, correlation_id: str = None):
        """
        Handle list-schedules command - list all scheduled run times.

        Usage:
          list-schedules
        """
        try:
            schedules = self.db_manager.get_active_run_times()

            if not schedules:
                response = "📅 **No Scheduled Run Times**\n\n"
                response += "Use `add-schedule` to set up automated daily runs.\n"
                response += "Example: `add-schedule \"09:00\" \"America/New_York\"`"
            else:
                # Calculate next run
                from datetime import datetime
                import pytz

                response = f"📅 **Scheduled Run Times** ({len(schedules)} active)\n\n"

                for schedule in schedules:
                    response += f"**ID {schedule['id']}:** {schedule['run_time']} {schedule['timezone']}\n"

                # Show next run time
                next_run = self._calculate_next_run()
                if next_run:
                    now = datetime.now(pytz.UTC)
                    time_until = next_run - now
                    hours = int(time_until.total_seconds() // 3600)
                    minutes = int((time_until.total_seconds() % 3600) // 60)

                    response += f"\n🕐 **Next Run:** {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    response += f"⏱️  **Time Until:** {hours}h {minutes}m"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error listing schedules: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list schedules: {str(e)}", correlation_id=correlation_id)

    def _handle_remove_schedule(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle remove-schedule command - remove a scheduled run time.

        Usage:
          remove-schedule <id>
          remove-schedule 1
        """
        try:
            if not args or len(args) < 1:
                self._send_error_response(
                    sender,
                    "Usage: remove-schedule <id>\nExample: remove-schedule 1\nUse `list-schedules` to see IDs.",
                    correlation_id=correlation_id
                )
                return

            try:
                schedule_id = int(args[0])
            except ValueError:
                self._send_error_response(
                    sender,
                    f"Invalid schedule ID: {args[0]}. Must be a number.",
                    correlation_id=correlation_id
                )
                return

            # Remove from database
            success = self.db_manager.remove_run_time(schedule_id)

            if success:
                response = f"✅ **Schedule Removed**\n\n"
                response += f"Removed schedule ID {schedule_id}.\n"
                response += f"Use `list-schedules` to see remaining times."
            else:
                response = f"⚠️  **Schedule Not Found**\n\n"
                response += f"No schedule with ID {schedule_id}.\n"
                response += f"Use `list-schedules` to see valid IDs."

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error removing schedule: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to remove schedule: {str(e)}", correlation_id=correlation_id)

    # ============================================================================
    # Freshness Management (Phase 8)
    # ============================================================================

    def _handle_list_new_jobs(self, sender: str, correlation_id: str = None):
        """
        Handle list-new-jobs command - show only brand new jobs from latest scrape.

        Usage:
          list-new-jobs
        """
        try:
            # Get all jobs with is_new=1
            with self.db_manager._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT j.*, c.name as company_name
                    FROM job_postings j
                    JOIN companies c ON j.company_id = c.id
                    WHERE j.is_new = 1 AND j.is_active = 1
                    ORDER BY j.scraped_date DESC
                    LIMIT 50
                """)
                jobs = [dict(row) for row in cursor.fetchall()]

            if not jobs:
                response = "🆕 **No New Jobs**\n\n"
                response += "No brand new jobs found in the latest scrape.\n"
                response += "Run `run-now` to scrape for new postings."
            else:
                response = f"🆕 **New Jobs** ({len(jobs)} found)\n\n"
                response += "These jobs were just discovered in the latest scrape!\n\n"

                # Score and sort by match quality
                scored_jobs = []
                for job in jobs:
                    score = self._filter_and_score_job(job)
                    if score > 0:
                        scored_jobs.append({'job': job, 'score': score})

                scored_jobs.sort(key=lambda x: x['score'], reverse=True)

                if scored_jobs:
                    response += "**Top Matches:**\n\n"
                    for item in scored_jobs[:10]:
                        job = item['job']
                        score = item['score']
                        response += f"🔥 **{score} pts** - {job['title']}\n"
                        response += f"   🏢 {job['company_name']}\n"
                        response += f"   📍 {job.get('location', 'No location')}\n"
                        response += f"   🔗 Job ID: {job['id']}\n\n"

                    response += f"\nℹ️ Use `score-jobs <id>` to see full details."
                else:
                    response += "⚠️ None of the new jobs match your criteria."

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error listing new jobs: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list new jobs: {str(e)}", correlation_id=correlation_id)

    def _handle_list_stale_jobs(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle list-stale-jobs command - show jobs older than N days.

        Usage:
          list-stale-jobs [days]
          list-stale-jobs 14
        """
        try:
            # Get threshold (default 14 days)
            threshold_days = int(args[0]) if args and len(args) > 0 else 14

            # Calculate date threshold
            from datetime import datetime, timedelta
            threshold_date = (datetime.now() - timedelta(days=threshold_days)).isoformat()

            # Get stale jobs
            with self.db_manager._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT j.*, c.name as company_name
                    FROM job_postings j
                    JOIN companies c ON j.company_id = c.id
                    WHERE j.first_seen_date < ? AND j.is_active = 1
                    ORDER BY j.first_seen_date ASC
                    LIMIT 50
                """, (threshold_date,))
                jobs = [dict(row) for row in cursor.fetchall()]

            if not jobs:
                response = f"📅 **No Stale Jobs**\n\n"
                response += f"No jobs older than {threshold_days} days found."
            else:
                response = f"📅 **Stale Jobs** ({len(jobs)} found)\n\n"
                response += f"Jobs first seen more than {threshold_days} days ago:\n\n"

                for job in jobs[:20]:
                    # Calculate age
                    first_seen = datetime.fromisoformat(job['first_seen_date'].replace('Z', '+00:00'))
                    age_days = (datetime.now(first_seen.tzinfo) if first_seen.tzinfo else datetime.now() - first_seen).days

                    response += f"⏰ **{age_days} days old** - {job['title']}\n"
                    response += f"   🏢 {job['company_name']}\n"
                    response += f"   👁️ Seen {job['times_seen']} times\n"
                    response += f"   🔗 Job ID: {job['id']}\n\n"

                if len(jobs) > 20:
                    response += f"\n... and {len(jobs) - 20} more\n"

                response += f"\n💡 **Tip:** These jobs may be filled but not removed from the website.\n"
                response += f"Consider focusing on newer postings for better response rates."

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error listing stale jobs: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list stale jobs: {str(e)}", correlation_id=correlation_id)

    # ============================================================================
    # Web Scraping (Phase 4)
    # ============================================================================

    def _check_robots_txt(self, base_url: str, user_agent: str = "RobotRay JobHunter") -> bool:
        """
        Check if scraping is allowed by robots.txt.

        Args:
            base_url: Base URL of the website
            user_agent: User agent string

        Returns:
            True if allowed, False if disallowed
        """
        try:
            from urllib.robotparser import RobotFileParser
            from urllib.parse import urlparse, urljoin

            parsed = urlparse(base_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.read()

            can_fetch = rp.can_fetch(user_agent, base_url)
            if not can_fetch:
                self.logger.warning(f"jobhunter-service: robots.txt disallows scraping: {base_url}")
            return can_fetch

        except Exception as e:
            self.logger.debug(f"jobhunter-service: Could not check robots.txt for {base_url}: {e}")
            # If robots.txt doesn't exist or fails, assume allowed but log it
            return True

    def _fetch_with_retry(self, url: str, max_retries: int = 3, timeout: int = 30, politeness_delay: float = 1.0) -> str:
        """
        Fetch URL with exponential backoff retry logic.

        Args:
            url: URL to fetch
            max_retries: Maximum number of retry attempts
            timeout: Timeout in seconds

        Returns:
            HTML content as string

        Raises:
            Exception: If all retries fail
        """
        import requests

        # Politeness delay before making request (avoid overwhelming servers)
        if politeness_delay > 0:
            time.sleep(politeness_delay)

        for attempt in range(max_retries):
            try:
                self.logger.debug(f"jobhunter-service: Fetching {url} (attempt {attempt + 1}/{max_retries})")

                headers = {
                    'User-Agent': 'RobotRay JobHunter/1.0 (Career Page Monitor)',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1'
                }

                response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                response.raise_for_status()

                self.logger.debug(f"jobhunter-service: Successfully fetched {url} ({len(response.text)} bytes)")
                return response.text

            except requests.exceptions.Timeout:
                self.logger.warning(f"jobhunter-service: Timeout fetching {url} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    sleep_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    time.sleep(sleep_time)
                else:
                    raise

            except requests.exceptions.HTTPError as e:
                self.logger.error(f"jobhunter-service: HTTP error fetching {url}: {e} (attempt {attempt + 1}/{max_retries})")
                if e.response.status_code >= 500:  # Retry on server errors
                    if attempt < max_retries - 1:
                        sleep_time = 2 ** attempt
                        time.sleep(sleep_time)
                    else:
                        raise
                else:
                    # Client errors (4xx) - don't retry
                    raise

            except requests.exceptions.RequestException as e:
                self.logger.error(f"jobhunter-service: Request error fetching {url}: {e} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    sleep_time = 2 ** attempt
                    time.sleep(sleep_time)
                else:
                    raise

    def _scrape_generic_html(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """
        Generic HTML parser that attempts to extract job listings.

        Strategy:
        1. Look for common job listing patterns (class/id names)
        2. Extract title, URL, location, description
        3. Return list of job dictionaries

        Args:
            html: HTML content
            base_url: Base URL for resolving relative links

        Returns:
            List of job dictionaries with keys: title, url, location, description
        """
        from urllib.parse import urljoin

        soup = BeautifulSoup(html, 'lxml')
        jobs = []

        # Common selectors for job listings (ordered by specificity)
        job_selectors = [
            {'tag': 'div', 'class_': ['job-listing', 'job-item', 'position', 'career-item']},
            {'tag': 'li', 'class_': ['job', 'position', 'opening']},
            {'tag': 'tr', 'class_': ['job-row', 'position-row']},
            {'tag': 'article', 'class_': ['job', 'position']},
        ]

        job_elements = []
        for selector in job_selectors:
            for class_name in selector['class_']:
                elements = soup.find_all(selector['tag'], class_=lambda x: x and class_name in x.lower() if x else False)
                if elements:
                    self.logger.debug(f"jobhunter-service: Found {len(elements)} job elements using selector {selector['tag']}.{class_name}")
                    job_elements.extend(elements)
                    break
            if job_elements:
                break

        # Fallback: Look for direct job posting links (Greenhouse, Lever, Workday, etc.)
        if not job_elements:
            self.logger.debug("jobhunter-service: No job elements found with standard selectors, looking for direct job links")

            # Common job board platforms
            job_board_patterns = [
                'greenhouse.io',
                'lever.co',
                'workday.com',
                'jobvite.com',
                'smartrecruiters.com',
                'breezy.hr',
                'recruitee.com'
            ]

            # Find all links that point to job boards OR contain '/job' in URL
            links = soup.find_all('a', href=True)

            for link in links:
                link_href = link.get('href', '')

                # Check if link points to a job board or has '/job' pattern
                is_job_link = (
                    any(platform in link_href for platform in job_board_patterns) or
                    '/job' in link_href.lower()
                )

                if is_job_link:
                    # Use the link itself as the job element (simpler extraction)
                    job_elements.append(link)

        # Extract job data from elements (no arbitrary limit - we deduplicate in database)
        for elem in job_elements:
            try:
                # Check if element IS a link (from fallback) or contains a link
                if elem.name == 'a' and elem.get('href'):
                    # Element is the link itself
                    title = elem.get_text(strip=True)
                    url = urljoin(base_url, elem['href'])
                    # Try to find location in the link text (often appended)
                    location = None  # Will be parsed from title if needed
                else:
                    # Element is a container, extract title from children
                    title = None
                    title_selectors = [
                        elem.find('h2'),
                        elem.find('h3'),
                        elem.find('h4'),
                        elem.find(class_=lambda x: x and 'title' in x.lower() if x else False),
                        elem.find('a')
                    ]
                    for selector in title_selectors:
                        if selector:
                            title = selector.get_text(strip=True)
                            break

                    if not title:
                        continue  # Skip if no title found

                    # Extract URL from child link
                    url = None
                    link = elem.find('a', href=True)
                    if link:
                        url = urljoin(base_url, link['href'])

                    # Extract location from container
                    location = None
                    location_elem = elem.find(class_=lambda x: x and 'location' in x.lower() if x else False)
                    if location_elem:
                        location = location_elem.get_text(strip=True)

                if not title:
                    continue  # Skip if no title found

                # Extract description/summary
                description = None
                desc_elem = elem.find(class_=lambda x: x and ('description' in x.lower() or 'summary' in x.lower()) if x else False)
                if desc_elem:
                    description = desc_elem.get_text(strip=True)[:500]  # Limit to 500 chars

                # Validate URL - must be a specific job URL, not base career page
                job_url = url or base_url

                # Skip if URL doesn't look like a job posting
                # Valid patterns: /jobs/, /job/, /careers/job, /positions/, /opening/
                valid_job_patterns = ['/job', '/position', '/opening']
                is_valid_url = any(pattern in job_url.lower() for pattern in valid_job_patterns)

                if not is_valid_url:
                    self.logger.debug(f"jobhunter-service: Skipping non-job URL: {title} ({job_url})")
                    continue

                # Skip if title looks like a navigation link
                skip_titles = ['careers', 'view all', 'see all', 'browse', 'apply']
                if title.lower().strip() in skip_titles:
                    self.logger.debug(f"jobhunter-service: Skipping navigation link: {title}")
                    continue

                # Create job dictionary
                job = {
                    'title': title,
                    'url': job_url,
                    'location': location,
                    'description': description,
                    'scraped_date': datetime.now().isoformat()
                }

                jobs.append(job)
                self.logger.debug(f"jobhunter-service: Extracted job: {title}")

            except Exception as e:
                self.logger.warning(f"jobhunter-service: Error extracting job from element: {e}")
                continue

        self.logger.info(f"jobhunter-service: Extracted {len(jobs)} jobs from HTML")
        return jobs

    def _deep_scrape_job(self, job_id: int, job_url: str) -> bool:
        """
        Follow job URL to scrape full details (Phase 5.5).

        Args:
            job_id: Database ID of job to update
            job_url: URL to scrape

        Returns:
            True if successful, False otherwise
        """
        try:
            # Fetch job detail page with politeness delay
            self.logger.info(f"jobhunter-service: Deep scraping job {job_id}: {job_url}")
            html = self._fetch_with_retry(job_url, politeness_delay=2.0)

            if not html:
                self.logger.warning(f"jobhunter-service: Failed to fetch job {job_id}")
                return False

            soup = BeautifulSoup(html, 'lxml')

            # Extract full description (try multiple common selectors)
            description = None

            # Try Greenhouse-specific selectors
            desc_elem = (
                soup.find('div', id='content') or
                soup.find('div', class_='content') or
                soup.find('div', id='main-content') or
                soup.find('div', class_='posting-description') or
                soup.find('div', {'data-qa': 'job-description'})
            )

            if desc_elem:
                description = desc_elem.get_text(separator='\n', strip=True)
            else:
                # Fallback: try to find the largest text block
                all_divs = soup.find_all('div')
                if all_divs:
                    largest_div = max(all_divs, key=lambda d: len(d.get_text()))
                    description = largest_div.get_text(separator='\n', strip=True)

            # Extract salary if present
            salary_text = None
            salary_min = None
            salary_max = None

            # Look for salary patterns in text
            if description:
                salary_patterns = [
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\s*-\s*\$(\d{1,3}(?:,\d{3})*(?:k)?)',  # $100,000 - $150,000 or $100k - $150k
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\+',  # $100,000+ or $100k+
                ]

                for pattern in salary_patterns:
                    match = re.search(pattern, description, re.IGNORECASE)
                    if match:
                        salary_text = match.group(0)
                        # Parse min/max if range
                        if '-' in salary_text:
                            salary_min = self._parse_salary(match.group(1))
                            salary_max = self._parse_salary(match.group(2))
                        else:
                            salary_min = self._parse_salary(match.group(1))
                        break

            # Update database with full details
            if description or salary_text:
                self.db_manager.update_job_details(
                    job_id=job_id,
                    description=description,
                    salary_text=salary_text,
                    salary_min=salary_min,
                    salary_max=salary_max
                )
                self.logger.info(f"jobhunter-service: Successfully deep scraped job {job_id}")
                return True
            else:
                self.logger.warning(f"jobhunter-service: No additional details extracted for job {job_id}")
                return False

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error deep scraping job {job_id}: {e}")
            return False

    def _deep_scrape_matches(self, matched_jobs: List[Dict[str, Any]]) -> int:
        """
        Deep scrape a list of matched jobs to get full details (Phase 5.5).

        Args:
            matched_jobs: List of job dictionaries with 'id' and 'url'

        Returns:
            Number of successfully scraped jobs
        """
        success_count = 0
        total = len(matched_jobs)

        self.logger.info(f"jobhunter-service: Starting deep scrape for {total} matched jobs")

        for i, job in enumerate(matched_jobs, 1):
            self.logger.info(f"jobhunter-service: Deep scraping {i}/{total}: {job.get('title', 'Unknown')}")

            # Deep scrape with politeness delay
            if self._deep_scrape_job(job['id'], job['url']):
                success_count += 1

            # Rate limiting: 2 second delay between deep scrapes
            if i < total:  # Don't sleep after the last one
                time.sleep(2.0)

        self.logger.info(f"jobhunter-service: Deep scrape complete: {success_count}/{total} successful")
        return success_count

    def _scrape_company(self, company_id: int, company_name: str, careers_url: str,
                       check_robots: bool = False, politeness_delay: float = 2.0) -> List[Dict[str, Any]]:
        """
        Scrape jobs from a company's career page with deduplication.

        Args:
            company_id: Company ID from database
            company_name: Company name (for logging)
            careers_url: URL of career page
            check_robots: Whether to check robots.txt (default: True)
            politeness_delay: Delay in seconds before request (default: 2.0)

        Returns:
            List of newly discovered jobs (deduplicated)
        """
        import hashlib

        try:
            self.logger.info(f"jobhunter-service: Scraping {company_name} ({careers_url})")

            # Check robots.txt first (be a good citizen)
            if check_robots:
                if not self._check_robots_txt(careers_url):
                    self.logger.warning(f"jobhunter-service: {company_name}: Scraping disallowed by robots.txt. Skipping.")
                    return []

            # Fetch HTML with politeness delay
            html = self._fetch_with_retry(careers_url, politeness_delay=politeness_delay)

            # Parse HTML to extract jobs
            jobs = self._scrape_generic_html(html, careers_url)

            if not jobs:
                self.logger.warning(f"jobhunter-service: {company_name}: No jobs found")
                return []

            # Deduplicate using job hash
            new_jobs = []
            for job in jobs:
                # Create hash from title + url
                job_hash_input = f"{job['title']}:{job['url']}"
                job_hash = hashlib.sha256(job_hash_input.encode()).hexdigest()

                # Check if job already exists in database
                if self.db_manager.job_exists_by_hash(company_id, job_hash):
                    self.logger.debug(f"jobhunter-service: Job already exists (skipping): {job['title']}")
                    continue

                # Add company_id and job_hash to job dict
                job['company_id'] = company_id
                job['job_hash'] = job_hash
                job['is_new'] = True  # Mark as new for freshness detection (Phase 8)

                new_jobs.append(job)

            self.logger.info(f"jobhunter-service: {company_name}: Found {len(jobs)} jobs, {len(new_jobs)} are new")
            return new_jobs

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error scraping {company_name}: {e}")
            return []

    # ============================================================================
    # Authentication (Phase 4.5)
    # ============================================================================

    # TODO: Implement _detect_auth_requirement() in Phase 4.5
    # TODO: Implement _authenticate_form_login() in Phase 4.5
    # TODO: Implement _handle_set_credentials() in Phase 4.5

    # ============================================================================
    # Job Filtering & Scoring (Phase 5)
    # ============================================================================

    def _filter_and_score_job(self, job: Dict[str, Any]) -> int:
        """
        Filter and score a job against active criteria.

        Args:
            job: Job dictionary from database with keys: title, url, location, description

        Returns:
            Score (unbounded integer). Jobs >= 60 are considered matches. Scores can exceed 100 if multiple high-value criteria match.
        """
        # Combine all job text for matching
        job_text = f"{job.get('title', '')} {job.get('location', '')} {job.get('description', '')}"
        job_text_lower = job_text.lower()

        # Extract numbers for range matching (salary, years)
        extracted_numbers = self._extract_numbers_from_text(job_text)

        # Add salary from database columns if available (from deep scrape)
        if job.get('salary_min'):
            extracted_numbers.append(job['salary_min'])
        if job.get('salary_max'):
            extracted_numbers.append(job['salary_max'])

        # Load active criteria
        simple_criteria = self.db_manager.get_active_criteria()
        groups = self.db_manager.get_active_groups()

        # Score individual criteria (simple criteria)
        individual_score = 0
        for criterion in simple_criteria:
            matched = False

            # Match based on criterion type (Phase 3 simple criteria)
            if criterion.get('criterion_value', '').lower() in job_text_lower:
                matched = True

            if matched:
                individual_score += criterion.get('weight', 0)

        # Score grouped criteria (with range support from Phase 3.5)
        group_score = 0
        for group in groups:
            if group['match_type'] == 'any':
                # OR logic - match any value
                for value_obj in group['values']:
                    value = value_obj['value']

                    # Check if value is a range pattern
                    if self._is_range_pattern(value):
                        min_val, max_val = self._parse_range(value)
                        if self._matches_range(min_val, max_val, extracted_numbers):
                            group_score += group['weight']
                            break
                    else:
                        # Substring matching
                        if value.lower() in job_text_lower:
                            group_score += group['weight']
                            break

            elif group['match_type'] == 'tiered':
                # Tiered - check in order (preferred first)
                for value_obj in sorted(group['values'], key=lambda x:
                        {'preferred': 0, 'acceptable': 1, 'undesirable': 2}.get(x.get('tier_level', ''), 3)):
                    value = value_obj['value']
                    tier_weight = value_obj.get('tier_weight') or 0

                    # Check if value is a range pattern
                    if self._is_range_pattern(value):
                        min_val, max_val = self._parse_range(value)
                        if self._matches_range(min_val, max_val, extracted_numbers):
                            group_score += tier_weight
                            break
                    else:
                        # Substring matching
                        if value.lower() in job_text_lower:
                            group_score += tier_weight
                            break

        # Calculate base score (no cap - jobs can score higher if they match more criteria)
        base_score = individual_score + group_score

        # Apply freshness bonus/penalty (Phase 8)
        freshness_adjustment = self._calculate_freshness_adjustment(job)
        total_score = base_score + freshness_adjustment

        return int(total_score)

    def _calculate_freshness_adjustment(self, job: Dict[str, Any]) -> int:
        """
        Calculate freshness bonus or penalty for a job (Phase 8).

        Bonus for fresh jobs:
        - Brand new (is_new=True): +15 points
        - 1-3 days old: +10 points
        - 4-7 days old: +5 points

        Penalty for stale jobs:
        - 14-30 days old: -5 points
        - >30 days old: -10 points

        Args:
            job: Job dictionary with freshness fields

        Returns:
            Adjustment to score (positive for fresh, negative for stale)
        """
        # Check if this is a brand new job
        if job.get('is_new', 0):
            return 15  # Maximum bonus for brand new postings

        # Calculate age in days
        from datetime import datetime
        first_seen_str = job.get('first_seen_date')
        if not first_seen_str:
            return 0  # No freshness data, no adjustment

        try:
            first_seen = datetime.fromisoformat(first_seen_str.replace('Z', '+00:00'))
            now = datetime.now(first_seen.tzinfo) if first_seen.tzinfo else datetime.now()
            age_days = (now - first_seen).days

            # Bonus for fresh jobs
            if age_days <= 3:
                return 10
            elif age_days <= 7:
                return 5
            # No adjustment for normal age (8-13 days)
            elif age_days <= 13:
                return 0
            # Penalty for stale jobs
            elif age_days <= 30:
                return -5
            else:
                return -10  # Heavy penalty for very old postings (likely filled)

        except (ValueError, AttributeError):
            return 0  # Parse error, no adjustment

    def _extract_salary(self, text: str) -> tuple:
        """
        Extract salary information from job text.

        Args:
            text: Job description text

        Returns:
            Tuple of (salary_text, salary_min, salary_max)
        """
        salary_patterns = [
            r'\$\s*(\d+[kK]?)\s*-\s*\$?\s*(\d+[kK]?)',  # $100k - $150k
            r'\$\s*(\d+,\d+)\s*-\s*\$?\s*(\d+,\d+)',    # $100,000 - $150,000
            r'(\d+[kK]?)\s*-\s*(\d+[kK]?)\s*(?:USD|per year)',  # 100k - 150k USD
        ]

        for pattern in salary_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                salary_text = match.group(0)
                salary_min = self._parse_number(match.group(1))
                salary_max = self._parse_number(match.group(2))
                return (salary_text, salary_min, salary_max)

        return (None, None, None)

    # ============================================================================
    # Scheduler (Phase 6 & 7)
    # ============================================================================

    def _scheduler_loop(self):
        """
        Main scheduler loop - runs continuously until service stops (Phase 6).

        Calculates next run time based on active schedules and sleeps until then.
        Triggers the complete workflow: scrape → score → deep-match → notify.
        """
        self.logger.info("jobhunter-service: Scheduler loop started")

        while self._running:
            try:
                # Calculate next run time
                next_run = self._calculate_next_run()

                if next_run is None:
                    # No active schedules - sleep and check again later
                    self.logger.debug("jobhunter-service: No active schedules - waiting 60 seconds")
                    self._shutdown_event.wait(60)
                    continue

                # Calculate time until next run
                now = datetime.now(timezone.utc)
                time_until_run = (next_run - now).total_seconds()

                if time_until_run > 0:
                    self.logger.info(f"jobhunter-service: Next scheduled scrape: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {time_until_run/60:.1f} minutes)")

                    # Sleep until next run (or until shutdown)
                    if self._shutdown_event.wait(timeout=time_until_run):
                        # Shutdown signal received
                        break
                else:
                    # Time has already passed - run immediately
                    self.logger.info("jobhunter-service: Scheduled time has passed - running immediately")

                # Check if still running after sleep
                if not self._running:
                    break

                # Run the daily scrape workflow
                self.logger.info("jobhunter-service: 🔄 Starting scheduled scrape workflow")
                self._run_daily_scrape()

            except Exception as e:
                self.logger.error(f"jobhunter-service: Error in scheduler loop: {e}")
                import traceback
                self.logger.error(f"jobhunter-service: Traceback: {traceback.format_exc()}")
                # Sleep 5 minutes on error, then retry
                self._shutdown_event.wait(300)

        self.logger.info("jobhunter-service: Scheduler loop stopped")

    def _calculate_next_run(self) -> Optional[datetime]:
        """
        Calculate the next scheduled run time based on active schedules (Phase 6/7).

        Returns:
            Next run datetime (UTC) or None if no active schedules
        """
        try:
            # Get active run times from database
            schedules = self.db_manager.get_active_run_times()

            if not schedules:
                return None

            import pytz
            now = datetime.now(timezone.utc)
            next_runs = []

            for schedule in schedules:
                run_time_str = schedule['run_time']  # Format: "HH:MM"
                tz_name = schedule.get('timezone', 'America/New_York')

                try:
                    # Parse time
                    hour, minute = map(int, run_time_str.split(':'))

                    # Get timezone
                    tz = pytz.timezone(tz_name)

                    # Create datetime for today in the schedule's timezone
                    local_now = now.astimezone(tz)
                    scheduled_time = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                    # If time has passed today, schedule for tomorrow
                    if scheduled_time <= local_now:
                        scheduled_time += timedelta(days=1)

                    # Convert to UTC
                    scheduled_utc = scheduled_time.astimezone(timezone.utc)
                    next_runs.append(scheduled_utc)

                except Exception as e:
                    self.logger.error(f"jobhunter-service: Error parsing schedule {run_time_str} ({tz_name}): {e}")
                    continue

            if not next_runs:
                return None

            # Return the soonest run time
            return min(next_runs)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error calculating next run: {e}")
            return None

    def _run_daily_scrape(self):
        """
        Orchestrate the complete daily scraping workflow (Phase 6).

        Workflow:
        1. Scrape all active companies (light scrape)
        2. Score all jobs against criteria
        3. Deep scrape high-scoring matches
        4. Send notifications (Phase 9 - future)
        """
        try:
            start_time = time.time()
            self.logger.info("=" * 60)
            self.logger.info("jobhunter-service: 🚀 Starting Daily Scrape Workflow")
            self.logger.info("=" * 60)

            # Step 1: Get active companies
            companies = self.db_manager.get_active_companies()
            if not companies:
                self.logger.warning("jobhunter-service: No active companies to scrape")
                return

            self.logger.info(f"jobhunter-service: 📋 Found {len(companies)} active companies")

            # Step 2: Light scrape all companies
            total_jobs_scraped = 0
            total_new_jobs = 0

            for i, company in enumerate(companies, 1):
                try:
                    self.logger.info(f"jobhunter-service: 🔍 Scraping {i}/{len(companies)}: {company['name']}")

                    jobs = self._scrape_company(
                        company_id=company['id'],
                        company_name=company['name'],
                        careers_url=company['careers_url'],
                        scraper_type=company['scraper_type']
                    )

                    # Count new jobs
                    new_count = 0
                    for job in jobs:
                        job_hash = self._generate_job_hash(job['title'], job['url'])
                        if not self.db_manager.job_exists_by_hash(company['id'], job_hash):
                            new_count += 1
                            self.db_manager.add_job_posting(
                                company_id=company['id'],
                                job_hash=job_hash,
                                title=job['title'],
                                url=job['url'],
                                location=job.get('location'),
                                description=job.get('description')
                            )

                    total_jobs_scraped += len(jobs)
                    total_new_jobs += new_count

                    self.logger.info(f"jobhunter-service:    ✅ {len(jobs)} jobs found, {new_count} new")

                    # Rate limiting between companies
                    if i < len(companies):
                        time.sleep(self.config.get('rate_limit_delay', 3))

                except Exception as e:
                    self.logger.error(f"jobhunter-service:    ❌ Failed to scrape {company['name']}: {e}")
                    continue

            self.logger.info(f"jobhunter-service: \n📊 Light Scrape Summary: {total_jobs_scraped} total, {total_new_jobs} new jobs")

            # Step 3: Score all recent jobs
            self.logger.info("jobhunter-service: \n🎯 Scoring jobs against criteria...")
            recent_jobs = self.db_manager.get_recent_jobs(limit=1000)
            matched_jobs = []

            min_score = self.config.get('min_match_score', 60)
            for job in recent_jobs:
                score = self._filter_and_score_job(job)
                if score >= min_score:
                    matched_jobs.append({
                        'job': job,
                        'score': score,
                        'id': job['id'],
                        'url': job['url'],
                        'title': job.get('title', 'Unknown')
                    })

            # Sort by score descending
            matched_jobs.sort(key=lambda x: x['score'], reverse=True)

            self.logger.info(f"jobhunter-service:    ✅ {len(matched_jobs)} jobs scored >= {min_score}")

            # Step 4: Deep scrape top matches
            if matched_jobs:
                deep_scrape_limit = min(20, len(matched_jobs))  # Limit to top 20
                self.logger.info(f"jobhunter-service: \n🔬 Deep scraping top {deep_scrape_limit} matches...")

                success_count = self._deep_scrape_matches(matched_jobs[:deep_scrape_limit])
                self.logger.info(f"jobhunter-service:    ✅ Successfully deep scraped {success_count}/{deep_scrape_limit} jobs")

            # Step 5: TODO - Send notifications (Phase 9)
            # self._send_job_notifications(matched_jobs)

            # Summary
            elapsed = time.time() - start_time
            self.logger.info("\n" + "=" * 60)
            self.logger.info("jobhunter-service: ✨ Daily Scrape Workflow Complete")
            self.logger.info(f"jobhunter-service:    • Companies scraped: {len(companies)}")
            self.logger.info(f"jobhunter-service:    • Total jobs found: {total_jobs_scraped}")
            self.logger.info(f"jobhunter-service:    • New jobs: {total_new_jobs}")
            self.logger.info(f"jobhunter-service:    • High-scoring matches: {len(matched_jobs)}")
            self.logger.info(f"jobhunter-service:    • Deep scraped: {deep_scrape_limit if matched_jobs else 0}")
            self.logger.info(f"jobhunter-service:    • Time elapsed: {elapsed/60:.1f} minutes")
            self.logger.info("=" * 60)

        except Exception as e:
            self.logger.error(f"jobhunter-service: Error in daily scrape workflow: {e}")
            import traceback
            self.logger.error(f"jobhunter-service: Traceback: {traceback.format_exc()}")

    # ============================================================================
    # Freshness Detection (Phase 8)
    # ============================================================================

    # TODO: Implement _calculate_freshness_bonus() in Phase 8

    # ============================================================================
    # Notifications (Phase 9)
    # ============================================================================

    # TODO: Implement _send_job_notification() in Phase 9
