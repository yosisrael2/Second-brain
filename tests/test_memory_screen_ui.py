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
    assert "memory-detail" in html        # the person detail card
    assert "memory-add-person" in html    # the add-person modal
    assert "memory-back" in html          # phone back button


def test_memory_js_renders_list_sections() -> None:
    js = _memory_js()
    # the left list groups: Family first, then People / Groups / Places & orgs
    for section in ("Family", "People", "Groups", "Places"):
        assert section in js
