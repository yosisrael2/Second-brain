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
    """Major 2 (part 2, server-owned rule): for single-valued basic fields
    the SERVER supersedes — its response carries the closed row as
    `replaced` and the client offers an Undo from that, deleting nothing
    itself. The Undo is ONE op (POST /undo-supersede): the replaced row is
    closed history, not deleted, so /restore would 409 it."""
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
      check('the client does not delete the old birthday itself',
        !deletes.some((u) => u.includes('f-old-bday')),
        `deletes=${JSON.stringify(deletes)}`);
      const toast = document.getElementById('memory-undo');
      check('the supersede offers an Undo', toast && !toast.hidden,
        `hidden=${toast ? toast.hidden : 'no toast element'}`);
      if (!toast || toast.hidden) return;
      document.getElementById('memory-undo-btn').click();
      await settle(400);
      const undoCalls = calls.filter((c) => c.method === 'POST'
        && c.url.includes('/undo-supersede'));
      const callLog = JSON.stringify(calls.filter((c) => c.method !== 'GET')
        .map((c) => [c.method, c.url, c.status]));
      check('the Undo is one undo-supersede call on the confirmed row',
        undoCalls.length === 1 && undoCalls[0].url.includes('f-new-bday')
        && undoCalls[0].status === 200,
        `undoCalls=${callLog}`);
      const conflicts = calls.filter((c) => c.status === 409);
      check('the whole confirm+undo round-trip has no 409s', conflicts.length === 0,
        `conflicts=${JSON.stringify(conflicts.map((c) => [c.method, c.url, c.status]))}`);
    """, M2B_FIXTURES)
    assert_check(payload, "the proposal offers a confirm button")
    assert_check(payload, "the client does not delete the old birthday itself")
    assert_check(payload, "the supersede offers an Undo")
    assert_check(payload, "the Undo is one undo-supersede call on the confirmed row")
    assert_check(payload, "the whole confirm+undo round-trip has no 409s")


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
      check('the move PATCH carries the new value',
        patches.length === 1 && patches[0].body && patches[0].body.value === '3.5',
        `patches=${JSON.stringify(patches)}`);
    """, M3_FIXTURES)
    assert_check(payload, "the move form opened")
    assert_check(payload, "the move PATCHes the existing birthday instead of POSTing a second one")
    assert_check(payload, "the move PATCH carries the new value")


def test_move_deletes_the_memory_after_the_write_succeeds() -> None:
    """B1 (PR #136 review): Move-to-basic-info is a MOVE, not a copy — after
    the basic-info write succeeds the source memory must be soft-DELETEd,
    or the Undo (a /restore) would 409 on a fact that was never deleted."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const card = [...document.querySelectorAll('.mm-mem')]
        .find((c) => c.textContent.includes('Birthday party'));
      check('the memory card is rendered', Boolean(card));
      if (!card) return;
      const menu = [...card.querySelectorAll('button')]
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
      if (!form) return;
      const field = form.querySelector('select');
      field.value = 'birthday';
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Move');
      save.click();
      await settle(400);
      const deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url);
      const patches = calls.filter((c) => c.method === 'PATCH').map((c) => c.url);
      check('the memory is deleted after the basic-info write (move, not copy)',
        deletes.some((u) => u.includes('f-note')),
        `deletes=${JSON.stringify(deletes)} patches=${JSON.stringify(patches)}`);
    """,
        M3_FIXTURES,
    )
    assert_check(payload, "the memory card is rendered")
    assert_check(payload, "the memory is deleted after the basic-info write (move, not copy)")


def test_move_undo_restores_overwritten_value_via_the_new_row() -> None:
    """B2 (PR #136 review): patch_fact closes the old row and opens a NEW id,
    so the Undo of a PATCHed move must retarget the row the server CREATED —
    patching the old id again 409s ('cannot edit a closed fact'). Also covers
    the overwrite leg: ticking 'also on Rona' over her EXISTING birthday must
    stay undoable, restoring what she had."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const card = [...document.querySelectorAll('.mm-mem')]
        .find((c) => c.textContent.includes('Birthday party'));
      if (!card) return;
      const menu = [...card.querySelectorAll('button')]
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
      if (!form) return;
      const field = form.querySelector('select');
      field.value = 'birthday';
      field.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(80);
      const box = form.querySelector('input[type=checkbox]');
      check('the also-on-Rona checkbox is offered for a shared birthday', Boolean(box));
      if (!box) return;
      box.click();
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Move');
      save.click();
      await settle(400);
      const undoBtn = document.getElementById('memory-undo-btn');
      const toastShown = undoBtn && !document.getElementById('memory-undo').hidden;
      check('the move offers an Undo', Boolean(toastShown));
      if (!undoBtn) return;
      undoBtn.click();
      await settle(400);
      // The PATCH response created new-N for Rona's closed f-bday-2; the undo
      // must PATCH new-N back to her old value. Patching f-bday-2 would be a
      // 409 against the honest stub (closed rows refuse edits).
      const undoPatches = calls.filter((c) => c.method === 'PATCH' && /new-\\d+$/.test(c.url));
      check('the Undo patches the NEW row id back to the previous value',
        undoPatches.length === 1 && undoPatches[0].body && undoPatches[0].body.value === '2.2.2011',
        `undoPatches=${JSON.stringify(undoPatches)}`);
      const conflicts = calls.filter((c) => c.status === 409);
      const conflictLog = JSON.stringify(conflicts.map((c) => [c.method, c.url, c.status]));
      check('no request hit a closed row (no 409s during move+undo)',
        conflicts.length === 0, `conflicts=${conflictLog}`);
    """,
        M3_OVERWRITE_FIXTURES,
    )
    assert_check(payload, "the also-on-Rona checkbox is offered for a shared birthday")
    assert_check(payload, "the move offers an Undo")
    assert_check(payload, "the Undo patches the NEW row id back to the previous value")
    assert_check(payload, "no request hit a closed row (no 409s during move+undo)")


