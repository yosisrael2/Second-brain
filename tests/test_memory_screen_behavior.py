"""Behavior tests for the Memory screen: the REAL served files executed in
jsdom (node), driven like a user — no string-presence assertions.

Skips when node/jsdom is unavailable (CI); runs fully where the redesign
toolchain is installed.
"""

from tests._jsdom_screen import assert_check, screen_behavior

# One kid with a grade fact; the review inbox empty.
FIXTURES = {
    "entities": [{
        "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
        "facts": [
            {"id": "f1", "subject_id": "e1", "predicate": "grade", "value": "ה",
             "object_id": None, "status": "confirmed", "provenance": {}},
            {"id": "f2", "subject_id": "e1", "predicate": "note",
             "value": "swims on Fridays", "object_id": None,
             "status": "confirmed", "provenance": {}},
        ],
    }],
    "proposed": [],
}


def test_composer_draft_survives_a_filter_chip_click() -> None:
    """Major 4: tapping a filter chip re-renders the detail and used to wipe
    the half-written memory (the composer's text lives only in the DOM)."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const composer = document.getElementById('memory-new');
      check('composer rendered', Boolean(composer));
      if (!composer) return;
      composer.value = 'draft that must survive';
      composer.dispatchEvent(new window.Event('input', { bubbles: true }));
      const chip = document.querySelector('.mm-filter');
      check('a filter chip is rendered', Boolean(chip));
      if (!chip) return;
      chip.click();
      await settle(150);
      const after = document.getElementById('memory-new');
      check('composer draft survives a filter-chip re-render',
        after && after.value === 'draft that must survive',
        `value=${JSON.stringify(after ? after.value : null)}`);
    """, FIXTURES)
    assert_check(payload, "composer rendered")
    assert_check(payload, "a filter chip is rendered")
    assert_check(payload, "composer draft survives a filter-chip re-render")


def test_composer_category_survives_a_filter_chip_click() -> None:
    """Major 4 (part 2): the composer's category choice is draft state too."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const composer = document.getElementById('memory-new');
      const category = document.querySelector('.mm-composer-cat');
      check('composer rendered', Boolean(composer) && Boolean(category));
      if (!composer || !category) return;
      composer.value = 'draft text';
      composer.dispatchEvent(new window.Event('input', { bubbles: true }));
      category.value = 'routine';
      category.dispatchEvent(new window.Event('change', { bubbles: true }));
      document.querySelector('.mm-filter').click();
      await settle(150);
      const after = document.querySelector('.mm-composer-cat');
      check('composer category survives a filter-chip re-render',
        after && after.value === 'routine',
        `category=${JSON.stringify(after ? after.value : null)}`);
    """, FIXTURES)
    assert_check(payload, "composer category survives a filter-chip re-render")


def test_screen_catches_up_after_typing_stops() -> None:
    """Major 5: dataKey is stamped BEFORE the busy check, so a poll that
    lands while the user types marks the payload as seen; the next poll
    (user idle) sees no change and never renders — the screen stays stale."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const composer = document.getElementById('memory-new');
      check('composer rendered', Boolean(composer));
      if (!composer) return;
      composer.focus();                    // memoryBusy() now true
      composer.value = 'typing…';
      composer.dispatchEvent(new window.Event('input', { bubbles: true }));
      // the poll lands NEW data while the user is typing
      fixtures.entities[0].facts.push({
        id: 'f2', subject_id: 'e1', predicate: 'note', value: 'fresh fact',
        object_id: null, status: 'confirmed', provenance: {},
      });
      await window.MemoryTab.refresh();    // skipped render, key stamped
      await settle(100);
      composer.blur();                     // the user stops typing
      await window.MemoryTab.refresh();    // must now render the fresh data
      await settle(150);
      check('screen catches up after typing stops',
        document.body.textContent.includes('fresh fact'),
        'the fresh fact never rendered');
    """, FIXTURES)
    assert_check(payload, "screen catches up after typing stops")


# --- Major 2: confirming a proposal must not wipe multi-valued fields --------

M2_FIXTURES = {
    "entities": [{
        "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
        "facts": [
            {"id": "f-ita", "subject_id": "e1", "predicate": "applies_to",
             "value": "Itamar", "object_id": None, "status": "confirmed",
             "provenance": {}},
        ],
    }],
    "proposed": [{
        "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
        "facts": [
            {"id": "f-rona", "subject_id": "e1", "predicate": "applies_to",
             "value": "Rona", "object_id": None, "status": "proposed",
             "provenance": {}},
        ],
    }],
}


