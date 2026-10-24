"""C3 Google Drive connector: pure mapping, allowlist walk, changes sync.

The connector is tested offline: the google-api-python-client service is
injected (``service_factory``), so no test ever touches Google. Fixtures are
hand-written to match the documented Drive v3 shapes (Hebrew + English text,
no real user data).

Deletion semantics for the changes feed need prior membership knowledge ("was
this file ever in the allowlist?"), so the incremental cursor carries the
known in-allowlist file-id set inside the opaque JSON blob (the contract lets
the connector define the cursor format; gtasks' plan blesses the same
pattern).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from qdrant_client import QdrantClient

from brain.config import Settings, get_settings
from brain.connectors import get as get_connector
from brain.connectors.base import (
    AttachmentRef,
    ConnectorError,
    ConnectorRetryableError,
    SourceRecord,
)
from brain.connectors.google.drive import (
    GDRIVE_SCOPE,
    DriveConnector,
    cap_text,
    file_kind,
    parse_folder_ids,
)
from brain.connectors.testing import RecordingEnrichment
from brain.schemas import RAW_TEXT_MAX_LENGTH

TZ = ZoneInfo("Asia/Jerusalem")
FIXTURES = Path(__file__).parent / "fixtures" / "connectors" / "gdrive"

ROOT = "1F0ldR00tAll0wListEd00000000000000001"
SUB = "1F0ldSubN3stEd000000000000000000002"
DOC_ID = "1A2b3C4d5E6f7G8h9I0jK1L2mN3oP4qR5sT6uV7w"
SHEET_ID = "1Sh33tM0ck456789abcdefghijklmnopqrstuv"
SLIDE_ID = "1Sl1d3sM0ck456789abcdefghijklmnopqrstu"
PDF_ID = "1PdFfM0ck456789abcdefghijklmnopqrstuvw"
IMAGE_ID = "1ImggM0ck456789abcdefghijklmnopqrstuvx"
BINARY_ID = "1Z1pM0ck456789abcdefghijklmnopqrstuvzy"
TRASHED_ID = "1Tr4sh3dM0ck456789abcdefghijklmnopqrstu"
MOVED_OUT_ID = "1M0v3dO0tM0ck456789abcdefghijklmnopqrs"
GONE_ID = "1P3rmA0n3ntlyG0n3456789abcdefghijkl"
OUTSIDE_PARENT = "1F0ld3rN0tAll0w3d000000000000000003"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class _HttpishError(Exception):
    """Duck-typed stand-in for googleapiclient HttpError (``.resp.status``)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


class _Req:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class FakeFiles:
    """files.list/export/get_media over pre-baked pages (one page per call)."""

    def __init__(
        self,
        pages_by_folder: dict[str, list[dict[str, Any]]] | None = None,
        exports: dict[tuple[str, str], bytes] | None = None,
        media: dict[str, bytes] | None = None,
    ) -> None:
        self.pages = pages_by_folder or {}
        self.exports = exports or {}
        self.export_errors: dict[tuple[str, str], Exception] = {}
        self.media = media or {}
        self.calls: list[tuple[str, ...]] = []

    def list(
        self,
        *,
        q: str | None = None,
        fields: str | None = None,
        pageSize: int | None = None,
        pageToken: str | None = None,
    ) -> _Req:
        assert q is not None and q.startswith("'") and "' in parents" in q, q
        folder = q.split("'")[1]
        self.calls.append(("files.list", folder, pageToken))
        pages = self.pages.get(folder) or []
        result = pages.pop(0) if pages else {"files": []}
        return _Req(result=result)

    def export(self, *, fileId: str, mimeType: str) -> _Req:
        self.calls.append(("export", fileId, mimeType))
        error = self.export_errors.get((fileId, mimeType))
        if error is not None:
            return _Req(error=error)
        return _Req(result=self.exports.get((fileId, mimeType), b""))

    def get_media(self, *, fileId: str) -> _Req:
        self.calls.append(("get_media", fileId))
        return _Req(result=self.media.get(fileId))


class FakeChanges:
    """changes.getStartPageToken / changes.list over pre-baked pages.

    ``errors`` maps page index -> (fail count, status): the first ``fail
    count`` calls for that page index raise that status instead of serving
    the page (simulates an invalid/expired page token).
    """

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        start_token: str = "777",
        errors: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self.start_token = start_token
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, ...]] = []

    def getStartPageToken(self) -> _Req:
        self.calls.append(("changes.getStartPageToken",))
        return _Req(result={"startPageToken": self.start_token})

    def list(
        self,
        *,
        pageToken: str | None = None,
        restrictToMyDrive: bool | None = None,
        spaces: str | None = None,
        fields: str | None = None,
    ) -> _Req:
        self.calls.append(("changes.list", pageToken))
        index = len([c for c in self.calls if c[0] == "changes.list"]) - 1
        fail_count, status = self.errors.get(index, (0, 0))
        if fail_count:
            self.errors[index] = (fail_count - 1, status)
            return _Req(error=_HttpishError(status))
        result = self._pages.pop(0) if self._pages else {"changes": []}
        return _Req(result=result)