M3_OVERWRITE_FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [
                {
                    "id": "f-note",
                    "subject_id": "e1",
                    "predicate": "note",
                    "value": "Birthday party on 3.5 for Rona",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                },
            ],
        },
        {
            "id": "e2",
            "kind": "person",
            "name": "Rona",
            "aliases": [],
            "facts": [
                {
                    "id": "f-bday-2",
                    "subject_id": "e2",
                    "predicate": "birthday",
                    "value": "2.2.2011",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                },
            ],
        },
    ],
    "proposed": [],
}


def test_move_failure_rolls_back_the_writes() -> None:
    """M2 (PR #136 review): the move claims to be atomic — on a per-subject
    error the values already written to OTHER subjects must be rolled back
    (created rows deleted, patched rows restored), and the memory stays."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const card = [...document.querySelectorAll('.mm-mem')]
        .find((c) => c.textContent.includes('Birthday party'));
      if (!card) return;
      const menu = [...card.querySelectorAll('button')]
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
      if (!form) return;
      const field = form.querySelector('select');
      field.value = 'birthday';
      field.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(80);
      const box = form.querySelector('input[type=checkbox]');
      if (!box) return;
      box.click();
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Move');
      save.click();
      await settle(400);
      // Noa (no birthday) writes first -> POST new-1; Rona's stale closed row
      // (valid_to set in the fixture) makes the PATCH 409 like the real server.
      const created = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      check('the write to Noa went through first',
        created.some((c) => c.body && c.body.subject_id === 'e1' && c.status === 201),
        `posts=${JSON.stringify(created)}`);
      const rolledBack = calls.filter((c) => c.method === 'DELETE'
        && /^\\/memory\\/facts\\/new-\\d+$/.test(c.url));
      check('the created value is rolled back after the later subject fails',
        rolledBack.length === 1, `rollbacks=${JSON.stringify(rolledBack)}`);
      const memoryDeleted = calls.some((c) => c.method === 'DELETE' && c.url.includes('f-note'));
      check('the source memory survives a failed move', !memoryDeleted);
    """,
        M3_STALE_FIXTURES,
    )
    assert_check(payload, "the write to Noa went through first")
    assert_check(payload, "the created value is rolled back after the later subject fails")
    assert_check(payload, "the source memory survives a failed move")


M3_STALE_FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [
                {
                    "id": "f-note",
                    "subject_id": "e1",
                    "predicate": "note",
                    "value": "Birthday party on 3.5 for Rona",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                },
            ],
        },
        # Rona's birthday row is STALE on the client: valid_to is set, so the
        # honest stub refuses to PATCH it (409) exactly like the real backend.
        {
            "id": "e2",
            "kind": "person",
            "name": "Rona",
            "aliases": [],
            "facts": [
                {
                    "id": "f-bday-stale",
                    "subject_id": "e2",
                    "predicate": "birthday",
                    "value": "2.2.2011",
                    "object_id": None,
                    "status": "confirmed",
                    "valid_to": "2020-01-01T00:00:00Z",
                    "provenance": {},
                },
            ],
        },
    ],
    "proposed": [],
}


def test_move_checkbox_unchecks_when_its_field_hides_it() -> None:
    """Minor b (PR #136 review): ticking 'also on Rona' for a birthday and
    then switching the field (birthday -> work) hides the checkbox but left
    it CHECKED — the move silently wrote onto Rona. Hiding must uncheck."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const card = [...document.querySelectorAll('.mm-mem')]
        .find((c) => c.textContent.includes('Birthday party'));
      if (!card) return;
      const menu = [...card.querySelectorAll('button')]
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
      if (!form) return;
      const field = form.querySelector('select');
      field.value = 'birthday';
      field.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(80);
      const box = form.querySelector('input[type=checkbox]');
      if (!box) return;
      box.click();
      check('the checkbox is checked while its field is selected', box.checked);
      field.value = 'work';
      field.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(80);
      check('switching the field unchecks the hidden checkbox', !box.checked,
        `checked=${box.checked}`);
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Move');
      save.click();
      await settle(400);
      const toRona = calls.filter((c) => c.body && c.body.subject_id === 'e2');
      const ronaPatches = calls.filter((c) => c.method === 'PATCH' && c.url.includes('f-bday-2'));
      check('nothing was written to Rona after the field changed',
        toRona.length === 0 && ronaPatches.length === 0,
        `toRona=${JSON.stringify(toRona)} ronaPatches=${JSON.stringify(ronaPatches)}`);
    """,
        M3_OVERWRITE_FIXTURES,
    )
    assert_check(payload, "the checkbox is checked while its field is selected")
    assert_check(payload, "switching the field unchecks the hidden checkbox")
    assert_check(payload, "nothing was written to Rona after the field changed")


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