def test_confirming_applies_to_keeps_the_other_links() -> None:
    """Major 2: confirming one applies_to proposal must not soft-delete the
    person's other applies_to links (multi-valued by design)."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.getElementById('memory-review-btn').click();
      await settle(150);
      const confirmBtn = [...document.querySelectorAll('#memory-review-list button')]
        .find((b) => b.textContent.trim() === 'Confirm');
      check('the proposal offers a confirm button', Boolean(confirmBtn));
      if (!confirmBtn) return;
      confirmBtn.click();
      await settle(250);
      const deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url);
      check('confirming applies_to does not delete the other link',
        !deletes.some((u) => u.includes('f-ita')),
        `deletes=${JSON.stringify(deletes)}`);
    """, M2_FIXTURES)
    assert_check(payload, "the proposal offers a confirm button")
    assert_check(payload, "confirming applies_to does not delete the other link")


M2B_FIXTURES = {
    "entities": [{
        "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
        "facts": [
            {"id": "f-old-bday", "subject_id": "e1", "predicate": "birthday",
             "value": "1.1.2010", "object_id": None, "status": "confirmed",
             "provenance": {}},
        ],
    }],
    "proposed": [{
        "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
        "facts": [
            {"id": "f-new-bday", "subject_id": "e1", "predicate": "birthday",
             "value": "2.2.2011", "object_id": None, "status": "proposed",
             "provenance": {}},
        ],
    }],
}


def test_confirming_a_new_birthday_supersedes_with_undo() -> None:
    """Major 2 (part 2): for single-valued basic fields the replaced value is
    closed (supersede) — and its Undo must be offered, not silent."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.getElementById('memory-review-btn').click();
      await settle(150);
      const confirmBtn = [...document.querySelectorAll('#memory-review-list button')]
        .find((b) => b.textContent.trim() === 'Confirm');
      check('the proposal offers a confirm button', Boolean(confirmBtn));
      if (!confirmBtn) return;
      confirmBtn.click();
      await settle(300);
      const deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url);
      check('the old birthday is superseded',
        deletes.some((u) => u.includes('f-old-bday')),
        `deletes=${JSON.stringify(deletes)}`);
      const toast = document.getElementById('memory-undo');
      check('the supersede offers an Undo', toast && !toast.hidden,
        `hidden=${toast ? toast.hidden : 'no toast element'}`);
      if (toast && !toast.hidden) {
        document.getElementById('memory-undo-btn').click();
        await settle(250);
        const restores = calls.filter((c) => c.url.includes('/restore')).map((c) => c.url);
        check('Undo restores the replaced birthday',
          restores.some((u) => u.includes('f-old-bday')),
          `restores=${JSON.stringify(restores)}`);
      }
    """, M2B_FIXTURES)
    assert_check(payload, "the proposal offers a confirm button")
    assert_check(payload, "the old birthday is superseded")
    assert_check(payload, "the supersede offers an Undo")
    assert_check(payload, "Undo restores the replaced birthday")


# --- Major 3: move-to-basic-info ---------------------------------------------

M3_FIXTURES = {
    "entities": [
        {"id": "e1", "kind": "person", "name": "Noa", "aliases": [], "facts": [
            {"id": "f-note", "subject_id": "e1", "predicate": "note",
             "value": "Birthday party on 3.5 for Rona", "object_id": None,
             "status": "confirmed", "provenance": {}},
            {"id": "f-bday", "subject_id": "e1", "predicate": "birthday",
             "value": "1.1.2010", "object_id": None, "status": "confirmed",
             "provenance": {}},
        ]},
        {"id": "e2", "kind": "person", "name": "Rona", "aliases": [], "facts": []},
    ],
    "proposed": [],
}


def test_move_form_checkboxes_default_to_unchecked() -> None:
    """Major 3: the 'also on <other>' checkboxes wrote relation/work/phone
    onto other people silently — they must default to unchecked."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const menu = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '···');
      check('the memory card has an actions menu', Boolean(menu));
      if (!menu) return;
      menu.click();
      await settle(100);
      const promote = [...document.querySelectorAll('button')]
        .find((b) => b.textContent === 'Move to basic info');
      check('the menu offers move-to-basic-info', Boolean(promote));
      if (!promote) return;
      promote.click();
      await settle(150);
      const boxes = [...document.querySelectorAll('.mm-promote-form input[type=checkbox]')];
      check('extra-subject checkboxes default to unchecked',
        boxes.length === 0 || boxes.every((b) => !b.checked),
        `checked=${JSON.stringify(boxes.map((b) => b.checked))}`);
    """, M3_FIXTURES)
    assert_check(payload, "the memory card has an actions menu")
    assert_check(payload, "the menu offers move-to-basic-info")
    assert_check(payload, "extra-subject checkboxes default to unchecked")