class FakeDriveService:
    """Mimics the google-api-python-client builder shape: namespaces are
    zero-arg callables (``service.files()``, ``service.changes()``)."""

    def __init__(self, files: FakeFiles, changes: FakeChanges) -> None:
        self._files = files
        self._changes = changes

    def files(self) -> FakeFiles:
        return self._files

    def changes(self) -> FakeChanges:
        return self._changes


def make_connector(
    *,
    folders: tuple[str, ...] = (ROOT,),
    text_cap: int = RAW_TEXT_MAX_LENGTH,
    max_depth: int = 1,
    files: FakeFiles | None = None,
    changes: FakeChanges | None = None,
) -> tuple[DriveConnector, FakeFiles, FakeChanges]:
    files = files if files is not None else FakeFiles()
    changes = changes if changes is not None else FakeChanges()
    service = FakeDriveService(files, changes)
    connector = DriveConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: service,
        folders=folders,
        text_cap=text_cap,
        max_depth=max_depth,
        tz=TZ,
    )
    return connector, files, changes


def incr_cursor(
    *,
    token: str = "90210",
    known: list[str] | None = None,
    folders: list[tuple[str, int]] | None = None,
) -> str:
    return json.dumps(
        {"v": 2, "mode": "incr", "token": token, "queue": [], "page": None,
         "folder": None, "depth": 0, "known": known or [],
         "folders": [list(f) for f in folders or []]}
    )


def _http_error(status: int, *, reason: str = "") -> _HttpishError:
    """Duck-typed HttpError with the JSON error body in ``.content`` (the
    shape googleapiclient raises; the reason strings live in
    error.errors[].reason / error.details[].reason)."""
    error = _HttpishError(status)
    error.content = json.dumps(
        {
            "error": {
                "code": status,
                "errors": [{"reason": reason, "message": f"http {status}"}],
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": reason}
                ],
            }
        }
    ).encode()
    return error


def exports_for(*pairs: tuple[str, str, bytes]) -> dict[tuple[str, str], bytes]:
    return {(file_id, mime): data for file_id, mime, data in pairs}


# --- pure helpers ---------------------------------------------------------------


class TestPureHelpers:
    def test_file_kind_table(self):
        assert file_kind("application/vnd.google-apps.folder") == "folder"
        assert file_kind("application/vnd.google-apps.document") == "doc"
        assert file_kind("application/vnd.google-apps.spreadsheet") == "sheet"
        assert file_kind("application/vnd.google-apps.presentation") == "slide"
        assert file_kind("application/pdf") == "pdf"
        assert file_kind("image/jpeg") == "image"
        assert file_kind("image/heic") == "image"
        assert file_kind("application/zip") == "binary"

    def test_cap_text(self):
        assert cap_text("abc", 5) == ("abc", False)
        assert cap_text("abcdef", 3) == ("abc", True)

    def test_parse_folder_ids(self):
        assert parse_folder_ids("a, b,,c,a ") == ("a", "b", "c")
        assert parse_folder_ids("") == ()
        assert parse_folder_ids("   ") == ()


class TestSettings:
    def test_gdrive_env_aliases(self, monkeypatch):
        monkeypatch.setenv("BRAIN_GDRIVE_FOLDERS", "foldA, foldB")
        monkeypatch.setenv("BRAIN_GDRIVE_TEXT_CAP", "1234")
        monkeypatch.setenv("BRAIN_GDRIVE_DEPTH", "3")
        get_settings.cache_clear()
        try:
            settings = Settings(_env_file=None)
        finally:
            get_settings.cache_clear()
        assert settings.gdrive_folders == "foldA, foldB"
        assert settings.gdrive_text_cap == 1234
        assert settings.gdrive_depth == 3
        assert parse_folder_ids(settings.gdrive_folders) == ("foldA", "foldB")

    def test_gdrive_defaults(self, monkeypatch):
        for key in ("BRAIN_GDRIVE_FOLDERS", "BRAIN_GDRIVE_TEXT_CAP", "BRAIN_GDRIVE_DEPTH"):
            monkeypatch.delenv(key, raising=False)
        get_settings.cache_clear()
        try:
            settings = Settings(_env_file=None)
        finally:
            get_settings.cache_clear()
        assert settings.gdrive_folders == ""
        assert settings.gdrive_text_cap == RAW_TEXT_MAX_LENGTH  # the runner clamps anyway
        assert settings.gdrive_depth == 1


