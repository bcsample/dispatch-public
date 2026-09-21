from brief.dedupe import dedupe
from brief.models import Item


def _i(url, title="t"):
    return Item(source_name="S", source_type="news", title=title, url=url)


def test_dedupe_within_batch_and_against_seen():
    items = [_i("http://a"), _i("http://a"), _i("http://b")]
    out = dedupe(items, seen=set())
    assert len(out) == 2  # the two distinct urls
    out2 = dedupe(items, seen={_i("http://a").content_hash})
    assert [i.url for i in out2] == ["http://b"]