def test_move_to_basic_info_patches_an_existing_value() -> None:
    """Major 3 (part 2): when the profile already carries a fact for the
    chosen field, the move must PATCH that row (edit = new version), not
    POST a second parallel value."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const menu = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '···');
      if (!menu) return;
      menu.click();
      await settle(100);
      const promote = [...document.querySelectorAll('button')]
        .find((b) => b.textContent === 'Move to basic info');
      if (!promote) return;
      promote.click();
      await settle(150);
      const form = document.querySelector('.mm-promote-form');
      check('the move form opened', Boolean(form));
      if (!form) return;
      const field = form.querySelector('select');
      field.value = 'birthday';
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Move');
      save.click();
      await settle(300);
      const posts = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      const patches = calls.filter((c) => c.method === 'PATCH');
      check('the move PATCHes the existing birthday instead of POSTing a second one',
        posts.length === 0, `posts=${JSON.stringify(posts.map((c) => c.body))}`);
    """, M3_FIXTURES)
    assert_check(payload, "the move form opened")
    assert_check(payload, "the move PATCHes the existing birthday instead of POSTing a second one")


# --- Undo stack: a second delete must not erase the first delete's Undo ------

def test_undo_stack_restores_both_deleted_memories() -> None:
    """A second delete replaced the first delete's undoFn (single slot), so
    undoing two quick deletes lost the first memory. The stack keeps both:
    Undo pops the latest, then the previous."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const del = async (needle) => {
        const card = [...document.querySelectorAll('.mm-mem')]
          .find((c) => c.textContent.includes(needle));
        check(`card found for ${needle}`, Boolean(card));
        if (!card) return;
        const menu = [...card.querySelectorAll('button')]
          .find((b) => b.textContent.trim() === '···');
        check(`menu found for ${needle}`, Boolean(menu));
        if (!menu) return;
        menu.click();
        await settle(100);
        // renderDetail rebuilt the DOM — re-query the card
        const freshCard = [...document.querySelectorAll('.mm-mem')]
          .find((c) => c.textContent.includes(needle));
        const delBtn = freshCard
          ? [...freshCard.querySelectorAll('button')].find((b) => b.textContent === 'Delete')
          : null;
        check(`delete button found for ${needle}`, Boolean(delBtn));
        if (!delBtn) return;
        delBtn.click();                    // arm
        delBtn.click();                    // confirm
        await settle(150);
      };
      await del('first memory');
      await del('second memory');
      check('the undo toast is visible after two deletes',
        document.getElementById('memory-undo') && !document.getElementById('memory-undo').hidden);
      document.getElementById('memory-undo-btn').click();
      await settle(250);
      const firstRestores = calls.filter((c) => c.url.includes('/restore')).map((c) => c.url);
      check('the first Undo restores the latest delete',
        firstRestores.some((u) => u.includes('f-note-2')),
        `restores=${JSON.stringify(firstRestores)}`);
      check('the toast stays for the earlier delete',
        document.getElementById('memory-undo') && !document.getElementById('memory-undo').hidden);
      document.getElementById('memory-undo-btn').click();
      await settle(150);
      const allRestores = calls.filter((c) => c.url.includes('/restore')).map((c) => c.url);
      check('the second Undo restores the earlier delete',
        allRestores.some((u) => u.includes('f-note-1')),
        `restores=${JSON.stringify(allRestores)}`);
    """, {
        "entities": [{
            "id": "e1", "kind": "person", "name": "Noa", "aliases": [],
            "facts": [
                {"id": "f-note-1", "subject_id": "e1", "predicate": "note",
                 "value": "first memory", "object_id": None, "status": "confirmed",
                 "provenance": {}},
                {"id": "f-note-2", "subject_id": "e1", "predicate": "note",
                 "value": "second memory", "object_id": None, "status": "confirmed",
                 "provenance": {}},
            ],
        }],
        "proposed": [],
    })
    for name in ("card found for first memory", "delete button found for first memory",
                 "card found for second memory", "delete button found for second memory",
                 "the undo toast is visible after two deletes",
                 "the first Undo restores the latest delete",
                 "the toast stays for the earlier delete",
                 "the second Undo restores the earlier delete"):
        assert_check(payload, name)


def test_connection_form_other_type_wording() -> None:
    """'other' rendered as 'Noa is the connected of Rona' — the wording must
    read 'Noa is connected to Rona'."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const addConn = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.includes('Add connection'));
      check('the add-connection affordance exists', Boolean(addConn));
      if (!addConn) return;
      addConn.click();
      await settle(150);
      const form = document.querySelector('.mm-conn-form');
      check('the connection form opened', Boolean(form));
      if (!form) return;
      const typeSelect = form.querySelector('select[aria-label="connection type"]');
      typeSelect.value = 'other';
      typeSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(100);
      const lead = form.querySelector('.mm-form-lead');
      check('the other type reads "is connected to"',
        lead && /is connected to$/i.test(lead.textContent.trim()),
        `lead=${JSON.stringify(lead ? lead.textContent : null)}`);
    """, {
        "entities": [
            {"id": "e1", "kind": "person", "name": "Noa", "aliases": [], "facts": []},
            {"id": "e2", "kind": "person", "name": "Rona", "aliases": [], "facts": []},
        ],
        "proposed": [],
    })
    assert_check(payload, "the add-connection affordance exists")
    assert_check(payload, "the connection form opened")
    assert_check(payload, "the other type reads \"is connected to\"")