class TestRegistry:
    def test_gdrive_registered(self):
        assert get_connector("gdrive") is DriveConnector
        assert DriveConnector.name == "gdrive"
        assert GDRIVE_SCOPE == "https://www.googleapis.com/auth/drive.readonly"

    def test_connector_builds_the_drive_v3_service(self):
        """The ClassVars must reach the service factory: a forgotten/typoed
        API_NAME/API_VERSION would build the wrong API (review round 4)."""
        seen: dict[str, str] = {}

        def factory(token: str, scopes: list[str], api_name: str, api_version: str) -> object:
            seen["api_name"] = api_name
            seen["api_version"] = api_version
            return object()  # never used

        connector = DriveConnector(
            token_loader=lambda: "refresh-token", service_factory=factory
        )
        connector._service()
        assert seen == {"api_name": "drive", "api_version": "v3"}


# --- full-sync walk ------------------------------------------------------------


class TestFullSyncWalk:
    def test_depth_one_lists_direct_children_only(self):
        pages = {ROOT: [_fixture("files_page_1"), _fixture("files_page_2")]}
        connector, files, _ = make_connector(max_depth=1, files=FakeFiles(pages))
        result = connector.fetch(None, limit=100)
        kinds = sorted(r.meta["kind"] for r in result.records if not r.deleted)
        assert kinds == ["binary", "doc", "pdf"]  # subfolder + its file excluded
        assert not any(r.meta.get("kind") == "folder" for r in result.records)
        assert not result.reset
        state = json.loads(result.next_cursor)
        assert state["mode"] == "incr" and state["token"] == "777"

    def test_depth_two_includes_nested_folder_files(self):
        pages = {
            ROOT: [_fixture("files_page_1"), _fixture("files_page_2")],
            SUB: [_fixture("files_page_sub")],
        }
        connector, files, _ = make_connector(max_depth=2, files=FakeFiles(pages))
        result = connector.fetch(None, limit=100)
        kept_ids = {r.external_id for r in result.records if not r.deleted}
        assert "1Sh33tN3st3dM0ck456789abcdefghijklmno" in kept_ids
        assert ("files.list", SUB, None) in files.calls

    def test_depth_cap_excludes_beyond(self):
        pages = {ROOT: [_fixture("files_page_1")], SUB: [_fixture("files_page_sub")]}
        connector, files, _ = make_connector(max_depth=1, files=FakeFiles(pages))
        result = connector.fetch(None, limit=100)
        assert all(r.external_id != "1Sh33tN3st3dM0ck456789abcdefghijklmno"
                   for r in result.records)
        assert ("files.list", SUB, None) not in files.calls

    def test_trashed_child_yields_deleted_record(self):
        pages = {ROOT: [{"files": [_fixture("trashed_doc")]}]}
        connector, _, _ = make_connector(files=FakeFiles(pages))
        result = connector.fetch(None, limit=100)
        deleted = [r for r in result.records if r.deleted]
        assert len(deleted) == 1
        assert deleted[0].external_id == TRASHED_ID
        assert deleted[0].meta["delete_reason"] == "trashed"

    def test_walk_pages_resumable_via_cursor(self):
        pages = {ROOT: [_fixture("files_page_1"), _fixture("files_page_2")]}
        connector, _, _ = make_connector(files=FakeFiles(pages))
        batch1 = connector.fetch(None, limit=1)
        state = json.loads(batch1.next_cursor)
        assert state["mode"] == "full"  # page-aligned stop, queue pending
        batch2 = connector.fetch(batch1.next_cursor, limit=100)
        ids = [r.external_id for r in batch1.records] + [
            r.external_id for r in batch2.records
        ]
        assert {DOC_ID, BINARY_ID} <= set(ids)
        final = json.loads(batch2.next_cursor)
        assert final["mode"] == "incr" and final["token"] == "777"
        assert set(final["known"]) >= {DOC_ID, BINARY_ID}

    def test_empty_allowlist_makes_no_api_calls(self):
        connector, files, changes = make_connector(folders=())
        result = connector.fetch(None, limit=10)
        assert result.records == []
        assert result.next_cursor is None
        assert files.calls == [] and changes.calls == []
        assert connector._service_instance is None  # service never even built

    def test_empty_walk_still_captures_changes_token(self):
        pages = {ROOT: [{"files": []}]}
        connector, _, changes = make_connector(files=FakeFiles(pages))
        result = connector.fetch(None, limit=100)
        assert result.records == []
        assert changes.calls == [("changes.getStartPageToken",)]
        assert json.loads(result.next_cursor)["token"] == "777"


# --- content mapping -----------------------------------------------------------


