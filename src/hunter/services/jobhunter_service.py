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
- @jobhunter help
- @jobhunter add-company <name> <url> [scraper_type]
- @jobhunter list-companies
- @jobhunter set-criteria <type> <value> <weight>
- @jobhunter list-criteria
- @jobhunter add-schedule <time> [timezone]
- @jobhunter list-schedules
- @jobhunter run-now
- @jobhunter matches [limit]
- @jobhunter status
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
from pathlib import Path

from anthropic import Anthropic, APIStatusError, BadRequestError, NotFoundError
from anthropic import APITimeoutError as AnthropicTimeoutError

try:
    from openai import OpenAI
    from openai import APITimeoutError as OpenAITimeoutError
    from openai import APIStatusError as OpenAIStatusError
except ImportError:
    OpenAI = None
    OpenAITimeoutError = None
    OpenAIStatusError = None

_TIMEOUT_ERRORS = tuple(
    e for e in [AnthropicTimeoutError, OpenAITimeoutError] if e is not None
)

# HTTP-status errors from either SDK. The Anthropic and OpenAI clients each have
# their own APIStatusError hierarchy, so a 500 from the OpenAI-compat proxy is
# NOT an instance of anthropic.APIStatusError — both must be caught explicitly.
_API_STATUS_ERRORS = tuple(
    e for e in [APIStatusError, OpenAIStatusError] if e is not None
)

# Retry on rate limits / overload (429, 529) and transient upstream proxy
# failures (500, 502, 503, 504). The Claude Code OpenAI-compat proxy returns
# 500 "No response from Claude Code" when the backing session times out — treat
# as transient.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _retry_wait(status: Optional[int], attempt: int) -> int:
    """Backoff seconds: longer for rate limit (429/529), shorter for proxy 5xx."""
    if status in (429, 529):
        return 30 * (attempt + 1)
    return 10 * (attempt + 1)

from robotray.services.base import BaseService
from robotray.services.jobhunter_db import JobHunterDatabaseManager
from robotray.core.messaging import SecureMessage, MessagePriority
from robotray.core.secrets_manager import SecretsManager
from robotray.utils.secret_helper import get_secret_for_user

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
            name: Service name (usually "jobhunter")
            config: Service configuration
        """
        super().__init__(name, config)

        # Get configuration
        db_path = self.config.get('database_path', 'data/jobhunter.db')

        # ADR-009 F3: jobhunter is a fully-personal service — the whole
        # workspace (companies, criteria, schedules, jobs, matches) belongs
        # to one owner. Until per-user workspaces exist, every data command
        # is identity-gated to this owner and scheduled results deliver
        # privately (notify_user), never @broadcast.
        self.owner_user_id = int(self.config.get('owner_user_id', 1))

        self.default_criteria = self.config.get('default_criteria', [])
        self.default_criterion_groups = self.config.get('default_criterion_groups', [])
        self.default_schedules = self.config.get('default_schedules', [])

        # Load scoring thresholds (user-configurable)
        thresholds = self.config.get('scoring_thresholds', {})
        self.threshold_high_priority = thresholds.get('high_priority', 70)
        self.threshold_good_match = thresholds.get('good_match', 60)
        self.threshold_review = thresholds.get('review', 40)
        self.threshold_low_priority = thresholds.get('low_priority', 0)

        # Logging verbosity (default: false for concise executive summary)
        self.verbose_logging = self.config.get('verbose_logging', False)

        # Web scraping timeout (seconds)
        self.request_timeout = self.config.get('request_timeout', 30)

        # AI Analysis Configuration (Phase 11)
        ai_config = self.config.get('ai_analysis', {})
        self.ai_enabled = ai_config.get('enabled', False)
        self.ai_provider = ai_config.get('provider', 'anthropic')
        self.ai_base_url = ai_config.get('base_url', None)
        self.ai_timeout = ai_config.get('api_timeout', 120)
        self.ai_model = ai_config.get('model', 'default')
        self.ai_summary_model = ai_config.get('summary_model', 'default')  # final hiring summary
        self.ai_max_tokens = ai_config.get('max_tokens', 1000)
        self.ai_summary_max_tokens = ai_config.get('summary_max_tokens', 2000)  # More tokens for summary
        self.ai_temperature = ai_config.get('temperature', 0.3)
        self.ai_prescreen_threshold = ai_config.get('prescreen_threshold', 70)  # Pre-filter by criteria score
        self.ai_min_score = ai_config.get('min_score_threshold', 90)
        self.ai_cost_limit_daily = ai_config.get('cost_limit_daily', 5.00)
        self.cv_path = Path(ai_config.get('cv_path', 'data/cv/resume.md'))
        self.cv_dir = self.cv_path.parent
        self.goal_path = self.cv_dir / 'goal.md'
        self.ai_prompt_template = ai_config.get('prompt_template', None)  # User-customizable job analysis prompt
        self.ai_summary_prompt_template = ai_config.get('summary_prompt_template', None)  # User-customizable summary prompt

        # Initialize AI client if enabled
        self.anthropic_client: Optional[Anthropic] = None
        self._openai_client = None
        self._cv_content: Optional[str] = None
        self._goal_content: Optional[str] = None

        # Notification preferences
        self.send_individual_notifications = self.config.get('send_individual_notifications', False)

        self._user_db = None  # Lazy-initialized (must be before _resolve_api_key)

        if self.ai_enabled:
            try:
                self.secrets_manager = SecretsManager()

                if self.ai_provider == 'openai':
                    api_key = self._resolve_api_key("openai_api_key", "OPENAI_API_KEY")
                    if api_key and OpenAI is not None:
                        self._openai_client = OpenAI(api_key=api_key, base_url=self.ai_base_url, timeout=float(self.ai_timeout), max_retries=0)
                        self.logger.info(
                            f"jobhunter: AI analysis enabled (openai provider at {self.ai_base_url}), "
                            f"model {self.ai_model}, min score: {self.ai_min_score}"
                        )
                    else:
                        self.logger.error(
                            "jobhunter: OpenAI provider selected but no API key or openai package missing."
                        )
                        self.ai_enabled = False
                else:
                    api_key = self._resolve_api_key("anthropic_api_key", "ANTHROPIC_API_KEY")
                    if api_key:
                        self.anthropic_client = Anthropic(api_key=api_key, timeout=float(self.ai_timeout))
                        self.logger.info(
                            f"jobhunter: AI analysis enabled (anthropic provider), "
                            f"model {self.ai_model}, min score: {self.ai_min_score}"
                        )
                    else:
                        self.logger.error(
                            "jobhunter: AI analysis enabled but no API key found. "
                            "Set via @user set-secret anthropic_api_key <key>"
                        )
                        self.ai_enabled = False

                if self.ai_enabled:
                    # Load CV content (cached in memory)
                    if self.cv_path.exists():
                        self._cv_content = self.cv_path.read_text()
                        self.logger.info(
                            f"jobhunter: CV loaded from {self.cv_path} "
                            f"({len(self._cv_content)} characters)"
                        )
                    else:
                        self.logger.warning(
                            f"jobhunter: CV not found at {self.cv_path}, "
                            "AI analysis will work without CV context"
                        )

                    # Load search goal (cached in memory)
                    if self.goal_path.exists():
                        self._goal_content = self.goal_path.read_text().strip()
                        self.logger.info(
                            f"jobhunter: Search goal loaded from {self.goal_path} "
                            f"({len(self._goal_content)} characters)"
                        )
                    else:
                        self.logger.info(
                            "jobhunter: No search goal found. "
                            "Set one with: @jobhunter goal <your goal>"
                        )

            except Exception as e:
                self.logger.error(f"jobhunter: Failed to initialize AI client: {e}")
                self.ai_enabled = False

        # Per-user client cache (ADR-006)
        self._clients = {}  # user_id -> client (Anthropic or OpenAI)

        # Initialize database manager (will be initialized during start_service)
        self.db_manager: Optional[JobHunterDatabaseManager] = None

        # Threading components
        self._message_thread: Optional[threading.Thread] = None
        self._scheduler_thread: Optional[threading.Thread] = None  # Phase 6
        self._running = False
        self._shutdown_event = threading.Event()

        self.logger.info(f"jobhunter: Initialized with database at {db_path}")

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
            self.logger.info("jobhunter: Starting JobHunter service...")

            # Initialize database
            db_path = self.config.get('database_path', 'data/jobhunter.db')

            # Check if database file exists (to determine if this is first run)
            is_first_run = not os.path.exists(db_path)

            self.db_manager = JobHunterDatabaseManager(db_path)
            self.db_manager.initialize_database()

            # Initialize default criteria, groups, and schedules ONLY on first run
            if is_first_run:
                self.logger.info("jobhunter: First run detected - initializing default criteria, groups, and schedules")

                if self.default_criteria:
                    self.db_manager.initialize_default_criteria(self.default_criteria)

                if self.default_criterion_groups:
                    self.db_manager.initialize_default_groups(self.default_criterion_groups)

                if self.default_schedules:
                    self.db_manager.initialize_default_schedules(self.default_schedules)
            else:
                self.logger.info("jobhunter: Existing database found - skipping default initialization")

            # Verify initialization
            criteria_count = len(self.db_manager.get_active_criteria())
            groups_count = len(self.db_manager.get_active_groups())
            schedule_count = len(self.db_manager.get_active_run_times())
            self.logger.info(f"jobhunter: Database initialized: {criteria_count} criteria, {groups_count} groups, {schedule_count} schedules")

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
            self.logger.info("jobhunter: Message loop started")

            # Start scheduler thread (Phase 6)
            self._scheduler_thread = self.create_service_thread(
                target=self._scheduler_loop,
                name="scheduler-loop",
                daemon=True
            )
            self._scheduler_thread.start()
            self.logger.info("jobhunter: Scheduler loop started")

            self.logger.info("jobhunter: JobHunter service started successfully")

        except Exception as e:
            self.logger.error(f"jobhunter: Failed to start JobHunter service: {e}")
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
            self.logger.info("jobhunter: Stopping JobHunter service...")

            # Signal threads to stop
            self._running = False
            self._shutdown_event.set()

            # Wait for threads to complete (with timeout)
            if self._message_thread and self._message_thread.is_alive():
                self._message_thread.join(timeout=5.0)
                if self._message_thread.is_alive():
                    self.logger.warning("jobhunter: Message loop thread did not stop gracefully")

            # TODO Phase 6: Stop scheduler thread

            self.logger.info("jobhunter: JobHunter service stopped successfully")

        except Exception as e:
            self.logger.error(f"jobhunter: Error stopping JobHunter service: {e}")
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
                    "database_path": self.config.get('database_path', 'data/jobhunter.db')
                }
            }

        except Exception as e:
            self.logger.error(f"jobhunter: Health check failed: {e}")
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
            self.logger.info("jobhunter: Starting message processing loop")

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
                        self.logger.error(f"jobhunter: Error in message loop: {e}")
                    threading.Event().wait(0.1)

            self.logger.info("jobhunter: Message loop stopped")

        except Exception as e:
            self.logger.error(f"jobhunter: Critical error in message loop: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback: {traceback.format_exc()}")

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

            self.logger.info(f"jobhunter: 📨 Processing '{command}' from {sender}{correlation_info}")
            self.logger.debug(f"jobhunter: Command '{command}' has {len(args)} argument(s)")

            # ADR-009 F3: every command except help touches the owner's
            # personal workspace — enforce the owner gate (ADR-003: channels
            # assert identity, services enforce it). help stays open.
            if command != "help" and not self._authorize_owner(sender, correlation_id, message):
                return

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
            elif command == "top-10":
                self._handle_top_jobs(sender, correlation_id)
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
            # AI Analysis Commands (Phase 11)
            elif command == "analyze-job":
                self._handle_analyze_job(sender, args, correlation_id)
            elif command == "ai-stats":
                self._handle_ai_stats(sender, args, correlation_id)
            elif command == "list-ai-approved":
                self._handle_list_ai_approved(sender, args, correlation_id)
            elif command == "goal":
                self._handle_goal(sender, args, correlation_id)
            elif command == "load-cv":
                self._handle_load_cv(sender, args, correlation_id)
            # Run History Commands
            elif command == "history":
                self._handle_history(sender, args, correlation_id)
            elif command == "last-run":
                self._handle_last_run(sender, correlation_id)
            elif command == "run-detail":
                self._handle_run_detail(sender, args, correlation_id)
            else:
                self._send_error_response(
                    sender,
                    f"Unknown command: {command}. Try '@jobhunter help' for available commands.",
                    correlation_id=correlation_id
                )

        except Exception as e:
            self.logger.error(f"jobhunter: Error handling message: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback: {traceback.format_exc()}")

    # ============================================================================
    # Command Handlers
    # ============================================================================

    def _handle_help(self, sender: str, correlation_id: str = None):
        """Handle help command."""
        help_text = """**JobHunter Service Commands**

**Company Management:**
• **add-company** `<name> <url> [scraper_type]` — Add company to monitor
  Types: generic (default), greenhouse, lever, ashby
  Example: `@jobhunter add-company Anthropic https://www.anthropic.com/careers`
• **list-companies** — Show all tracked companies
• **remove-company** `<id>` — Remove a company
• **scrape-company** `<id>` — Manually scrape a company's career page

**Search Criteria:**
• **add-criteria** `<name> <type> [weight]` — Create a criterion
  Types: simple, group-any, group-all, group-tiered
  Example: `@jobhunter add-criteria remote simple 15`
• **add-criteria-value** `<name> <value> [tier] [weight]` — Add value to group criterion
  Tiers (group-tiered only): preferred, acceptable, undesirable
  Ranges: `"200k+"`, `"100k-150k"`, `"5+"`
  Example: `@jobhunter add-criteria-value salary "200k+" preferred 25`
• **list-criteria** — Show all criteria with IDs
• **remove-criteria** `<id>` — Remove criterion by ID
• **remove-criteria-value** `<name> <value>` — Remove value from group criterion
• **test-criteria** `<job_text>` — Test scoring against sample text
  Example: `@jobhunter test-criteria Senior Engineer - Remote - $250k`

**Job Scoring & Matching:**
• **score-jobs** `[limit] [min_score]` — Score jobs against criteria (default: 50, 0)
  Example: `@jobhunter score-jobs 20 70`
• **score-jobs** `<job_id>` — Full details for a specific job
• **deep-match** `[min_score] [limit]` — Deep scrape matched jobs (default: 60, 50)
• **list-new-jobs** — Show newly detected jobs
• **list-stale-jobs** `[days]` — Jobs older than N days (default: 14)

