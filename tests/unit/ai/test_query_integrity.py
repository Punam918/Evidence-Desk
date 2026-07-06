import json

import pytest
from evidencedesk.ai.rewrite import select_query
from evidencedesk.providers.base import ModelResult


@pytest.mark.parametrize(
    "original,rewritten,accepted",
    [
        ("Why doesn't it apply?", "Why does the policy apply?", False),
        ("Why doesn't it apply?", "Why does the policy not apply?", True),
        ("Can it apply?", "Can the policy not apply?", False),
        ("Does it allow -30 days?", "Does the rule allow 30 days?", False),
        ("Does it allow −30 days?", "Does the rule allow -30 days?", True),
        ("Does it cover 30%?", "Does the plan cover 30?", False),
        ("Does it cover 30 %?", "Does the plan cover 30%?", True),
        ("Does it affect TS-999?", "Does the policy affect TS-998?", False),
        ("Does it affect OAuth2?", "Does the policy affect OAuth?", False),
        ("Does it cover SSO?", "Does the policy cover SSO?", True),
        ('Does it exclude "team red"?', "Does the policy exclude team blue?", False),
        ('Does it exclude "team red"?', "Does the policy exclude team red?", True),
        ("Is it not not allowed?", "Is the policy not allowed?", False),
        ("Does it apply except on weekends?", "Does the policy apply on weekends?", False),
    ],
)
def test_invalid_rewrites_fall_back_to_original_without_changing_scope(
    original, rewritten, accepted
):
    result = ModelResult("completed", None, None, "fixture", "", json.dumps({"query": rewritten}))
    decision = select_query(original, result, lambda _: 30)
    assert decision.query == (rewritten if accepted else original)
    assert decision.reason == ("rewritten" if accepted else "invalid_rewrite")