class TestContentMapping:
    def test_doc_export_becomes_text(self):
        payload = _fixture("doc")
        exports = exports_for((DOC_ID, "text/plain", "מרכיבים:\n- חלב\n- לחם".encode()))
        pages = {ROOT: [{"files": [payload]}]}
        connector, files, _ = make_connector(files=FakeFiles(pages, exports=exports))
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == DOC_ID)
        assert record.source == "gdrive"
        assert record.title == "רשימת קניות / Shopping list"
        assert record.version == "18432"
        assert record.occurred_at is not None
        assert record.occurred_at.isoformat() == "2026-10-21T09:24:15.123000+00:00"
        assert record.url == payload["webViewLink"]
        assert "מרכיבים" in record.text
        assert "modified 2026-10-21 12:24 (Asia/Jerusalem)" in record.text
        assert record.meta["mime_type"] == "application/vnd.google-apps.document"
        assert record.meta["kind"] == "doc"
        assert record.attachments == ()
        assert not record.deleted
        assert ("export", DOC_ID, "text/plain") in files.calls

    def test_sheet_csv_truncated_at_cap_with_meta(self):
        csv = "a,b\n" + "1,2\n" * 40_000  # ~160k chars
        exports = exports_for((SHEET_ID, "text/csv", csv.encode()))
        pages = {ROOT: [{"files": [_fixture("sheet")]}]}
        connector, files, _ = make_connector(files=FakeFiles(pages, exports=exports))
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == SHEET_ID)
        assert record.meta["truncated"] is True
        assert record.meta["text_cap"] == RAW_TEXT_MAX_LENGTH
        assert record.text.count("\n") < 40_000  # actually capped
        assert ("export", SHEET_ID, "text/csv") in files.calls

    def test_slide_export_text(self):
        payload = _fixture("slide")
        exports = exports_for((SLIDE_ID, "text/plain", b"Slide one\nSlide two"))
        pages = {ROOT: [{"files": [payload]}]}
        connector, files, _ = make_connector(files=FakeFiles(pages, exports=exports))
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == SLIDE_ID)
        assert "Slide one" in record.text
        assert ("export", SLIDE_ID, "text/plain") in files.calls

    def test_text_cap_applied_to_doc(self):
        long_text = "x" * 60_000
        exports = exports_for((DOC_ID, "text/plain", long_text.encode()))
        pages = {ROOT: [{"files": [_fixture("doc")]}]}
        connector, _, _ = make_connector(
            files=FakeFiles(pages, exports=exports),  # default cap: the item max
        )
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == DOC_ID)
        assert record.meta["truncated"] is True
        assert record.meta["text_cap"] == RAW_TEXT_MAX_LENGTH
        assert record.text.count("x") == RAW_TEXT_MAX_LENGTH  # exactly the cap

    def test_pdf_becomes_attachment_ref_and_fetch_attachment_bytes(self):
        pages = {ROOT: [{"files": [_fixture("pdf")]}]}
        connector, files, _ = make_connector(
            files=FakeFiles(pages, media={PDF_ID: b"%PDF-1.7 fake"})
        )
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == PDF_ID)
        assert len(record.attachments) == 1
        ref = record.attachments[0]
        assert ref.ref_id == PDF_ID
        assert ref.filename == "contract.pdf"
        assert ref.mime_type == "application/pdf"
        assert ref.size == 845312
        assert not any(c[0] == "export" for c in files.calls)  # no export for PDFs
        assert not any(c[0] == "get_media" for c in files.calls)  # bytes lazy
        assert connector.fetch_attachment(record, ref) == b"%PDF-1.7 fake"
        assert any(c[0] == "get_media" for c in files.calls)

    def test_image_becomes_attachment_ref(self):
        pages = {ROOT: [{"files": [_fixture("image")]}]}
        connector, _, _ = make_connector(files=FakeFiles(pages))
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == IMAGE_ID)
        assert record.attachments[0].mime_type == "image/jpeg"
        assert record.attachments[0].filename == "whiteboard.jpg"
        assert record.attachments[0].ref_id == IMAGE_ID

    def test_unknown_binary_is_metadata_only(self):
        pages = {ROOT: [{"files": [_fixture("binary")]}]}
        connector, files, _ = make_connector(files=FakeFiles(pages))
        result = connector.fetch(None, limit=10)
        record = next(r for r in result.records if r.external_id == BINARY_ID)
        assert record.attachments == ()
        assert record.text and "backup-2026-08.zip" in record.text
        assert record.meta["kind"] == "binary"
        assert record.meta["size"] == 1_048_576
        assert not any(c[0] == "export" for c in files.calls)

    def test_export_4xx_keeps_metadata_record_with_content_error(self):
        payload = _fixture("doc")
        files = FakeFiles(exports=exports_for((DOC_ID, "text/plain", b"t")))
        files.export_errors = {(DOC_ID, "text/plain"): _HttpishError(403)}
        page = {"changes": [{"fileId": DOC_ID, "file": payload}], "newStartPageToken": "5"}
        connector, _, _ = make_connector(files=files, changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(), limit=10)
        assert len(result.records) == 1
        assert result.records[0].meta["content_error"].startswith("http 403")
        assert payload["name"] in result.records[0].text
        assert not result.records[0].deleted

    def test_export_5xx_is_retryable(self):
        payload = _fixture("doc")
        files = FakeFiles()
        files.export_errors = {(DOC_ID, "text/plain"): _HttpishError(500)}
        page = {"changes": [{"fileId": DOC_ID, "file": payload}], "newStartPageToken": "6"}
        connector, _, _ = make_connector(files=files, changes=FakeChanges([page]))
        with pytest.raises(ConnectorRetryableError):
            connector.fetch(incr_cursor(), limit=10)