**Workflow & Scheduling:**
• **run-now** — Trigger full workflow: scrape → score → deep-match → AI analysis
• **top-10** — Show top 10 AI-scored jobs from last run
• **add-schedule** `<HH:MM> [timezone]` — Add daily run time
  Example: `@jobhunter add-schedule "09:00" "America/New_York"`
• **list-schedules** — Show scheduled run times
• **remove-schedule** `<id>` — Remove a schedule

**Run History:**
• **history** `[limit]` — Recent run history table (default: 10)
• **last-run** — Summary of the most recent completed run
• **run-detail** `<id>` — Full details for a specific run
  Example: `@jobhunter run-detail 5`

**AI Analysis:** (requires `ai_analysis.enabled: true` in config)
• **analyze-job** `<job_id>` — AI analysis vs CV + criteria + goal
  Returns: score (0-100), reasoning, recommendation (APPLY/REVIEW/SKIP)
• **ai-stats** `[days]` — AI usage cost statistics (default: 7)
• **list-ai-approved** `[limit]` — Jobs approved by AI (default: 20)

**Profile:**
• **goal** — View current search goal
• **goal** `<text>` — Set search goal (guides AI matching)
  Example: `@jobhunter goal VP Sales at a startup, remote, 250k base`
• **load-cv** — Extract CV from PDF in `data/cv/` directory

**Service:**
• **status** — Service health, config, and statistics
• **help** — This help message