def _conn_fixture(noah_facts, rona_facts):
    """Two people (Noa e1, Rona e2) with the given fact lists."""
    return {
        "entities": [
            {"id": "e1", "kind": "person", "name": "Noa", "aliases": [], "facts": noah_facts},
            {"id": "e2", "kind": "person", "name": "Rona", "aliases": [], "facts": rona_facts},
        ],
        "proposed": [],
    }


def _conn_fact(fid, subject, target, predicate="connection:parent"):
    return {
        "id": fid,
        "subject_id": subject,
        "predicate": predicate,
        "object_id": target,
        "value": None,
        "status": "confirmed",
        "provenance": {},
    }


def test_connection_form_blocks_the_reverse_stored_duplicate() -> None:
    """Minor d (PR #136 review): the duplicate guard only looked at links
    STORED on this profile. The same relation stored on the other side
    (Rona's 'parent' fact pointing at Noa) IS the duplicate of adding 'Noa
    is the child of Rona' — it must be blocked, not stacked twice."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const profileBtn = document.querySelector('.mm-row[data-entity="e1"]');
      check('the profile opens', Boolean(profileBtn));
      if (!profileBtn) return;
      profileBtn.click();
      await settle(200);
      const add = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '+ Add connection');
      check('the add-connection form opens', Boolean(add));
      if (!add) return;
      add.click();
      await settle(150);
      const form = document.querySelector('.mm-conn-form');
      if (!form) return;
      form.querySelector('select[aria-label="connection type"]').value = 'child';
      form.querySelector('select[aria-label="connected to"]').value = 'e2';
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Save');
      save.click();
      await settle(300);
      const posted = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      check('the reverse-stored duplicate is blocked (no POST)',
        posted.length === 0, `posts=${JSON.stringify(posted.map((c) => c.body))}`);
      const toastText = document.getElementById('toast').textContent;
      check('the block explains the duplicate', toastText.includes('already connected'),
        `toast=${toastText}`);
    """,
        _conn_fixture([], [_conn_fact("f-rona-parent", "e2", "e1")]),
    )
    assert_check(payload, "the profile opens")
    assert_check(payload, "the add-connection form opens")
    assert_check(payload, "the reverse-stored duplicate is blocked (no POST)")
    assert_check(payload, "the block explains the duplicate")


def test_connection_form_does_not_call_a_contradiction_a_duplicate() -> None:
    """Minor d (PR #136 review): an existing 'Noa is the parent of Rona'
    link and a new 'Noa is the child of Rona' CONTRADICT each other — they
    are not the same fact, so the guard must not block it as a duplicate."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const profileBtn = document.querySelector('.mm-row[data-entity="e1"]');
      if (!profileBtn) return;
      profileBtn.click();
      await settle(200);
      const add = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '+ Add connection');
      if (!add) return;
      add.click();
      await settle(150);
      const form = document.querySelector('.mm-conn-form');
      if (!form) return;
      form.querySelector('select[aria-label="connection type"]').value = 'child';
      form.querySelector('select[aria-label="connected to"]').value = 'e2';
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Save');
      save.click();
      await settle(300);
      const toastText = document.getElementById('toast').textContent;
      const posted = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      check('a contradictory link is not blocked as a duplicate',
        !toastText.includes('already connected') && posted.length === 1,
        `toast=${toastText} posts=${JSON.stringify(posted.map((c) => c.body))}`);
    """,
        _conn_fixture([_conn_fact("f-noa-parent", "e1", "e2")], []),
    )
    assert_check(payload, "a contradictory link is not blocked as a duplicate")


def test_connection_form_derived_duplicate_does_not_say_edit_or_delete() -> None:
    """Minor d (PR #136 review): a derived parent link (from the Role field,
    fact: null) cannot be edited or deleted — the duplicate warning must
    not tell the user to 'edit or delete' something that has no backing row."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const profileBtn = document.querySelector('.mm-row[data-entity="e1"]');
      if (!profileBtn) return;
      profileBtn.click();
      await settle(200);
      const add = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '+ Add connection');
      if (!add) return;
      add.click();
      await settle(150);
      const form = document.querySelector('.mm-conn-form');
      if (!form) return;
      form.querySelector('select[aria-label="connection type"]').value = 'parent';
      form.querySelector('select[aria-label="connected to"]').value = 'e2';
      const save = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Save');
      save.click();
      await settle(300);
      const toastText = document.getElementById('toast').textContent;
      const posted = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      check('the derived link is still blocked (no POST)', posted.length === 0,
        `posts=${JSON.stringify(posted.map((c) => c.body))}`);
      check('the derived-duplicate warning does not say edit or delete',
        toastText.includes('already connected') && !toastText.includes('edit or delete'),
        `toast=${toastText}`);
    """,
        _conn_fixture(
            [
                {
                    "id": "f-role",
                    "subject_id": "e1",
                    "predicate": "relation",
                    "value": "mother",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                }
            ],
            [
                {
                    "id": "f-grade",
                    "subject_id": "e2",
                    "predicate": "grade",
                    "value": "4",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                }
            ],
        ),
    )
    assert_check(payload, "the derived link is still blocked (no POST)")
    assert_check(payload, "the derived-duplicate warning does not say edit or delete")


RENAME_FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [],
        }
    ],
    "proposed": [],
}


