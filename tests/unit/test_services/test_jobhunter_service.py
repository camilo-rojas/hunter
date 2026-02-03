#!/usr/bin/env python3
"""
JobHunter Service Test Suite

Tests the JobHunter service implementation including:
- Database schema initialization
- Service lifecycle (start/stop)
- Default criteria and schedule loading
- Health monitoring
- Database CRUD operations
"""

import pytest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import Mock

from robotray.services.jobhunter_service import JobHunterService
from robotray.services.jobhunter_db import JobHunterDatabaseManager


class TestJobHunterDatabase:
    """Test JobHunter database manager."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def db_manager(self, temp_db):
        """Create database manager with temporary database."""
        manager = JobHunterDatabaseManager(temp_db)
        manager.initialize_database()
        return manager

    def test_database_initialization(self, db_manager, temp_db):
        """Test database schema creation."""
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        # Verify all tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        expected_tables = {
            'companies',
            'company_credentials',
            'job_postings',
            'job_matches',
            'scrape_history',
            'search_criteria',
            'run_schedules'
        }

        assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"
        conn.close()

    def test_add_company(self, db_manager):
        """Test adding a company."""
        company_id = db_manager.add_company(
            name="Test Company",
            careers_url="https://example.com/careers",
            scraper_type="generic",
            notes="Test company"
        )

        assert company_id > 0

        # Verify company was added
        companies = db_manager.get_active_companies()
        assert len(companies) == 1
        assert companies[0]['name'] == "Test Company"
        assert companies[0]['careers_url'] == "https://example.com/careers"

    def test_duplicate_company_raises_error(self, db_manager):
        """Test that adding duplicate company raises error."""
        db_manager.add_company(
            name="Test Company",
            careers_url="https://example.com/careers"
        )

        with pytest.raises(ValueError, match="already exists"):
            db_manager.add_company(
                name="Test Company",
                careers_url="https://example.com/different-url"
            )

    def test_add_criterion(self, db_manager):
        """Test adding search criteria."""
        criterion_id = db_manager.add_criterion(
            criterion_type="keyword",
            criterion_value="python",
            weight=10
        )

        assert criterion_id > 0

        # Verify criterion was added
        criteria = db_manager.get_active_criteria()
        assert len(criteria) == 1
        assert criteria[0]['criterion_type'] == "keyword"
        assert criteria[0]['criterion_value'] == "python"
        assert criteria[0]['weight'] == 10

    def test_duplicate_criterion_raises_error(self, db_manager):
        """Test that adding duplicate criterion raises error."""
        db_manager.add_criterion(
            criterion_type="keyword",
            criterion_value="python",
            weight=10
        )

        with pytest.raises(ValueError, match="already exists"):
            db_manager.add_criterion(
                criterion_type="keyword",
                criterion_value="python",
                weight=15
            )

    def test_add_run_schedule(self, db_manager):
        """Test adding run schedule."""
        schedule_id = db_manager.add_run_time(
            run_time="09:00",
            tz="America/New_York"
        )

        assert schedule_id > 0

        # Verify schedule was added
        schedules = db_manager.get_active_run_times()
        assert len(schedules) == 1
        assert schedules[0]['run_time'] == "09:00"
        assert schedules[0]['timezone'] == "America/New_York"

    def test_initialize_default_criteria(self, db_manager):
        """Test loading default criteria."""
        default_criteria = [
            {"type": "keyword", "value": "python", "weight": 10},
            {"type": "location", "value": "Remote", "weight": 20},
            {"type": "min_salary", "value": "120000", "weight": 20},
        ]

        db_manager.initialize_default_criteria(default_criteria)

        criteria = db_manager.get_active_criteria()
        assert len(criteria) == 3

        # Verify criteria types
        types = {c['criterion_type'] for c in criteria}
        assert types == {'keyword', 'location', 'min_salary'}

    def test_initialize_default_criteria_skips_if_exists(self, db_manager):
        """Test that default criteria are not loaded if table is not empty."""
        # Add one criterion manually
        db_manager.add_criterion("keyword", "java", 10)

        # Try to initialize defaults
        default_criteria = [
            {"type": "keyword", "value": "python", "weight": 10},
        ]
        db_manager.initialize_default_criteria(default_criteria)

        # Should still only have 1 criterion (the manual one)
        criteria = db_manager.get_active_criteria()
        assert len(criteria) == 1
        assert criteria[0]['criterion_value'] == "java"

    def test_initialize_default_schedules(self, db_manager):
        """Test loading default schedules."""
        default_schedules = [
            {"time": "09:00", "timezone": "America/New_York"},
            {"time": "13:00", "timezone": "America/New_York"},
            {"time": "17:00", "timezone": "America/New_York"},
        ]

        db_manager.initialize_default_schedules(default_schedules)

        schedules = db_manager.get_active_run_times()
        assert len(schedules) == 3

        # Verify times
        times = {s['run_time'] for s in schedules}
        assert times == {'09:00', '13:00', '17:00'}

    def test_remove_company(self, db_manager):
        """Test soft-deleting a company."""
        company_id = db_manager.add_company("Test Company", "https://example.com/careers")

        # Remove company
        db_manager.remove_company(company_id)

        # Should not appear in active companies
        active_companies = db_manager.get_active_companies()
        assert len(active_companies) == 0

        # But should still exist in database (soft delete)
        company = db_manager.get_company_by_id(company_id)
        assert company is not None
        assert company['active'] == 0


class TestJobHunterServiceLifecycle:
    """Test JobHunter service lifecycle."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def service_config(self, temp_db):
        """Service configuration for testing."""
        return {
            'database_path': temp_db,
            'default_criteria': [
                {"type": "keyword", "value": "python", "weight": 10},
                {"type": "location", "value": "Remote", "weight": 20},
            ],
            'default_schedules': [
                {"time": "09:00", "timezone": "America/New_York"},
            ]
        }

    @pytest.fixture
    def service(self, service_config):
        """Create JobHunter service instance."""
        return JobHunterService("jobhunter-service", service_config)

    def test_service_initialization(self, service):
        """Test service initialization."""
        assert service.name == "jobhunter-service"
        assert service.db_manager is None  # Not initialized until start_service

    def test_service_start(self, service):
        """Test service start."""
        service.start_service()

        assert service.db_manager is not None

        # Verify defaults loaded
        criteria = service.db_manager.get_active_criteria()
        assert len(criteria) == 2

        schedules = service.db_manager.get_active_run_times()
        assert len(schedules) == 1

        service.stop_service()

    def test_service_health_check(self, service):
        """Test service health check."""
        service.start_service()

        health = service.health_check()

        assert health['status'] == 'healthy'
        assert 'message' in health
        assert 'details' in health
        assert health['details']['active_criteria'] == 2
        assert health['details']['active_schedules'] == 1
        assert health['details']['active_companies'] == 0

        service.stop_service()

    def test_service_health_check_before_start(self, service):
        """Test health check before service is started."""
        health = service.health_check()

        assert health['status'] == 'unhealthy'
        assert 'not initialized' in health['message'].lower()

    def test_service_stop(self, service):
        """Test service stop."""
        service.start_service()
        service.stop_service()

        # Verify shutdown event is set
        assert service._shutdown_event.is_set()
        assert service._running is False