# --- incremental changes -------------------------------------------------------


class TestIncrementalChanges:
    def test_new_and_changed_files_become_records(self):
        payload = _fixture("doc")
        page = {"changes": [{"fileId": DOC_ID, "file": payload}], "newStartPageToken": "888"}
        connector, _, _ = make_connector(
            files=FakeFiles(exports=exports_for((DOC_ID, "text/plain", b"hello"))),
            changes=FakeChanges([page]),
        )
        result = connector.fetch(incr_cursor(token="900", known=[]), limit=10)
        assert len(result.records) == 1
        record = result.records[0]
        assert record.external_id == DOC_ID
        assert record.version == "18432"
        assert "hello" in record.text
        assert record.url == payload["webViewLink"]
        assert not record.deleted
        state = json.loads(result.next_cursor)
        assert state["mode"] == "incr" and state["token"] == "888"
        assert state["known"] == [DOC_ID]

    def test_change_outside_allowlist_ignored(self):
        payload = _fixture("moved_out")  # lives under a non-allowlisted parent
        page = {"changes": [{"fileId": MOVED_OUT_ID, "file": payload}],
                "newStartPageToken": "2"}
        connector, _, _ = make_connector(changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(known=[]), limit=10)
        assert result.records == []

    def test_change_for_allowlisted_folder_itself_skipped(self):
        folder = _fixture("folder")
        page = {"changes": [{"fileId": ROOT, "file": folder}], "newStartPageToken": "7"}
        connector, _, _ = make_connector(changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(known=[]), limit=10)
        assert result.records == []

    def test_change_moved_out_of_allowlist_deleted(self):
        payload = _fixture("moved_out")
        page = {"changes": [{"fileId": MOVED_OUT_ID, "file": payload}],
                "newStartPageToken": "2"}
        connector, _, _ = make_connector(changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(known=[MOVED_OUT_ID]), limit=10)
        assert len(result.records) == 1
        assert result.records[0].deleted is True
        assert result.records[0].meta["delete_reason"] == "moved_out"
        assert json.loads(result.next_cursor)["known"] == []

    def test_change_trashed_deleted(self):
        payload = _fixture("trashed_doc")
        page = {"changes": [{"fileId": TRASHED_ID, "file": payload}],
                "newStartPageToken": "3"}
        connector, _, _ = make_connector(changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(known=[TRASHED_ID]), limit=10)
        assert result.records[0].deleted is True
        assert result.records[0].meta["delete_reason"] == "trashed"

    def test_change_removed_true_deleted(self):
        page = {"changes": [{"fileId": GONE_ID, "removed": True}],
                "newStartPageToken": "4"}
        connector, _, _ = make_connector(changes=FakeChanges([page]))
        result = connector.fetch(incr_cursor(known=[GONE_ID]), limit=10)
        assert result.records[0].deleted is True
        assert result.records[0].meta["delete_reason"] == "removed"
        assert result.records[0].title == ""  # no payload available

    def test_changes_pagination_then_new_start_token_as_cursor(self):
        page1 = _fixture("changes_page_1")
        page2 = _fixture("changes_page_2")
        connector, files, changes = make_connector(
            changes=FakeChanges([page1, page2]),
            files=FakeFiles(exports=exports_for((DOC_ID, "text/plain", b"t"))),
        )
        result = connector.fetch(
            incr_cursor(token="900", known=[MOVED_OUT_ID, TRASHED_ID, GONE_ID]), limit=100
        )
        tokens = [c[1] for c in changes.calls if c[0] == "changes.list"]
        assert tokens == ["900", "tok-changes-page-2"]  # nextPageToken followed
        assert json.loads(result.next_cursor)["token"] == "90210"  # newStartPageToken
        # page 1: doc kept (folder change + outside file skipped);
        # page 2: moved-out deleted, trashed deleted, removed deleted.
        assert len(result.records) == 4
        deleted_ids = {r.external_id for r in result.records if r.deleted}
        assert deleted_ids == {MOVED_OUT_ID, TRASHED_ID, GONE_ID}
        assert ("export", DOC_ID, "text/plain") in files.calls

    def test_invalid_page_token_404_resets_with_full_resync(self):
        pages = {ROOT: [{"files": [_fixture("doc")]}]}
        files = FakeFiles(pages, exports=exports_for((DOC_ID, "text/plain", b"doc text")))
        changes = FakeChanges(start_token="808", errors={0: (1, 404)})
        connector, _, _ = make_connector(files=files, changes=changes)
        result = connector.fetch(incr_cursor(token="stale"), limit=100)
        assert result.reset is True
        assert [r.external_id for r in result.records] == [DOC_ID]
        assert json.loads(result.next_cursor)["token"] == "808"

    def test_invalid_page_token_410_also_resets(self):
        changes = FakeChanges(start_token="809", errors={0: (1, 410)})
        connector, files, _ = make_connector(files=FakeFiles(), changes=changes)
        result = connector.fetch(incr_cursor(token="stale"), limit=100)
        assert result.reset is True
        assert result.records == []

    def test_corrupt_cursor_resets(self):
        connector, files, changes = make_connector(
            files=FakeFiles(), changes=FakeChanges(start_token="810")
        )
        result = connector.fetch("{not-json", limit=100)
        assert result.reset is True
        assert changes.calls == [("changes.getStartPageToken",)]

    def test_fetch_attachment_via_get_media(self):
        connector, files, _ = make_connector(files=FakeFiles(media={PDF_ID: b"%PDF bytes"}))
        record = SourceRecord(source="gdrive", external_id=PDF_ID, version="1",
                              title="contract.pdf", text="", occurred_at=None)
        ref = AttachmentRef(ref_id=PDF_ID, filename="contract.pdf",
                            mime_type="application/pdf", size=10)
        assert connector.fetch_attachment(record, ref) == b"%PDF bytes"
        assert ("get_media", PDF_ID) in files.calls

    def test_fetch_attachment_non_bytes_raises(self):
        connector, _, _ = make_connector(files=FakeFiles())  # get_media returns None
        record = SourceRecord(source="gdrive", external_id="x", version=None,
                              title="t", text="", occurred_at=None)
        ref = AttachmentRef(ref_id="nope", filename="f", mime_type="application/pdf")
        with pytest.raises(ConnectorError):
            connector.fetch_attachment(record, ref)