**Notes:**
• Multi-word company names use underscores: `Y_Combinator`
• Quote multi-word values: `"US West"`, `"Sales Engineering"`
• Scores: 70+ high priority, 60-69 good match, <60 review carefully
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
            response += f"Next: Run `@jobhunter run-now` to scrape immediately, or wait for next scheduled run."

            self._send_response(sender, response, correlation_id=correlation_id)
            self.logger.info(f"jobhunter: Added company '{name}' (ID: {company_id})")

        except ValueError as e:
            self._send_error_response(sender, str(e), correlation_id=correlation_id)
        except Exception as e:
            self.logger.error(f"jobhunter: Error adding company: {e}")
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
                response += "Add companies with: `@jobhunter add-company \"Company Name\" \"https://example.com/careers\"`"
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
            self.logger.error(f"jobhunter: Error listing companies: {e}")
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
            self.logger.info(f"jobhunter: Removed company '{company['name']}' (ID: {company_id})")

        except Exception as e:
            self.logger.error(f"jobhunter: Error removing company: {e}")
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
            groups = self.db_manager.get_active_groups()
            schedules = self.db_manager.get_active_run_times()

            # Total criteria = simple criteria + group criteria
            total_criteria = len(criteria) + len(groups)

            response = f"**JobHunter Service Status**\n\n"
            response += f"🟢 Status: {health['status']}\n"
            response += f"📊 Active companies: {len(companies)}\n"
            response += f"🎯 Active criteria: {total_criteria} ({len(criteria)} simple, {len(groups)} groups)\n"
            response += f"⏰ Scheduled runs: {len(schedules)}\n"

            # AI Analysis status
            if self.ai_enabled:
                response += f"🤖 AI analysis: enabled (notify score >= {self.ai_min_score})\n"
                response += f"   Model: {self.ai_model}\n"
                response += f"   💰 Cost filter: Only analyze jobs with criteria score >= {self.ai_prescreen_threshold}\n"
                cv_status = "✅" if self._cv_content else "❌"
                response += f"   CV loaded: {cv_status}\n"
                goal_status = "✅" if self._goal_content else "❌ (set with: goal <text>)"
                response += f"   Search goal: {goal_status}\n"

                # Show AI cost statistics
                ai_stats = self.db_manager.get_ai_stats(days=30)  # Last 30 days
                if ai_stats['total_analyses'] > 0:
                    response += f"   📊 Last 30 days: {ai_stats['total_analyses']} analyses, "
                    response += f"${ai_stats['total_cost']:.4f} total cost\n"
                    response += f"   💰 Avg cost per job: ${ai_stats['avg_cost_per_job']:.4f}\n"
                else:
                    response += f"   📊 No AI analyses yet\n"
            else:
                response += f"🤖 AI analysis: disabled\n"

            response += "\n"

            if schedules:
                response += "**Upcoming Runs:**\n"
                for schedule in schedules[:3]:
                    response += f"• {schedule['run_time']} {schedule['timezone']}\n"

                # Calculate next run
                next_run = self._calculate_next_run()
                if next_run:
                    import pytz
                    ny_tz = pytz.timezone('America/New_York')
                    next_run_ny = next_run.astimezone(ny_tz)
                    response += f"\n⏰ **Next run:** {next_run_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            else:
                response += "⚠️ No schedules configured. Use `add-schedule` to set up automated runs.\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter: Error getting status: {e}")
            self._send_error_response(
                sender,
                f"Failed to get status: {str(e)}",
                correlation_id=correlation_id
            )

    def _handle_top_jobs(self, sender: str, correlation_id: str = None):
        """Handle top-10 command — show top 10 AI-scored jobs, sorted by age-adjusted score."""
        try:
            if not self.db_manager:
                self._send_error_response(sender, "Database not initialized.", correlation_id=correlation_id)
                return

            jobs = self.db_manager.get_ai_approved_jobs(min_score=0, limit=30)

            if not jobs:
                self._send_response(
                    sender,
                    "No AI-scored jobs found. Run `@jobhunter run-now` to scrape and analyze jobs.",
                    correlation_id=correlation_id,
                )
                return

            # Compute effective scores and sort by age-adjusted AI score
            enriched = []
            for job in jobs:
                ai_score = int(job.get('ai_score', 0))
                age_penalty = self._calculate_freshness_adjustment(job)
                effective_ai = ai_score + age_penalty
                criteria_score = self._filter_and_score_job(job)
                enriched.append({
                    **job,
                    'effective_ai': effective_ai,
                    'criteria_score': criteria_score,
                    'age_penalty': age_penalty,
                })

            # Only show active jobs (effective > 0)
            enriched = [j for j in enriched if j['effective_ai'] > 0]
            enriched.sort(key=lambda x: x['effective_ai'], reverse=True)
            top = enriched[:10]

            lines = [f"**Top {len(top)} AI-Scored Jobs** (age-adjusted)\n"]

            for i, job in enumerate(top, 1):
                title = job.get('title', 'Unknown')
                company = job.get('company_name', 'Unknown Company')
                location = job.get('location', 'N/A')
                ai_score = int(job.get('ai_score', 0))
                eff = job['effective_ai']
                criteria = job['criteria_score']
                penalty = job['age_penalty']
                recommendation = job.get('ai_recommendation', '—')
                reasoning = job.get('ai_reasoning', '')
                url = job.get('url', '')

                # Truncate reasoning to ~2 sentences
                summary = reasoning
                if summary:
                    sentences = summary.replace('. ', '.|').split('|')
                    summary = '. '.join(s.strip() for s in sentences[:2])
                    if not summary.endswith('.'):
                        summary += '.'

                penalty_str = f"+{penalty}" if penalty >= 0 else str(penalty)
                lines.append(f"**{i}. {title}** — {company}")
                lines.append(f"📍 {location} | 🏆 AI: {ai_score} (eff: {eff}, age: {penalty_str}) | 📊 Criteria: {criteria} | 💡 {recommendation}")
                if summary:
                    lines.append(f"📝 {summary}")
                if url:
                    lines.append(f"🔗 [Apply]({url})")
                lines.append("---")

            self._send_response(sender, '\n'.join(lines), correlation_id=correlation_id, content_type="text/markdown")

        except Exception as e:
            self.logger.error(f"jobhunter: Error in top-10: {e}")
            self._send_error_response(sender, f"Failed to get top jobs: {e}", correlation_id=correlation_id)

    # ============================================================================
    # Response Helpers
    # ============================================================================

    def _send_response(self, recipient: str, content: str, correlation_id: str = None, content_type: str = "text/plain"):
        """
        Send a response message.

        Args:
            recipient: Target service name
            content: Message content
            correlation_id: Optional correlation ID for request tracking
            content_type: MIME type - "text/plain", "text/markdown", "text/html", "application/json"
        """
        try:
            from robotray.core.messaging import MessageMetadata

            metadata = MessageMetadata(
                sender_service=self.name,
                target_service=recipient,
                priority=MessagePriority.NORMAL,
                correlation_id=correlation_id,
                content_type=content_type
            )
            message = SecureMessage(
                payload={"content": content},
                metadata=metadata
            )
            self.message_queues.outbound_queue.put_nowait(message)
            self.logger.debug(f"jobhunter: Sent response to {recipient} (type: {content_type})")
        except Exception as e:
            self.logger.error(f"jobhunter: Failed to send response: {e}")

    def _send_error_response(self, recipient: str, error_message: str, correlation_id: str = None):
        """Send an error response message."""
        content = f"❌ Error: {error_message}"
        self._send_response(recipient, content, correlation_id=correlation_id)

    def _authorize_owner(self, sender: str, correlation_id: Optional[str],
                         message) -> bool:
        """Owner gate for the personal job-hunt workspace (ADR-009 F3).

        Returns True only when the caller's asserted identity is this
        service's configured owner. A missing identity or a non-owner is
        refused (the non-owner gets the same 'not your workspace' message —
        no existence oracle about whose it is). Reply routing is unchanged:
        the refusal goes back to the requesting channel.
        """
        try:
            user_id = int(message.metadata.user_id) \
                if message.metadata.user_id is not None else None
        except (TypeError, ValueError):
            user_id = None

        if user_id is None:
            self._send_error_response(
                sender,
                "JobHunter is a personal workspace and your channel didn't "
                "attach an identity. Use an authenticated channel (web login, "
                "or a /link-verified telegram chat).",
                correlation_id=correlation_id,
            )
            return False

        if user_id != self.owner_user_id:
            self.logger.warning(
                f"jobhunter: refused command from non-owner user {user_id} "
                f"(owner is {self.owner_user_id})")
            self._send_error_response(
                sender,
                "This JobHunter workspace belongs to another user.",
                correlation_id=correlation_id,
            )
            return False

        return True

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
            self.logger.error(f"jobhunter: Error adding criterion: {e}")
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
            self.logger.error(f"jobhunter: Error listing criteria: {e}")
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
            self.logger.error(f"jobhunter: Error removing criterion: {e}")
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
            self.logger.error(f"jobhunter: Error adding criterion value: {e}")
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
            self.logger.error(f"jobhunter: Error removing criterion value: {e}")
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
            self.logger.debug(f"jobhunter: DEBUG: Extracted numbers from text: {extracted_numbers}")

            for group in groups:
                self.logger.debug(f"jobhunter: DEBUG: Checking group '{group['name']}' (match_type={group['match_type']}, {len(group.get('values', []))} values)")
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
                            self.logger.debug(f"jobhunter: DEBUG:   '{value}' is range → ({min_val}, {max_val})")
                            if self._matches_range(min_val, max_val, extracted_numbers):
                                self.logger.debug(f"jobhunter: DEBUG:   MATCHED! Adding {int(tier_weight):+d} points")
                                matched = True
                                group_matches.append(f"{group['name']}: {value} [numeric match] ({tier}, {int(tier_weight):+d})")
                            else:
                                self.logger.debug(f"jobhunter: DEBUG:   No match")
                        else:
                            # Fall back to substring matching
                            self.logger.debug(f"jobhunter: DEBUG:   '{value}' is string, checking substring match")
                            if value.lower() in job_text_lower:
                                self.logger.debug(f"jobhunter: DEBUG:   MATCHED! Adding {int(tier_weight):+d} points")
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
            self.logger.error(f"jobhunter: Error testing criteria: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to test criteria: {str(e)}", correlation_id=correlation_id)

    def _handle_scrape_company(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle scrape-company command - manually trigger scraping for a company.

        Usage:
            @jobhunter scrape-company <company_id>
            @jobhunter scrape-company <company_name>
        """
        try:
            if not args:
                self._send_error_response(
                    sender,
                    "❌ Missing company ID or name\n\nUsage: @jobhunter scrape-company <company_id|name>",
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
                    f"❌ Company lookup by name not yet implemented. Please use company ID.\n\nTip: Use '@jobhunter list-companies' to see IDs",
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
                careers_url=company['careers_url'],
                scraper_type=company.get('scraper_type', 'generic')
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
                    self.logger.debug(f"jobhunter: Saved job {job_id}: {job['title']}")
                except Exception as e:
                    if 'UNIQUE constraint' in str(e):
                        duplicate_count += 1
                        self.logger.debug(f"jobhunter: Skipping duplicate: {job['title']}")
                    else:
                        self.logger.error(f"jobhunter: Error saving job '{job['title']}': {e}")

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
            self.logger.error(f"jobhunter: Error scraping company: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to scrape company: {str(e)}", correlation_id=correlation_id)

    def _handle_score_jobs(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle score-jobs command - score recent jobs against criteria.

        Usage:
            @jobhunter score-jobs [limit] [min_score]
            @jobhunter score-jobs <job_id>  # Show details for specific job
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
            self.logger.error(f"jobhunter: Error scoring jobs: {e}")
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
            self.logger.error(f"jobhunter: Error showing job details: {e}")
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
            self.logger.error(f"jobhunter: Error deep scraping matches: {e}")
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
            # Log at debug level - user already knows they triggered it
            self.logger.debug("jobhunter: Manual workflow triggered by user")

            response = "🚀 Starting job search workflow...\n"
            self._send_response(sender, response, correlation_id=correlation_id)

            # Run the workflow in the current thread (blocking)
            self._run_daily_scrape(trigger_type='manual')

            # Send completion message
            final_response = "\n✅ **Workflow Complete!**\n"
            final_response += "Check the logs for detailed results or use 'score-jobs' to see matches."

            self._send_response(sender, final_response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter: Error in manual run: {e}")
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
            self.logger.error(f"jobhunter: Error adding schedule: {e}")
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
                    # Convert to America/New_York for display
                    ny_tz = pytz.timezone('America/New_York')
                    next_run_ny = next_run.astimezone(ny_tz)
                    now = datetime.now(pytz.UTC)
                    time_until = next_run - now
                    hours = int(time_until.total_seconds() // 3600)
                    minutes = int((time_until.total_seconds() % 3600) // 60)

                    response += f"\n🕐 **Next Run:** {next_run_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    response += f"⏱️  **Time Until:** {hours}h {minutes}m"

            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter: Error listing schedules: {e}")
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
            self.logger.error(f"jobhunter: Error removing schedule: {e}")
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
            self.logger.error(f"jobhunter: Error listing new jobs: {e}")
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
            self.logger.error(f"jobhunter: Error listing stale jobs: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list stale jobs: {str(e)}", correlation_id=correlation_id)

    # ============================================================================
    # AI Analysis Commands (Phase 11)
    # ============================================================================

    def _handle_analyze_job(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle analyze-job command - manually analyze a specific job with AI.

        Usage:
          analyze-job <job_id>
          analyze-job 123
        """
        try:
            if not self.ai_enabled:
                self._send_error_response(
                    sender,
                    "❌ AI analysis is not enabled. Check configuration.",
                    correlation_id=correlation_id
                )
                return

            if not args or len(args) < 1:
                self._send_error_response(
                    sender,
                    "❌ Usage: analyze-job <job_id>",
                    correlation_id=correlation_id
                )
                return

            job_id = int(args[0])

            # Get job from database
            job = self.db_manager.get_job_by_id(job_id)
            if not job:
                self._send_error_response(
                    sender,
                    f"❌ Job ID {job_id} not found",
                    correlation_id=correlation_id
                )
                return

            # Run AI analysis
            self.logger.info(f"jobhunter: Running AI analysis on job {job_id}: {job.get('title')}")
            ai_result = self._analyze_job_with_cv(job)

            if not ai_result:
                self._send_error_response(
                    sender,
                    f"❌ AI analysis failed for job {job_id}",
                    correlation_id=correlation_id
                )
                return

            # Save results
            self.db_manager.update_job_match_ai_analysis(
                job_id=job_id,
                ai_score=ai_result['ai_score'],
                ai_reasoning=ai_result['reasoning'],
                ai_recommendation=ai_result['recommendation']
            )

            # Format response
            response = f"🤖 **AI Job Analysis**\n\n"
            response += f"**Job:** {job.get('title', 'Unknown')}\n"
            response += f"**Company:** {job.get('company_name', 'Unknown')}\n"
            response += f"**URL:** {job['url']}\n\n"
            response += f"**AI Score:** {ai_result['ai_score']}/100\n"
            response += f"**Recommendation:** {ai_result['recommendation']}\n\n"
            response += f"**Analysis:**\n{ai_result['reasoning']}\n\n"
            response += f"**Fit Summary:**\n{ai_result['fit_summary']}\n\n"

            if ai_result.get('concerns'):
                response += f"**Concerns:**\n"
                for concern in ai_result['concerns']:
                    response += f"  • {concern}\n"
            else:
                response += f"**No concerns identified**\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except ValueError:
            self._send_error_response(
                sender,
                "❌ Invalid job ID. Must be a number.",
                correlation_id=correlation_id
            )
        except Exception as e:
            self.logger.error(f"jobhunter: Error analyzing job: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to analyze job: {str(e)}", correlation_id=correlation_id)

    def _handle_ai_stats(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle ai-stats command - show AI analysis cost statistics.

        Usage:
          ai-stats [days]
          ai-stats 30
        """
        try:
            if not self.ai_enabled:
                self._send_error_response(
                    sender,
                    "❌ AI analysis is not enabled. Check configuration.",
                    correlation_id=correlation_id
                )
                return

            # Get period (default 30 days)
            days = int(args[0]) if args and len(args) > 0 else 30

            # Get stats from database
            stats = self.db_manager.get_ai_stats(days=days)

            # Format response
            response = f"📊 **AI Analysis Statistics** (Last {days} days)\n\n"
            response += f"**Usage:**\n"
            response += f"  • Total analyses: {stats['total_analyses']}\n"
            response += f"  • Total tokens: {stats['total_tokens']:,}\n"
            response += f"  • Input tokens: {stats['total_input_tokens']:,}\n"
            response += f"  • Output tokens: {stats['total_output_tokens']:,}\n\n"

            response += f"**Cost:**\n"
            response += f"  • Total cost: ${stats['total_cost']:.4f}\n"
            response += f"  • Avg cost/job: ${stats['avg_cost_per_job']:.4f}\n"
            response += f"  • Daily limit: ${self.ai_cost_limit_daily:.2f}\n\n"

            if stats['last_analysis_date']:
                response += f"**Last Analysis:** {stats['last_analysis_date']}\n\n"

            response += f"**Model:** {self.ai_model}\n"
            response += f"**Min Score Threshold:** {self.ai_min_score}\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except ValueError:
            self._send_error_response(
                sender,
                "❌ Invalid days parameter. Must be a number.",
                correlation_id=correlation_id
            )
        except Exception as e:
            self.logger.error(f"jobhunter: Error getting AI stats: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to get AI stats: {str(e)}", correlation_id=correlation_id)

    def _handle_list_ai_approved(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle list-ai-approved command - show jobs approved by AI analysis.

        Usage:
          list-ai-approved [limit]
          list-ai-approved 10
        """
        try:
            if not self.ai_enabled:
                self._send_error_response(
                    sender,
                    "❌ AI analysis is not enabled. Check configuration.",
                    correlation_id=correlation_id
                )
                return

            # Get limit (default 20)
            limit = int(args[0]) if args and len(args) > 0 else 20

            # Get AI-approved jobs from database
            jobs = self.db_manager.get_ai_approved_jobs(
                min_score=self.ai_min_score,
                limit=limit
            )

            if not jobs:
                response = f"🤖 **AI-Approved Jobs**\n\n"
                response += f"No jobs with AI score >= {self.ai_min_score} found.\n"
                response += f"Run 'run-now' to trigger a scrape with AI analysis."
            else:
                response = f"🤖 **AI-Approved Jobs** ({len(jobs)} found)\n\n"
                response += f"Jobs with AI score >= {self.ai_min_score}:\n\n"

                for job in jobs[:20]:
                    response += f"**{int(job['ai_score'])}/100** - {job['title']}\n"
                    response += f"   🏢 {job['company_name']}\n"
                    response += f"   📍 {job.get('location', 'Location not specified')}\n"
                    response += f"   💡 {job['ai_recommendation']}\n"
                    response += f"   🔗 {job['url']}\n"

                    # Add reasoning snippet
                    reasoning = job.get('ai_reasoning', '')
                    if reasoning and len(reasoning) > 150:
                        reasoning = reasoning[:147] + "..."
                    if reasoning:
                        response += f"   📝 {reasoning}\n"

                    response += "\n"

                if len(jobs) > 20:
                    response += f"... and {len(jobs) - 20} more\n"

            self._send_response(sender, response, correlation_id=correlation_id)

        except ValueError:
            self._send_error_response(
                sender,
                "❌ Invalid limit parameter. Must be a number.",
                correlation_id=correlation_id
            )
        except Exception as e:
            self.logger.error(f"jobhunter: Error listing AI-approved jobs: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(sender, f"Failed to list AI-approved jobs: {str(e)}", correlation_id=correlation_id)

    # ============================================================================
    # Goal & CV Management
    # ============================================================================

    def _handle_goal(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle goal command - view or set the search goal.

        Usage:
          goal              - Show current search goal
          goal <text>       - Set a new search goal
        """
        try:
            if not args:
                # Show current goal
                if self._goal_content:
                    response = "🎯 **My Search Goal**\n\n"
                    response += self._goal_content
                else:
                    response = (
                        "🎯 **No search goal set.**\n\n"
                        "Set your goal in first person to guide AI job matching:\n"
                        "  @jobhunter goal Im looking for a VP Sales role "
                        "at a frontier AI company, remote preferred..."
                    )
                self._send_response(sender, response, correlation_id=correlation_id)
                return

            # Set new goal
            goal_text = " ".join(args)

            # Ensure cv directory exists
            self.cv_dir.mkdir(parents=True, exist_ok=True)

            # Write goal to file
            self.goal_path.write_text(goal_text + "\n")
            self._goal_content = goal_text

            self.logger.info(
                f"jobhunter: Search goal updated ({len(goal_text)} characters)"
            )

            response = "🎯 **Search goal updated!**\n\n"
            response += f"{goal_text}\n\n"
            response += (
                "This goal will be included in all AI job analysis to "
                "better match positions to your personal preferences."
            )
            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter: Error handling goal command: {e}")
            self._send_error_response(
                sender, f"Failed to handle goal: {str(e)}", correlation_id=correlation_id
            )

    def _handle_load_cv(self, sender: str, args: List[str], correlation_id: str = None):
        """
        Handle load-cv command - find PDF in cv directory and create resume.md.

        Usage:
          load-cv           - Auto-detect PDF in data/cv/ directory
        """
        try:
            # Ensure cv directory exists
            self.cv_dir.mkdir(parents=True, exist_ok=True)

            # Find PDF files in the cv directory
            pdf_files = list(self.cv_dir.glob("*.pdf"))

            if not pdf_files:
                self._send_error_response(
                    sender,
                    f"❌ No PDF files found in {self.cv_dir}/\n"
                    f"Place your CV as a PDF file there and try again.",
                    correlation_id=correlation_id,
                )
                return

            # Use the most recently modified PDF
            pdf_path = max(pdf_files, key=lambda p: p.stat().st_mtime)

            self.logger.info(
                f"jobhunter: Loading CV from {pdf_path.name}"
            )

            # Extract text from PDF
            try:
                import PyPDF2

                text_parts = []
                with open(pdf_path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    for page_num, page in enumerate(reader.pages):
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(page_text)

                if not text_parts:
                    self._send_error_response(
                        sender,
                        f"❌ Could not extract text from {pdf_path.name}. "
                        "The PDF may be image-based (scanned). "
                        "Try saving your CV as a text-based PDF.",
                        correlation_id=correlation_id,
                    )
                    return

                extracted_text = "\n\n".join(text_parts)

            except ImportError:
                self._send_error_response(
                    sender,
                    "❌ PyPDF2 library not installed. Run: pip install PyPDF2",
                    correlation_id=correlation_id,
                )
                return

            # Build resume.md content
            md_content = f"# CV - Extracted from {pdf_path.name}\n\n"
            md_content += extracted_text

            # Append existing goal reference if goal exists
            if self._goal_content:
                md_content += "\n\n---\n\n"
                md_content += "## Search Goal\n\n"
                md_content += f"See goal.md for current search goal.\n"

            # Write resume.md
            self.cv_path.write_text(md_content)

            # Reload into memory
            self._cv_content = md_content

            self.logger.info(
                f"jobhunter: CV extracted from {pdf_path.name} -> "
                f"resume.md ({len(md_content)} characters, "
                f"{len(text_parts)} pages)"
            )

            response = f"📄 **CV Loaded Successfully**\n\n"
            response += f"Source: `{pdf_path.name}`\n"
            response += f"Pages extracted: {len(text_parts)}\n"
            response += f"Characters: {len(md_content):,}\n"
            response += f"Saved to: `{self.cv_path}`\n\n"
            response += (
                "Your CV is now available for AI job analysis. "
                "The AI will compare job postings against your profile."
            )
            self._send_response(sender, response, correlation_id=correlation_id)

        except Exception as e:
            self.logger.error(f"jobhunter: Error loading CV: {e}")
            import traceback

            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self._send_error_response(
                sender, f"Failed to load CV: {str(e)}", correlation_id=correlation_id
            )

    # ============================================================================
    # Run History Commands
    # ============================================================================

    def _handle_history(self, sender: str, args: List[str], correlation_id: str = None):
        """Show recent run history as a table."""
        limit = 10
        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                pass

        runs = self.db_manager.get_runs(limit=limit)
        if not runs:
            self._send_response(sender, "No run history found. Use `run-now` to start a workflow.", correlation_id=correlation_id)
            return

        lines = [f"**Run History** (last {len(runs)})\n"]
        lines.append("| ID | Date | Trigger | Status | Jobs | New | Matches | AI Ok | Duration |")
        lines.append("|-----|------|---------|--------|------|-----|---------|-------|----------|")
        for r in runs:
            date_str = r['run_date'][:16].replace('T', ' ') if r['run_date'] else '—'
            dur = f"{r['duration_seconds']:.0f}s" if r['duration_seconds'] else '—'
            lines.append(
                f"| {r['id']} | {date_str} | {r['trigger_type']} | {r['status']} "
                f"| {r['jobs_found']} | {r['new_jobs']} | {r['matches']} "
                f"| {r['ai_approved']} | {dur} |"
            )

        self._send_response(sender, '\n'.join(lines), correlation_id=correlation_id, content_type="text/markdown")

    def _handle_last_run(self, sender: str, correlation_id: str = None):
        """Show the full summary from the most recent completed run."""
        run = self.db_manager.get_latest_run()
        if not run:
            self._send_response(sender, "No completed runs found yet.", correlation_id=correlation_id)
            return

        date_str = run['run_date'][:16].replace('T', ' ') if run['run_date'] else '—'
        dur = f"{run['duration_seconds']:.0f}s" if run['duration_seconds'] else '—'

        response = f"**Last Run** (#{run['id']} — {date_str}, {run['trigger_type']}, {dur})\n\n"
        response += f"Jobs: {run['jobs_found']} found, {run['new_jobs']} new, {run['matches']} matches, {run['ai_approved']} AI approved\n\n"

        if run['summary_text']:
            response += f"---\n\n{run['summary_text']}"
        else:
            response += "_No AI summary for this run._"

        self._send_response(sender, response, correlation_id=correlation_id, content_type="text/markdown")

    def _handle_run_detail(self, sender: str, args: List[str], correlation_id: str = None):
        """Show full details for a specific run by ID."""
        if not args:
            self._send_error_response(sender, "Usage: `run-detail <id>`", correlation_id=correlation_id)
            return

        try:
            run_id = int(args[0])
        except ValueError:
            self._send_error_response(sender, "Invalid run ID. Must be a number.", correlation_id=correlation_id)
            return

        run = self.db_manager.get_run(run_id)
        if not run:
            self._send_error_response(sender, f"Run #{run_id} not found.", correlation_id=correlation_id)
            return

        date_str = run['run_date'][:16].replace('T', ' ') if run['run_date'] else '—'
        dur = f"{run['duration_seconds']:.1f}s" if run['duration_seconds'] else '—'

        lines = [
            f"**Run #{run['id']}**\n",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Date | {date_str} |",
            f"| Trigger | {run['trigger_type']} |",
            f"| Status | {run['status']} |",
            f"| Companies | {run['companies_scraped']} |",
            f"| Jobs Found | {run['jobs_found']} |",
            f"| New Jobs | {run['new_jobs']} |",
            f"| Matches | {run['matches']} |",
            f"| Deep Scraped | {run['deep_scraped']} |",
            f"| AI Analyzed | {run['ai_analyzed']} |",
            f"| AI Approved | {run['ai_approved']} |",
            f"| Duration | {dur} |",
        ]

        if run['error_message']:
            lines.append(f"\n**Error:** {run['error_message']}")

        if run['summary_text']:
            lines.append(f"\n---\n\n**Summary:**\n\n{run['summary_text']}")

        self._send_response(sender, '\n'.join(lines), correlation_id=correlation_id, content_type="text/markdown")

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
                self.logger.warning(f"jobhunter: robots.txt disallows scraping: {base_url}")
            return can_fetch

        except Exception as e:
            self.logger.debug(f"jobhunter: Could not check robots.txt for {base_url}: {e}")
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
                self.logger.debug(f"jobhunter: Fetching {url} (attempt {attempt + 1}/{max_retries})")

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

                self.logger.debug(f"jobhunter: Successfully fetched {url} ({len(response.text)} bytes)")
                return response.text

            except requests.exceptions.Timeout:
                self.logger.warning(f"jobhunter: Timeout fetching {url} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    sleep_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    time.sleep(sleep_time)
                else:
                    raise

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code < 500:
                    # Client errors (4xx) are permanent for this URL — one concise
                    # WARNING (no retry counter, no traceback), then raise.
                    self.logger.warning(
                        f"jobhunter: {e.response.status_code} for {url} — source looks "
                        f"dead/moved; consider deactivating the company")
                    raise
                self.logger.error(f"jobhunter: HTTP error fetching {url}: {e} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:   # retry server errors (5xx)
                    sleep_time = 2 ** attempt
                    time.sleep(sleep_time)
                else:
                    raise

            except requests.exceptions.RequestException as e:
                self.logger.error(f"jobhunter: Request error fetching {url}: {e} (attempt {attempt + 1}/{max_retries})")
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
                    self.logger.debug(f"jobhunter: Found {len(elements)} job elements using selector {selector['tag']}.{class_name}")
                    job_elements.extend(elements)
                    break
            if job_elements:
                break

        # Fallback: Look for direct job posting links (Greenhouse, Lever, Workday, etc.)
        if not job_elements:
            self.logger.debug("jobhunter: No job elements found with standard selectors, looking for direct job links")

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
                    self.logger.debug(f"jobhunter: Skipping non-job URL: {title} ({job_url})")
                    continue

                # Skip if title looks like a navigation link
                skip_titles = ['careers', 'view all', 'see all', 'browse', 'apply']
                if title.lower().strip() in skip_titles:
                    self.logger.debug(f"jobhunter: Skipping navigation link: {title}")
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
                self.logger.debug(f"jobhunter: Extracted job: {title}")

            except Exception as e:
                self.logger.warning(f"jobhunter: Error extracting job from element: {e}")
                continue

        if self.verbose_logging:
            self.logger.info(f"jobhunter: Extracted {len(jobs)} jobs from HTML")
        return jobs

    def _deep_scrape_job(self, job_id: int, job_url: str) -> bool:
        """
        Follow job URL to scrape full details (Phase 5.5).

        For Greenhouse jobs, uses the API. For others, parses HTML.

        Args:
            job_id: Database ID of job to update
            job_url: URL to scrape

        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if this is a Greenhouse job
            if 'greenhouse.io' in job_url:
                return self._deep_scrape_greenhouse_job(job_id, job_url)

            # Check if this is an Ashby job
            if 'ashbyhq.com' in job_url:
                return self._deep_scrape_ashby_job(job_id, job_url)

            # Check if this is a Lever job
            if 'lever.co' in job_url:
                return self._deep_scrape_lever_job(job_id, job_url)

            # Fetch job detail page with politeness delay (non-Greenhouse/Ashby/Lever)
            if self.verbose_logging:
                self.logger.info(f"jobhunter: Deep scraping job {job_id}: {job_url}")
            html = self._fetch_with_retry(job_url, politeness_delay=2.0)

            if not html:
                if self.verbose_logging:
                    self.logger.warning(f"jobhunter: Failed to fetch job {job_id}")
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
                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Successfully deep scraped job {job_id}")
                return True
            else:
                if self.verbose_logging:
                    self.logger.warning(f"jobhunter: No additional details extracted for job {job_id}")
                return False

        except Exception as e:
            self.logger.error(f"jobhunter: Error deep scraping job {job_id}: {e}")
            return False

    def _deep_scrape_greenhouse_job(self, job_id: int, job_url: str) -> bool:
        """
        Deep scrape a Greenhouse job using their API.

        Args:
            job_id: Database ID of job to update
            job_url: Greenhouse job URL (e.g., https://job-boards.greenhouse.io/anthropic/jobs/5101832008)

        Returns:
            True if successful, False otherwise
        """
        try:
            import requests
            import re
            from urllib.parse import urlparse

            # Extract company slug and job ID from URL
            # URL format: https://job-boards.greenhouse.io/{company}/jobs/{job_id}
            path_parts = urlparse(job_url).path.strip('/').split('/')
            if len(path_parts) >= 3 and 'jobs' in path_parts:
                jobs_index = path_parts.index('jobs')
                company_slug = path_parts[jobs_index - 1]
                greenhouse_job_id = path_parts[jobs_index + 1]

                # Call Greenhouse API for individual job
                api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{greenhouse_job_id}"

                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Fetching Greenhouse job details from API: {api_url}")

                headers = {
                    'User-Agent': 'RobotRay JobHunter/1.0 (Career Page Monitor)',
                    'Accept': 'application/json'
                }
                response = requests.get(api_url, headers=headers, timeout=self.request_timeout)

                if response.status_code == 200:
                    job_data = response.json()

                    # Extract description (content field)
                    description = job_data.get('content', '')

                    # Extract posted date (first_published field)
                    posted_date = job_data.get('first_published', None)

                    # Extract salary if present in description
                    salary_text = None
                    salary_min = None
                    salary_max = None

                    if description:
                        # Look for salary patterns
                        salary_patterns = [
                            r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\s*-\s*\$(\d{1,3}(?:,\d{3})*(?:k)?)',
                            r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\+',
                        ]

                        for pattern in salary_patterns:
                            match = re.search(pattern, description, re.IGNORECASE)
                            if match:
                                salary_text = match.group(0)
                                if '-' in salary_text:
                                    salary_min = self._parse_salary(match.group(1))
                                    salary_max = self._parse_salary(match.group(2))
                                else:
                                    salary_min = self._parse_salary(match.group(1))
                                break

                    # Update database
                    if description:
                        self.db_manager.update_job_details(
                            job_id=job_id,
                            description=description,
                            salary_text=salary_text,
                            salary_min=salary_min,
                            salary_max=salary_max,
                            posted_date=posted_date
                        )
                        if self.verbose_logging:
                            self.logger.info(
                                f"jobhunter: Greenhouse API returned {len(description)} chars for job {job_id}"
                            )
                        return True
                    else:
                        self.logger.warning(f"jobhunter: No content in Greenhouse API response for job {job_id}")
                        return False
                else:
                    self.logger.warning(f"jobhunter: Greenhouse API returned {response.status_code} for job {job_id}")
                    return False
            else:
                self.logger.warning(f"jobhunter: Could not parse Greenhouse URL: {job_url}")
                return False

        except Exception as e:
            self.logger.error(f"jobhunter: Error deep scraping Greenhouse job {job_id}: {e}")
            return False

    def _deep_scrape_ashby_job(self, job_id: int, job_url: str) -> bool:
        """
        Deep scrape an Ashby job to get full details.

        Ashby job detail pages contain the full job description in window.__appData JSON.

        Args:
            job_id: Database ID of job to update
            job_url: Ashby job URL (e.g., https://jobs.ashbyhq.com/elevenlabs/uuid)

        Returns:
            True if successful, False otherwise
        """
        try:
            import json
            import re

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Deep scraping Ashby job {job_id}: {job_url}")

            # Fetch the job detail page
            html = self._fetch_with_retry(job_url, politeness_delay=2.0)
            if not html:
                self.logger.warning(f"jobhunter: Failed to fetch Ashby job page {job_id}")
                return False

            # Extract __appData JSON using brace counting (same as light scrape)
            app_data_start = html.find('window.__appData')
            if app_data_start == -1:
                app_data_start = html.find('__appData')

            if app_data_start == -1:
                self.logger.warning(f"jobhunter: No __appData found in Ashby job page {job_id}")
                return False

            # Find the opening brace
            json_start = html.find('{', app_data_start)
            if json_start == -1:
                return False

            # Use brace counting to find the matching closing brace
            brace_count = 0
            json_end = json_start
            for i, char in enumerate(html[json_start:], start=json_start):
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break

            json_str = html[json_start:json_end]
            try:
                app_data = json.loads(json_str)
            except json.JSONDecodeError as e:
                self.logger.warning(f"jobhunter: Failed to parse Ashby job JSON: {e}")
                return False

            # Extract job description from the data
            # Ashby uses 'posting' key for job detail pages (not 'job' or 'jobPosting')
            description = None
            posting = app_data.get('posting') or app_data.get('job') or app_data.get('jobPosting') or {}

            # Try various fields for description
            description = (
                posting.get('descriptionHtml') or
                posting.get('descriptionPlainText') or
                posting.get('descriptionPlain') or
                posting.get('description') or
                app_data.get('descriptionHtml') or
                app_data.get('description', '')
            )

            # Clean HTML tags if present
            if description and '<' in description:
                # Strip HTML but preserve some structure
                soup = BeautifulSoup(description, 'lxml')
                description = soup.get_text(separator='\n', strip=True)

            # Extract salary if present
            salary_text = None
            salary_min = None
            salary_max = None

            # Check for salary in compensation field
            compensation = posting.get('compensation') or posting.get('compensationTierSummary')
            if compensation:
                salary_text = compensation if isinstance(compensation, str) else str(compensation)

            # Also look for salary patterns in description
            if description and not salary_text:
                salary_patterns = [
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\s*-\s*\$(\d{1,3}(?:,\d{3})*(?:k)?)',
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\+',
                ]

                for pattern in salary_patterns:
                    match = re.search(pattern, description, re.IGNORECASE)
                    if match:
                        salary_text = match.group(0)
                        if '-' in salary_text:
                            salary_min = self._parse_salary(match.group(1))
                            salary_max = self._parse_salary(match.group(2))
                        else:
                            salary_min = self._parse_salary(match.group(1))
                        break

            # Update database
            if description and len(description) > 50:
                self.db_manager.update_job_details(
                    job_id=job_id,
                    description=description,
                    salary_text=salary_text,
                    salary_min=salary_min,
                    salary_max=salary_max
                )
                if self.verbose_logging:
                    self.logger.info(
                        f"jobhunter: Ashby deep scrape returned {len(description)} chars for job {job_id}"
                    )
                return True
            else:
                self.logger.warning(
                    f"jobhunter: No valid description found in Ashby job page {job_id} "
                    f"(got {len(description) if description else 0} chars)"
                )
                return False

        except Exception as e:
            self.logger.error(f"jobhunter: Error deep scraping Ashby job {job_id}: {e}")
            return False

    def _deep_scrape_lever_job(self, job_id: int, job_url: str) -> bool:
        """
        Deep scrape a Lever job to get full details.

        Lever job URLs contain a unique ID that can be used to fetch from the API.
        URL format: https://jobs.lever.co/{company}/{job_uuid}

        Args:
            job_id: Database ID of job to update
            job_url: Lever job URL

        Returns:
            True if successful, False otherwise
        """
        try:
            import json
            import re
            import requests
            from urllib.parse import urlparse

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Deep scraping Lever job {job_id}: {job_url}")

            # Extract company and job UUID from URL
            # URL format: https://jobs.lever.co/{company}/{job_uuid}
            parsed = urlparse(job_url)
            path_parts = parsed.path.strip('/').split('/')

            if len(path_parts) < 2:
                self.logger.warning(f"jobhunter: Could not parse Lever job URL: {job_url}")
                return False

            company_slug = path_parts[0]
            job_uuid = path_parts[1]

            # Fetch job details from Lever API
            api_url = f"https://api.lever.co/v0/postings/{company_slug}/{job_uuid}"

            headers = {
                'User-Agent': 'RobotRay JobHunter/1.0 (Career Page Monitor)',
                'Accept': 'application/json'
            }

            response = requests.get(api_url, headers=headers, timeout=self.request_timeout)

            if response.status_code != 200:
                self.logger.warning(f"jobhunter: Lever API returned {response.status_code} for job {job_id}")
                return False

            job_data = response.json()

            # Extract description
            description = job_data.get('descriptionPlain', '') or job_data.get('description', '')

            # Clean HTML from description if present
            if description and '<' in description:
                soup = BeautifulSoup(description, 'lxml')
                description = soup.get_text(separator='\n', strip=True)

            # Extract salary if present in description
            salary_text = None
            salary_min = None
            salary_max = None

            if description:
                salary_patterns = [
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\s*-\s*\$(\d{1,3}(?:,\d{3})*(?:k)?)',
                    r'\$(\d{1,3}(?:,\d{3})*(?:k)?)\+',
                ]

                for pattern in salary_patterns:
                    match = re.search(pattern, description, re.IGNORECASE)
                    if match:
                        salary_text = match.group(0)
                        if '-' in salary_text:
                            salary_min = self._parse_salary(match.group(1))
                            salary_max = self._parse_salary(match.group(2))
                        else:
                            salary_min = self._parse_salary(match.group(1))
                        break

            # Update database
            if description and len(description) > 50:
                self.db_manager.update_job_details(
                    job_id=job_id,
                    description=description,
                    salary_text=salary_text,
                    salary_min=salary_min,
                    salary_max=salary_max
                )
                if self.verbose_logging:
                    self.logger.info(
                        f"jobhunter: Lever API returned {len(description)} chars for job {job_id}"
                    )
                return True
            else:
                self.logger.warning(
                    f"jobhunter: No valid description from Lever API for job {job_id}"
                )
                return False

        except Exception as e:
            self.logger.error(f"jobhunter: Error deep scraping Lever job {job_id}: {e}")
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

        if self.verbose_logging:
            self.logger.info(f"jobhunter: Starting deep scrape for {total} matched jobs")

        for i, job in enumerate(matched_jobs, 1):
            if self.verbose_logging:
                self.logger.info(f"jobhunter: Deep scraping {i}/{total}: {job.get('title', 'Unknown')}")

            # Deep scrape with politeness delay
            if self._deep_scrape_job(job['id'], job['url']):
                success_count += 1

            # Rate limiting: 2 second delay between deep scrapes
            if i < total:  # Don't sleep after the last one
                time.sleep(2.0)

        if self.verbose_logging:
            self.logger.info(f"jobhunter: Deep scrape complete: {success_count}/{total} successful")
        return success_count

    def _scrape_greenhouse(self, careers_url: str) -> List[Dict[str, Any]]:
        """
        Scrape jobs from a Greenhouse job board using their API.

        Modern Greenhouse boards use client-side JavaScript to load jobs from their API.
        This method tries multiple approaches:
        1. Direct API call (boards-api.greenhouse.io) - most reliable
        2. Embedded JSON in HTML <script> tags (older boards)
        3. Structured HTML parsing (fallback)

        Args:
            careers_url: URL of Greenhouse job board
                        (e.g., https://www.anthropic.com/careers/jobs OR
                               https://job-boards.greenhouse.io/anthropic/jobs)

        Returns:
            List of job dictionaries with keys: title, url, location, description
        """
        try:
            import json
            import re
            import requests
            from urllib.parse import urlparse

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Using Greenhouse parser for {careers_url}")

            # Extract company slug from URL
            # Examples:
            # - https://www.anthropic.com/careers/jobs -> "anthropic"
            # - https://job-boards.greenhouse.io/anthropic/jobs -> "anthropic"
            # - https://boards.greenhouse.io/anthropic -> "anthropic"
            company_slug = None
            parsed = urlparse(careers_url)
            domain = parsed.netloc
            path_parts = parsed.path.strip('/').split('/')

            # Strategy 1: If URL contains greenhouse.io, extract from path
            if 'greenhouse.io' in domain:
                # Path like: /anthropic/jobs or /anthropic
                if path_parts and path_parts[0] and path_parts[0] not in ['careers', 'jobs']:
                    company_slug = path_parts[0]

            # Strategy 2: Extract from domain (e.g., www.anthropic.com -> anthropic)
            if not company_slug:
                domain_parts = domain.split('.')
                # Remove common prefixes (www, jobs, careers, etc.)
                if len(domain_parts) >= 2:
                    # Get the second-to-last part (e.g., anthropic from www.anthropic.com)
                    # or first part if no subdomain
                    if domain_parts[0] in ['www', 'jobs', 'careers', 'apply']:
                        company_slug = domain_parts[1] if len(domain_parts) > 1 else domain_parts[0]
                    else:
                        company_slug = domain_parts[0]

            jobs = []

            # Method 1: Try Greenhouse API directly (most reliable for modern boards)
            if company_slug:
                api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Trying Greenhouse API: {api_url}")

                try:
                    headers = {
                        'User-Agent': 'RobotRay JobHunter/1.0 (Career Page Monitor)',
                        'Accept': 'application/json'
                    }
                    response = requests.get(api_url, headers=headers, timeout=self.request_timeout)

                    if response.status_code == 200:
                        data = response.json()
                        if 'jobs' in data:
                            for job_data in data['jobs']:
                                location_name = ''
                                if isinstance(job_data.get('location'), dict):
                                    location_name = job_data['location'].get('name', '')
                                elif isinstance(job_data.get('location'), str):
                                    location_name = job_data['location']

                                jobs.append({
                                    'title': job_data.get('title', 'Unknown'),
                                    'url': job_data.get('absolute_url', ''),
                                    'location': location_name,
                                    'description': job_data.get('content', '')
                                })

                            if self.verbose_logging:
                                self.logger.info(f"jobhunter: Greenhouse API returned {len(jobs)} jobs")

                            return jobs
                    else:
                        self.logger.debug(f"jobhunter: Greenhouse API returned {response.status_code}, trying HTML fallback")

                except Exception as e:
                    self.logger.debug(f"jobhunter: Greenhouse API failed: {e}, trying HTML fallback")

            # Method 2: Fetch HTML and look for embedded JSON (older boards)
            html = self._fetch_with_retry(careers_url, politeness_delay=2.0)
            if not html:
                if self.verbose_logging:
                    self.logger.warning(f"jobhunter: Failed to fetch Greenhouse board HTML")
                return []

            soup = BeautifulSoup(html, 'lxml')

            # Look for embedded JSON data in <script> tags
            script_tags = soup.find_all('script', type='application/json')
            for script in script_tags:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and 'jobs' in data:
                        for job_data in data['jobs']:
                            jobs.append({
                                'title': job_data.get('title', 'Unknown'),
                                'url': job_data.get('absolute_url', job_data.get('url', '')),
                                'location': job_data.get('location', {}).get('name', '') if isinstance(job_data.get('location'), dict) else job_data.get('location', ''),
                                'description': job_data.get('content', '')
                            })
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    self.logger.debug(f"jobhunter: Skipping script tag (not job data): {e}")
                    continue

            if jobs:
                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Found {len(jobs)} jobs from embedded JSON")
                return jobs

            # Method 2: Parse structured HTML (fallback)
            if not jobs:
                self.logger.debug(f"jobhunter: No JSON data found, parsing Greenhouse HTML structure")

                # Greenhouse uses <div class="opening"> for job listings
                job_elements = soup.find_all('div', class_='opening')

                for elem in job_elements:
                    # Extract title and URL
                    link = elem.find('a')
                    if not link:
                        continue

                    title = link.get_text(strip=True)
                    url = link.get('href', '')

                    # Make URL absolute if needed
                    if url and not url.startswith('http'):
                        if careers_url.endswith('/jobs'):
                            base_url = careers_url.rsplit('/jobs', 1)[0]
                        else:
                            base_url = careers_url.rstrip('/')
                        url = f"{base_url}/jobs/{url.lstrip('/')}"

                    # Extract location (usually in a <span> or <div> with class containing 'location')
                    location = ''
                    location_elem = elem.find(['span', 'div'], class_=re.compile(r'location', re.IGNORECASE))
                    if location_elem:
                        location = location_elem.get_text(strip=True)

                    jobs.append({
                        'title': title,
                        'url': url,
                        'location': location,
                        'description': ''  # Will be filled by deep scrape
                    })

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Greenhouse parser found {len(jobs)} jobs")
            return jobs

        except requests.exceptions.Timeout:
            self.logger.warning(f"jobhunter: Greenhouse API timed out ({self.request_timeout}s) — skipping")
            return []
        except requests.exceptions.HTTPError as e:
            # Permanent source problem (dead board etc.) — already logged concisely
            # by _fetch_with_retry; no traceback (tracebacks are for real bugs).
            self.logger.warning(f"jobhunter: Greenhouse source unavailable: {e}")
            return []
        except Exception as e:
            self.logger.error(f"jobhunter: Error in Greenhouse parser: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback: {traceback.format_exc()}")
            return []

    def _scrape_ashby(self, careers_url: str) -> List[Dict[str, Any]]:
        """
        Scrape jobs from an Ashby job board.

        Ashby boards embed job data as JSON in window.__appData within a <script> tag.
        Structure: { jobPostings: [...], organization: {...}, ... }

        Args:
            careers_url: URL of Ashby job board (e.g., https://jobs.ashbyhq.com/elevenlabs)

        Returns:
            List of job dictionaries with keys: title, url, location, description, department
        """
        try:
            import json
            import re

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Using Ashby parser for {careers_url}")

            # Fetch the HTML page
            html = self._fetch_with_retry(careers_url, politeness_delay=2.0)
            if not html:
                self.logger.warning(f"jobhunter: Failed to fetch Ashby board HTML")
                return []

            jobs = []

            # Method 1: Extract window.__appData JSON from script tags
            # Find the start of __appData and extract JSON using brace matching
            app_data_start = html.find('window.__appData')
            if app_data_start == -1:
                app_data_start = html.find('__appData')

            app_data = None
            if app_data_start != -1:
                # Find the opening brace
                json_start = html.find('{', app_data_start)
                if json_start != -1:
                    # Use brace counting to find the matching closing brace
                    brace_count = 0
                    json_end = json_start
                    for i, char in enumerate(html[json_start:], start=json_start):
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                json_end = i + 1
                                break

                    json_str = html[json_start:json_end]
                    try:
                        app_data = json.loads(json_str)
                        if self.verbose_logging:
                            self.logger.info(f"jobhunter: Extracted Ashby __appData ({len(json_str)} chars)")
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"jobhunter: Failed to parse Ashby JSON: {e}")

            if app_data:
                # Extract job postings from the data
                # Ashby structure: jobPostings can be at top level OR inside jobBoard
                job_postings = app_data.get('jobPostings', [])
                if not job_postings and 'jobBoard' in app_data:
                    job_postings = app_data['jobBoard'].get('jobPostings', [])

                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Ashby __appData contains {len(job_postings)} job postings")

                for job_data in job_postings:
                    # Skip unlisted jobs
                    if not job_data.get('isListed', True):
                        continue

                    # Build job URL
                    job_id = job_data.get('id', '')
                    job_url = f"{careers_url.rstrip('/')}/{job_id}" if job_id else ''

                    # Extract location info
                    location = job_data.get('locationName', '')
                    workplace_type = job_data.get('workplaceType', '')
                    if workplace_type and workplace_type.lower() == 'remote':
                        location = f"Remote - {location}" if location else "Remote"

                    # Get department/team info
                    department = job_data.get('departmentName', '')
                    team = job_data.get('teamName', '')
                    dept_info = f"{department} / {team}" if department and team else (department or team)

                    jobs.append({
                        'title': job_data.get('title', 'Unknown'),
                        'url': job_url,
                        'location': location,
                        'description': dept_info,  # Store department as description placeholder
                        'department': dept_info,
                        'employment_type': job_data.get('employmentType', ''),
                    })

                if self.verbose_logging:
                    self.logger.info(f"jobhunter: Ashby parser extracted {len(jobs)} listed jobs")

                return jobs

            # Method 2: Fallback - try parsing HTML structure
            if not jobs:
                self.logger.debug(f"jobhunter: No __appData found, trying HTML parsing for Ashby")
                soup = BeautifulSoup(html, 'lxml')

                # Ashby uses various class names for job listings
                # Look for job cards/links
                job_links = soup.find_all('a', href=re.compile(r'/[a-f0-9-]{36}'))  # UUID pattern in URLs

                for link in job_links:
                    title_elem = link.find(['h3', 'h4', 'div', 'span'])
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        url = link.get('href', '')

                        # Make URL absolute
                        if url and not url.startswith('http'):
                            url = f"{careers_url.rstrip('/')}{url}"

                        # Try to find location nearby
                        location = ''
                        parent = link.parent
                        if parent:
                            location_elem = parent.find(string=re.compile(r'Remote|Hybrid|On-?site', re.IGNORECASE))
                            if location_elem:
                                location = location_elem.strip()

                        jobs.append({
                            'title': title,
                            'url': url,
                            'location': location,
                            'description': ''
                        })

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Ashby parser found {len(jobs)} jobs total")

            return jobs

        except requests.exceptions.Timeout:
            self.logger.warning(f"jobhunter: Ashby API timed out ({self.request_timeout}s) — skipping")
            return []
        except Exception as e:
            self.logger.error(f"jobhunter: Error in Ashby parser: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return []

    def _scrape_lever(self, careers_url: str) -> List[Dict[str, Any]]:
        """
        Scrape jobs from a Lever job board using their public API.

        Lever provides a JSON API that returns all job postings with full descriptions.
        API endpoint: https://api.lever.co/v0/postings/{company}?mode=json

        Args:
            careers_url: URL of Lever job board (e.g., https://jobs.lever.co/mistral)

        Returns:
            List of job dictionaries with keys: title, url, location, description, department
        """
        try:
            import json
            import requests
            from urllib.parse import urlparse

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Using Lever parser for {careers_url}")

            # Extract company slug from URL
            # URL format: https://jobs.lever.co/{company}
            parsed = urlparse(careers_url)
            path_parts = parsed.path.strip('/').split('/')
            company_slug = path_parts[0] if path_parts else None

            if not company_slug:
                self.logger.warning(f"jobhunter: Could not extract company slug from Lever URL: {careers_url}")
                return []

            # Call Lever API
            api_url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Fetching Lever API: {api_url}")

            headers = {
                'User-Agent': 'RobotRay JobHunter/1.0 (Career Page Monitor)',
                'Accept': 'application/json'
            }

            response = requests.get(api_url, headers=headers, timeout=self.request_timeout)

            if response.status_code != 200:
                self.logger.warning(f"jobhunter: Lever API returned {response.status_code}")
                return []

            job_data = response.json()

            if not job_data:
                self.logger.info(f"jobhunter: Lever API returned no jobs for {company_slug}")
                return []

            jobs = []
            for job in job_data:
                # Extract location from categories
                categories = job.get('categories', {})
                location = categories.get('location', '')
                all_locations = categories.get('allLocations', [])
                if all_locations and len(all_locations) > 1:
                    location = ', '.join(all_locations)

                # Add workplace type if available
                workplace_type = job.get('workplaceType', '')
                if workplace_type and workplace_type.lower() == 'remote':
                    location = f"Remote - {location}" if location else "Remote"

                # Get department/team
                department = categories.get('team', '')

                # Get description (Lever provides full description in API!)
                description = job.get('descriptionPlain', '') or job.get('description', '')

                # Clean HTML from description if present
                if description and '<' in description:
                    soup = BeautifulSoup(description, 'lxml')
                    description = soup.get_text(separator='\n', strip=True)

                jobs.append({
                    'title': job.get('text', 'Unknown'),
                    'url': job.get('hostedUrl', ''),
                    'location': location,
                    'description': description,  # Full description from API
                    'department': department,
                    'posted_date': job.get('createdAt'),  # Unix timestamp in ms
                })

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Lever API returned {len(jobs)} jobs for {company_slug}")

            return jobs

        except requests.exceptions.Timeout as e:
            self.logger.warning(f"jobhunter: Lever API timed out for {company_slug} ({self.request_timeout}s) — skipping")
            return []
        except Exception as e:
            self.logger.error(f"jobhunter: Error in Lever parser: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return []

    def _scrape_company(self, company_id: int, company_name: str, careers_url: str,
                       scraper_type: str = 'generic', check_robots: bool = False,
                       politeness_delay: float = 2.0) -> List[Dict[str, Any]]:
        """
        Scrape jobs from a company's career page with deduplication.

        Args:
            company_id: Company ID from database
            company_name: Company name (for logging)
            careers_url: URL of career page
            scraper_type: Type of scraper to use ('generic', 'greenhouse', 'ashby', 'lever', 'workday')
            check_robots: Whether to check robots.txt (default: False)
            politeness_delay: Delay in seconds before request (default: 2.0)

        Returns:
            List of newly discovered jobs (deduplicated)
        """
        import hashlib

        try:
            if self.verbose_logging:
                self.logger.info(f"jobhunter: Scraping {company_name} ({careers_url}) [scraper: {scraper_type}]")

            # Check robots.txt first (be a good citizen)
            if check_robots:
                if not self._check_robots_txt(careers_url):
                    self.logger.warning(f"jobhunter: {company_name}: Scraping disallowed by robots.txt. Skipping.")
                    return []

            # Route to appropriate scraper based on type
            if scraper_type.lower() == 'greenhouse':
                jobs = self._scrape_greenhouse(careers_url)
            elif scraper_type.lower() == 'ashby':
                jobs = self._scrape_ashby(careers_url)
            elif scraper_type.lower() == 'lever':
                jobs = self._scrape_lever(careers_url)
            elif scraper_type.lower() == 'workday':
                # TODO: Implement Workday parser (Phase 2 - Future, may need browser automation)
                self.logger.warning(f"jobhunter: Workday parser not implemented yet, falling back to generic")
                html = self._fetch_with_retry(careers_url, politeness_delay=politeness_delay)
                jobs = self._scrape_generic_html(html, careers_url)
            else:
                # Generic HTML parser (default)
                html = self._fetch_with_retry(careers_url, politeness_delay=politeness_delay)
                jobs = self._scrape_generic_html(html, careers_url)

            if not jobs:
                self.logger.warning(f"jobhunter: {company_name}: No jobs found")
                return []

            # Deduplicate using job hash
            new_jobs = []
            for job in jobs:
                # Create hash from title + url
                job_hash_input = f"{job['title']}:{job['url']}"
                job_hash = hashlib.sha256(job_hash_input.encode()).hexdigest()

                # Check if job already exists in database
                if self.db_manager.job_exists_by_hash(company_id, job_hash):
                    self.logger.debug(f"jobhunter: Job already exists (skipping): {job['title']}")
                    continue

                # Add company_id and job_hash to job dict
                job['company_id'] = company_id
                job['job_hash'] = job_hash
                job['is_new'] = True  # Mark as new for freshness detection (Phase 8)

                new_jobs.append(job)

            if self.verbose_logging:
                self.logger.info(f"jobhunter: {company_name}: Found {len(jobs)} jobs, {len(new_jobs)} are new")
            return new_jobs

        except Exception as e:
            self.logger.error(f"jobhunter: Error scraping {company_name}: {e}")
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
        Calculate freshness bonus or age penalty for a job.

        - Brand new (is_new=True): +15 points
        - First week (0-6 days): +10 points
        - After that: -10 points per week old

        Args:
            job: Job dictionary with freshness fields

        Returns:
            Adjustment to score (positive for fresh, negative for stale)
        """
        if job.get('is_new', 0):
            return 15  # Maximum bonus for brand new postings

        from datetime import datetime
        first_seen_str = job.get('first_seen_date')
        if not first_seen_str:
            return 0

        try:
            first_seen = datetime.fromisoformat(first_seen_str.replace('Z', '+00:00'))
            now = datetime.now(first_seen.tzinfo) if first_seen.tzinfo else datetime.now()
            age_days = (now - first_seen).days
            weeks_old = age_days // 7

            if weeks_old == 0:
                return 10  # Bonus for first week
            return -(weeks_old * 10)  # -10 per week after that
        except (ValueError, AttributeError):
            return 0

    # ============================================================================
    # AI Analysis Methods (Phase 11)
    # ============================================================================

    def _get_user_db(self):
        """Lazy-initialize UserDatabaseManager for per-user secret lookups."""
        if self._user_db is None:
            from robotray.auth.user_db import UserDatabaseManager
            self._user_db = UserDatabaseManager()
        return self._user_db

    def _resolve_api_key(self, secret_key: str, env_var: str) -> Optional[str]:
        """Resolve API key from per-user secrets, global SecretsManager, or env var."""
        # Try per-user secret (admin user_id=1) — covers @user set-secret
        try:
            user_db = self._get_user_db()
            value = user_db.get_user_secret(1, secret_key)
            if value:
                self.logger.info(f"jobhunter: Found {secret_key} via per-user secret")
                return value
            else:
                self.logger.debug(f"jobhunter: No per-user secret found for {secret_key}")
        except Exception as e:
            self.logger.debug(f"jobhunter: Per-user secret lookup failed for {secret_key}: {e}")
        # Try global SecretsManager
        value = self.secrets_manager.get_secret(secret_key)
        if value:
            self.logger.debug(f"jobhunter: Found {secret_key} via global SecretsManager")
            return value
        # Try environment variable
        value = os.getenv(env_var)
        if value:
            self.logger.debug(f"jobhunter: Found {secret_key} via env var {env_var}")
        return value

    def _get_client(self, user_id: int = 0):
        """Get LLM client for a user (per-user key or global fallback)."""
        secret_key = "openai_api_key" if self.ai_provider == "openai" else "anthropic_api_key"
        default_client = self._openai_client if self.ai_provider == "openai" else self.anthropic_client

        if user_id and user_id not in self._clients:
            api_key = get_secret_for_user(
                secret_key,
                user_id=user_id,
                user_db=self._get_user_db(),
                secrets_manager=self.secrets_manager,
            )
            if api_key:
                if self.ai_provider == "openai":
                    self._clients[user_id] = OpenAI(api_key=api_key, base_url=self.ai_base_url, timeout=float(self.ai_timeout), max_retries=0)
                else:
                    self._clients[user_id] = Anthropic(api_key=api_key, timeout=float(self.ai_timeout))
            else:
                return default_client
        return self._clients.get(user_id, default_client)

    def _call_llm_with_retry(self, user_id: int = 0, **kwargs) -> Any:
        """Call LLM API with retry on overloaded/rate-limit/timeout errors."""
        client = self._get_client(user_id)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if self.ai_provider == "openai":
                    return self._openai_create(client, **kwargs)
                else:
                    return client.messages.create(**kwargs)
            except BadRequestError as e:
                if "credit balance" in str(e):
                    self.logger.warning(
                        "jobhunter: ⚠️ API credit balance too low. "
                        "Disabling AI analysis for this session."
                    )
                    self.ai_enabled = False
                    raise
                raise
            except NotFoundError as e:
                model = kwargs.get('model', 'unknown')
                self.logger.error(
                    f"jobhunter: Model '{model}' not found (deprecated?). "
                    f"Disabling AI analysis for this session. Update the model in config."
                )
                self.ai_enabled = False
                raise
            except _TIMEOUT_ERRORS as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    self.logger.warning(
                        f"jobhunter: API request timed out, "
                        f"retry {attempt + 1}/{max_retries} in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    self.logger.warning("jobhunter: API request timed out after all retries")
                    raise
            except _API_STATUS_ERRORS as e:
                # Both Anthropic and OpenAI status errors land here (e.g. the
                # proxy's 500 "No response from Claude Code"). Retry transient
                # statuses; otherwise re-raise for the caller to handle.
                status = getattr(e, 'status_code', None)
                if status in _RETRYABLE_STATUS and attempt < max_retries - 1:
                    wait = _retry_wait(status, attempt)
                    self.logger.warning(
                        f"jobhunter: LLM API error {status} ({str(e)[:120]}), "
                        f"retry {attempt + 1}/{max_retries} in {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                raise

    def _openai_create(self, client, **kwargs):
        """Convert Anthropic-style kwargs to OpenAI format and call API."""
        messages = list(kwargs.get("messages", []))
        if "system" in kwargs and kwargs["system"]:
            # Prepend system context into first user message — some proxies
            # drop {"role": "system"} messages and top-level system params.
            system_text = kwargs["system"]
            if messages and messages[0]["role"] == "user":
                messages[0] = {
                    "role": "user",
                    "content": f"<system>\n{system_text}\n</system>\n\n{messages[0]['content']}",
                }
            else:
                messages.insert(0, {"role": "user", "content": f"<system>\n{system_text}\n</system>"})
        return client.chat.completions.create(
            model=kwargs.get("model", self.ai_model),
            messages=messages,
            max_tokens=kwargs.get("max_tokens", self.ai_max_tokens),
            temperature=kwargs.get("temperature", self.ai_temperature),
        )

    def _extract_text(self, response) -> str:
        """Extract text from LLM response."""
        if self.ai_provider == "openai":
            return response.choices[0].message.content
        return response.content[0].text

    def _extract_input_tokens(self, response) -> int:
        """Extract input token count from LLM response."""
        if self.ai_provider == "openai":
            return getattr(response.usage, 'prompt_tokens', 0)
        return response.usage.input_tokens

    def _extract_output_tokens(self, response) -> int:
        """Extract output token count from LLM response."""
        if self.ai_provider == "openai":
            return getattr(response.usage, 'completion_tokens', 0)
        return response.usage.output_tokens

    def _analyze_job_with_cv(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Analyze job using Claude API comparing against criteria and CV (Phase 11).

        This method sends the job description, user criteria, and CV to Claude
        for semantic analysis. Claude evaluates the overall fit and provides
        a detailed explanation.

        Args:
            job: Job dictionary with description, title, location, etc.

        Returns:
            Dictionary with:
                - ai_score: 0-100 score
                - reasoning: Detailed explanation
                - fit_summary: 2-3 sentence summary
                - concerns: List of potential issues
                - recommendation: "APPLY" | "REVIEW" | "SKIP"
            Returns None if AI is disabled or analysis fails.
        """
        llm_client = self._openai_client if self.ai_provider == "openai" else self.anthropic_client
        if not self.ai_enabled or not llm_client:
            self.logger.warning(f"jobhunter: AI analysis skipped - enabled={self.ai_enabled}, client={llm_client is not None}")
            return None

        try:
            # Get active criteria for context
            criteria = self.db_manager.get_active_criteria()
            criteria_summary = self._format_criteria_for_ai(criteria)

            # Build the prompt (use template from config if available)
            if self.ai_prompt_template:
                # Use user-customizable template from config
                prompt = self.ai_prompt_template.format(
                    cv_content=self._cv_content if self._cv_content else "[CV not provided]",
                    goal_content=self._goal_content if self._goal_content else "[No search goal set]",
                    criteria_summary=criteria_summary,
                    job_title=job.get('title', 'Unknown'),
                    job_company=job.get('company_name', 'Unknown'),
                    job_location=job.get('location', 'Not specified'),
                    job_url=job['url'],
                    job_description=job.get('description', 'No description available')
                )
            else:
                # Fallback to default prompt
                goal_section = ""
                if self._goal_content:
                    goal_section = f"""
CANDIDATE'S SEARCH GOAL (in their own words):
{self._goal_content}
"""
                prompt = f"""You are an expert job matching assistant. Analyze this job opportunity.

USER PROFILE (CV):
{self._cv_content if self._cv_content else "[CV not provided]"}
{goal_section}
USER CRITERIA:
{criteria_summary}

JOB POSTING:
Title: {job.get('title', 'Unknown')}
Company: {job.get('company_name', 'Unknown')}
Location: {job.get('location', 'Not specified')}
URL: {job['url']}

Description:
{job.get('description', 'No description available')}

TASK:
Evaluate this job against the user's profile, search goal, and criteria. Be SELECTIVE - only score 90+ for truly excellent matches where the user's experience, skills, and preferences align very well. Pay special attention to the candidate's search goal for role type, compensation, location, and work style preferences.

Provide:
1. Overall fit score (0-100) - Be selective, 90+ means excellent match
2. Detailed reasoning explaining the score (2-3 sentences)
3. Summary of fit (1-2 sentences)
4. Any concerns or red flags (list, can be empty)
5. Recommendation:
   - APPLY (90+): Excellent match, strong alignment
   - REVIEW (70-89): Good match but needs review
   - SKIP (<70): Not a good fit

Respond in JSON format:
{{
    "ai_score": <0-100>,
    "reasoning": "<detailed analysis in 2-3 sentences>",
    "fit_summary": "<1-2 sentences>",
    "concerns": ["<concern1>", "<concern2>"],
    "recommendation": "APPLY|REVIEW|SKIP"
}}"""

            # Call LLM API
            self.logger.debug(f"jobhunter: Calling LLM API for job {job.get('id', 'unknown')}...")
            response = self._call_llm_with_retry(
                model=self.ai_model,
                max_tokens=self.ai_max_tokens,
                temperature=self.ai_temperature,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse response (handle markdown-wrapped JSON)
            self.logger.debug(f"jobhunter: Got response, extracting text...")
            result_text = self._extract_text(response)

            # Log raw response for debugging
            self.logger.debug(f"jobhunter: Raw AI response (first 500 chars): {result_text[:500]}")

            # Strip markdown code blocks if present
            if '```' in result_text:
                # Extract JSON from code block
                import re
                json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', result_text, re.DOTALL)
                if json_match:
                    result_text = json_match.group(1)
                    self.logger.debug(f"jobhunter: Extracted JSON from markdown: {result_text[:300]}")
                else:
                    self.logger.warning(f"jobhunter: Found ``` but couldn't extract JSON, trying full text")

            # Try to parse JSON
            result_text = result_text.strip()
            if not result_text:
                raise ValueError("Empty response from AI")

            # Extract only the JSON object (handle extra text after JSON)
            # Find the first { and last } to extract just the JSON portion
            first_brace = result_text.find('{')
            if first_brace == -1:
                raise ValueError("No JSON object found in response")

            # Find matching closing brace
            brace_count = 0
            last_brace = -1
            for i in range(first_brace, len(result_text)):
                if result_text[i] == '{':
                    brace_count += 1
                elif result_text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        last_brace = i
                        break

            if last_brace == -1:
                raise ValueError("Unmatched braces in JSON response")

            json_only = result_text[first_brace:last_brace + 1]
            result = json.loads(json_only)

            # Calculate cost
            input_tokens = self._extract_input_tokens(response)
            output_tokens = self._extract_output_tokens(response)
            cost = self._calculate_ai_cost(input_tokens, output_tokens)

            # Record usage for cost tracking
            self.db_manager.record_ai_analysis(
                job_id=job['id'],
                tokens_input=input_tokens,
                tokens_output=output_tokens,
                model_used=self.ai_model,
                cost_estimate=cost
            )

            self.logger.debug(
                f"jobhunter: AI analysis complete for job {job['id']}: "
                f"score={result['ai_score']}, recommendation={result['recommendation']}, "
                f"cost=${cost:.4f}"
            )

            return result

        except json.JSONDecodeError as e:
            self.logger.error(f"jobhunter: Failed to parse AI response as JSON: {e}")
            self.logger.error(f"jobhunter: Raw response (first 1000 chars): {result_text[:1000] if 'result_text' in locals() else 'N/A'}")
            return None
        except (KeyError, ValueError) as e:
            self.logger.error(f"jobhunter: AI response error: {type(e).__name__}: {e}")
            self.logger.error(f"jobhunter: Error repr: {repr(e)}")
            if 'result' in locals():
                self.logger.error(f"jobhunter: Response keys: {list(result.keys())}")
                self.logger.error(f"jobhunter: Response content: {result}")
            elif 'result_text' in locals():
                self.logger.error(f"jobhunter: Raw response (first 1000 chars): {result_text[:1000]}")
            else:
                self.logger.error(f"jobhunter: No result_text available - error before API call?")
            import traceback
            self.logger.error(f"jobhunter: Traceback:\n{traceback.format_exc()}")
            return None
        except BadRequestError as e:
            if "credit balance" in str(e):
                self.logger.warning(
                    "jobhunter: ⚠️ Anthropic API credit balance too low — skipping AI analysis. "
                    "Fund your account at https://console.anthropic.com/settings/plans"
                )
            else:
                self.logger.error(f"jobhunter: AI analysis failed: {e}")
            return None
        except _TIMEOUT_ERRORS:
            self.logger.warning(
                f"jobhunter: AI analysis timed out for job {job.get('id', '?')} — "
                "LLM gateway not responding, skipping this job"
            )
            return None
        except _API_STATUS_ERRORS as e:
            # Transient/server-side LLM gateway error (e.g. 500 "No response from
            # Claude Code") that survived retries. Operational, not a bug — log
            # concisely without a traceback and skip this job.
            status = getattr(e, 'status_code', '?')
            self.logger.warning(
                f"jobhunter: LLM gateway error {status} analyzing job "
                f"{job.get('id', '?')} after retries — skipping ({str(e)[:120]})"
            )
            return None
        except Exception as e:
            self.logger.error(f"jobhunter: AI analysis failed: {type(e).__name__}: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback:\n{traceback.format_exc()}")
            return None

    def _format_criteria_for_ai(self, criteria: List[Dict[str, Any]]) -> str:
        """
        Format active criteria into a readable string for AI prompt.

        Args:
            criteria: List of criterion dictionaries

        Returns:
            Formatted string describing user criteria
        """
        if not criteria:
            return "No specific criteria defined"

        lines = []
        for criterion in criteria:
            ctype = criterion['criterion_type']
            value = criterion['criterion_value']
            weight = criterion['weight']
            lines.append(f"- {ctype}: {value} (importance: {weight}/100)")

        return "\n".join(lines)

    def _calculate_ai_cost(self, input_tokens: int, output_tokens: int, model: str = None) -> float:
        """
        Calculate cost of AI API call.

        Pricing (as of 2026):
        - claude-haiku-4-5-20251001: Input $0.25/1M, Output $1.25/1M
        - claude-sonnet-4-20250514: Input $3/1M, Output $15/1M

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            model: Model name (defaults to self.ai_model)

        Returns:
            Cost in USD
        """
        if model is None:
            model = self.ai_model

        # Determine pricing based on model
        if 'haiku' in model.lower():
            input_cost_per_million = 0.25
            output_cost_per_million = 1.25
        elif 'sonnet' in model.lower():
            input_cost_per_million = 3.0
            output_cost_per_million = 15.0
        elif 'opus' in model.lower():
            input_cost_per_million = 15.0
            output_cost_per_million = 75.0
        else:
            # Default to Haiku pricing
            input_cost_per_million = 0.25
            output_cost_per_million = 1.25

        input_cost = (input_tokens / 1_000_000) * input_cost_per_million
        output_cost = (output_tokens / 1_000_000) * output_cost_per_million

        return input_cost + output_cost

    def _generate_hiring_summary(self, approved_jobs: List[Dict[str, Any]]) -> Optional[str]:
        """
        Generate a professional hiring assistant summary using Claude Sonnet.

        Takes all AI-approved jobs and creates a curated, concise summary
        picking the top 1-3 (max 5) positions with detailed reasoning.

        Args:
            approved_jobs: List of job match dicts with AI analysis results

        Returns:
            Formatted message string, or None if generation fails
        """
        llm_client = self._openai_client if self.ai_provider == "openai" else self.anthropic_client
        if not self.ai_enabled or not llm_client or not approved_jobs:
            return None

        try:
            # Get active criteria for context
            criteria = self.db_manager.get_active_criteria()
            criteria_summary = self._format_criteria_for_ai(criteria)

            # Format all approved jobs for the summary prompt
            jobs_data = []
            for match in approved_jobs:
                job = match['job']
                ai_result = match.get('ai_result', {})

                # Calculate notification status
                notified_date = job.get('notified_date')
                first_seen_date = job.get('first_seen_date')
                notification_status = "NEW - Not previously sent"

                if notified_date:
                    from datetime import datetime
                    try:
                        notified_dt = datetime.fromisoformat(notified_date.replace('Z', '+00:00'))
                        now = datetime.now(notified_dt.tzinfo) if notified_dt.tzinfo else datetime.now()
                        days_since_notified = (now - notified_dt).days

                        if days_since_notified == 0:
                            notification_status = "SENT TODAY - Already notified earlier today"
                        elif days_since_notified == 1:
                            notification_status = "SENT YESTERDAY - Reminder"
                        elif days_since_notified <= 3:
                            notification_status = f"SENT {days_since_notified} DAYS AGO - Follow-up"
                        else:
                            notification_status = f"Previously sent {days_since_notified} days ago"
                    except:
                        notification_status = "Previously sent"

                # Calculate job freshness
                freshness_note = ""
                if job.get('is_new', 0):
                    freshness_note = "🔥 BRAND NEW - Just posted today!"
                elif first_seen_date:
                    try:
                        seen_dt = datetime.fromisoformat(first_seen_date.replace('Z', '+00:00'))
                        now = datetime.now(seen_dt.tzinfo) if seen_dt.tzinfo else datetime.now()
                        days_old = (now - seen_dt).days

                        if days_old <= 2:
                            freshness_note = "⚡ FRESH - Posted within last 2 days"
                        elif days_old <= 7:
                            freshness_note = "Recent - Posted within last week"
                        else:
                            freshness_note = f"Posted {days_old} days ago"
                    except:
                        freshness_note = ""

                job_info = {
                    'title': job.get('title', 'Unknown'),
                    'company': job.get('company_name', 'Unknown'),
                    'location': job.get('location', 'Not specified'),
                    'url': job.get('url', '#'),
                    'ai_score': ai_result.get('ai_score', 0),
                    'recommendation': ai_result.get('recommendation', 'REVIEW'),
                    'reasoning': ai_result.get('reasoning', ''),
                    'concerns': ai_result.get('concerns', []),
                    'notification_status': notification_status,
                    'freshness_note': freshness_note,
                    'posted_date': job.get('posted_date', 'Unknown'),
                    'description_preview': job.get('description', '')[:500] if job.get('description') else ''
                }
                jobs_data.append(job_info)

            # Build the prompt for Sonnet
            jobs_text = "\n\n".join([
                f"**Job {i+1}:**\n"
                f"- Title: {j['title']}\n"
                f"- Company: {j['company']}\n"
                f"- Location: {j['location']}\n"
                f"- AI Score: {j['ai_score']}/100\n"
                f"- Recommendation: {j['recommendation']}\n"
                f"- Notification History: {j['notification_status']}\n"
                f"- Freshness: {j['freshness_note']}\n"
                f"- Posted: {j['posted_date']}\n"
                f"- URL: {j['url']}\n"
                f"- Initial Analysis: {j['reasoning']}\n"
                f"- Concerns: {', '.join(j['concerns']) if j['concerns'] else 'None noted'}\n"
                for i, j in enumerate(jobs_data)
            ])

            # Build the prompt (use template from config if available)
            if self.ai_summary_prompt_template:
                # Use user-customizable template from config
                prompt = self.ai_summary_prompt_template.format(
                    cv_content=self._cv_content if self._cv_content else "[CV not provided]",
                    goal_content=self._goal_content if self._goal_content else "[No search goal set]",
                    criteria_summary=criteria_summary,
                    jobs_text=jobs_text,
                    job_count=len(approved_jobs)
                )
            else:
                # Fallback to default prompt
                goal_section = ""
                if self._goal_content:
                    goal_section = f"""
YOUR CLIENT'S SEARCH GOAL (in their own words):
{self._goal_content}
"""
                prompt = f"""You are a professional hiring assistant helping your client find their next career opportunity.

YOUR CLIENT'S PROFILE:
{self._cv_content if self._cv_content else "[CV not provided]"}
{goal_section}
THEIR JOB SEARCH CRITERIA:
{criteria_summary}

CANDIDATE POSITIONS (already pre-screened):
{jobs_text}

YOUR TASK:
Review these {len(approved_jobs)} pre-approved positions and select the TOP 1-3 opportunities (maximum 5 only if they're all truly excellent). Write a professional, concise message to your client as their hiring assistant.

IMPORTANT GUIDELINES:
- Be selective: Pick only the BEST 1-3 positions (up to 5 if all are exceptional)
- Write in a professional but warm tone (you're their trusted advisor)
- For each selected position, explain:
  * Why it's a strong fit against their criteria and experience
  * **NOTIFICATION CONTEXT:** If previously sent, mention it (e.g., "I sent this yesterday - still worth considering because..."). If NEW/FRESH, emphasize time-sensitivity!
  * Any urgency factors (🔥 brand new postings are TIME-SENSITIVE - act fast!)
  * Potential concerns or gaps they should be aware of
  * Include the direct application link
- Be honest about concerns - they want your real assessment
- Format with clear headers and bullet points
- Include position links inline like: [Position Title](URL)
- **CRITICAL:** Use the "Notification History" and "Freshness" fields to provide context about whether this is a reminder or a new opportunity

START YOUR MESSAGE WITH:
"Hi! I've reviewed today's opportunities and here are my top recommendations for you:"

Then provide your curated selections with detailed analysis."""

            # Call LLM for the summary
            self.logger.debug(f"jobhunter: Generating hiring summary with {self.ai_summary_model} for {len(approved_jobs)} jobs...")

            response = self._call_llm_with_retry(
                model=self.ai_summary_model,
                max_tokens=self.ai_summary_max_tokens,
                temperature=0.5,  # Slightly higher for more natural writing
                messages=[{"role": "user", "content": prompt}]
            )

            summary_text = self._extract_text(response)

            # Calculate cost
            input_tokens = self._extract_input_tokens(response)
            output_tokens = self._extract_output_tokens(response)
            cost = self._calculate_ai_cost(input_tokens, output_tokens, model=self.ai_summary_model)

            # Record the summary generation cost
            # Use the first approved job's ID (summary analyzes multiple jobs)
            first_job_id = approved_jobs[0]['job']['id'] if approved_jobs else 0
            self.db_manager.record_ai_analysis(
                job_id=first_job_id,
                tokens_input=input_tokens,
                tokens_output=output_tokens,
                model_used=self.ai_summary_model,
                cost_estimate=cost,
                analysis_type='summary'
            )

            self.logger.info(
                f"jobhunter: Hiring summary generated: "
                f"{output_tokens} tokens, cost=${cost:.4f}"
            )

            return summary_text

        except BadRequestError as e:
            if "credit balance" in str(e):
                self.logger.warning(
                    "jobhunter: ⚠️ Anthropic API credit balance too low — skipping hiring summary. "
                    "Fund your account at https://console.anthropic.com/settings/plans"
                )
            else:
                self.logger.error(f"jobhunter: Failed to generate hiring summary: {e}")
            return None
        except _API_STATUS_ERRORS as e:
            # Transient/server-side LLM gateway error that survived retries.
            # Operational, not a bug — log concisely without a traceback.
            status = getattr(e, 'status_code', '?')
            self.logger.warning(
                f"jobhunter: LLM gateway error {status} generating hiring summary "
                f"after retries — skipping ({str(e)[:120]})"
            )
            return None
        except Exception as e:
            self.logger.error(f"jobhunter: Failed to generate hiring summary: {type(e).__name__}: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback:\n{traceback.format_exc()}")
            return None

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
        self.logger.info("jobhunter: Scheduler loop started")

        while self._running:
            try:
                # Calculate next run time
                next_run = self._calculate_next_run()

                if next_run is None:
                    # No active schedules - sleep and check again later
                    self.logger.debug("jobhunter: No active schedules - waiting 60 seconds")
                    self._shutdown_event.wait(60)
                    continue

                # Calculate time until next run
                now = datetime.now(timezone.utc)
                time_until_run = (next_run - now).total_seconds()

                if time_until_run > 0:
                    self.logger.info(f"jobhunter: Next scheduled scrape: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {time_until_run/60:.1f} minutes)")

                    # Sleep until next run (or until shutdown)
                    if self._shutdown_event.wait(timeout=time_until_run):
                        # Shutdown signal received
                        break
                else:
                    # Time has already passed - run immediately
                    self.logger.info("jobhunter: Scheduled time has passed - running immediately")

                # Check if still running after sleep
                if not self._running:
                    break

                # Run the daily scrape workflow
                self.logger.info("jobhunter: 🔄 Starting scheduled scrape workflow")
                self._run_daily_scrape()

            except Exception as e:
                self.logger.error(f"jobhunter: Error in scheduler loop: {e}")
                import traceback
                self.logger.error(f"jobhunter: Traceback: {traceback.format_exc()}")
                # Sleep 5 minutes on error, then retry
                self._shutdown_event.wait(300)

        self.logger.info("jobhunter: Scheduler loop stopped")

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
                    self.logger.error(f"jobhunter: Error parsing schedule {run_time_str} ({tz_name}): {e}")
                    continue

            if not next_runs:
                return None

            # Return the soonest run time
            return min(next_runs)

        except Exception as e:
            self.logger.error(f"jobhunter: Error calculating next run: {e}")
            return None

    def _run_daily_scrape(self, trigger_type: str = 'scheduled'):
        """
        Orchestrate the complete daily scraping workflow (Phase 6).

        Workflow:
        1. Scrape all active companies (light scrape)
        2. Score all jobs against criteria
        3. Deep scrape high-scoring matches
        4. Send notifications (Phase 9 - future)
        """
        run_id = None
        try:
            start_time = time.time()
            run_id = self.db_manager.create_run(trigger_type)
            self.logger.debug("=" * 60)
            self.logger.debug("jobhunter: 🚀 Starting Daily Scrape Workflow")
            self.logger.debug("=" * 60)

            # Step 1: Get active companies
            companies = self.db_manager.get_active_companies()
            if not companies:
                self.logger.warning("jobhunter: No active companies to scrape")
                return

            if self.verbose_logging:
                self.logger.info(f"jobhunter: 📋 Found {len(companies)} active companies")

            # Step 2: Light scrape all companies
            total_jobs_scraped = 0
            total_new_jobs = 0

            for i, company in enumerate(companies, 1):
                try:
                    if self.verbose_logging:
                        self.logger.info(f"jobhunter: 🔍 Scraping {i}/{len(companies)}: {company['name']}")

                    jobs = self._scrape_company(
                        company_id=company['id'],
                        company_name=company['name'],
                        careers_url=company['careers_url'],
                        scraper_type=company.get('scraper_type', 'generic')
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

                    if self.verbose_logging:
                        self.logger.info(f"jobhunter:    ✅ {len(jobs)} jobs found, {new_count} new")

                    # Rate limiting between companies
                    if i < len(companies):
                        time.sleep(self.config.get('rate_limit_delay', 3))

                except Exception as e:
                    self.logger.error(f"jobhunter:    ❌ Failed to scrape {company['name']}: {e}")
                    continue

            if self.verbose_logging:
                self.logger.info(f"jobhunter: \n📊 Light Scrape Summary: {total_jobs_scraped} total, {total_new_jobs} new jobs")

            # Step 3: Score all recent jobs
            if self.verbose_logging:
                self.logger.info("jobhunter: \n🎯 Scoring jobs against criteria...")
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

            if self.verbose_logging:
                self.logger.info(f"jobhunter:    ✅ {len(matched_jobs)} jobs scored >= {min_score}")

            # Step 3b: Re-validate existing AI-scored jobs against current criteria
            # Jobs with effective AI score > 0 stay active and get re-analyzed.
            # Jobs with effective AI score <= 0 are frozen (kept for reference, skipped).
            # Jobs that fail criteria threshold get their AI scores cleared.
            existing_ai_jobs = self.db_manager.get_ai_approved_jobs(min_score=0, limit=500)
            invalidated_ids = []
            frozen_count = 0
            for ai_job in existing_ai_jobs:
                criteria_score = self._filter_and_score_job(ai_job)
                if criteria_score < self.ai_prescreen_threshold:
                    invalidated_ids.append(ai_job['id'])
                    continue

                # Check effective AI score (ai_score + age penalty)
                ai_score = ai_job.get('ai_score', 0) or 0
                age_penalty = self._calculate_freshness_adjustment(ai_job)
                effective = ai_score + age_penalty
                if effective <= 0:
                    frozen_count += 1
                    continue  # Frozen — keep in DB but don't re-analyze

                # Active — add to pipeline if not already present
                already_in = any(m['id'] == ai_job['id'] for m in matched_jobs)
                if not already_in:
                    matched_jobs.append({
                        'job': ai_job,
                        'score': criteria_score,
                        'id': ai_job['id'],
                        'url': ai_job['url'],
                        'title': ai_job.get('title', 'Unknown')
                    })

            if invalidated_ids:
                self.db_manager.clear_ai_scores(invalidated_ids)
                self.logger.info(
                    f"jobhunter: 🗑️ Cleared {len(invalidated_ids)} stale AI scores "
                    f"(criteria score < {self.ai_prescreen_threshold})"
                )
            if frozen_count:
                self.logger.info(f"jobhunter: 🧊 {frozen_count} jobs frozen (effective AI score <= 0)")

            # Re-sort after adding existing AI jobs
            matched_jobs.sort(key=lambda x: x['score'], reverse=True)

            # Step 4: Deep scrape top matches
            deep_scraped_jobs = []
            if matched_jobs:
                deep_scrape_limit = min(20, len(matched_jobs))  # Limit to top 20
                if self.verbose_logging:
                    self.logger.info(f"jobhunter: \n🔬 Deep scraping top {deep_scrape_limit} matches...")

                success_count = self._deep_scrape_matches(matched_jobs[:deep_scrape_limit])
                if self.verbose_logging:
                    self.logger.info(f"jobhunter:    ✅ Successfully deep scraped {success_count}/{deep_scrape_limit} jobs")

                # Get the deep-scraped jobs with full descriptions
                for match in matched_jobs[:deep_scrape_limit]:
                    job = self.db_manager.get_job_by_id(match['job']['id'])
                    # Check if description was updated by deep scrape (description field)
                    if job and job.get('description') and len(job.get('description', '')) > 100:
                        match['job'] = job  # Update with full description
                        deep_scraped_jobs.append(match)
                    elif self.verbose_logging:
                        self.logger.warning(f"jobhunter: Job {job['id']} has no full description after deep scrape")

            # Step 5: AI Analysis (Phase 11)
            ai_approved_jobs = []
            if self.ai_enabled and deep_scraped_jobs:
                # Pre-filter by criteria score to save AI costs
                ai_candidates = [m for m in deep_scraped_jobs if m['score'] >= self.ai_prescreen_threshold]
                skipped_count = len(deep_scraped_jobs) - len(ai_candidates)

                if skipped_count > 0:
                    self.logger.info(
                        f"jobhunter: 💰 Skipped {skipped_count} jobs with criteria score < {self.ai_prescreen_threshold} "
                        f"(saved ~${skipped_count * 0.0017:.4f} in AI costs)"
                    )

                if ai_candidates:
                    self.logger.debug(f"jobhunter: 🤖 Running AI analysis on {len(ai_candidates)} high-scoring jobs (criteria >= {self.ai_prescreen_threshold})...")
                else:
                    self.logger.debug(f"jobhunter: ℹ️  No jobs met AI prescreen threshold ({self.ai_prescreen_threshold})")

                ai_analysis_count = 0
                for i, match in enumerate(ai_candidates, 1):
                    job = match['job']

                    if self.verbose_logging:
                        self.logger.info(f"jobhunter:    Analyzing {i}/{len(ai_candidates)}: {job.get('title', 'Unknown')}")

                    # Run AI analysis
                    ai_result = self._analyze_job_with_cv(job)

                    if ai_result:
                        ai_analysis_count += 1

                        # Save AI results to database
                        self.db_manager.update_job_match_ai_analysis(
                            job_id=job['id'],
                            ai_score=ai_result['ai_score'],
                            ai_reasoning=ai_result['reasoning'],
                            ai_recommendation=ai_result['recommendation']
                        )

                        # Only approve jobs with score >= threshold
                        if ai_result['ai_score'] >= self.ai_min_score:
                            match['ai_result'] = ai_result
                            ai_approved_jobs.append(match)

                            if self.verbose_logging:
                                self.logger.info(
                                    f"jobhunter:       ✅ APPROVED: Score={ai_result['ai_score']}, "
                                    f"Rec={ai_result['recommendation']}"
                                )
                        else:
                            if self.verbose_logging:
                                self.logger.info(
                                    f"jobhunter:       ❌ FILTERED: Score={ai_result['ai_score']} "
                                    f"(threshold={self.ai_min_score})"
                                )

                self.logger.info(
                    f"jobhunter:    ✅ AI analysis complete: {ai_analysis_count} analyzed, "
                    f"{len(ai_approved_jobs)} approved (score >= {self.ai_min_score})"
                )

            # Step 6: Send notifications (Phase 9)
            # With AI enabled, generate a curated summary (NO individual notifications)
            # Without AI, notify about all matched jobs (original behavior)
            summary = None
            if self.ai_enabled and ai_approved_jobs:
                # Generate professional hiring assistant summary
                summary = self._generate_hiring_summary(ai_approved_jobs)

                if summary:
                    # Private owner delivery (ADR-009 F3) — job matches are
                    # personal; never @broadcast to every session/chat
                    self.notify_user(
                        self.owner_user_id,
                        f"**🎯 JobHunter: Today's Top Opportunities**\n\n{summary}",
                        content_type="text/markdown"
                    )
                    self.logger.debug(f"jobhunter: Sent hiring summary for {len(ai_approved_jobs)} approved jobs")
                else:
                    # Summary generation failed - log error but don't spam with individual notifications
                    self.logger.error("jobhunter: Summary generation failed - no notifications sent")
                    self.notify_user(
                        self.owner_user_id,
                        f"⚠️ **JobHunter**: Found {len(ai_approved_jobs)} approved jobs but failed to generate summary. Check logs for details.",
                        content_type="text/markdown"
                    )
            elif not self.ai_enabled and matched_jobs:
                # AI disabled: send individual notifications for all matches
                self._send_job_notifications(matched_jobs)

            # Summary
            elapsed = time.time() - start_time
            self.logger.info("\n" + "=" * 60)
            self.logger.info("jobhunter: ✨ Daily Scrape Workflow Complete")
            self.logger.info(f"jobhunter:    • Companies scraped: {len(companies)}")
            self.logger.info(f"jobhunter:    • Total jobs found: {total_jobs_scraped}")
            self.logger.info(f"jobhunter:    • New jobs: {total_new_jobs}")
            self.logger.info(f"jobhunter:    • High-scoring matches: {len(matched_jobs)}")
            self.logger.info(f"jobhunter:    • Deep scraped: {len(deep_scraped_jobs)}")

            if self.ai_enabled:
                self.logger.info(f"jobhunter:    • AI approved (>={self.ai_min_score}): {len(ai_approved_jobs)}")
                if ai_approved_jobs:
                    self.logger.info(f"jobhunter:    • Notification: Curated summary sent (1 message)")
                else:
                    self.logger.info(f"jobhunter:    • Notification: None (no approved jobs)")
            else:
                self.logger.info(f"jobhunter:    • Notifications sent: {len(matched_jobs)}")

            self.logger.info(f"jobhunter:    • Time elapsed: {elapsed/60:.1f} minutes")
            self.logger.info("=" * 60)

            # Persist run results
            if run_id:
                ai_analyzed_count = len(ai_approved_jobs) if self.ai_enabled else 0
                self.db_manager.update_run(
                    run_id,
                    status='completed',
                    companies_scraped=len(companies),
                    jobs_found=total_jobs_scraped,
                    new_jobs=total_new_jobs,
                    matches=len(matched_jobs),
                    deep_scraped=len(deep_scraped_jobs),
                    ai_analyzed=ai_analyzed_count,
                    ai_approved=len(ai_approved_jobs) if self.ai_enabled else 0,
                    summary_text=summary if self.ai_enabled and ai_approved_jobs else None,
                    duration_seconds=elapsed
                )

        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in locals() else 0
            self.logger.error(f"jobhunter: Error in daily scrape workflow: {e}")
            import traceback
            self.logger.error(f"jobhunter: Traceback: {traceback.format_exc()}")
            if run_id:
                self.db_manager.update_run(
                    run_id,
                    status='failed',
                    error_message=str(e),
                    duration_seconds=elapsed
                )

    # ============================================================================
    # Freshness Detection (Phase 8)
    # ============================================================================

    # TODO: Implement _calculate_freshness_bonus() in Phase 8

    # ============================================================================
    # Notifications (Phase 9)
    # ============================================================================

    def _send_job_notifications(self, matched_jobs: List[Dict[str, Any]]):
        """
        Send Telegram notifications for matched jobs, prioritizing NEW postings (Phase 9).

        Args:
            matched_jobs: List of job dictionaries with score, job data, and metadata
        """
        try:
            if not matched_jobs:
                self.logger.info("jobhunter: No matched jobs to notify")
                return

            # Get notification settings
            min_score = self.config.get('min_match_score', 60)
            max_notifications = self.config.get('max_notifications_per_run', 10)

            # Filter jobs that meet notification threshold
            notify_jobs = [m for m in matched_jobs if m['score'] >= min_score]

            if not notify_jobs:
                self.logger.info(f"jobhunter: No jobs scored >= {min_score} (notification threshold)")
                return

            # Sort by score descending, then limit to top N
            sorted_jobs = sorted(notify_jobs, key=lambda x: x['score'], reverse=True)
            if len(sorted_jobs) > max_notifications:
                self.logger.info(f"jobhunter: Limiting notifications to top {max_notifications} jobs")
                sorted_jobs = sorted_jobs[:max_notifications]

            new_jobs = [m for m in sorted_jobs if m['job'].get('is_new', 0) == 1]

            self.logger.info(f"jobhunter: Sending notifications for {len(sorted_jobs)} jobs ({len(new_jobs)} new, {len(sorted_jobs) - len(new_jobs)} existing)")

            # Send header message
            if new_jobs:
                header = f"🆕 *JobHunter Alert:* {len(new_jobs)} NEW job(s) found!"
            else:
                header = f"📋 *JobHunter Alert:* {len(sorted_jobs)} job(s) matched your criteria"

            # Private owner delivery (ADR-009 F3), not @broadcast
            self.notify_user(self.owner_user_id, header, content_type="text/markdown")

            # Send individual job notifications
            for i, match in enumerate(sorted_jobs, 1):
                self._send_job_notification(match, index=i, total=len(sorted_jobs))
                time.sleep(0.5)  # Rate limiting between notifications

            # Mark NEW jobs as seen after notification
            for match in new_jobs:
                self.db_manager.mark_job_as_seen(match['job']['id'])

            self.logger.info(f"jobhunter: ✅ Sent {len(sorted_jobs)} job notifications")

        except Exception as e:
            self.logger.error(f"jobhunter: Error sending notifications: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")

    def _send_job_notification(self, match: Dict[str, Any], index: int = 1, total: int = 1):
        """
        Send a single job notification with Telegram markdown formatting.

        Args:
            match: Job match dictionary with 'job', 'score', and metadata
            index: Notification number in batch
            total: Total number of notifications
        """
        try:
            job = match['job']
            score = match['score']

            # Build notification message with Telegram markdown (* for bold, _ for italic)
            message = f"\n*Job {index}/{total}*"

            # Add freshness indicator
            if job.get('is_new', 0) == 1:
                message += " 🆕 *NEW!*"

            message += f"\n*Score:* {score}"
            message += f"\n\n*{job['title']}*"

            # Location
            if job.get('location'):
                message += f"\n📍 {job['location']}"

            # Company
            company_name = job.get('company_name', 'Unknown Company')
            message += f"\n🏢 {company_name}"

            # Salary (if available from deep scrape)
            if job.get('salary_text'):
                message += f"\n💰 {job['salary_text']}"
            elif job.get('salary_min') and job.get('salary_max'):
                message += f"\n💰 ${job['salary_min']:,} - ${job['salary_max']:,}"
            elif job.get('salary_min'):
                message += f"\n💰 ${job['salary_min']:,}+"

            # Freshness information
            if job.get('first_seen_date'):
                from datetime import datetime
                first_seen = datetime.fromisoformat(job['first_seen_date'].replace('Z', '+00:00'))
                now = datetime.now(first_seen.tzinfo) if first_seen.tzinfo else datetime.now()
                age_days = (now - first_seen).days

                if age_days == 0:
                    message += f"\n⏱️ First seen today"
                elif age_days == 1:
                    message += f"\n⏱️ First seen yesterday"
                elif age_days <= 7:
                    message += f"\n⏱️ First seen {age_days} days ago"

            # URL (Telegram auto-linkifies URLs, no markdown needed)
            message += f"\n\n🔗 *Apply:* {job['url']}"

            # Private owner delivery (ADR-009 F3), not @broadcast
            self.notify_user(self.owner_user_id, message, content_type="text/markdown")

            if self.verbose_logging:
                self.logger.info(f"jobhunter: Sent notification for: {job['title']}")

        except Exception as e:
            self.logger.error(f"jobhunter: Error sending job notification: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