def test_rename_fires_one_patch_not_two() -> None:
    """Enter AND change both reach the save handler — the guard must let
    exactly ONE PATCH through (the pre-fix double PATCH renamed twice)."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('.mm-profile-actions .btn').click();
      await settle(150);
      const input = document.querySelector('.mm-rename');
      check('edit mode shows the rename input', Boolean(input));
      if (!input) return;
      input.value = 'Noa Levi';
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
      input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await settle(120);
      input.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(250);
      const patches = calls.filter((c) => c.method === 'PATCH' && c.url.includes('/entities/e1'));
      check('renaming fires exactly one PATCH', patches.length === 1,
        `patches=${JSON.stringify(patches.map((p) => p.url))}`);
      check('the PATCH carries the new name',
        patches.length === 1 && patches[0].body && patches[0].body.name === 'Noa Levi',
        `body=${JSON.stringify(patches.map((p) => p.body))}`);
    """,
        RENAME_FIXTURES,
    )
    assert_check(payload, "edit mode shows the rename input")
    assert_check(payload, "renaming fires exactly one PATCH")
    assert_check(payload, "the PATCH carries the new name")


def test_drafts_survive_a_poll_on_the_detail_forms() -> None:
    """The basic-info editor, the alias form and the memory-edit form keep
    their typed text when a poll re-renders after blur (mmTrackDraft)."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('.mm-row[data-entity="e1"]').click();
      await settle(200);
      // 1. basic-info field editor
      document.querySelector('button[aria-label="edit Work"]').click();
      await settle(120);
      let field = document.querySelector('.mm-form input[aria-label="Work"]');
      check('the work editor opened', Boolean(field));
      if (field) {
        field.focus();
        field.value = 'Safeguarding engineer';
        field.dispatchEvent(new window.Event('input', { bubbles: true }));
        field.blur();
        fixtures.entities[0].facts.push({
          id: 'f-fresh', subject_id: 'e1', predicate: 'note', value: 'fresh note',
          object_id: null, status: 'confirmed', provenance: {},
        });
        await window.MemoryTab.refresh();
        await settle(150);
        field = document.querySelector('.mm-form input[aria-label="Work"]');
        check('the work draft survives the poll',
          Boolean(field) && field.value === 'Safeguarding engineer',
          `value=${field ? field.value : 'form closed'}`);
      }
      // 2. alias form
      document.querySelector('button[aria-label="add alias"]').click();
      await settle(120);
      let alias = document.querySelector('input[aria-label="alias"]');
      check('the alias form opened', Boolean(alias));
      if (alias) {
        alias.focus();
        alias.value = 'נועלי';
        alias.dispatchEvent(new window.Event('input', { bubbles: true }));
        alias.blur();
        fixtures.entities[0].facts.push({
          id: 'f-fresh-2', subject_id: 'e1', predicate: 'note', value: 'another fresh note',
          object_id: null, status: 'confirmed', provenance: {},
        });
        await window.MemoryTab.refresh();
        await settle(150);
        check('the alias-poll note rendered (the poll really re-rendered)',
          document.body.textContent.includes('another fresh note'),
          `found=${document.body.textContent.includes('another fresh note')}`);
        alias = document.querySelector('input[aria-label="alias"]');
        check('the alias draft survives the poll',
          Boolean(alias) && alias.value === 'נועלי',
          `value=${alias ? alias.value : 'form closed'}`);
      }
      // 3. memory edit form
      const menu = document.querySelector('.mm-mem .mm-more-btn');
      menu.click();
      await settle(120);
      const edit = [...document.querySelectorAll('.mm-mem-actions button')]
        .find((b) => b.textContent === 'Edit');
      check('the memory edit form opens from the menu', Boolean(edit));
      if (edit) {
        edit.click();
        await settle(120);
        const area = document.querySelector('textarea[aria-label="memory text"]');
        check('the memory textarea opened', Boolean(area));
        if (area) {
          area.focus();
          area.value = 'rewritten memory text';
          area.dispatchEvent(new window.Event('input', { bubbles: true }));
          area.blur();
          fixtures.entities[0].facts.push({
            id: 'f-fresh-3', subject_id: 'e1', predicate: 'routine', value: 'third fresh note',
            object_id: null, status: 'confirmed', provenance: {},
          });
          await window.MemoryTab.refresh();
          await settle(150);
          check('the memedit-poll note rendered (the poll really re-rendered)',
            document.body.textContent.includes('third fresh note'),
            `found=${document.body.textContent.includes('third fresh note')}`);
          const after = document.querySelector('textarea[aria-label="memory text"]');
          check('the memory-edit draft survives the poll',
            Boolean(after) && after.value === 'rewritten memory text',
            `value=${after ? after.value : 'form closed'}`);
        }
      }
    """,
        {
            "entities": [
                {
                    "id": "e1",
                    "kind": "person",
                    "name": "Noa",
                    "aliases": [],
                    "facts": [
                        {
                            "id": "f-work",
                            "subject_id": "e1",
                            "predicate": "work",
                            "value": "Chef",
                            "object_id": None,
                            "status": "confirmed",
                            "provenance": {},
                        },
                        {
                            "id": "f-note",
                            "subject_id": "e1",
                            "predicate": "note",
                            "value": "original memory",
                            "object_id": None,
                            "status": "confirmed",
                            "provenance": {},
                        },
                    ],
                }
            ],
            "proposed": [],
        },
    )
    assert_check(payload, "the work editor opened")
    assert_check(payload, "the work draft survives the poll")
    assert_check(payload, "the alias form opened")
    assert_check(payload, "the alias-poll note rendered (the poll really re-rendered)")
    assert_check(payload, "the alias draft survives the poll")
    assert_check(payload, "the memory edit form opens from the menu")
    assert_check(payload, "the memory textarea opened")
    assert_check(payload, "the memedit-poll note rendered (the poll really re-rendered)")
    assert_check(payload, "the memory-edit draft survives the poll")


