"""Photo avatars (A1) — API contract, tests first.

A person's avatar photo is a confirmed fact predicate 'photo' (single-valued:
a new upload supersedes, history kept, undo via the existing ops). The BYTES
never enter the DB — they ride the content-addressed AttachmentFileStore
(files_dir, sha256-named); the fact carries value=sha256 and
provenance {sha256, mime, size_bytes, source: 'upload'}.

Contract under test:
  POST   /memory/entities/{id}/photo  multipart, one file. 201 + FactRead
         (with `replaced` when a supersede happened).
         404 unknown entity; 409 kind != person; 413 > 2 MB; 415 sniffed
         non-image (jpeg/png/webp only, magic bytes — never the client mime).
  GET    /memory/entities/{id}/photo  200 bytes of the CURRENT photo,
         Cache-Control private, ETag = sha256 (If-None-Match -> 304);
         404 when there is none.
  DELETE /memory/entities/{id}/photo  204, soft-deletes the current photo
         fact (undo = POST /memory/facts/{id}/restore).

Binding/auth: the routes live on the existing /memory router inside
brain.main:app (127.0.0.1 binding, scripts/run-server.sh) — no new exposure.
"""

import base64
import hashlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from brain.config import get_settings
from brain.main import create_app

_ENDPOINT = "/memory"
MAX_BYTES = 2 * 1024 * 1024

# A real 1x1 PNG (valid file, sniffable header).
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
GIF_MAGIC = b"GIF89a" + b"\x01\x00\x01\x00\x00\xff\x00,"


