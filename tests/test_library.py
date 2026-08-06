"""Remembered CSV uploads and the edits saved against them."""

from __future__ import annotations

import pytest

from app.contacts import parse_csv
from app.library import CsvLibrary, apply_saved_edits, safe_slug

CSV_TEXT = (
    "First Name,Last Name,Title,Company,Email\n"
    "Ada,Lovelace,VP,Analytical Engines,ada@engines.example\n"
    "Grace,Hopper,Chief,Compiler Works,grace@compilers.example\n"
    "Katherine,Johnson,Director,Orbital,katherine@orbital.example\n"
)


@pytest.fixture
def library(paths):
    return CsvLibrary(paths.library)


def test_an_upload_is_remembered_with_its_bytes(library):
    entry = library.save_upload(name="apollo.csv", raw=CSV_TEXT.encode(), row_count=3)

    assert entry.name == "apollo.csv"
    assert entry.row_count == 3
    assert library.read_csv(entry.list_id) == CSV_TEXT.encode()


def test_the_same_file_reuses_its_entry_and_keeps_edits(library):
    first = library.save_upload(name="apollo.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.record_edits(first.list_id, edits={0: {"title": "Countess"}}, removed=[])

    again = library.save_upload(name="apollo.csv", raw=CSV_TEXT.encode(), row_count=3)

    assert again.list_id == first.list_id
    assert again.edits == {"0": {"title": "Countess"}}
    assert len(library.list_all()) == 1


def test_a_different_file_becomes_its_own_entry(library):
    library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.save_upload(name="b.csv", raw=(CSV_TEXT + "X,Y,Z,W,x@y.example\n").encode(), row_count=4)

    assert len(library.list_all()) == 2


def test_only_changed_cells_are_stored(library):
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)

    library.record_edits(entry.list_id, edits={1: {"company": "Compilers Inc", "bogus": "x"}}, removed=[])
    reloaded = library.load(entry.list_id)

    assert reloaded.edits == {"1": {"company": "Compilers Inc"}}
    assert reloaded.edited_count == 1


def test_edits_are_replayed_onto_a_fresh_parse(library):
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.record_edits(entry.list_id, edits={0: {"title": "Countess"}}, removed=[])

    contacts = parse_csv(CSV_TEXT, 100).contacts
    kept, edited = apply_saved_edits(contacts, library.load(entry.list_id))

    assert kept[0].title == "Countess"
    assert edited == {0: ["title"]}


def test_removed_rows_stay_removed_on_reload(library):
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.record_edits(entry.list_id, edits={}, removed=[1])

    contacts = parse_csv(CSV_TEXT, 100).contacts
    kept, _edited = apply_saved_edits(contacts, library.load(entry.list_id))

    assert [contact.email for contact in kept] == [
        "ada@engines.example",
        "katherine@orbital.example",
    ]


def test_edit_highlighting_follows_rows_past_a_removal(library):
    # Row 2 is edited and row 0 removed, so the edit lands at kept index 1.
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.record_edits(entry.list_id, edits={2: {"company": "Orbital Analytics"}}, removed=[0])

    contacts = parse_csv(CSV_TEXT, 100).contacts
    kept, edited = apply_saved_edits(contacts, library.load(entry.list_id))

    assert kept[1].company == "Orbital Analytics"
    assert edited == {1: ["company"]}


def test_forgetting_edits_leaves_the_original(library):
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)
    library.record_edits(entry.list_id, edits={0: {"title": "Countess"}}, removed=[1])

    library.clear_edits(entry.list_id)
    contacts = parse_csv(CSV_TEXT, 100).contacts
    kept, edited = apply_saved_edits(contacts, library.load(entry.list_id))

    assert len(kept) == 3
    assert edited == {}
    assert kept[0].title == "VP"


def test_deleting_removes_the_record_and_the_csv(library):
    entry = library.save_upload(name="a.csv", raw=CSV_TEXT.encode(), row_count=3)

    assert library.delete(entry.list_id)
    assert library.load(entry.list_id) is None
    assert library.read_csv(entry.list_id) is None
    assert not library.delete(entry.list_id)


def test_lists_are_ordered_by_most_recently_used(library):
    # Timestamps have second resolution, so they are set rather than raced.
    first = library.save_upload(name="old.csv", raw=b"Email\na@b.example\n", row_count=1)
    second = library.save_upload(name="new.csv", raw=b"Email\nc@d.example\n", row_count=1)
    for entry, when in ((first, "2026-01-01T00:00:00+00:00"), (second, "2026-06-01T00:00:00+00:00")):
        entry.last_used_at = when
        library._save_record(entry)

    assert [item.name for item in library.list_all()] == ["new.csv", "old.csv"]

    library.touch(first.list_id)

    assert [item.name for item in library.list_all()] == ["old.csv", "new.csv"]


def test_editing_a_list_that_no_longer_exists_is_not_fatal(library):
    assert library.record_edits("gone", edits={0: {"title": "x"}}, removed=[]) is None
    assert library.clear_edits("gone") is None


@pytest.mark.parametrize("list_id", ["../escape", "with/slash", ".hidden", ""])
def test_list_ids_cannot_escape_the_directory(library, list_id):
    with pytest.raises(ValueError, match="invalid list id"):
        library.load(list_id)


def test_slugs_stay_filesystem_safe():
    assert safe_slug("Apollo Export (1).csv") == "Apollo-Export-1-.csv"
    assert safe_slug("../../etc/passwd") == "etc-passwd"
    assert safe_slug("") == "contacts"
