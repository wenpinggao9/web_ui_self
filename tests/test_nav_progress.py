"""框架级提交后导航进度推断 (无业务字段假设)."""
from __future__ import annotations

from core.execution.entity_discover import (
    canonical_url_entity_map,
    extract_url_entity_map,
    pick_primary_url_id,
    url_entity_maps_differ,
)
from core.execution.nav_progress import detect_submit_navigation_progress
from core.execution.script_helpers import (
    _scan_pages_for_nav_outcome,
    classify_navigation_outcome,
)


def test_entity_map_case_insensitive():
    assert pick_primary_url_id("https://x/app?taskid=1") == ("1", "taskid")
    assert pick_primary_url_id("https://x/app?taskId=2") == ("2", "taskId")


def test_url_entity_maps_differ_generic_keys():
    before = "https://host/app/view?entityId=111"
    after = "https://host/app/view?entityId=222"
    assert url_entity_maps_differ(before, after) is True
    assert classify_navigation_outcome(before, after) == "resource_id_changed"


def test_classify_route_changed_without_business_path():
    before = "https://host/app/100/edit"
    after = "https://host/app/200/edit"
    assert classify_navigation_outcome(before, after) == "resource_id_changed"


def test_scan_pages_prefers_entity_change():
    class _Page:
        def __init__(self, url: str) -> None:
            self.url = url

        def is_closed(self) -> bool:
            return False

    before = "https://host/workspace?recordId=111"
    list_page = _Page("https://host/list")
    next_page = _Page("https://host/workspace?recordId=222")
    out, hit = _scan_pages_for_nav_outcome(
        [_Page(before), list_page, next_page],
        before,
        entity_before="111",
    )
    assert out == "resource_id_changed"
    assert hit is next_page


def test_dom_entity_progress_when_url_unchanged():
    class _Page:
        url = "https://host/form?taskId=100"

        def is_closed(self) -> bool:
            return False

    def _body(_p):
        return "任务ID：200\n提交成功"

    out = detect_submit_navigation_progress(
        "https://host/form?taskId=100",
        _Page(),
        entity_before="100",
        classify_fn=classify_navigation_outcome,
        url_safe_fn=lambda p: p.url,
        page_usable_fn=lambda _p: True,
        read_body_fn=_body,
        check_dom=True,
    )
    assert out == "resource_id_changed"


def test_canonical_map_preserves_entity():
    assert canonical_url_entity_map("https://x?uniqId=9") == {"uniqid": "9"}