def test_drafts_survive_a_poll_on_connection_review_and_promote() -> None:
    """The connection form's selects, the review 'edit first' field and the
    promote value keep their state across a poll re-render after blur."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('.mm-row[data-entity="e1"]').click();
      await settle(200);
      // 1. connection form (two selects)
      const addConn = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === '+ Add connection');
      addConn.click();
      await settle(120);
      let form = document.querySelector('.mm-conn-form');
      check('the connection form opened', Boolean(form));
      if (form) {
        const type = form.querySelector('select[aria-label="connection type"]');
        type.focus();
        type.value = 'friend';
        type.dispatchEvent(new window.Event('change', { bubbles: true }));
        type.blur();   // the user moves on; a synthetic focusout does not blur
        fixtures.entities[0].facts.push({
          id: 'f-fresh', subject_id: 'e1', predicate: 'note', value: 'fresh note',
          object_id: null, status: 'confirmed', provenance: {},
        });
        await window.MemoryTab.refresh();
        await settle(150);
        check('the conn-poll note rendered (the poll really re-rendered)',
          document.body.textContent.includes('fresh note'),
          `found=${document.body.textContent.includes('fresh note')}`);
        form = document.querySelector('.mm-conn-form');
        const type2 = form && form.querySelector('select[aria-label="connection type"]');
        check('the connection type choice survives the poll',
          Boolean(type2) && type2.value === 'friend',
          `value=${type2 ? type2.value : 'form closed'}`);
      }
      // 2. review 'edit first'
      document.getElementById('memory-review-btn').click();
      await settle(150);
      const editFirst = [...document.querySelectorAll('#memory-review-list button')]
        .find((b) => b.textContent.trim() === 'Edit first');
      check('the review edit opens', Boolean(editFirst));
      if (editFirst) {
        editFirst.click();
        await settle(120);
        const corrected = document.querySelector('input[aria-label="corrected value"]');
        check('the corrected-value field opened', Boolean(corrected));
        if (corrected) {
          corrected.focus();
          corrected.value = '3.3.2012';
          corrected.dispatchEvent(new window.Event('input', { bubbles: true }));
          corrected.blur();
          fixtures.proposed[0].facts.push({
            id: 'f-prop-2', subject_id: 'e1', predicate: 'note', value: 'review-poll note',
            object_id: null, status: 'proposed', provenance: {},
          });
          await window.MemoryTab.refresh();
          await settle(150);
          check('the review-poll note rendered (the poll really re-rendered)',
            document.body.textContent.includes('review-poll note'),
            `found=${document.body.textContent.includes('review-poll note')}`);
          const after = document.querySelector('input[aria-label="corrected value"]');
          check('the review draft survives the poll',
            Boolean(after) && after.value === '3.3.2012',
            `value=${after ? after.value : 'form closed'}`);
        }
      }
      // 3. promote value
      document.getElementById('memory-review-btn').click();
      await settle(150);
      document.querySelector('.mm-row[data-entity="e1"]').click();
      await settle(200);
      const menu = document.querySelector('.mm-mem .mm-more-btn');
      menu.click();
      await settle(120);
      const promote = [...document.querySelectorAll('button')]
        .find((b) => b.textContent === 'Move to basic info');
      check('the promote form opens from the menu', Boolean(promote));
      if (promote) {
        promote.click();
        await settle(120);
        const value = document.querySelector('.mm-promote-form input[aria-label="value"]');
        check('the promote value opened', Boolean(value));
        if (value) {
          value.focus();
          value.value = '9.9.2015';
          value.dispatchEvent(new window.Event('input', { bubbles: true }));
          value.blur();
          fixtures.entities[0].facts.push({
            id: 'f-fresh-p', subject_id: 'e1', predicate: 'routine', value: 'promote-poll note',
            object_id: null, status: 'confirmed', provenance: {},
          });
          await window.MemoryTab.refresh();
          await settle(150);
          check('the promote-poll note rendered (the poll really re-rendered)',
            document.body.textContent.includes('promote-poll note'),
            `found=${document.body.textContent.includes('promote-poll note')}`);
          const after = document.querySelector('.mm-promote-form input[aria-label="value"]');
          check('the promote draft survives the poll',
            Boolean(after) && after.value === '9.9.2015',
            `value=${after ? after.value : 'form closed'}`);
        }
      }
    """,
        {
            "entities": [
                {
                    "id": "e1",
                    "kind": "person",
                    "name": "Noa",
                    "aliases": [],
                    "facts": [
                        {
                            "id": "f-note",
                            "subject_id": "e1",
                            "predicate": "note",
                            "value": "swims on Fridays",
                            "object_id": None,
                            "status": "confirmed",
                            "provenance": {},
                        },
                    ],
                }
            ],
            "proposed": [
                {
                    "id": "e1",
                    "kind": "person",
                    "name": "Noa",
                    "aliases": [],
                    "facts": [
                        {
                            "id": "f-prop-bday",
                            "subject_id": "e1",
                            "predicate": "birthday",
                            "value": "3.3.2011",
                            "object_id": None,
                            "status": "proposed",
                            "provenance": {},
                        },
                    ],
                }
            ],
        },
    )
    assert_check(payload, "the connection form opened")
    assert_check(payload, "the conn-poll note rendered (the poll really re-rendered)")
    assert_check(payload, "the connection type choice survives the poll")
    assert_check(payload, "the review edit opens")
    assert_check(payload, "the corrected-value field opened")
    assert_check(payload, "the review-poll note rendered (the poll really re-rendered)")
    assert_check(payload, "the review draft survives the poll")
    assert_check(payload, "the promote form opens from the menu")
    assert_check(payload, "the promote value opened")
    assert_check(payload, "the promote-poll note rendered (the poll really re-rendered)")
    assert_check(payload, "the promote draft survives the poll")