# --- runner-level semantics (C3: new / unchanged / changed / deleted) -----------
#
# The REAL ConnectorRunner + IngestService on brain_w10_test; only the Drive
# service is faked. Proves MY connector's records behave per the runner
# contract end to end (create -> skip -> update -> soft-delete).


@pytest.fixture(name="runner_db")
def runner_db_fixture(database: None):  # noqa: F811
    """Fresh connector_state/connector_records/memory_items for the run."""
    from sqlalchemy import text

    from brain.db.session import SessionLocal, engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state"):
            conn.execute(text(f"DELETE FROM {table}"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "memory_items"):
            conn.execute(text(f"DELETE FROM {table}"))


def _file_payload(**overrides: Any) -> dict[str, Any]:
    payload = _fixture("doc")
    payload.update(overrides)
    return payload


class TestRunnerSemantics:
    def test_new_unchanged_changed_deleted_through_the_real_runner(self, runner_db):
        from datetime import UTC, datetime

        from tests.fakes import StubClassifier

        from brain.ai.classify import FALLBACK_CLASSIFICATION
        from brain.ai.embedder import FakeEmbedder
        from brain.ai.vectorstore import QdrantVectorStore
        from brain.connectors.runner import ConnectorRunner
        from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo
        from brain.repositories.items import ItemsRepository
        from brain.services.ingest import IngestService

        class _InMemoryIndexer:
            def __init__(self) -> None:
                from brain.services.indexing import QdrantItemIndexer

                self._inner = QdrantItemIndexer(
                    FakeEmbedder(dim=8), QdrantVectorStore(client=QdrantClient(":memory:"))
                )
                self.calls = 0

            def index(self, item: Any) -> None:
                self.calls += 1
                self._inner.index(item)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        files = FakeFiles(
            {ROOT: [{"files": [_file_payload()]}]},
            exports=exports_for((DOC_ID, "text/plain", b"v1 body")),
        )
        changes = FakeChanges(start_token="777")
        connector, _, _ = make_connector(files=files, changes=changes)
        repo = ItemsRepository(runner_db)
        enrichment = RecordingEnrichment()
        ingest = IngestService(
            repo,
            StubClassifier(dict(FALLBACK_CLASSIFICATION)),
            _InMemoryIndexer(),
            enrichment=None,
        )
        indexer = _InMemoryIndexer()
        runner = ConnectorRunner(
            connector,
            ingest=ingest,
            repo=repo,
            state=ConnectorStateRepo(runner_db),
            records=ConnectorRecordRepo(runner_db),
            indexer=indexer,
            enrichment=enrichment,
            clock=lambda: datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
        )

        # Run 1 — full walk: the doc is new -> created.
        first = runner.run()
        assert first.created == 1
        ledger = ConnectorRecordRepo(runner_db).get("gdrive", DOC_ID)
        assert ledger is not None and ledger.version == "18432"
        item_id = ledger.item_id

        # Run 2 — empty changes feed: same cursor domain, nothing happens.
        changes._pages.append({"newStartPageToken": "800", "changes": []})
        second = runner.run()
        assert (second.created, second.updated, second.deleted) == (0, 0, 0)
        assert second.skipped == 0 and second.fetched == 0

        # Run 3 — the doc changed (version bump): updated in place + re-index.
        changes._pages.append(
            {
                "newStartPageToken": "900",
                "changes": [
                    {"fileId": DOC_ID, "file": _file_payload(version="18433")}
                ],
            }
        )
        files.exports[(DOC_ID, "text/plain")] = b"v2 body"
        third = runner.run()
        assert (third.created, third.updated) == (0, 1)
        row = repo.get(item_id)
        assert row is not None and row.raw_text.endswith("v2 body")
        assert row.meta["version"] == "18433"
        assert indexer.calls == 1  # re-indexed exactly on the update
        assert [i for i in enrichment.enqueued] == [item_id]

        # Run 4 — the doc is trashed: soft-deleted, never hard-deleted.
        changes._pages.append(
            {
                "newStartPageToken": "1000",
                "changes": [
                    {"fileId": DOC_ID, "file": _file_payload(trashed=True)}
                ],
            }
        )
        fourth = runner.run()
        assert fourth.deleted == 1
        row = repo.get(item_id)
        assert row is not None  # still in the DB
        assert row.meta["source_deleted"] is True
        assert row.superseded_by == row.id  # hidden from ask retrieval


# --- review round 1: subfolder incrementality + rate-limit 403 ------------------


class TestSubfolderIncremental:
    """Review bug 1: the walk ingests subfolder files (depth > 1), so the
    changes feed must treat walked subfolders as membership, not as
    'moved out'. The cursor carries the walked folder set with depths."""

    def test_edit_of_file_in_walked_subfolder_is_kept_not_deleted(self):
        pages = {
            ROOT: [_fixture("files_page_1"), _fixture("files_page_2")],
            SUB: [_fixture("files_page_sub")],
        }
        connector, files, _ = make_connector(
            max_depth=2,
            files=FakeFiles(pages, exports=exports_for((DOC_ID, "text/plain", b"t"))),
        )
        full = connector.fetch(None, limit=100)
        state = json.loads(full.next_cursor)
        assert state["folders"] == [[SUB, 2]]  # the walked subfolder rides along
        # Next edit of the subfolder file: parents={SUB} — must KEEP.
        page = {
            "newStartPageToken": "901",
            "changes": [
                {
                    "fileId": "1Sh33tN3st3dM0ck456789abcdefghijklmno",
                    "file": _fixture("sheet_in_sub"),
                }
            ],
        }
        connector2, files2, _ = make_connector(
            max_depth=2,
            files=FakeFiles(exports=exports_for(
                ("1Sh33tN3st3dM0ck456789abcdefghijklmno", "text/csv", b"a,b")
            )),
            changes=FakeChanges([page]),
        )
        result = connector2.fetch(
            incr_cursor(token="900", known=["1Sh33tN3st3dM0ck456789abcdefghijklmno"],
                        folders=[[SUB, 2]]),
            limit=100,
        )
        assert len(result.records) == 1
        assert result.records[0].external_id == "1Sh33tN3st3dM0ck456789abcdefghijklmno"
        assert result.records[0].deleted is False

    def test_new_subfolder_then_file_ingested_incrementally(self):
        new_folder = {
            "kind": "drive#file",
            "id": "1N3wSubF0ld3r00000000000000000000004",
            "name": "New folder",
            "mimeType": "application/vnd.google-apps.folder",
            "version": "1",
            "modifiedTime": "2026-10-22T10:00:00.000Z",
            "parents": [ROOT],
            "trashed": False,
        }
        new_file = {
            "kind": "drive#file",
            "id": "1F1l3N3wSubF0ld3r00000000000000000004",
            "name": "Inside new folder",
            "mimeType": "application/vnd.google-apps.document",
            "version": "7",
            "modifiedTime": "2026-10-22T10:05:00.000Z",
            "webViewLink": "https://docs.google.com/document/d/x/edit",
            "parents": ["1N3wSubF0ld3r00000000000000000000004"],
            "trashed": False,
        }
        # Same page: the folder change comes AFTER the file change (API order
        # is not guaranteed) — the connector must still ingest the file.
        page = {
            "newStartPageToken": "950",
            "changes": [
                {"fileId": "1F1l3N3wSubF0ld3r00000000000000000004",
                 "file": new_file},
                {"fileId": "1N3wSubF0ld3r00000000000000000000004",
                 "file": new_folder},
            ],
        }
        connector, _, _ = make_connector(
            folders=(ROOT,),
            max_depth=2,  # the new subfolder sits at depth 2, within the cap
            changes=FakeChanges([page]),
            files=FakeFiles(exports=exports_for(
                ("1F1l3N3wSubF0ld3r00000000000000000004", "text/plain", b"content")
            )),
        )
        result = connector.fetch(incr_cursor(token="900", folders=[]), limit=100)
        assert [r.external_id for r in result.records if not r.deleted] == [
            "1F1l3N3wSubF0ld3r00000000000000000004"
        ]
        final = json.loads(result.next_cursor)
        assert ["1N3wSubF0ld3r00000000000000000000004", 2] in final["folders"]

    def test_walked_folder_trashed_drops_out_files_surface_moved_out(self):
        sub_change = {
            "kind": "drive#file",
            "id": SUB,
            "name": "ארכיון / Archive",
            "mimeType": "application/vnd.google-apps.folder",
            "version": "5",
            "modifiedTime": "2026-10-22T11:00:00.000Z",
            "webViewLink": "https://drive.google.com/drive/folders/x",
            "parents": [ROOT],
            "trashed": True,
        }
        drop_page = {"newStartPageToken": "960", "changes": [
            {"fileId": SUB, "file": sub_change}
        ]}
        later_page = {
            "newStartPageToken": "970",
            "changes": [
                {"fileId": "1Sh33tN3st3dM0ck456789abcdefghijklmno",
                 "file": dict(_fixture("sheet_in_sub"), version="34")}
            ],
        }
        connector, _, _ = make_connector(
            changes=FakeChanges([drop_page, later_page]),
            files=FakeFiles(),
        )
        cursor = incr_cursor(
            token="900", known=["1Sh33tN3st3dM0ck456789abcdefghijklmno"],
            folders=[[SUB, 2]],
        )
        batch1 = connector.fetch(cursor, limit=100)  # the folder drops out
        assert batch1.records == []  # folders never become records
        state = json.loads(batch1.next_cursor)
        assert state["folders"] == []  # dropped from membership
        batch2 = connector.fetch(batch1.next_cursor, limit=100)
        assert len(batch2.records) == 1
        deleted = batch2.records[0]
        assert deleted.deleted is True
        assert deleted.meta["delete_reason"] == "moved_out"

    def test_old_version_cursor_resets(self):
        connector, _, changes = make_connector(
            files=FakeFiles(), changes=FakeChanges(start_token="811")
        )
        old = json.dumps({"v": 1, "mode": "incr", "token": "t", "known": []})
        result = connector.fetch(old, limit=100)
        assert result.reset is True


class TestRateLimitExport:
    """Review bug 2: Drive rate-limits with HTTP 403 + reason
    rateLimitExceeded / userRateLimitExceeded (Drive 'handle errors' doc).
    Those must be retryable, not silent permanent content loss."""

    def _doc_change_page(self) -> dict[str, Any]:
        return {
            "newStartPageToken": "5",
            "changes": [{"fileId": DOC_ID, "file": _fixture("doc")}],
        }

    def test_rate_limit_403_is_retryable(self):
        files = FakeFiles()
        files.export_errors = {
            (DOC_ID, "text/plain"): _http_error(
                403, reason="userRateLimitExceeded"
            )
        }
        connector, _, _ = make_connector(
            files=files, changes=FakeChanges([self._doc_change_page()])
        )
        with pytest.raises(ConnectorRetryableError):
            connector.fetch(incr_cursor(), limit=100)

    def test_plain_403_forbidden_is_content_error(self):
        files = FakeFiles()
        files.export_errors = {(DOC_ID, "text/plain"): _http_error(403, reason="forbidden")}
        connector, _, _ = make_connector(
            files=files, changes=FakeChanges([self._doc_change_page()])
        )
        result = connector.fetch(incr_cursor(), limit=100)
        assert len(result.records) == 1
        assert result.records[0].meta["content_error"].startswith("http 403")
