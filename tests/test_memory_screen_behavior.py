"""Behavior tests for the Memory screen: the REAL served files executed in
jsdom (node), driven like a user — no string-presence assertions.

Skips when node/jsdom is unavailable (CI); runs fully where the redesign
toolchain is installed.
"""

from tests._jsdom_screen import assert_check, screen_behavior

# One kid with a grade fact; the review inbox empty.
FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [
                {
                    "id": "f1",
                    "subject_id": "e1",
                    "predicate": "grade",
                    "value": "ה",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                },
                {
                    "id": "f2",
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
    "proposed": [],
}


def test_composer_draft_survives_a_filter_chip_click() -> None:
    """Major 4: tapping a filter chip re-renders the detail and used to wipe
    the half-written memory (the composer's text lives only in the DOM)."""
    payload = screen_behavior(
        """
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
    """,
        FIXTURES,
    )
    assert_check(payload, "composer rendered")
    assert_check(payload, "a filter chip is rendered")
    assert_check(payload, "composer draft survives a filter-chip re-render")


def test_composer_category_survives_a_filter_chip_click() -> None:
    """Major 4 (part 2): the composer's category choice is draft state too."""
    payload = screen_behavior(
        """
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
    """,
        FIXTURES,
    )
    assert_check(payload, "composer category survives a filter-chip re-render")


def test_screen_catches_up_after_typing_stops() -> None:
    """Major 5: dataKey is stamped BEFORE the busy check, so a poll that
    lands while the user types marks the payload as seen; the next poll
    (user idle) sees no change and never renders — the screen stays stale."""
    payload = screen_behavior(
        """
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
    """,
        FIXTURES,
    )
    assert_check(payload, "screen catches up after typing stops")


# --- Major 2: confirming a proposal must not wipe multi-valued fields --------

M2_FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [
                {
                    "id": "f-ita",
                    "subject_id": "e1",
                    "predicate": "applies_to",
                    "value": "Itamar",
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
                    "id": "f-rona",
                    "subject_id": "e1",
                    "predicate": "applies_to",
                    "value": "Rona",
                    "object_id": None,
                    "status": "proposed",
                    "provenance": {},
                },
            ],
        }
    ],
}


def test_confirming_applies_to_keeps_the_other_links() -> None:
    """Major 2: confirming one applies_to proposal must not soft-delete the
    person's other applies_to links (multi-valued by design)."""
    payload = screen_behavior(
        """
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
    """,
        M2_FIXTURES,
    )
    assert_check(payload, "the proposal offers a confirm button")
    assert_check(payload, "confirming applies_to does not delete the other link")


M2B_FIXTURES = {
    "entities": [
        {
            "id": "e1",
            "kind": "person",
            "name": "Noa",
            "aliases": [],
            "facts": [
                {
                    "id": "f-old-bday",
                    "subject_id": "e1",
                    "predicate": "birthday",
                    "value": "1.1.2010",
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
                    "id": "f-new-bday",
                    "subject_id": "e1",
                    "predicate": "birthday",
                    "value": "2.2.2011",
                    "object_id": None,
                    "status": "proposed",
                    "provenance": {},
                },
            ],
        }
    ],
}


def test_confirming_a_new_birthday_supersedes_with_undo() -> None:
    """Major 2 (part 2, server-owned rule): for single-valued basic fields
    the SERVER supersedes — its response carries the closed rows as
    `replaced` and the client offers an Undo from that, deleting nothing
    itself."""
    payload = screen_behavior(
        """
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
      // The honest stub refuses a restore while the replacement is still
      // current (as services/memory.py does) — so the undo must DELETE the
      // confirmed replacement FIRST, then restore the old row, with no 409s.
      const replacementDeleted = calls.findIndex((c) => c.method === 'DELETE'
        && c.url.includes('f-new-bday'));
      const restoredAt = calls.findIndex((c) => c.method === 'POST'
        && c.url.includes('f-old-bday/restore'));
      const callsNoGet = calls.filter((c) => c.method !== 'GET')
        .map((c) => [c.method, c.url, c.status]);
      check('Undo restores the replaced birthday', restoredAt !== -1,
        `calls=${JSON.stringify(callsNoGet)}`);
      check('the Undo deletes the confirmed replacement before restoring the old row',
        replacementDeleted !== -1 && restoredAt !== -1 && replacementDeleted < restoredAt,
        `calls=${JSON.stringify(callsNoGet)}`);
      const conflicts = calls.filter((c) => c.status === 409);
      check('the whole confirm+undo round-trip has no 409s', conflicts.length === 0,
        `conflicts=${JSON.stringify(conflicts.map((c) => [c.method, c.url, c.status]))}`);
    """,
        M2B_FIXTURES,
    )
    assert_check(payload, "the proposal offers a confirm button")
    assert_check(payload, "the client does not delete the old birthday itself")
    assert_check(payload, "the supersede offers an Undo")
    assert_check(payload, "Undo restores the replaced birthday")
    assert_check(payload, "the Undo deletes the confirmed replacement before restoring the old row")
    assert_check(payload, "the whole confirm+undo round-trip has no 409s")