# --- Photo avatars (A1): img vs initials, onerror fallback, upload/remove -----

AVATAR_FIXTURES = {
    "entities": [
        {"id": "e-photo", "kind": "person", "name": "Noa", "aliases": [], "facts": [
            {"id": "f-photo", "subject_id": "e-photo", "predicate": "photo",
             "value": "abc123", "object_id": None, "status": "confirmed",
             "provenance": {"sha256": "abc123", "mime": "image/png"}},
        ]},
        {"id": "e-plain", "kind": "person", "name": "Rona", "aliases": [], "facts": []},
        {"id": "e-group", "kind": "channel", "name": "3rd grade parents",
         "aliases": [], "facts": []},
    ],
    "proposed": [],
}


def test_avatar_renders_the_photo_and_falls_back_on_load_error() -> None:
    """avatar() renders an <img> (alt = name) when the entity carries a
    current photo fact, initials otherwise; a failed image load falls back
    to the initials span."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const personWith = document.querySelector(
        `[data-entity="e-photo"] .mm-av-photo`);
      check('the person with a photo renders an img avatar',
        Boolean(personWith) && personWith.tagName === 'IMG',
        personWith ? personWith.outerHTML.slice(0, 120) : 'no img found');
      if (personWith) {
        check('the img points at the photo endpoint with ?v=',
          personWith.getAttribute('src') === '/memory/entities/e-photo/photo?v=abc123',
          `src=${personWith.getAttribute('src')}`);
        check('the img is lazy with alt = name',
          personWith.getAttribute('loading') === 'lazy'
          && personWith.getAttribute('alt') === 'Noa',
          `loading=${personWith.getAttribute('loading')} alt=${personWith.getAttribute('alt')}`);
      }
      const personWithout = document.querySelector(
        `[data-entity="e-plain"] .mm-av`);
      check('the person without a photo keeps the initials avatar',
        Boolean(personWithout) && personWithout.tagName === 'SPAN',
        personWithout ? personWithout.outerHTML.slice(0, 120) : 'no avatar found');
      const group = document.querySelector(`[data-entity="e-group"] .mm-av`);
      check('a channel keeps the group icon (no img)',
        Boolean(group) && group.tagName !== 'IMG', group ? group.tagName : 'none');
      if (personWith) {
        personWith.dispatchEvent(new window.Event('error'));
        await settle(80);
        const after = document.querySelector(`[data-entity="e-photo"] .mm-av`);
        check('a failed image load falls back to initials',
          Boolean(after) && after.tagName === 'SPAN'
          && !after.classList.contains('mm-av-photo'),
          after ? after.outerHTML.slice(0, 120) : 'no avatar found');
      }
    """, AVATAR_FIXTURES)
    assert_check(payload, "the person with a photo renders an img avatar")
    assert_check(payload, "the img points at the photo endpoint with ?v=")
    assert_check(payload, "the img is lazy with alt = name")
    assert_check(payload, "the person without a photo keeps the initials avatar")
    assert_check(payload, "a channel keeps the group icon (no img)")
    assert_check(payload, "a failed image load falls back to initials")


def test_profile_photo_controls_upload_undo_and_remove() -> None:
    """The profile offers Upload/Change/Remove for a person only. Upload is
    a POST with a FormData body; when the server supersedes, the Undo is
    one /undo-supersede call. Remove is DELETE + Undo via /restore. A
    channel profile has no photo control."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('[data-entity="e-photo"]').click();
      await settle(150);
      const input = document.querySelector('#memory-detail input[type="file"]');
      check('the person profile has a photo upload control', Boolean(input));
      const file = new window.File(['fake-bytes'], 'me.png', { type: 'image/png' });
      Object.defineProperty(input, 'files', { value: [file] });
      input.dispatchEvent(new window.Event('change', { bubbles: true }));
      await settle(300);
      const uploads = calls.filter((c) => c.method === 'POST' && c.url.endsWith('/photo'));
      check('the upload POSTs the photo endpoint with a FormData body',
        uploads.length === 1 && uploads[0].status === 201 && uploads[0].body?.__form === true,
        JSON.stringify(uploads.map((u) => [u.status, u.body && Object.keys(u.body)])));
      const toast = document.getElementById('memory-undo');
      check('the superseding upload offers an Undo', toast && !toast.hidden);
      if (toast && !toast.hidden) {
        document.getElementById('memory-undo-btn').click();
        await settle(300);
        const undoCalls = calls.filter((c) => c.method === 'POST'
          && c.url.includes('/undo-supersede'));
        check('the upload Undo is one undo-supersede call',
          undoCalls.length === 1, JSON.stringify(undoCalls.map((u) => u.url)));
      }
      const remove = [...document.querySelectorAll('#memory-detail button')]
        .find((b) => b.dataset.photoRemove === '1');
      check('the person profile has a remove control', Boolean(remove));
      if (remove) {
        remove.click();
        await settle(300);
        const deletes = calls.filter((c) => c.method === 'DELETE' && c.url.endsWith('/photo'));
        check('remove DELETEs the photo endpoint', deletes.length === 1,
          JSON.stringify(deletes.map((d) => d.url)));
        const undoBtn = document.getElementById('memory-undo');
        check('the remove offers an Undo', undoBtn && !undoBtn.hidden);
        if (undoBtn && !undoBtn.hidden) {
          document.getElementById('memory-undo-btn').click();
          await settle(300);
          const restores = calls.filter((c) => c.method === 'POST'
            && c.url.includes('/restore'));
          check('the remove Undo is one /restore call', restores.length === 1,
            JSON.stringify(restores.map((r) => r.url)));
        }
      }
    """, AVATAR_FIXTURES)
    assert_check(payload, "the person profile has a photo upload control")
    assert_check(payload, "the upload POSTs the photo endpoint with a FormData body")
    assert_check(payload, "the superseding upload offers an Undo")
    assert_check(payload, "the upload Undo is one undo-supersede call")
    assert_check(payload, "the person profile has a remove control")
    assert_check(payload, "remove DELETEs the photo endpoint")
    assert_check(payload, "the remove offers an Undo")
    assert_check(payload, "the remove Undo is one /restore call")


