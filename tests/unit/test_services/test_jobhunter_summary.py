"""The daily digest (the hiring summary) when the model's reply is empty.

2026-11-12, 13 and 14: through the LLM proxy the model spent its whole
900-token budget before writing, the reply came back empty with
finish_reason "length", and the owner got "failed to generate summary"
three days running. The digest now asks for no reasoning, retries once with
twice the room when a reply is empty at its limit, and says why it failed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hunter.services.jobhunter_service import JobHunterService


def _reply(text, finish="stop", tokens=700):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=3000, completion_tokens=tokens))


@pytest.fixture
def svc():
    s = JobHunterService.__new__(JobHunterService)
    s.logger = MagicMock()
    s.ai_provider, s.ai_enabled, s._openai_client = "openai", True, object()
    s.ai_summary_model, s.ai_summary_max_tokens, s.ai_summary_reasoning_effort = "claude-sonnet-5-5", 900, "none"
    s._cv_content, s._goal_content, s.ai_summary_prompt_template = "", "", None
    s.db_manager = MagicMock()
    s.db_manager.get_active_criteria.return_value = []
    s._format_criteria_for_ai = lambda c: ""
    s._calculate_ai_cost = lambda i, o, model=None: 0.03
    s.calls = []
    return s


def _approved():
    return [{"job": {"id": 7, "title": "VP Sales", "company_name": "Acme", "url": "https://x/7",
                     "first_seen_date": "2026-11-14"},
             "ai_result": {"score": 80, "reasoning": "fits", "recommendation": "APPLY"}}]


def _answer(svc, *replies):
    queue = list(replies)

    def call(**kw):
        svc.calls.append(kw)
        return queue.pop(0)
    svc._call_llm_with_retry = call


def test_a_normal_digest_asks_for_no_reasoning(svc):
    _answer(svc, _reply("Today's top 1:\n🎯 *[VP Sales](https://x/7)* — Acme"))
    assert svc._generate_hiring_summary(_approved()).startswith("Today's top 1:")
    (call,) = svc.calls
    assert call["max_tokens"] == 900 and call["reasoning_effort"] == "none"


def test_an_empty_reply_at_the_limit_is_retried_once_with_twice_the_room(svc):
    _answer(svc, _reply("", "length", 900), _reply("Today's top 1: …", "stop", 680))
    assert svc._generate_hiring_summary(_approved()) == "Today's top 1: …"
    assert [c["max_tokens"] for c in svc.calls] == [900, 1800]
    assert "came back empty at its 900-token limit" in svc.logger.warning.call_args_list[0].args[0]
    # the cost covers both calls
    assert svc.db_manager.record_ai_analysis.call_args.kwargs["tokens_output"] == 1580


def test_empty_twice_says_why_and_returns_nothing(svc):
    _answer(svc, _reply(None, "length", 900), _reply("", "length", 1800))
    assert svc._generate_hiring_summary(_approved()) is None
    assert svc._summary_failure == "cut off at 1800 tokens before any text"
    assert len(svc.calls) == 2


def test_an_empty_reply_that_was_not_cut_off_is_not_retried(svc):
    _answer(svc, _reply("   ", "stop", 3))
    assert svc._generate_hiring_summary(_approved()) is None
    assert svc._summary_failure == "the model returned no text" and len(svc.calls) == 1


def test_a_cut_off_digest_with_text_is_sent_as_it_is(svc):
    _answer(svc, _reply("Today's top 3:\n🎯 first …", "length", 900))
    assert svc._generate_hiring_summary(_approved()).startswith("Today's top 3:")
    assert len(svc.calls) == 1
    assert "sending what arrived" in svc.logger.warning.call_args.args[0]


def test_reasoning_effort_is_passed_to_the_proxy_only_when_set(svc):
    client = MagicMock()
    svc.ai_model, svc.ai_max_tokens, svc.ai_temperature = "m", 2000, 0.3
    svc._openai_create(client, model="m", max_tokens=900, messages=[{"role": "user", "content": "x"}],
                       reasoning_effort="none")
    assert client.chat.completions.create.call_args.kwargs["extra_body"] == {"reasoning_effort": "none"}
    svc._openai_create(client, model="m", max_tokens=900, messages=[{"role": "user", "content": "x"}])
    assert "extra_body" not in client.chat.completions.create.call_args.kwargs


def test_anthropic_replies_read_every_text_block(svc):
    svc.ai_provider = "anthropic"
    r = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="…"),
                                 SimpleNamespace(type="text", text="Today's top 1")], stop_reason="end_turn")
    assert svc._extract_text(r) == "Today's top 1" and not svc._hit_token_limit(r)
    assert svc._hit_token_limit(SimpleNamespace(stop_reason="max_tokens"))