def test_composer_accepts_an_optional_title_and_renders_it() -> None:
    """Minor a (PR #136 review): the add-memory form gains an optional Title
    field — sent with the POST and rendered on the memory cards."""
    payload = screen_behavior(
        """
      await window.MemoryTab.refresh();
      await settle(150);
      const existing = [...document.querySelectorAll('.mm-mem')]
        .find((c) => c.textContent.includes('first recital'));
      check('the stored title renders on the card',
        Boolean(existing) && existing.textContent.includes('Recital night'),
        `card=${existing ? existing.textContent.slice(0, 120) : 'missing'}`);
      const titleInput = document.querySelector('input[aria-label="title"]');
      check('the composer offers a title field', Boolean(titleInput));
      if (!titleInput) return;
      titleInput.value = 'Swim class';
      titleInput.dispatchEvent(new window.Event('input', { bubbles: true }));
      const input = document.getElementById('memory-new');
      input.value = 'She swam a whole length';
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
      const save = [...document.querySelectorAll('.mm-composer button')]
        .find((b) => b.textContent === 'Save');
      save.click();
      await settle(300);
      const posts = calls.filter((c) => c.method === 'POST' && c.url === '/memory/facts');
      check('the POST carries the title and the text',
        posts.length === 1 && posts[0].body && posts[0].body.title === 'Swim class'
        && posts[0].body.value === 'She swam a whole length',
        `posts=${JSON.stringify(posts.map((c) => c.body))}`);
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
                            "id": "f-note-1",
                            "subject_id": "e1",
                            "predicate": "note",
                            "value": "she remembered her first recital",
                            "object_id": None,
                            "status": "confirmed",
                            "provenance": {"title": "Recital night"},
                        },
                    ],
                }
            ],
            "proposed": [],
        },
    )
    assert_check(payload, "the stored title renders on the card")
    assert_check(payload, "the composer offers a title field")
    assert_check(payload, "the POST carries the title and the text")


# --- Drafts on every form (round-2 decision): type, blur, poll with
# --- changed data — the typed text must survive the re-render.


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


# --- Major 3: move-to-basic-info ---------------------------------------------

M3_FIXTURES = {
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
                {
                    "id": "f-bday",
                    "subject_id": "e1",
                    "predicate": "birthday",
                    "value": "1.1.2010",
                    "object_id": None,
                    "status": "confirmed",
                    "provenance": {},
                },
            ],
        },
        {"id": "e2", "kind": "person", "name": "Rona", "aliases": [], "facts": []},
    ],
    "proposed": [],
}


def test_move_form_checkboxes_default_to_unchecked() -> None:
    """Major 3: the 'also on <other>' checkboxes wrote relation/work/phone
    onto other people silently — they must default to unchecked."""
    payload = screen_behavior(
        """
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
    """,
        M3_FIXTURES,
    )
    assert_check(payload, "the memory card has an actions menu")
    assert_check(payload, "the menu offers move-to-basic-info")
    assert_check(payload, "extra-subject checkboxes default to unchecked")


def test_move_to_basic_info_patches_an_existing_value() -> None:
    """Major 3 (part 2): when the profile already carries a fact for the
    chosen field, the move must PATCH that row (edit = new version), not
    POST a second parallel value."""
    payload = screen_behavior(
        """
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
    """,
        M3_FIXTURES,
    )
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
    payload = screen_behavior(
        """
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
                            "id": "f-note-1",
                            "subject_id": "e1",
                            "predicate": "note",
                            "value": "first memory",
                            "object_id": None,
                            "status": "confirmed",
                            "provenance": {},
                        },
                        {
                            "id": "f-note-2",
                            "subject_id": "e1",
                            "predicate": "note",
                            "value": "second memory",
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
    for name in (
        "card found for first memory",
        "delete button found for first memory",
        "card found for second memory",
        "delete button found for second memory",
        "the undo toast is visible after two deletes",
        "the first Undo restores the latest delete",
        "the toast stays for the earlier delete",
        "the second Undo restores the earlier delete",
    ):
        assert_check(payload, name)


def test_connection_form_other_type_wording() -> None:
    """'other' rendered as 'Noa is the connected of Rona' — the wording must
    read 'Noa is connected to Rona'."""
    payload = screen_behavior(
        """
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
    """,
        {
            "entities": [
                {"id": "e1", "kind": "person", "name": "Noa", "aliases": [], "facts": []},
                {"id": "e2", "kind": "person", "name": "Rona", "aliases": [], "facts": []},
            ],
            "proposed": [],
        },
    )
    assert_check(payload, "the add-connection affordance exists")
    assert_check(payload, "the connection form opened")
    assert_check(payload, 'the other type reads "is connected to"')