def test_channel_profile_has_no_photo_control() -> None:
    """A channel/group entity must not offer a photo upload."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('[data-entity="e-group"]').click();
      await settle(150);
      check('the channel profile has no photo upload control',
        !document.querySelector('#memory-detail input[type="file"]'));
      check('the channel profile has no photo remove control',
        ![...document.querySelectorAll('#memory-detail button')]
          .some((b) => b.dataset.photoRemove === '1'));
    """, AVATAR_FIXTURES)
    assert_check(payload, "the channel profile has no photo upload control")
    assert_check(payload, "the channel profile has no photo remove control")


# --- review R2: B1 (cache-busting src) + B2 (the sha never shows as text) -----

LEAK_FIXTURES = {
    "entities": [
        {"id": "e1", "kind": "person", "name": "Dana", "aliases": [], "facts": [
            {"id": "f-photo", "subject_id": "e1", "predicate": "photo",
             "value": "497790947d4666760ce38f3c00e852c71f",
             "object_id": None, "status": "confirmed", "provenance": {"source": "upload"}},
        ]},
        {"id": "e2", "kind": "person", "name": "Noa", "aliases": [], "facts": [
            {"id": "f-photo2", "subject_id": "e2", "predicate": "photo",
             "value": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
             "object_id": None, "status": "confirmed", "provenance": {"source": "upload"}},
        ]},
    ],
    "proposed": [{
        "id": "e1", "kind": "person", "name": "Dana", "aliases": [], "facts": [
            {"id": "f-prop-photo", "subject_id": "e1", "predicate": "photo",
             "value": "aaaabbbbccccddddaaaabbbbccccddddaaaabbbbccccddddaaaabbbbccccdddd",
             "object_id": None, "status": "proposed", "provenance": {}},
        ],
    }],
}


def test_avatar_src_is_cache_busted_by_the_fact_value() -> None:
    """B1: every photo shares ONE URL, and GET sends max-age=3600 — after a
    change (or an undo) the browser would serve the OLD bytes from its
    cache. The src carries ?v=<the fact's sha256> so a new value is a new
    URL; the server ignores v."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      const img1 = document.querySelector('[data-entity="e1"] img.mm-av-photo');
      const img2 = document.querySelector('[data-entity="e2"] img.mm-av-photo');
      const want1 = '/memory/entities/e1/photo?v=497790947d4666760ce38f3c00e852c71f';
      check('the avatar src carries the sha as ?v=',
        Boolean(img1) && img1.getAttribute('src') === want1,
        img1 ? img1.getAttribute('src') : 'no img');
      check('a different photo value yields a different src', Boolean(img2)
        && img2.getAttribute('src') !== img1.getAttribute('src'),
        `${img1 && img1.getAttribute('src')} vs ${img2 && img2.getAttribute('src')}`);
    """, LEAK_FIXTURES)
    assert_check(payload, "the avatar src carries the sha as ?v=")
    assert_check(payload, "a different photo value yields a different src")


def test_the_photo_sha_never_renders_as_text() -> None:
    """B2: the profile's basic-info rows, the review inbox and the history
    drawer must not print the sha. The history drawer shows a photo row as
    'photo' plus its dates."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('[data-entity="e1"]').click();
      await settle(150);
      const detail = document.getElementById('memory-detail');
      const detailText = detail ? detail.textContent : '';
      check('the basic-info rows never show the sha',
        !detailText.includes('497790947d4666760ce38f3c00e852c71f'),
        detailText.slice(0, 200));
      document.getElementById('memory-review-btn').click();
      await settle(150);
      const review = document.getElementById('memory-review-list');
      const reviewText = review ? review.textContent : '';
      check('the review inbox never shows a proposed photo sha',
        reviewText && !reviewText.includes('aaaabbbbccccdddd'),
        reviewText.slice(0, 200));
      // the history drawer for the CONFIRMED photo fact (opened directly —
      // the profile's History buttons belong to valued basic fields)
      window.__mem.openMemoryDrawer('f-photo');
      await settle(300);
      const drawer = document.getElementById('memory-drawer-body');
      const drawerText = drawer ? drawer.textContent : '';
      const hasSha = drawerText.includes('497790947d4666760ce38f3c00e852c71f');
      check('the history drawer shows the photo row without the sha',
        drawerText.includes('photo') && !hasSha, drawerText.slice(0, 240));
    """, LEAK_FIXTURES)
    assert_check(payload, "the basic-info rows never show the sha")
    assert_check(payload, "the review inbox never shows a proposed photo sha")
    assert_check(payload, "the history drawer shows the photo row without the sha")


