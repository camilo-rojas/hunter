#!/usr/bin/env python3
"""
JobHunter Service Setup Verification

Tests:
1. Database initialization
2. Service lifecycle (start/stop)
3. Default criteria and schedules loaded
4. Health check
"""

import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from robotray.services.jobhunter_service import JobHunterService
from robotray.services.jobhunter_db import JobHunterDatabaseManager
import yaml

def test_database_manager():
    """Test database manager initialization."""
    print("\n" + "="*60)
    print("TEST 1: Database Manager Initialization")
    print("="*60)

    # Clean up any existing test database
    test_db = "data/test_jobhunter.db"
    if os.path.exists(test_db):
        os.remove(test_db)
        print(f"✓ Cleaned up existing test database")

    # Initialize database manager
    db_manager = JobHunterDatabaseManager(test_db)
    db_manager.initialize_database()
    print(f"✓ Database manager initialized: {test_db}")

    # Verify tables exist by querying them
    try:
        companies = db_manager.get_active_companies()
        criteria = db_manager.get_active_criteria()
        schedules = db_manager.get_active_run_times()
        print(f"✓ All tables accessible")
        print(f"  - Companies: {len(companies)}")
        print(f"  - Criteria: {len(criteria)}")
        print(f"  - Schedules: {len(schedules)}")
    except Exception as e:
        print(f"✗ Error querying tables: {e}")
        return False

    return True

def test_default_initialization():
    """Test loading default criteria and schedules."""
    print("\n" + "="*60)
    print("TEST 2: Default Criteria and Schedules")
    print("="*60)

    # Clean up
    test_db = "data/test_jobhunter_defaults.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    # Initialize with defaults
    db_manager = JobHunterDatabaseManager(test_db)
    db_manager.initialize_database()

    # Load default criteria
    default_criteria = [
        {"type": "keyword", "value": "python", "weight": 10},
        {"type": "keyword", "value": "senior engineer", "weight": 15},
        {"type": "location", "value": "Remote", "weight": 20},
        {"type": "min_salary", "value": "120000", "weight": 20},
        {"type": "required_skill", "value": "python", "weight": 10},
    ]
    db_manager.initialize_default_criteria(default_criteria)

    # Load default schedules
    default_schedules = [
        {"time": "09:00", "timezone": "America/New_York"},
        {"time": "13:00", "timezone": "America/New_York"},
        {"time": "17:00", "timezone": "America/New_York"},
    ]
    db_manager.initialize_default_schedules(default_schedules)

    # Verify
    criteria = db_manager.get_active_criteria()
    schedules = db_manager.get_active_run_times()

    print(f"✓ Loaded {len(criteria)} criteria")
    for c in criteria:
        print(f"  - {c['criterion_type']}: {c['criterion_value']} (weight: {c['weight']})")

    print(f"✓ Loaded {len(schedules)} schedules")
    for s in schedules:
        print(f"  - {s['run_time']} {s['timezone']}")

    if len(criteria) != 5:
        print(f"✗ Expected 5 criteria, got {len(criteria)}")
        return False

    if len(schedules) != 3:
        print(f"✗ Expected 3 schedules, got {len(schedules)}")
        return False

    return True

def test_service_lifecycle():
    """Test service start/stop."""
    print("\n" + "="*60)
    print("TEST 3: Service Lifecycle")
    print("="*60)

    # Load config
    config_path = "config/services/jobhunter.yaml"
    with open(config_path, 'r') as f:
        service_config = yaml.safe_load(f)

    config = service_config.get('config', {})

    # Override database path for testing
    test_db = "data/test_jobhunter_service.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    config['database_path'] = test_db

    # Initialize service
    service = JobHunterService("jobhunter", config)
    print(f"✓ Service initialized")

    # Start service
    try:
        service.start_service()
        print(f"✓ Service started")
    except Exception as e:
        print(f"✗ Failed to start service: {e}")
        return False

    # Check health
    health = service.health_check()
    print(f"✓ Health check: {health['status']}")
    print(f"  Message: {health['message']}")
    for key, value in health.get('details', {}).items():
        print(f"  - {key}: {value}")

    if health['status'] != 'healthy':
        print(f"✗ Service is not healthy: {health['message']}")
        return False

    # Stop service
    try:
        service.stop_service()
        print(f"✓ Service stopped")
    except Exception as e:
        print(f"✗ Failed to stop service: {e}")
        return False

    return True

def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("JobHunter Service - Phase 1 Verification")
    print("="*60)

    results = []

    # Test 1: Database Manager
    results.append(("Database Manager", test_database_manager()))

    # Test 2: Default Initialization
    results.append(("Default Initialization", test_default_initialization()))

    # Test 3: Service Lifecycle
    results.append(("Service Lifecycle", test_service_lifecycle()))

    # Summary
    print("\n" + "="*60)
    print("PHASE 1 VERIFICATION SUMMARY")
    print("="*60)

    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False

    if all_passed:
        print("\n🎉 Phase 1: Database Foundation - COMPLETE")
        print("\nVerification checklist:")
        print("✅ Service starts/stops cleanly")
        print("✅ All database tables created with proper schema")
        print("✅ Default criteria inserted (5 rows)")
        print("✅ Default schedules inserted (3 rows)")
        print("✅ Health check returns 'healthy' status")
        print("\nReady to proceed to Phase 2: Message Handling")
        return 0
    else:
        print("\n❌ Phase 1 verification FAILED")
        print("Please review errors above and fix before proceeding.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