class TestJobHunterMessageHandling:
    """Test JobHunter message handling and command routing (Phase 2)."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def service(self, temp_db):
        """Create and start JobHunter service."""
        config = {
            'database_path': temp_db,
            'default_criteria': [],
            'default_schedules': []
        }
        svc = JobHunterService("jobhunter-service", config)
        svc.start_service()
        yield svc
        svc.stop_service()

    def test_help_command(self, service):
        """Test help command."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        from unittest.mock import Mock
        import time

        # Create help command message
        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service",
            priority=MessagePriority.NORMAL,
            correlation_id="test-123"
        )
        message = SecureMessage(
            payload={"content": "help"},
            metadata=metadata
        )

        # Put message in inbound queue
        service.message_queues.inbound_queue.put_nowait(message)

        # Wait for processing
        time.sleep(0.5)

        # Check outbound queue for response
        assert not service.message_queues.outbound_queue.empty()
        response = service.message_queues.outbound_queue.get_nowait()

        assert response.metadata.target_service == "test-service"
        assert "JobHunter Service" in response.payload["content"]
        assert "add-company" in response.payload["content"]
        assert "AVAILABLE COMMANDS" in response.payload["content"]
        assert "QUICK START GUIDE" in response.payload["content"]

    def test_add_company_command(self, service):
        """Test add-company command."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        # Create add-company command (no quotes needed - use underscores for multi-word names)
        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": 'add-company TestCorp https://example.com/careers'},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        # Check response
        assert not service.message_queues.outbound_queue.empty()
        response = service.message_queues.outbound_queue.get_nowait()

        assert "✅" in response.payload["content"]
        assert "TestCorp" in response.payload["content"]

        # Verify company was added to database
        companies = service.db_manager.get_active_companies()
        assert len(companies) == 1
        assert companies[0]['name'] == "TestCorp"
        assert companies[0]['careers_url'] == "https://example.com/careers"

    def test_list_companies_empty(self, service):
        """Test list-companies with no companies."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": "list-companies"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        assert "No companies configured" in response.payload["content"]

    def test_list_companies_with_data(self, service):
        """Test list-companies with companies."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        # Add some companies
        service.db_manager.add_company("Company A", "https://a.com/careers")
        service.db_manager.add_company("Company B", "https://b.com/careers")

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": "list-companies"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        content = response.payload["content"]

        assert "Active Companies (2)" in content
        assert "Company A" in content
        assert "Company B" in content

    def test_remove_company_command(self, service):
        """Test remove-company command."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        # Add a company first
        company_id = service.db_manager.add_company("Test Corp", "https://example.com/careers")

        # Remove it
        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": f"remove-company {company_id}"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        assert "✅ Removed company" in response.payload["content"]

        # Verify company is removed
        companies = service.db_manager.get_active_companies()
        assert len(companies) == 0

    def test_status_command(self, service):
        """Test status command."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": "status"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        content = response.payload["content"]

        assert "JobHunter Service Status" in content
        assert "Active companies:" in content
        assert "Active criteria:" in content

    def test_unknown_command(self, service):
        """Test unknown command handling."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": "invalid-command"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        assert "❌ Error" in response.payload["content"]
        assert "Unknown command" in response.payload["content"]

    def test_add_company_validation(self, service):
        """Test add-company input validation."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        # Missing arguments
        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": "add-company"},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        assert "❌ Error" in response.payload["content"]
        assert "Usage:" in response.payload["content"]


class TestJobHunterCriteriaManagement:
    """Test JobHunter criteria management (Phase 3 - Unified Model)."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def service(self, temp_db):
        """Create and start JobHunter service."""
        config = {
            'database_path': temp_db,
            'default_criteria': [],
            'default_schedules': []
        }
        svc = JobHunterService("jobhunter-service", config)
        svc.start_service()
        yield svc
        svc.stop_service()

    def send_command(self, service, command):
        """Helper to send command and get response."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": command},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        return response.payload["content"]

    def test_add_simple_criteria(self, service):
        """Test adding simple criteria."""
        response = self.send_command(service, "add-criteria remote simple 15")

        assert "✅ Added simple criterion" in response
        assert "remote" in response
        assert "15" in response

        # Verify in database
        criteria = service.db_manager.get_active_criteria()
        assert len(criteria) == 1
        assert criteria[0]['criterion_value'] == "remote"
        assert criteria[0]['weight'] == 15

    def test_add_group_any_criteria(self, service):
        """Test adding group-any criterion."""
        response = self.send_command(service, "add-criteria geography group-any 15")

        assert "✅ Added group-any criterion" in response
        assert "geography" in response

        # Verify in database
        groups = service.db_manager.get_active_groups()
        assert len(groups) == 1
        assert groups[0]['name'] == "geography"
        assert groups[0]['match_type'] == "any"
        assert groups[0]['weight'] == 15

    def test_add_group_tiered_criteria(self, service):
        """Test adding group-tiered criterion."""
        response = self.send_command(service, "add-criteria department group-tiered 0")

        assert "✅ Added group-tiered criterion" in response
        assert "department" in response
        assert "tiered" in response.lower()

        # Verify in database
        groups = service.db_manager.get_active_groups()
        assert len(groups) == 1
        assert groups[0]['name'] == "department"
        assert groups[0]['match_type'] == "tiered"

    def test_add_criteria_value_to_group_any(self, service):
        """Test adding values to group-any criterion."""
        # Create group
        self.send_command(service, "add-criteria geography group-any 15")

        # Add values
        response1 = self.send_command(service, 'add-criteria-value geography "US West"')
        response2 = self.send_command(service, 'add-criteria-value geography "US East"')

        assert "✅ Added value" in response1
        assert "US West" in response1
        assert "✅ Added value" in response2

        # Verify in database
        groups = service.db_manager.get_active_groups()
        assert len(groups[0]['values']) == 2
        values = {v['value'] for v in groups[0]['values']}
        assert values == {"US West", "US East"}

    def test_add_criteria_value_to_group_tiered(self, service):
        """Test adding tiered values with weights."""
        # Create group
        self.send_command(service, "add-criteria department group-tiered 0")

        # Add tiered values
        response1 = self.send_command(service, 'add-criteria-value department "Sales Engineering" preferred 20')
        response2 = self.send_command(service, 'add-criteria-value department Marketing acceptable 10')
        response3 = self.send_command(service, 'add-criteria-value department "IT Support" undesirable -10')

        assert "✅" in response1
        assert "preferred" in response1
        assert "20" in response1

        # Verify in database
        groups = service.db_manager.get_active_groups()
        values = groups[0]['values']
        assert len(values) == 3

        # Check tiers
        tiers = {v['tier_level'] for v in values}
        assert tiers == {"preferred", "acceptable", "undesirable"}

        # Check weights
        pref = next(v for v in values if v['tier_level'] == 'preferred')
        assert pref['tier_weight'] == 20
        assert pref['value'] == "Sales Engineering"

    def test_add_criteria_value_tiered_missing_weight(self, service):
        """Test that tiered values require weight."""
        self.send_command(service, "add-criteria department group-tiered 0")

        response = self.send_command(service, 'add-criteria-value department Marketing preferred')

        assert "❌" in response or "⚠️" in response
        assert "weight" in response.lower()

    def test_list_criteria_unified(self, service):
        """Test list-criteria shows both simple and groups."""
        # Add simple criteria
        self.send_command(service, "add-criteria remote simple 15")
        self.send_command(service, "add-criteria python simple 20")

        # Add group
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')

        response = self.send_command(service, "list-criteria")

        # Should show both types
        assert "Simple Criteria" in response
        assert "Group Criteria" in response
        assert "remote" in response
        assert "python" in response
        assert "geography" in response
        assert "US West" in response

    def test_remove_simple_criteria(self, service):
        """Test removing simple criterion by ID."""
        self.send_command(service, "add-criteria python simple 20")

        # Get the ID
        criteria = service.db_manager.get_active_criteria()
        criterion_id = criteria[0]['id']

        response = self.send_command(service, f"remove-criteria {criterion_id}")

        assert "✅ Removed simple criterion" in response
        assert "python" in response

        # Verify removed from database
        criteria = service.db_manager.get_active_criteria()
        assert len(criteria) == 0

    def test_remove_group_criteria(self, service):
        """Test removing entire group by ID."""
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')

        # Get the ID
        groups = service.db_manager.get_active_groups()
        group_id = groups[0]['id']

        response = self.send_command(service, f"remove-criteria {group_id}")

        assert "✅ Removed group criterion" in response
        assert "geography" in response

        # Verify removed from database
        groups = service.db_manager.get_active_groups()
        assert len(groups) == 0

    def test_remove_criteria_value(self, service):
        """Test removing value from group."""
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')
        self.send_command(service, 'add-criteria-value geography "US East"')

        response = self.send_command(service, 'remove-criteria-value geography "US West"')

        assert "✅ Removed value" in response
        assert "US West" in response

        # Verify removed from database
        groups = service.db_manager.get_active_groups()
        assert len(groups[0]['values']) == 1
        assert groups[0]['values'][0]['value'] == "US East"

    def test_test_criteria_simple(self, service):
        """Test criteria matching with simple criteria."""
        self.send_command(service, "add-criteria remote simple 15")
        self.send_command(service, "add-criteria python simple 20")

        response = self.send_command(service, "test-criteria Senior Python Engineer - Remote - $150k")

        assert "Test Criteria Results" in response
        assert "remote" in response.lower()
        assert "python" in response.lower()
        assert "35" in response or "total" in response.lower()

    def test_test_criteria_group_any(self, service):
        """Test criteria matching with group-any."""
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')
        self.send_command(service, 'add-criteria-value geography "US East"')

        response = self.send_command(service, "test-criteria Remote position in US West")

        assert "geography" in response.lower() or "US West" in response
        assert "15" in response  # Should score once for any match

    def test_test_criteria_group_tiered(self, service):
        """Test criteria matching with tiered groups."""
        self.send_command(service, "add-criteria department group-tiered 0")
        self.send_command(service, 'add-criteria-value department "Sales Engineering" preferred 20')
        self.send_command(service, 'add-criteria-value department Marketing acceptable 10')

        response = self.send_command(service, "test-criteria Sales Engineering Manager")

        assert "Sales Engineering" in response
        assert "preferred" in response.lower()
        assert "20" in response

    def test_case_insensitive_matching(self, service):
        """Test that matching is case-insensitive."""
        self.send_command(service, "add-criteria remote simple 15")

        # Test with different cases
        response = self.send_command(service, "test-criteria REMOTE POSITION")

        assert "remote" in response.lower()
        assert "15" in response

    def test_multi_word_value_support(self, service):
        """Test multi-word values with quotes."""
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')

        # Should match "US West" as single value
        response = self.send_command(service, "test-criteria Position in US West region")

        assert "US West" in response
        assert "15" in response

    def test_invalid_criterion_type(self, service):
        """Test invalid criterion type."""
        response = self.send_command(service, "add-criteria test invalid-type 10")

        assert "❌" in response
        assert "invalid" in response.lower() or "type" in response.lower()

    def test_list_criteria_empty(self, service):
        """Test list-criteria with no criteria."""
        response = self.send_command(service, "list-criteria")

        assert "No criteria configured" in response
        assert "add-criteria" in response

    def test_remove_criteria_invalid_id(self, service):
        """Test remove-criteria with non-numeric ID."""
        response = self.send_command(service, "remove-criteria notanumber")

        assert "❌" in response
        assert "Invalid ID" in response or "number" in response.lower()

    def test_remove_criteria_nonexistent_id(self, service):
        """Test remove-criteria with ID that doesn't exist."""
        response = self.send_command(service, "remove-criteria 9999")

        assert "❌" in response
        assert "not found" in response.lower()

    def test_remove_group_cascades_to_values(self, service):
        """Test that removing a group also removes all its values (CASCADE)."""
        # Create group with multiple values
        self.send_command(service, "add-criteria geography group-any 15")
        self.send_command(service, 'add-criteria-value geography "US West"')
        self.send_command(service, 'add-criteria-value geography "US East"')
        self.send_command(service, 'add-criteria-value geography "Latin America"')

        # Verify values exist
        groups = service.db_manager.get_active_groups()
        assert len(groups) == 1
        assert len(groups[0]['values']) == 3

        # Get the group ID
        group_id = groups[0]['id']

        # Remove the group
        response = self.send_command(service, f"remove-criteria {group_id}")
        assert "✅ Removed group criterion" in response

        # Verify group is gone
        groups = service.db_manager.get_active_groups()
        assert len(groups) == 0

        # Verify values are also gone (CASCADE delete)
        # Check directly in database
        import sqlite3
        conn = sqlite3.connect(service.config['database_path'])
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM criterion_group_values WHERE group_id = ?", (group_id,))
        value_count = cursor.fetchone()[0]
        conn.close()

        assert value_count == 0, "Values should be cascade deleted when group is removed"


