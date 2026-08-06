from brief.models import Item
from brief.score import score_item, select

PROFILE = {
    "watch_entities": ["DIA", "SOCOM"],
    "primary_topics": ["HUMINT", "AI/ML"],
    "opportunity_keywords": ["analytics"],
    "negative_keywords": ["staff augmentation only"],
    "min_score": 35,
    "max_items_to_llm": 5,
    "recency_hours": 36,
}


def _item(title, text="", stype="news", trust="medium"):
    return Item(
        source_name="X", source_type=stype, title=title, raw_text=text, trust=trust
    )


def test_watch_entity_and_topic_score():
    it = _item("SOCOM seeks HUMINT analytics support")
    s = score_item(it, PROFILE)
    assert s >= 30 + 20 + 15  # SOCOM(30) + HUMINT(20) + analytics(15)
    assert "SOCOM" in it.entities and "HUMINT" in it.topics


def test_negative_keyword_penalizes():
    a = score_item(_item("DIA program"), PROFILE)
    b = score_item(_item("DIA program staff augmentation only"), PROFILE)
    assert b < a


def test_gov_and_trust_bonus():
    it = _item("DoD note", stype="gov", trust="high")
    assert score_item(it, PROFILE) >= 10 + 10  # gov(10) + high trust(10)


def test_select_filters_and_caps():
    items = [_item("SOCOM HUMINT analytics"), _item("cat video"), _item("DIA AI/ML")]
    kept = select(items, PROFILE)
    assert all(i.relevance_score >= 35 for i in kept)
    assert "cat video" not in [i.title for i in kept]