@pytest.fixture(name="photo_client")
def photo_client_fixture(
    tmp_path, monkeypatch: pytest.MonkeyPatch, clean_memory: None,
) -> Iterator[TestClient]:
    """TestClient with a hermetic files_dir (the photo bytes land in tmp)
    and the standard per-test memory truncation."""
    monkeypatch.setenv("JEV_API_KEY", "")
    monkeypatch.setenv("FILES_DIR", str(tmp_path / "files"))
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def _add_entity(client: TestClient, name: str, kind: str = "person") -> dict:
    response = client.post(f"{_ENDPOINT}/entities", json={"kind": kind, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, entity_id: str, data: bytes, name: str = "photo.png",
            mime: str = "image/png"):
    return client.post(
        f"{_ENDPOINT}/entities/{entity_id}/photo",
        files={"file": (name, data, mime)},
    )


def _photo_fact(client: TestClient, entity_id: str) -> dict | None:
    entity = client.get(f"{_ENDPOINT}").json()["entities"][0]
    facts = [f for f in entity["facts"] if f["predicate"] == "photo"]
    return facts[0] if facts else None


# --- upload happy path --------------------------------------------------------


def test_upload_then_get_serves_the_same_bytes_and_etag(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    response = _upload(photo_client, entity["id"], PNG_1PX)
    assert response.status_code == 201, response.text
    fact = response.json()
    assert fact["predicate"] == "photo"
    sha = hashlib.sha256(PNG_1PX).hexdigest()
    assert fact["value"] == sha
    assert fact["provenance"]["sha256"] == sha
    assert fact["provenance"]["mime"] == "image/png"
    assert fact["provenance"]["source"] == "upload"

    served = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert served.status_code == 200, served.text
    assert served.content == PNG_1PX
    assert served.headers["etag"] == f'"{sha}"'
    assert "private" in served.headers["cache-control"]


def test_uploaded_bytes_live_in_the_file_store_not_the_db(photo_client: TestClient) -> None:
    """The fact carries the sha; the DB never carries the bytes."""
    entity = _add_entity(photo_client, "Noa")
    made = _upload(photo_client, entity["id"], PNG_1PX).json()
    fact = _photo_fact(photo_client, entity["id"])
    assert fact is not None and fact["id"] == made["id"]
    assert fact["value"] == hashlib.sha256(PNG_1PX).hexdigest()
    # provenance only — no base64/blob payload anywhere in the fact row
    assert len(str(fact["provenance"])) < 300


def test_reuploading_the_same_image_is_a_no_op(photo_client: TestClient) -> None:
    """Same bytes -> same sha -> add_fact's unchanged-value no-op (no history
    spam)."""
    entity = _add_entity(photo_client, "Noa")
    first = _upload(photo_client, entity["id"], PNG_1PX).json()
    second = _upload(photo_client, entity["id"], PNG_1PX)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first["id"]
    history = photo_client.get(f"{_ENDPOINT}/facts/{first['id']}/history").json()
    assert len(history) == 1


# --- supersede / undo ---------------------------------------------------------


def test_second_upload_supersedes_and_history_keeps_two_rows(
    photo_client: TestClient,
) -> None:
    entity = _add_entity(photo_client, "Noa")
    first_png = PNG_1PX
    second_png = b"\x89PNG\r\n\x1a\n" + b"different-bytes" * 8
    first = _upload(photo_client, entity["id"], first_png).json()
    second = _upload(photo_client, entity["id"], second_png, name="photo2.png").json()
    assert second["value"] != first["value"]
    assert [r["id"] for r in second["replaced"]] == [first["id"]]

    history = photo_client.get(f"{_ENDPOINT}/facts/{second['id']}/history").json()
    assert [row["id"] for row in history] == [first["id"], second["id"]]

    # the GET serves the CURRENT photo's bytes
    served = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert served.content == second_png

    # undo = the existing undo-supersede op: the new row closes, the old one
    # reopens and the GET serves the first bytes again
    undo = photo_client.post(f"{_ENDPOINT}/facts/{second['id']}/undo-supersede")
    assert undo.status_code == 200, undo.text
    assert undo.json()["id"] == first["id"]
    served = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert served.content == first_png


# --- delete / restore ---------------------------------------------------------


def test_delete_photo_then_get_404_and_restore_restores(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    made = _upload(photo_client, entity["id"], PNG_1PX).json()
    deleted = photo_client.delete(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert deleted.status_code == 204, deleted.text
    assert photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo").status_code == 404

    # undo = the existing /restore on the soft-deleted fact row
    restored = photo_client.post(f"{_ENDPOINT}/facts/{made['id']}/restore")
    assert restored.status_code == 200, restored.text
    served = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert served.status_code == 200 and served.content == PNG_1PX


def test_delete_without_a_photo_404s(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    response = photo_client.delete(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert response.status_code == 404, response.text


# --- limits: size, sniffed type, entity kind -----------------------------------


def test_oversize_upload_is_413(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    big = PNG_1PX + b"\x00" * MAX_BYTES  # 2 MB + 1 byte, valid PNG header
    response = _upload(photo_client, entity["id"], big)
    assert response.status_code == 413, response.text
    assert _photo_fact(photo_client, entity["id"]) is None


def test_a_gif_is_415_even_with_a_png_client_mime(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    response = _upload(photo_client, entity["id"], GIF_MAGIC,
                       name="photo.png", mime="image/png")
    assert response.status_code == 415, response.text


def test_fake_png_is_415(photo_client: TestClient) -> None:
    """A text file named .png: the sniff must not trust the client mime."""
    entity = _add_entity(photo_client, "Noa")
    response = _upload(photo_client, entity["id"], b"definitely not an image",
                       name="photo.png", mime="image/png")
    assert response.status_code == 415, response.text


def test_webp_upload_is_accepted(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    webp = b"RIFF\x24\x00\x00\x00WEBPVPE8" + b"\x00" * 12
    response = _upload(photo_client, entity["id"], webp,
                       name="photo.webp", mime="image/webp")
    assert response.status_code == 201, response.text
    served = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo")
    assert served.content == webp
    assert served.headers["content-type"] == "image/webp"


def test_non_person_entity_is_409(photo_client: TestClient) -> None:
    group = _add_entity(photo_client, "School parents", kind="channel")
    response = _upload(photo_client, group["id"], PNG_1PX)
    assert response.status_code == 409, response.text


def test_unknown_entity_404s(photo_client: TestClient) -> None:
    response = _upload(photo_client, str(uuid.uuid4()), PNG_1PX)
    assert response.status_code == 404, response.text


def test_get_photo_of_a_non_person_404s(photo_client: TestClient) -> None:
    group = _add_entity(photo_client, "School parents", kind="channel")
    response = photo_client.get(f"{_ENDPOINT}/entities/{group['id']}/photo")
    assert response.status_code == 404, response.text


# --- conditional GET -----------------------------------------------------------


def test_get_photo_honors_if_none_match(photo_client: TestClient) -> None:
    entity = _add_entity(photo_client, "Noa")
    _upload(photo_client, entity["id"], PNG_1PX)
    sha = hashlib.sha256(PNG_1PX).hexdigest()
    etag = f'"{sha}"'
    response = photo_client.get(f"{_ENDPOINT}/entities/{entity['id']}/photo",
                                headers={"If-None-Match": etag})
    assert response.status_code == 304, response.text
    assert response.content == b""
