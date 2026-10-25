"""Memory Management screen — part B UI contract (red-first).

The cockpit has no JS test harness, so the UI ships with a thin CONTRACT
test against the files the API actually serves (ops.py's /cockpit/* routes)
plus real-browser screenshots (light/dark/phone, playwright, against a TEST
instance) as the behavioral evidence — the same pattern the Sources panel
(#64/#80) used.

Part B = master-detail: a per-person page (left list of people/sections,
right detail with one memory card per fact), full memory CRUD with Undo
(wired to the part-A archive/restore endpoints), add-person in one step,
and the phone layout (list first, tap opens detail, back button).
"""

from fastapi.testclient import TestClient

from brain.main import create_app


def _memory_js() -> str:
    client = TestClient(create_app())
    response = client.get("/cockpit/memory.js")
    assert response.status_code == 200
    return response.text


def _index_html() -> str:
    client = TestClient(create_app())
    response = client.get("/cockpit")
    assert response.status_code == 200
    return response.text


def test_memory_js_wires_the_part_a_endpoints() -> None:
    js = _memory_js()
    # alias chips: add + remove on an existing entity
    assert "/memory/entities/${entity.id}/aliases" in js
    # soft removal + Undo (archive/restore — the "remove" the screen uses)
    assert "/archive" in js
    assert "/restore" in js
    # fact Undo (reopen the same row)
    assert "/facts/${fact.id}/restore" in js
    # one-step add person (entity + facts together)
    assert "addPerson" in js


def test_memory_index_has_the_master_detail_skeleton() -> None:
    html = _index_html()
    assert "memory-left" in html          # the people list card
    assert "memory-detail" in html        # the person profile
    assert "memory-add-person" in html    # the add-person modal
    assert "memory-back" in html          # phone back button


def test_memory_js_renders_list_sections() -> None:
    js = _memory_js()
    # the left list groups by meaning: the household first, then everyone
    # else (relation unknown is flagged), then groups, then places & orgs
    for section in ("Household", "Other people", "Groups", "Places & orgs"):
        assert section in js
    assert "Relation unknown" in js


def test_memory_redesign_views_are_wired() -> None:
    """Profile (basic info vs memories), review inbox and family map."""
    html = _index_html()
    for node_id in ("memory-review", "memory-review-btn", "memory-review-count",
                    "memory-map", "memory-map-canvas", "memory-sync",
                    "memory-view-list", "memory-view-map"):
        assert f'id="{node_id}"' in html
    js = _memory_js()
    # the review inbox reads proposed facts and confirms them in place
    assert "/memory?status=proposed" in js
    assert "/memory/facts/${fact.id}/confirm" in js
    # basic info and memories are separate cards; connections are typed facts
    markers = (
        "basicInfoCard", "memoriesCard", "connectionsCard", 'CONNECTION_PREFIX = "connection:"',
    )
    for marker in markers:
        assert marker in js
    # the 5s poll becomes a quiet "Synced" status
    assert "Synced · listening to" in js


def test_memory_categories_match_the_prompt_block() -> None:
    """The screen's memory categories are the ones compact_block renders
    with their subject — a category added on one side only would silently
    drop out of the Ask prompt."""
    from brain.services.memory import CONNECTION_PREFIX, MEMORY_CATEGORIES

    js = _memory_js()
    assert 'const MEMORY_CATEGORIES = ["routine", "preference", "date", "note"];' in js
    assert MEMORY_CATEGORIES == {"routine", "preference", "date"}  # + 'note', rendered on its own
    assert f'const CONNECTION_PREFIX = "{CONNECTION_PREFIX}";' in js