# --- WhatsApp groups on a kid's profile (B1+B3) --------------------------------

GROUPS_FIXTURES = {
    "entities": [
        {"id": "e-kid", "kind": "person", "name": "Noa", "aliases": [], "facts": [
            {"id": "f-grade", "subject_id": "e-kid", "predicate": "grade",
             "value": "5", "object_id": None, "status": "confirmed", "provenance": {}},
        ]},
        {"id": "e-lonely", "kind": "person", "name": "Itamar", "aliases": [], "facts": [
            {"id": "f-grade2", "subject_id": "e-lonely", "predicate": "grade",
             "value": "2", "object_id": None, "status": "confirmed", "provenance": {}},
        ]},
        {"id": "e-adult", "kind": "person", "name": "אבא", "aliases": [], "facts": []},
    ],
    "proposed": [],
    "groupsByEntity": {
        "e-kid": {"groups": [{
            "channel": "ה' ספיר - הורים", "applies_to": "Noa",
            "conversations": [
                {"item_id": "i-1", "occurred_at": "2026-10-25T09:00:00+00:00",
                 "preview": "מחר פעילות מים — להביא מגבת", "kept_fact_id": None},
                {"item_id": "i-2", "occurred_at": "2026-10-24T09:00:00+00:00",
                 "preview": "ההצגה נדחית ליום ראשון", "kept_fact_id": "f-kept"},
            ],
        }]},
        "e-adult": {"groups": []},
    },
}


def test_groups_section_renders_keep_and_kept_states() -> None:
    """A kid's profile lists her groups' relevant conversations with Open +
    Keep; a kept conversation shows 'Kept ✓' disabled; keeping POSTs the
    keep endpoint (the response's fact rides the undo stack)."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('[data-entity="e-kid"]').click();
      await settle(300);
      const section = document.querySelector('#memory-detail .mm-groups-body');
      check('the kid profile has a groups section', Boolean(section));
      if (!section) return;
      check('the channel header renders', section.textContent.includes("ה' ספיר - הורים"));
      const row1 = [...section.querySelectorAll('.mm-group-row')]
        .find((r) => r.textContent.includes('להביא מגבת'));
      check('the open conversation has a keep button', Boolean(row1)
        && [...row1.querySelectorAll('button')].some((b) => b.textContent === 'Keep as memory'));
      const row2 = [...section.querySelectorAll('.mm-group-row')]
        .find((r) => r.textContent.includes('ההצגה נדחית'));
      const keptBtn = row2 && [...row2.querySelectorAll('button')]
        .find((b) => b.textContent === 'Kept ✓');
      check('the kept conversation shows Kept disabled', Boolean(keptBtn && keptBtn.disabled));
      if (!row1) return;
      [...row1.querySelectorAll('button')]
        .find((b) => b.textContent === 'Keep as memory').click();
      await settle(400);
      const keeps = calls.filter((c) => c.method === 'POST' && c.url.endsWith('/groups/keep'));
      check('the keep POSTs the keep endpoint', keeps.length === 1 && keeps[0].status === 201,
        JSON.stringify(keeps.map((k) => [k.url, k.status])));
      check('the keep offers an Undo', !document.getElementById('memory-undo').hidden);
      document.getElementById('memory-undo-btn').click();
      await settle(400);
      const undos = calls.filter((c) => c.method === 'DELETE' && c.url.includes('/memory/facts/'));
      check('the undo soft-deletes the kept note', undos.length === 1,
        JSON.stringify(undos.map((u) => u.url)));
    """, GROUPS_FIXTURES)
    assert_check(payload, "the kid profile has a groups section")
    assert_check(payload, "the channel header renders")
    assert_check(payload, "the open conversation has a keep button")
    assert_check(payload, "the kept conversation shows Kept disabled")
    assert_check(payload, "the keep POSTs the keep endpoint")
    assert_check(payload, "the keep offers an Undo")
    assert_check(payload, "the undo soft-deletes the kept note")


def test_groups_empty_state_and_hidden_for_non_kid() -> None:
    """A kid with no groups shows the empty state + hint; a non-kid person
    has no groups section at all."""
    payload = screen_behavior("""
      await window.MemoryTab.refresh();
      await settle(150);
      document.querySelector('[data-entity="e-lonely"]').click();
      await settle(300);
      let section = document.querySelector('#memory-detail .mm-groups-body');
      check('the empty state names the kid', Boolean(section)
        && section.textContent.includes('No WhatsApp groups linked to Itamar'),
        section ? section.textContent.slice(0, 120) : 'no section');
      document.querySelector('[data-entity="e-adult"]').click();
      await settle(300);
      check('a non-kid profile has no groups section',
        !document.querySelector('#memory-detail .mm-groups-body'));
    """, GROUPS_FIXTURES)
    assert_check(payload, "the empty state names the kid")
    assert_check(payload, "a non-kid profile has no groups section")