class TestJobHunterRangeSupport:
    """Test JobHunter range support for numeric criteria (Phase 3.5)."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def service(self, temp_db):
        """Create and start JobHunter service."""
        config = {
            'database_path': temp_db,
            'default_criteria': [],
            'default_schedules': []
        }
        svc = JobHunterService("jobhunter-service", config)
        svc.start_service()
        yield svc
        svc.stop_service()

    def send_command(self, service, command):
        """Helper to send command and get response."""
        from robotray.core.messaging import SecureMessage, MessagePriority, MessageMetadata
        import time

        metadata = MessageMetadata(
            sender_service="test-service",
            target_service="jobhunter-service"
        )
        message = SecureMessage(
            payload={"content": command},
            metadata=metadata
        )

        service.message_queues.inbound_queue.put_nowait(message)
        time.sleep(0.5)

        response = service.message_queues.outbound_queue.get_nowait()
        return response.payload["content"]

    def test_salary_range_greater_than(self, service):
        """Test salary range with 200k+ (greater than or equal)."""
        # Create tiered salary group
        self.send_command(service, "add-criteria salary group-tiered 0")
        self.send_command(service, 'add-criteria-value salary "200k+" preferred 20')

        # Test with salary above threshold
        response = self.send_command(service, "test-criteria Senior Engineer - $250k base salary")
        assert "salary" in response.lower()
        assert "200k+" in response
        assert "numeric match" in response.lower()
        assert "20" in response  # Should score +20

        # Test with salary below threshold (should not match)
        response = self.send_command(service, "test-criteria Junior Engineer - $150k base")
        assert "no matches" in response.lower() or "0" in response

    def test_salary_range_less_than(self, service):
        """Test salary range with 100k- (less than or equal)."""
        self.send_command(service, "add-criteria salary group-tiered 0")
        self.send_command(service, 'add-criteria-value salary "100k-" undesirable -10')

        # Test with salary below threshold (should match)
        response = self.send_command(service, "test-criteria Entry Level - $80k salary")
        assert "salary" in response.lower()
        assert "100k-" in response
        assert "-10" in response

        # Test with salary above threshold (should not match)
        response = self.send_command(service, "test-criteria Senior Role - $150k")
        assert "no matches" in response.lower() or "undesirable" not in response.lower()

    def test_salary_range_between(self, service):
        """Test salary range with 100k-150k (between)."""
        self.send_command(service, "add-criteria salary group-tiered 0")
        self.send_command(service, 'add-criteria-value salary "100k-150k" acceptable 10')

        # Test with salary in range
        response = self.send_command(service, "test-criteria Mid-level Role - $120k base")
        assert "salary" in response.lower()
        assert "100k-150k" in response
        assert "10" in response

        # Test with salary below range
        response = self.send_command(service, "test-criteria Junior - $80k")
        assert "no matches" in response.lower() or "acceptable" not in response.lower()

        # Test with salary above range
        response = self.send_command(service, "test-criteria Senior - $180k")
        assert "no matches" in response.lower() or "acceptable" not in response.lower()

    def test_multiple_salary_ranges_tiered(self, service):
        """Test tiered salary ranges (stops at first match)."""
        self.send_command(service, "add-criteria salary group-tiered 0")
        self.send_command(service, 'add-criteria-value salary "200k+" preferred 20')
        self.send_command(service, 'add-criteria-value salary "150k-200k" acceptable 10')
        self.send_command(service, 'add-criteria-value salary "100k-150k" acceptable 5')
        self.send_command(service, 'add-criteria-value salary "100k-" undesirable -10')

        # Test preferred tier ($250k)
        response = self.send_command(service, "test-criteria Senior Staff - $250k total comp")
        assert "200k+" in response
        assert "preferred" in response.lower()
        assert "+20" in response

        # Test acceptable tier high ($175k)
        response = self.send_command(service, "test-criteria Senior - $175k")
        assert "150k-200k" in response
        assert "acceptable" in response.lower()
        assert "+10" in response

        # Test acceptable tier low ($125k)
        response = self.send_command(service, "test-criteria Mid - $125k")
        assert "100k-150k" in response
        assert "+5" in response

        # Test undesirable tier ($80k)
        response = self.send_command(service, "test-criteria Junior - $80k")
        assert "100k-" in response
        assert "undesirable" in response.lower()
        assert "-10" in response

    def test_years_experience_range(self, service):
        """Test range support for years of experience."""
        self.send_command(service, "add-criteria experience group-tiered 0")
        self.send_command(service, 'add-criteria-value experience "5+" preferred 15')
        self.send_command(service, 'add-criteria-value experience "3-5" acceptable 10')

        # Test 5+ years
        response = self.send_command(service, "test-criteria Senior Role - 7+ years experience required")
        assert "experience" in response.lower()
        assert "5+" in response
        assert "+15" in response

        # Test 3-5 years
        response = self.send_command(service, "test-criteria Mid-level - 4 years experience")
        assert "3-5" in response
        assert "+10" in response

    def test_range_any_match_type(self, service):
        """Test range support works with 'any' match type (OR logic)."""
        self.send_command(service, "add-criteria salary group-any 15")
        self.send_command(service, 'add-criteria-value salary "150k+"')
        self.send_command(service, 'add-criteria-value salary "100k-150k"')

        # Test matching first range
        response = self.send_command(service, "test-criteria Role with $180k salary")
        assert "salary" in response.lower()
        assert "150k+" in response
        assert "15" in response  # Should score once

    def test_mixed_string_and_range_values(self, service):
        """Test that string values still work alongside range values."""
        self.send_command(service, "add-criteria compensation group-any 10")
        self.send_command(service, 'add-criteria-value compensation "equity"')
        self.send_command(service, 'add-criteria-value compensation "200k+"')

        # Test string matching
        response1 = self.send_command(service, "test-criteria Role with equity compensation")
        assert "compensation" in response1.lower()
        assert "equity" in response1

        # Test range matching
        response2 = self.send_command(service, "test-criteria Role with $250k salary")
        assert "compensation" in response2.lower()
        assert "200k+" in response2

    def test_various_salary_formats(self, service):
        """Test that various salary formats are recognized."""
        self.send_command(service, "add-criteria salary group-tiered 0")
        self.send_command(service, 'add-criteria-value salary "150k+" preferred 20')

        # Test $150k format
        response1 = self.send_command(service, "test-criteria Role - $180k")
        assert "salary" in response1.lower()

        # Test $150,000 format
        response2 = self.send_command(service, "test-criteria Role - $180,000 base")
        assert "salary" in response2.lower()

        # Test 150K format
        response3 = self.send_command(service, "test-criteria Role - 180K salary")
        assert "salary" in response3.lower()

    def test_fallback_to_string_matching(self, service):
        """Test that non-range values still use string matching."""
        self.send_command(service, "add-criteria location group-any 10")
        self.send_command(service, 'add-criteria-value location "Remote"')
        self.send_command(service, 'add-criteria-value location "US West"')

        response = self.send_command(service, "test-criteria Remote position in US West")
        assert "location" in response.lower()
        # Should match Remote (not numeric)
        assert "Remote" in response or "US West" in response


class TestPhase5_5DeepScraping:
    """Test Phase 5.5: Deep Scraping functionality."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def db_manager(self, temp_db):
        """Create database manager with temporary database."""
        from robotray.services.jobhunter_db import JobHunterDatabaseManager
        manager = JobHunterDatabaseManager(temp_db)
        manager.initialize_database()
        return manager

    @pytest.fixture
    def service(self, temp_db):
        """Create JobHunter service with temporary database."""
        from robotray.services.jobhunter_service import JobHunterService
        service = JobHunterService(
            name="jobhunter-service",
            config={"database_path": temp_db}
        )
        service.message_broker = Mock()
        service.start_service()
        return service

    def send_command(self, service, command):
        """Helper to send command and capture response."""
        from robotray.core.messaging import SecureMessage, MessageMetadata, MessagePriority

        responses = []

        def capture_response(message):
            responses.append(message.payload.get("content", ""))

        service.message_broker.send_message = capture_response

        metadata = MessageMetadata(
            sender_service="test-service",
            priority=MessagePriority.NORMAL
        )
        message = SecureMessage(
            service_name="jobhunter-service",
            payload={"content": command},
            metadata=metadata
        )

        service._handle_message_sync(message)
        return responses[0] if responses else ""

    # ============================================================================
    # Salary Parsing Tests
    # ============================================================================

    def test_parse_salary_various_formats(self, service):
        """Test _parse_salary with various formats."""
        # Test $150k format
        assert service._parse_salary("$150k") == 150000.0
        assert service._parse_salary("$150K") == 150000.0

        # Test $150,000 format
        assert service._parse_salary("$150,000") == 150000.0
        assert service._parse_salary("$200,000") == 200000.0

        # Test without dollar sign
        assert service._parse_salary("150k") == 150000.0
        assert service._parse_salary("150K") == 150000.0

        # Test plain numbers
        assert service._parse_salary("150000") == 150000.0
        assert service._parse_salary("200000") == 200000.0

    def test_parse_salary_edge_cases(self, service):
        """Test _parse_salary edge cases."""
        # Empty string
        assert service._parse_salary("") is None

        # Invalid format
        assert service._parse_salary("invalid") is None

        # With spaces
        assert service._parse_salary(" $150k ") == 150000.0
        assert service._parse_salary(" 150,000 ") == 150000.0

    # ============================================================================
    # Database Update Tests
    # ============================================================================

    def test_update_job_details(self, db_manager):
        """Test updating job with full details."""
        # Add company and job
        company_id = db_manager.add_company("Test Company", "https://test.com")
        job_id = db_manager.add_job_posting(
            company_id=company_id,
            job_hash="test_hash_1",
            title="Test Job",
            url="https://test.com/job/1",
            location="Remote"
        )

        # Update with full details
        success = db_manager.update_job_details(
            job_id=job_id,
            description="Full job description here...",
            salary_text="$150k-200k",
            salary_min=150000.0,
            salary_max=200000.0
        )

        assert success is True

        # Verify update
        job = db_manager.get_job_by_id(job_id)
        assert job['description'] == "Full job description here..."
        assert job['salary_text'] == "$150k-200k"
        assert job['salary_min'] == 150000.0
        assert job['salary_max'] == 200000.0

    def test_update_job_details_partial(self, db_manager):
        """Test partial update (only description, no salary)."""
        # Add company and job
        company_id = db_manager.add_company("Test Company", "https://test.com")
        job_id = db_manager.add_job_posting(
            company_id=company_id,
            job_hash="test_hash_2",
            title="Test Job 2",
            url="https://test.com/job/2"
        )

        # Update only description
        success = db_manager.update_job_details(
            job_id=job_id,
            description="Partial update - description only"
        )

        assert success is True

        # Verify update
        job = db_manager.get_job_by_id(job_id)
        assert job['description'] == "Partial update - description only"
        assert job['salary_text'] is None
        assert job['salary_min'] is None
        assert job['salary_max'] is None

    def test_update_job_details_coalesce(self, db_manager):
        """Test COALESCE behavior (preserves existing data)."""
        # Add company and job with initial description
        company_id = db_manager.add_company("Test Company", "https://test.com")
        job_id = db_manager.add_job_posting(
            company_id=company_id,
            job_hash="test_hash_3",
            title="Test Job 3",
            url="https://test.com/job/3",
            description="Initial description"
        )

        # Update with None description (should preserve existing)
        success = db_manager.update_job_details(
            job_id=job_id,
            description=None,
            salary_text="$100k"
        )

        assert success is True

        # Verify existing description preserved
        job = db_manager.get_job_by_id(job_id)
        assert job['description'] == "Initial description"  # Preserved
        assert job['salary_text'] == "$100k"  # Updated

    # ============================================================================
    # Deep Scraping Method Tests
    # ============================================================================

    def test_deep_scrape_job_with_mock(self, service, db_manager, monkeypatch):
        """Test _deep_scrape_job with mocked HTTP response."""
        from unittest.mock import Mock

        # Add company and job
        company_id = db_manager.add_company("Test Company", "https://test.com")
        job_id = db_manager.add_job_posting(
            company_id=company_id,
            job_hash="test_hash_deep",
            title="Test Job",
            url="https://test.com/job/1"
        )

        # Mock HTML response with Greenhouse structure
        mock_html = """
        <html>
            <body>
                <div id="content">
                    <h1>Senior Engineer</h1>
                    <p>We are looking for an experienced engineer...</p>
                    <p>Compensation: $200,000 - $250,000 per year</p>
                    <ul>
                        <li>5+ years experience</li>
                        <li>Python expertise</li>
                    </ul>
                </div>
            </body>
        </html>
        """

        # Mock _fetch_with_retry
        def mock_fetch(url, politeness_delay=0):
            return mock_html

        monkeypatch.setattr(service, '_fetch_with_retry', mock_fetch)

        # Deep scrape the job
        success = service._deep_scrape_job(job_id, "https://test.com/job/1")

        assert success is True

        # Verify details were extracted and saved
        job = db_manager.get_job_by_id(job_id)
        assert job['description'] is not None
        assert len(job['description']) > 100
        assert "experienced engineer" in job['description'].lower()
        assert job['salary_text'] == "$200,000 - $250,000"
        assert job['salary_min'] == 200000.0
        assert job['salary_max'] == 250000.0

    def test_deep_scrape_job_fetch_failure(self, service, db_manager, monkeypatch):
        """Test _deep_scrape_job when fetch fails."""
        # Add company and job
        company_id = db_manager.add_company("Test Company", "https://test.com")
        job_id = db_manager.add_job_posting(
            company_id=company_id,
            job_hash="test_hash_fail",
            title="Test Job",
            url="https://test.com/job/fail"
        )

        # Mock _fetch_with_retry to return None (failure)
        def mock_fetch(url, politeness_delay=0):
            return None

        monkeypatch.setattr(service, '_fetch_with_retry', mock_fetch)

        # Deep scrape should fail gracefully
        success = service._deep_scrape_job(job_id, "https://test.com/job/fail")

        assert success is False

    def test_deep_scrape_matches(self, service, db_manager, monkeypatch):
        """Test _deep_scrape_matches with multiple jobs."""
        from unittest.mock import Mock

        # Add company and jobs
        company_id = db_manager.add_company("Test Company", "https://test.com")

        jobs = []
        for i in range(3):
            job_id = db_manager.add_job_posting(
                company_id=company_id,
                job_hash=f"test_hash_{i}",
                title=f"Test Job {i}",
                url=f"https://test.com/job/{i}"
            )
            jobs.append({
                'id': job_id,
                'url': f"https://test.com/job/{i}",
                'title': f"Test Job {i}"
            })

        # Mock _deep_scrape_job to succeed for all
        def mock_deep_scrape(job_id, job_url):
            return True

        monkeypatch.setattr(service, '_deep_scrape_job', mock_deep_scrape)

        # Mock time.sleep to speed up test
        def mock_sleep(seconds):
            pass

        import time
        monkeypatch.setattr(time, 'sleep', mock_sleep)

        # Deep scrape all matches
        success_count = service._deep_scrape_matches(jobs)

        assert success_count == 3

    # ============================================================================
    # Command Handler Tests
    # ============================================================================

    def test_deep_match_command(self, service, db_manager):
        """Test deep-match command handler."""
        # Add company and criteria
        company_id = db_manager.add_company("Test Company", "https://test.com")
        self.send_command(service, "add-criteria python simple 20")

        # Add jobs with varying match scores
        job_ids = []
        for i in range(5):
            title = f"Python Engineer {i}" if i < 3 else f"Java Developer {i}"
            job_id = db_manager.add_job_posting(
                company_id=company_id,
                job_hash=f"test_hash_{i}",
                title=title,
                url=f"https://test.com/job/{i}",
                description=title
            )
            job_ids.append(job_id)

        # Mock _deep_scrape_matches
        from unittest.mock import Mock
        original_method = service._deep_scrape_matches

        def mock_deep_scrape_matches(matched_jobs):
            return len(matched_jobs)  # Return success count = all jobs

        service._deep_scrape_matches = mock_deep_scrape_matches

        # Run deep-match command
        response = self.send_command(service, "deep-match 15 5")

        # Verify response
        assert "Deep Scraping Matches" in response or "Deep Scrape Complete" in response

        # Restore original method
        service._deep_scrape_matches = original_method

    def test_deep_match_command_no_matches(self, service, db_manager):
        """Test deep-match command when no jobs match criteria."""
        # Add company and strict criteria
        company_id = db_manager.add_company("Test Company", "https://test.com")
        self.send_command(service, "add-criteria haskell simple 100")  # Very specific

        # Add jobs that won't match
        for i in range(3):
            db_manager.add_job_posting(
                company_id=company_id,
                job_hash=f"test_hash_{i}",
                title=f"Python Developer {i}",
                url=f"https://test.com/job/{i}",
                description="Python programming"
            )

        # Run deep-match command
        response = self.send_command(service, "deep-match 50 5")

        # Should indicate no matches
        assert "No jobs matched" in response

    def test_deep_match_command_no_jobs(self, service):
        """Test deep-match command with empty database."""
        response = self.send_command(service, "deep-match 60 10")

        # Should indicate no jobs
        assert "No jobs in database" in response or "No jobs matched" in response

    def test_deep_match_command_invalid_args(self, service):
        """Test deep-match command with invalid arguments."""
        response = self.send_command(service, "deep-match invalid limit")

        # Should show usage error
        assert "Usage:" in response or "Example:" in response

