"""The per-worker test-collection derivation (conftest) keeps suites isolated.

Concurrent suites from different workers share one Qdrant server; when every
suite pinned the same 'brain_items_test' collection, one worker's fixtures
wiped or seeded vectors another worker's assertions depended on. The suite
now derives its collection from the test DATABASE_URL (brain_w5_test ->
brain_items_w5_test). These tests pin the derivation contract.
"""

from brain.config import test_collection_for as derive_test_collection


def test_default_test_db_derives_default_collection() -> None:
    assert derive_test_collection("brain_test") == "brain_items_test"


def test_per_worker_test_db_gets_its_own_collection() -> None:
    assert derive_test_collection("brain_w5_test") == "brain_items_w5_test"
    assert derive_test_collection("brain_worker2_test") == "brain_items_worker2_test"


def test_non_brain_test_db_still_lands_in_a_test_collection() -> None:
    assert derive_test_collection("w5_test") == "brain_items_w5_test"


def test_derived_collection_is_never_the_live_one() -> None:
    assert derive_test_collection("brain_test") != "brain_items"
    assert derive_test_collection("brain_w5_test") != "brain_items"
