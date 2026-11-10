"""The model's job analysis reply, parsed (parse_analysis_reply).

2026-11-10: after the move to a model that reasons at length inside a
```json fence, replies ran into the token limit. The old parser found no
closing brace and discarded the whole analysis — three in one evening's
run, among them jobs scored 68 and 72. The score comes first in the reply:
a cut-off reply still carries it.
"""

import json

import pytest

from hunter.services.jobhunter_service import parse_analysis_reply

FULL = {"ai_score": 82, "reasoning": "Strong fit.", "fit_summary": "Good.",
        "concerns": ["travel"], "recommendation": "REVIEW"}


class TestCompleteReplies:
    def test_plain_json(self):
        assert parse_analysis_reply(json.dumps(FULL)) == (FULL, False)

    def test_fenced_json(self):
        assert parse_analysis_reply("```json\n" + json.dumps(FULL, indent=4) + "\n```") == (FULL, False)

    def test_text_around_it(self):
        reply = "Here is my analysis:\n" + json.dumps(FULL) + "\nLet me know if you want more."
        assert parse_analysis_reply(reply) == (FULL, False)

    def test_braces_and_quotes_inside_strings_do_not_count(self):
        tricky = dict(FULL, reasoning='Uses {templates} and a "quoted" term, } even this.')
        assert parse_analysis_reply("```json\n" + json.dumps(tricky) + "\n```") == (tricky, False)

    def test_a_complete_reply_is_not_filled_in(self):
        """Missing fields in a COMPLETE reply stay missing: the caller reports them."""
        result, cut_off = parse_analysis_reply('{"ai_score": 40}')
        assert result == {"ai_score": 40} and cut_off is False


class TestCutOffReplies:
    # the shape of the two replies lost on 2026-11-10 (fence opened, never closed)
    CUT_IN_REASONING = ('```json\n{\n    "ai_score": 68,\n    "reasoning": "This is a senior, strategic, '
                        'AI-focused leadership role, the type of company the candidate most wants. It pays above '
                        'target. But the core function is a significant departure from the candidate\'s sales and '
                        'GTM background, s')

    def test_cut_in_the_reasoning_keeps_the_score_and_what_was_written(self):
        result, cut_off = parse_analysis_reply(self.CUT_IN_REASONING)
        assert cut_off is True and result["ai_score"] == 68
        assert result["reasoning"].startswith("This is a senior") and result["reasoning"].endswith("…")
        assert result["recommendation"] == "SKIP"            # 68: below REVIEW's 70, as the prompt defines
        assert result["concerns"] == [] and result["fit_summary"] == ""

    @pytest.mark.parametrize("score,recommendation", [(95, "APPLY"), (90, "APPLY"), (72, "REVIEW"),
                                                      (70, "REVIEW"), (69, "SKIP")])
    def test_the_recommendation_follows_the_score(self, score, recommendation):
        result, _ = parse_analysis_reply('{"ai_score": %d, "reasoning": "Because' % score)
        assert result["recommendation"] == recommendation

    def test_cut_inside_the_concerns_list(self):
        reply = ('{"ai_score": 75, "reasoning": "Good.", "fit_summary": "Fits.", '
                 '"concerns": ["relocation", "equity is uncl')
        result, cut_off = parse_analysis_reply(reply)
        assert cut_off and result["concerns"] == ["relocation", "equity is uncl"]
        assert result["fit_summary"] == "Fits." and result["recommendation"] == "REVIEW"

    def test_cut_after_a_key_keeps_the_complete_fields(self):
        reply = '{"ai_score": 91, "reasoning": "Excellent match.", "fit_summary": "Yes.", "concerns":'
        result, cut_off = parse_analysis_reply(reply)
        assert cut_off and result["ai_score"] == 91 and result["fit_summary"] == "Yes."
        assert result["recommendation"] == "APPLY"

    def test_cut_in_the_middle_of_a_key(self):
        result, cut_off = parse_analysis_reply('{"ai_score": 55, "reasoning": "Weak.", "fit_su')
        assert cut_off and result["ai_score"] == 55 and result["recommendation"] == "SKIP"

    def test_cut_on_an_escape(self):
        result, cut_off = parse_analysis_reply('{"ai_score": 80, "reasoning": "A \\"quoted\\" word and then \\')
        assert cut_off and result["ai_score"] == 80

    def test_a_given_recommendation_is_kept(self):
        reply = '{"ai_score": 65, "recommendation": "REVIEW", "reasoning": "Borderline but'
        assert parse_analysis_reply(reply)[0]["recommendation"] == "REVIEW"

    @pytest.mark.parametrize("reply", ['{"ai_sc', '{"ai_score": ', '```json\n{\n    "ai_score"', '{"ai_score": "high", "reasoning": "x'])
    def test_cut_before_a_usable_score_is_an_error(self, reply):
        with pytest.raises(ValueError):
            parse_analysis_reply(reply)


class TestNoReply:
    @pytest.mark.parametrize("reply", ["", "   ", None])
    def test_empty(self, reply):
        with pytest.raises(ValueError, match="Empty"):
            parse_analysis_reply(reply)

    def test_no_object(self):
        with pytest.raises(ValueError, match="No JSON object"):
            parse_analysis_reply("I cannot analyze this job.")

    def test_broken_but_complete_json_is_still_a_json_error(self):
        with pytest.raises(json.JSONDecodeError):
            parse_analysis_reply('{"ai_score": 80, "reasoning": oops}')