# ============================================================================
# Phase 6: Scheduler Tests
# ============================================================================

class TestPhase6_Scheduler:
    """Test Phase 6 scheduler implementation."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp.close()
        yield temp.name
        Path(temp.name).unlink(missing_ok=True)

    @pytest.fixture
    def db_manager(self, temp_db):
        """Create database manager with temporary database."""
        manager = JobHunterDatabaseManager(temp_db)
        manager.initialize_database()
        return manager

    @pytest.fixture
    def service(self, temp_db):
        """Create JobHunter service instance for testing."""
        config = {
            'database_path': temp_db,
            'default_schedules': [
                {'time': '09:00', 'timezone': 'America/New_York'},
                {'time': '17:00', 'timezone': 'America/New_York'}
            ],
            'scoring_thresholds': {
                'high_priority': 70,
                'good_match': 60,
                'review': 40,
                'low_priority': 0
            }
        }
        service = JobHunterService('jobhunter-service', config)
        
        # Initialize database without starting threads
        service.db_manager = JobHunterDatabaseManager(temp_db)
        service.db_manager.initialize_database()
        
        # Mock message sending
        service._send_response = Mock()
        service._send_error_response = Mock()
        
        yield service

    def send_command(self, service, command_str):
        """Helper to send command and get response."""
        parts = command_str.split()
        command = parts[0]
        args = parts[1:] if len(parts) > 1 else []
        
        service._handle_message_sync('test-channel', command, args, correlation_id='test-123')
        
        # Get response from mock
        if service._send_response.called:
            return service._send_response.call_args[0][1]
        elif service._send_error_response.called:
            return service._send_error_response.call_args[0][1]
        return ""

    # ========================================================================
    # Job Hash Generation Tests
    # ========================================================================

    def test_generate_job_hash_consistency(self, service):
        """Test that job hash generation is consistent."""
        title = "Senior Python Engineer"
        url = "https://company.com/jobs/123"
        
        hash1 = service._generate_job_hash(title, url)
        hash2 = service._generate_job_hash(title, url)
        
        # Same inputs produce same hash
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 produces 64 hex characters

    def test_generate_job_hash_uniqueness(self, service):
        """Test that different jobs produce different hashes."""
        hash1 = service._generate_job_hash("Job A", "https://company.com/a")
        hash2 = service._generate_job_hash("Job B", "https://company.com/b")
        hash3 = service._generate_job_hash("Job A", "https://company.com/b")  # Same title, different URL
        
        assert hash1 != hash2
        assert hash1 != hash3
        assert hash2 != hash3

    def test_generate_job_hash_encoding(self, service):
        """Test hash generation with special characters."""
        title = "Engineer (Remote) – Full-Time"
        url = "https://company.com/jobs?id=123&ref=careers"
        
        hash_val = service._generate_job_hash(title, url)
        
        assert len(hash_val) == 64
        assert hash_val.isalnum()  # Only hex characters

    # ========================================================================
    # Timezone and Schedule Calculation Tests
    # ========================================================================

    def test_calculate_next_run_with_schedules(self, service, db_manager):
        """Test next run time calculation with active schedules."""
        import pytz
        from datetime import datetime, timezone
        
        # Add schedules to database
        db_manager.add_run_time("09:00", "America/New_York")
        db_manager.add_run_time("17:00", "America/New_York")
        
        # Calculate next run
        next_run = service._calculate_next_run()
        
        # Should return a datetime
        assert next_run is not None
        assert isinstance(next_run, datetime)
        assert next_run.tzinfo == timezone.utc

    def test_calculate_next_run_no_schedules(self, service):
        """Test next run calculation with no active schedules."""
        next_run = service._calculate_next_run()
        
        # Should return None when no schedules
        assert next_run is None

    def test_calculate_next_run_past_time_rolls_to_tomorrow(self, service, db_manager):
        """Test that past times roll over to next day."""
        from datetime import datetime, timezone, timedelta
        
        # Add a schedule for a time that has already passed today
        db_manager.add_run_time("00:00", "America/New_York")  # Midnight
        
        next_run = service._calculate_next_run()
        now = datetime.now(timezone.utc)
        
        # Next run should be in the future
        assert next_run > now
        
        # Should be within 24 hours
        time_diff = (next_run - now).total_seconds()
        assert time_diff <= 86400  # 24 hours in seconds

    def test_calculate_next_run_multiple_timezones(self, service, db_manager):
        """Test schedule calculation with multiple timezones."""
        # Add schedules in different timezones
        db_manager.add_run_time("09:00", "America/New_York")   # EST
        db_manager.add_run_time("09:00", "America/Los_Angeles")  # PST
        db_manager.add_run_time("09:00", "Europe/London")      # GMT
        
        next_run = service._calculate_next_run()
        
        # Should return the earliest upcoming time
        assert next_run is not None

    # ========================================================================
    # Workflow Orchestration Tests
    # ========================================================================

    def test_run_daily_scrape_no_companies(self, service, db_manager):
        """Test daily scrape with no companies configured."""
        # Mock the scraping method
        service._scrape_company = Mock(return_value=[])
        
        # Run workflow
        service._run_daily_scrape()
        
        # Should not attempt scraping
        assert not service._scrape_company.called

    def test_run_daily_scrape_with_companies(self, service, db_manager):
        """Test daily scrape orchestration with active companies."""
        # Add test company
        company_id = db_manager.add_company(
            name="Test Corp",
            careers_url="https://test.com/careers",
            scraper_type="generic"
        )
        
        # Mock scraping methods
        service._scrape_company = Mock(return_value=[
            {
                'title': 'Senior Engineer',
                'url': 'https://test.com/job/1',
                'location': 'Remote'
            }
        ])
        service._deep_scrape_matches = Mock(return_value=0)
        
        # Run workflow
        service._run_daily_scrape()
        
        # Verify scraping was called
        assert service._scrape_company.called
        assert service._scrape_company.call_args[1]['company_id'] == company_id

    def test_run_daily_scrape_deduplication(self, service, db_manager):
        """Test that workflow properly deduplicates jobs."""
        # Add company
        company_id = db_manager.add_company(
            name="Test Corp",
            careers_url="https://test.com/careers"
        )
        
        # Add existing job
        existing_hash = service._generate_job_hash("Engineer", "https://test.com/1")
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=existing_hash,
            title="Engineer",
            url="https://test.com/1"
        )
        
        # Mock scraper to return same job + new job
        service._scrape_company = Mock(return_value=[
            {'title': 'Engineer', 'url': 'https://test.com/1', 'location': 'Remote'},
            {'title': 'Designer', 'url': 'https://test.com/2', 'location': 'Remote'}
        ])
        service._deep_scrape_matches = Mock(return_value=0)
        
        # Run workflow
        service._run_daily_scrape()
        
        # Check database - should have 2 jobs total (1 existing + 1 new)
        jobs = db_manager.get_recent_jobs(days=1)
        assert len(jobs) == 2

    # ========================================================================
    # Manual Trigger (run-now) Tests
    # ========================================================================

    def test_run_now_command_triggers_workflow(self, service, db_manager):
        """Test that run-now command triggers the workflow."""
        # Add company
        db_manager.add_company(
            name="Test Corp",
            careers_url="https://test.com/careers"
        )
        
        # Mock workflow methods
        service._run_daily_scrape = Mock()
        
        # Send run-now command
        response = self.send_command(service, "run-now")
        
        # Verify workflow was triggered
        assert service._run_daily_scrape.called
        assert "Workflow Complete" in response or "Manual Workflow" in response

    def test_run_now_command_no_companies(self, service):
        """Test run-now with no companies configured."""
        # Mock workflow
        service._run_daily_scrape = Mock()
        
        # Send command
        response = self.send_command(service, "run-now")
        
        # Should still trigger (workflow handles empty case)
        assert service._run_daily_scrape.called

    # ========================================================================
    # Configurable Thresholds Tests
    # ========================================================================

    def test_thresholds_loaded_from_config(self, service):
        """Test that thresholds are loaded from configuration."""
        assert hasattr(service, 'threshold_high_priority')
        assert hasattr(service, 'threshold_good_match')
        assert hasattr(service, 'threshold_review')
        assert hasattr(service, 'threshold_low_priority')
        
        # Check default values
        assert service.threshold_high_priority == 70
        assert service.threshold_good_match == 60
        assert service.threshold_review == 40
        assert service.threshold_low_priority == 0

    def test_thresholds_custom_values(self, temp_db):
        """Test service with custom threshold values."""
        config = {
            'database_path': temp_db,
            'scoring_thresholds': {
                'high_priority': 80,
                'good_match': 65,
                'review': 50,
                'low_priority': 0
            }
        }
        service = JobHunterService('jobhunter-service', config)
        
        assert service.threshold_high_priority == 80
        assert service.threshold_good_match == 65
        assert service.threshold_review == 50

    def test_thresholds_fallback_to_defaults(self, temp_db):
        """Test that missing threshold config falls back to defaults."""
        config = {
            'database_path': temp_db,
            'scoring_thresholds': {}  # Empty config
        }
        service = JobHunterService('jobhunter-service', config)
        
        # Should use defaults
        assert service.threshold_high_priority == 70
        assert service.threshold_good_match == 60

    def test_thresholds_used_in_job_details(self, service, db_manager):
        """Test that configurable thresholds are used in job detail view."""
        # Add company and job
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        
        # Add criteria to ensure specific score
        response = self.send_command(service, "add-group departments tiered")
        response = self.send_command(service, "add-group-value departments sales preferred 75")
        
        # Add job that will score 75
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Sales Engineer", "https://test.com/1"),
            title="Sales Engineer",
            url="https://test.com/1",
            description="Sales position"
        )
        
        # Get job ID
        jobs = db_manager.get_recent_jobs(days=1)
        job_id = jobs[0]['id']
        
        # Request job details
        response = self.send_command(service, f"score-jobs {job_id}")
        
        # Should show HIGH PRIORITY (score 75 >= threshold 70)
        assert "HIGH PRIORITY" in response or "🔥" in response

    # ========================================================================
    # Enhanced Job Details View Tests
    # ========================================================================

    def test_job_details_basic_info(self, service, db_manager):
        """Test job details view shows basic information."""
        # Add company and job
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Engineer", "https://test.com/1"),
            title="Senior Python Engineer",
            url="https://test.com/jobs/1",
            location="San Francisco, CA"
        )
        
        # Get job ID
        jobs = db_manager.get_recent_jobs(days=1)
        job_id = jobs[0]['id']
        
        # Request details
        response = self.send_command(service, f"score-jobs {job_id}")
        
        # Verify basic info present
        assert "Senior Python Engineer" in response
        assert "San Francisco" in response or "No location" in response
        assert "https://test.com/jobs/1" in response

    def test_job_details_with_salary(self, service, db_manager):
        """Test job details view includes salary information."""
        # Add company and job with salary
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Engineer", "https://test.com/1"),
            title="Senior Engineer",
            url="https://test.com/1",
            salary_min=150000,
            salary_max=200000,
            salary_text="$150k-$200k"
        )
        
        # Get job ID
        jobs = db_manager.get_recent_jobs(days=1)
        job_id = jobs[0]['id']
        
        # Request details
        response = self.send_command(service, f"score-jobs {job_id}")
        
        # Should include salary info
        assert "150,000" in response or "150k" in response
        assert "200,000" in response or "200k" in response

    def test_job_details_with_description(self, service, db_manager):
        """Test job details view includes full description."""
        # Add company and job with description
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        description = "We are looking for a talented engineer to join our team. " * 20
        
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Engineer", "https://test.com/1"),
            title="Engineer",
            url="https://test.com/1",
            description=description
        )
        
        # Get job ID
        jobs = db_manager.get_recent_jobs(days=1)
        job_id = jobs[0]['id']
        
        # Request details
        response = self.send_command(service, f"score-jobs {job_id}")
        
        # Should include description text
        assert "talented engineer" in response.lower()

    def test_job_details_not_found(self, service):
        """Test job details for non-existent job ID."""
        response = self.send_command(service, "score-jobs 99999")
        
        # Should return error
        assert "not found" in response.lower() or "❌" in response

    def test_job_details_vs_list_view(self, service, db_manager):
        """Test that single ID triggers detail view, not list view."""
        # Add company and job
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Engineer", "https://test.com/1"),
            title="Engineer",
            url="https://test.com/1",
            description="Full description here"
        )
        
        # Get job ID
        jobs = db_manager.get_recent_jobs(days=1)
        job_id = jobs[0]['id']
        
        # Request with job ID (detail view)
        detail_response = self.send_command(service, f"score-jobs {job_id}")
        
        # Request with limit/threshold (list view)
        list_response = self.send_command(service, "score-jobs 10 0")
        
        # Detail view should have more content
        assert len(detail_response) > len(list_response) or "Full description" in detail_response

    # ========================================================================
    # Scoring Integration Tests
    # ========================================================================

    def test_scoring_without_cap(self, service, db_manager):
        """Test that scoring is no longer capped at 100."""
        # Add multiple high-value criteria
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        
        # Create criteria groups with high weights
        self.send_command(service, "add-group departments tiered")
        self.send_command(service, "add-group-value departments sales preferred 50")
        
        self.send_command(service, "add-group location tiered")
        self.send_command(service, "add-group-value location remote preferred 40")
        
        self.send_command(service, "add-group salary tiered")
        self.send_command(service, "add-group-value salary 200k+ preferred 30")
        
        # Add job matching all criteria
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Sales", "https://test.com/1"),
            title="Sales Engineer - Remote",
            url="https://test.com/1",
            description="Remote sales position",
            salary_min=250000,
            salary_max=300000
        )
        
        # Score the job
        jobs = db_manager.get_recent_jobs(days=1)
        score = service._filter_and_score_job(jobs[0])
        
        # Score should be able to exceed 100
        assert score == 50 + 40 + 30  # 120 points

    def test_salary_range_matching_with_database_columns(self, service, db_manager):
        """Test that salary matching uses database columns."""
        # Add salary criteria
        self.send_command(service, "add-group salary tiered")
        self.send_command(service, "add-group-value salary 200k-300k preferred 50")
        
        # Add company and job with salary in database columns
        company_id = db_manager.add_company("Test Corp", "https://test.com")
        db_manager.add_job_posting(
            company_id=company_id,
            job_hash=service._generate_job_hash("Engineer", "https://test.com/1"),
            title="Engineer",
            url="https://test.com/1",
            description="No salary mentioned in text",
            salary_min=250000,  # In range
            salary_max=280000   # In range
        )
        
        # Score the job
        jobs = db_manager.get_recent_jobs(days=1)
        score = service._filter_and_score_job(jobs[0])
        
        # Should match salary criteria (50 points)
        assert score == 50

    # ========================================================================
    # Error Handling Tests
    # ========================================================================

    def test_scheduler_handles_missing_timezone(self, service, db_manager):
        """Test scheduler gracefully handles missing timezone."""
        # Manually add schedule with no timezone
        with service.db_manager._get_connection() as conn:
            conn.execute("""
                INSERT INTO run_schedules (run_time, timezone, active, created_date)
                VALUES (?, NULL, 1, datetime('now'))
            """, ("12:00",))
        
        # Should not crash
        next_run = service._calculate_next_run()
        # May return None or a valid time with default timezone

    def test_workflow_continues_on_company_failure(self, service, db_manager):
        """Test workflow continues when one company fails to scrape."""
        # Add multiple companies
        db_manager.add_company("Good Corp", "https://good.com")
        db_manager.add_company("Bad Corp", "https://bad.com")
        
        # Mock scraper to fail on second company
        def mock_scrape(**kwargs):
            if 'bad.com' in kwargs.get('careers_url', ''):
                raise Exception("Scraping failed")
            return []
        
        service._scrape_company = Mock(side_effect=mock_scrape)
        service._deep_scrape_matches = Mock(return_value=0)
        
        # Should not crash
        try:
            service._run_daily_scrape()
        except Exception as e:
            pytest.fail(f"Workflow should handle failures gracefully: {e}")

