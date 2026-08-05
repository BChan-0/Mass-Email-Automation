"""Settings and batch persistence."""

from __future__ import annotations

import json

import pytest

from app.store import DEFAULT_SETTINGS, Batch, DraftRecord, load_settings, save_settings


def test_defaults_are_returned_when_no_file_exists(paths):
    settings = load_settings(paths.settings_file)

    assert settings["subject_template"] == DEFAULT_SETTINGS["subject_template"]


def test_saved_values_are_read_back(paths):
    save_settings(paths.settings_file, {"sender_name": "Bonnie", "cc": "team@example.com"})
    settings = load_settings(paths.settings_file)

    assert settings["sender_name"] == "Bonnie"
    assert settings["cc"] == "team@example.com"


def test_unknown_keys_are_not_persisted(paths):
    save_settings(paths.settings_file, {"sender_name": "Bonnie", "unexpected": "value"})

    assert "unexpected" not in json.loads(paths.settings_file.read_text(encoding="utf-8"))


def test_corrupt_settings_file_falls_back_to_defaults(paths):
    paths.settings_file.write_text("{not json", encoding="utf-8")

    assert load_settings(paths.settings_file) == DEFAULT_SETTINGS


def test_batches_round_trip(store):
    batch = store.new_batch(source_name="apollo.csv", subject_template="Hi {{company}}", attachment_names=["deck.pdf"])
    batch.drafts.append(DraftRecord(draft_id="d1", message_id="m1", to="a@b.example", subject="Hi Acme"))
    store.save(batch)

    reloaded = store.load(batch.batch_id)

    assert reloaded.source_name == "apollo.csv"
    assert reloaded.attachment_names == ["deck.pdf"]
    assert reloaded.drafts[0].draft_id == "d1"
    assert reloaded.live_count == 1


def test_batch_ids_are_unique(store):
    first = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    second = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])

    assert first.batch_id != second.batch_id


def test_list_batches_is_newest_first(store):
    older = store.new_batch(source_name="old.csv", subject_template="s", attachment_names=[])
    older.created_at = "2026-01-01T00:00:00+00:00"
    newer = store.new_batch(source_name="new.csv", subject_template="s", attachment_names=[])
    newer.created_at = "2026-06-01T00:00:00+00:00"
    store.save(older)
    store.save(newer)

    assert [batch.source_name for batch in store.list_batches()] == ["new.csv", "old.csv"]


def test_mark_deleted_stamps_only_the_named_drafts(store):
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    batch.drafts.extend(
        [
            DraftRecord(draft_id="d1", message_id="m1", to="a@b.example", subject="s"),
            DraftRecord(draft_id="d2", message_id="m2", to="c@d.example", subject="s"),
        ]
    )
    store.save(batch)

    store.mark_deleted(batch, {"d1"})
    reloaded = store.load(batch.batch_id)

    assert reloaded.drafts[0].deleted_at is not None
    assert reloaded.drafts[1].deleted_at is None
    assert reloaded.live_count == 1


def test_missing_batch_loads_as_none(store):
    assert store.load("nope") is None


def test_corrupt_batch_file_is_skipped(store, paths):
    (paths.batches / "broken.json").write_text("{not json", encoding="utf-8")

    assert store.list_batches() == []


def test_delete_record_removes_the_file(store):
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    store.save(batch)

    assert store.delete_record(batch.batch_id)
    assert store.load(batch.batch_id) is None
    assert not store.delete_record(batch.batch_id)


@pytest.mark.parametrize("batch_id", ["../escape", "with/slash", ".hidden", ""])
def test_batch_ids_cannot_escape_the_directory(store, batch_id):
    with pytest.raises(ValueError):
        store.load(batch_id)


def test_batch_from_dict_tolerates_missing_keys():
    batch = Batch.from_dict({"batch_id": "abc"})

    assert batch.batch_id == "abc"
    assert batch.drafts == []


def test_writes_are_atomic_and_leave_no_temp_files(store, paths):
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    store.save(batch)

    assert list(paths.batches.glob("*.tmp")) == []
    assert (paths.batches / f"{batch.batch_id}.json").exists()
