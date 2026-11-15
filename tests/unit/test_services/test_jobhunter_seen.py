"""A posting is recognised on every scrape, and closed when it is delisted.

Until 2026-11-14 the scraper hashed a posting as title:url and the scheduled
run stored title|url: a known posting was never recognised, never marked as
seen again, and nothing ever closed — Anthropic showed 3,353 "open" postings
and every company's list only grew. Scoring and AI ran over all of it.
"""

import hashlib
from unittest.mock import MagicMock

import pytest

from hunter.services.jobhunter_db import JobHunterDatabaseManager
from hunter.services.jobhunter_service import JobHunterService


@pytest.fixture
def svc(tmp_path):
    db = JobHunterDatabaseManager(str(tmp_path / "jh.db"))
    db.initialize_database() if hasattr(db, "initialize_database") else None
    s = JobHunterService.__new__(JobHunterService)
    s.logger, s.verbose_logging, s.db_manager, s._scrape_seen = MagicMock(), False, db, {}
    s.company_id = db.add_company("Acme", "https://boards.example/acme", "greenhouse")
    return s


def _board(svc, *postings):
    svc._scrape_greenhouse = lambda url: [{"title": t, "url": u, "location": "Remote"} for t, u in postings]


def _scrape(svc):
    return svc._scrape_company(svc.company_id, "Acme", "https://boards.example/acme", "greenhouse")


def _save(svc, jobs):
    for j in jobs:
        svc.db_manager.add_job_posting(company_id=svc.company_id, job_hash=j["job_hash"], title=j["title"],
                                       url=j["url"], location=j.get("location"))


def _rows(svc):
    with svc.db_manager._get_connection() as c:
        return {r["title"]: dict(r) for r in c.execute("SELECT * FROM job_postings")}


def test_a_known_posting_is_recognised_and_touched(svc):
    _board(svc, ("VP Sales", "https://x/1"), ("Head of Partnerships", "https://x/2"))
    first = _scrape(svc)
    assert len(first) == 2
    assert all(j["job_hash"] == svc._generate_job_hash(j["title"], j["url"]) for j in first)
    _save(svc, first)
    assert _scrape(svc) == []                                     # both known now
    assert all(r["times_seen"] == 2 for r in _rows(svc).values())


def test_a_row_saved_under_the_old_hash_is_still_recognised(svc):
    legacy = hashlib.sha256("VP Sales:https://x/1".encode()).hexdigest()
    svc.db_manager.add_job_posting(company_id=svc.company_id, job_hash=legacy, title="VP Sales", url="https://x/1")
    _board(svc, ("VP Sales", "https://x/1"))
    assert _scrape(svc) == []
    assert _rows(svc)["VP Sales"]["times_seen"] == 2


def test_a_delisted_posting_is_closed_and_leaves_scoring(svc):
    from datetime import datetime, timezone
    _board(svc, ("VP Sales", "https://x/1"), ("Old role", "https://x/9"))
    _save(svc, _scrape(svc))
    with svc.db_manager._get_connection() as c:                   # seen in an earlier run
        c.execute("UPDATE job_postings SET last_seen_date='2026-11-01T00:00:00+00:00'")
    started = datetime.now(timezone.utc).isoformat()
    _board(svc, ("VP Sales", "https://x/1"))
    _scrape(svc)
    assert svc.db_manager.deactivate_unseen_jobs(svc.company_id, started) == 1
    rows = _rows(svc)
    assert rows["VP Sales"]["is_active"] == 1 and rows["Old role"]["is_active"] == 0
    assert [j["title"] for j in svc.db_manager.get_recent_jobs(limit=50)] == ["VP Sales"]
    assert [j["title"] for j in svc.db_manager.get_recent_jobs(company_id=svc.company_id)] == ["VP Sales"]


def test_an_empty_or_failed_scrape_closes_nothing(svc):
    _board(svc, ("VP Sales", "https://x/1"))
    _save(svc, _scrape(svc))
    svc._scrape_seen = {}
    _board(svc)                                                    # the board returned nothing
    assert _scrape(svc) == []
    assert svc._scrape_seen == {}                                  # so the run will not close its postings


def test_an_inactive_company_s_postings_are_not_scored(svc):
    _board(svc, ("VP Sales", "https://x/1"))
    _save(svc, _scrape(svc))
    svc.db_manager.remove_company(svc.company_id)
    assert svc.db_manager.get_recent_jobs(limit=50) == []


def test_ai_approved_lists_only_open_postings_of_active_companies(svc):
    _board(svc, ("VP Sales", "https://x/1"), ("Old role", "https://x/9"))
    _save(svc, _scrape(svc))
    db = svc.db_manager
    rows = _rows(svc)
    with db._get_connection() as c:
        for title in ("VP Sales", "Old role"):
            c.execute("INSERT INTO job_matches (job_id, match_score, notified_date, ai_score) "
                      "VALUES (?, 80, '', 80)", (rows[title]["id"],))
        c.execute("UPDATE job_postings SET is_active=0 WHERE title='Old role'")
    assert [j["title"] for j in db.get_ai_approved_jobs(min_score=60)] == ["VP Sales"]
    db.remove_company(svc.company_id)
    assert db.get_ai_approved_jobs(min_score=60) == []


def test_approval_uses_the_age_adjusted_score():
    import inspect
    src = inspect.getsource(JobHunterService)
    assert "effective = ai_result['ai_score'] + self._calculate_freshness_adjustment(job)" in src
    assert "if effective >= self.ai_min_score:" in src
    s = JobHunterService.__new__(JobHunterService)
    from datetime import datetime, timedelta, timezone
    week3 = (datetime.now(timezone.utc) - timedelta(days=22)).isoformat()
    assert 72 + s._calculate_freshness_adjustment({"first_seen_date": week3}) == 42   # below the 60 bar
