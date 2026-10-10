"""検出元のディレクトリ（root）を複数持つこと（#24）。

実データは root が2つある（`${SURNAME}/Photo` と `person2Temp/${PERSON_2}携帯`）。設定は git 管理外で、
2026-10-02 に失ったとき **DB から root を戻せず**、共通の親で走査する危ない設定を
書きかけた。ここで守ること:

- root ごとに走査し、消えた行の削除と2割の安全弁も root ごと
- 走査し終えた root を DB（`ScanRoot`）に記録する。年フォルダだけの走査は root にしない
- 入れ子の root は止める
- v6 → v7 の移行で顔を減らさず、root は推定しない
- GUI と `select` が、root が複数でもそれを含む root からの相対で扱う
"""

import os
import sqlite3

import pytest

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui
from photoarchive_ai.migration import migrate_database, needs_migration
from photoarchive_ai.scanner import (
    ScanAborted,
    normalize_source_roots,
    scan_directories,
    scan_directory,
)
from photoarchive_ai.selection import copy_selected_media
from tests.helpers import write_image


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _paths(connection):
    return sorted(row["path"] for row in db.list_media(connection))


# ---------------------------------------------------------------------------
# 走査
# ---------------------------------------------------------------------------


def test_two_roots_are_both_scanned_and_recorded(tmp_path, connection):
    photo = tmp_path / "Photo"
    phone = tmp_path / "phone"
    write_image(photo / "2021" / "a.jpg")
    write_image(phone / "2021" / "b.jpg", color=(10, 200, 30))

    summary = scan_directories([str(photo), str(phone)], connection, workers=1)

    assert summary["processed"] == 2
    assert [part["root"] for part in summary["roots"]] == [str(photo), str(phone)]
    assert _paths(connection) == [str(photo / "2021/a.jpg"), str(phone / "2021/b.jpg")]
    assert db.list_scan_roots(connection) == sorted([str(photo), str(phone)])


def test_a_missing_root_does_not_make_the_other_roots_media_prunable(tmp_path, connection):
    """**片方の root だけを走査しても、もう片方のメディアは削除候補にならない。**

    root を1つ（もう1つは記録だけ）で走査し直したとき、外の 5,323 件（実データ）が
    「見つからない」に数えられると、2割の安全弁で止まるか、`--force-prune` で消える。
    """
    photo = tmp_path / "Photo"
    phone = tmp_path / "phone"
    write_image(photo / "a.jpg")
    for index in range(3):
        write_image(phone / f"b{index}.jpg", color=(10 + index, 200, 30))
    scan_directories([str(photo), str(phone)], connection, workers=1)

    summary = scan_directories([str(photo)], connection, workers=1)

    assert summary["pruned"] == 0
    assert len(_paths(connection)) == 4


def test_the_safety_valve_still_works_per_root(tmp_path, connection):
    photo = tmp_path / "Photo"
    phone = tmp_path / "phone"
    write_image(photo / "a.jpg")
    for index in range(5):
        write_image(phone / f"b{index}.jpg", color=(10 + index, 200, 30))
    scan_directories([str(photo), str(phone)], connection, workers=1)
    for index in range(1, 5):
        (phone / f"b{index}.jpg").unlink()

    with pytest.raises(ScanAborted):
        scan_directories([str(photo), str(phone)], connection, workers=1)

    assert len(_paths(connection)) == 6


def test_nested_roots_are_refused(tmp_path):
    """**親を root にすること自体が事故**（共通の親で走査すると他家の写真まで入る）。"""
    parent = tmp_path / "photo"
    (parent / "person2Temp").mkdir(parents=True)

    with pytest.raises(ValueError, match="入れ子"):
        normalize_source_roots([str(parent), str(parent / "person2Temp")])


def test_the_same_root_written_twice_is_scanned_once(tmp_path):
    root = tmp_path / "Photo"
    root.mkdir()

    assert normalize_source_roots([str(root), str(root) + "/", str(root / ".")]) == [root]


def test_a_root_given_through_a_symlink_is_recorded_by_its_real_path(tmp_path, connection):
    """`Media.path` は実体のパス。記録も実体にそろえないと、root で範囲を決められない。"""
    real = tmp_path / "nfs" / "Photo"
    write_image(real / "a.jpg")
    alias = tmp_path / "alias"
    os.symlink(real, alias)

    scan_directories([str(alias)], connection, workers=1)

    assert db.list_scan_roots(connection) == [str(real)]


# ---------------------------------------------------------------------------
# root の記録
# ---------------------------------------------------------------------------


def test_scanning_a_year_folder_inside_a_root_does_not_record_a_new_root(tmp_path, connection):
    root = tmp_path / "Photo"
    write_image(root / "2021" / "a.jpg")
    write_image(root / "2022" / "b.jpg", color=(10, 200, 30))
    scan_directory(str(root), connection, workers=1)

    scan_directory(str(root / "2021"), connection, workers=1)

    assert db.list_scan_roots(connection) == [str(root)]


def test_scanning_a_parent_of_a_recorded_root_stops_before_reading_anything(tmp_path, connection):
    """**共通の親を1つだけ渡しても、走査する前に止める**（PR #75 のレビュー (a)・利用者の決定）。

    root を思い出せずに親を渡すと他家の写真まで入る（2026-10-02）。入れ子の検査は同じ指定の
    中の親と子しか見ないので、記録と突き合わせる。以前は走査したうえで記録が親だけに
    置き換わり、正しい root で走査し直しても戻らなかった。
    """
    parent = tmp_path / "nanoPi"
    photo = parent / "${SURNAME}" / "Photo"
    write_image(photo / "a.jpg")
    write_image(parent / "katayama" / "other.jpg", color=(10, 200, 30))
    scan_directories([str(photo)], connection, workers=1)

    with pytest.raises(ScanAborted, match="記録済みの root"):
        scan_directories([str(parent)], connection, workers=1)

    assert _paths(connection) == [str(photo / "a.jpg")]
    assert db.list_scan_roots(connection) == [str(photo)]


def test_a_year_folder_inside_a_recorded_root_is_still_scanned(tmp_path, connection):
    """止めるのは親だけ。root の内側（年フォルダ）の走査は今までどおり通す。"""
    root = tmp_path / "Photo"
    write_image(root / "2021" / "a.jpg")
    scan_directories([str(root)], connection, workers=1)

    summary = scan_directories([str(root / "2021")], connection, workers=1)

    assert summary["total_files"] == 1


def test_recording_an_outer_root_never_drops_the_inner_records(tmp_path, connection):
    """記録は消さない。両方残れば、記録で走査するときに入れ子の検査で止まる。"""
    outer = tmp_path / "photo"
    inner = outer / "2021"
    inner.mkdir(parents=True)
    assert db.record_scan_root(connection, str(inner)) is True
    assert db.record_scan_root(connection, str(outer)) is True

    assert db.list_scan_roots(connection) == [str(outer), str(inner)]
    with pytest.raises(ValueError, match="入れ子"):
        normalize_source_roots(db.list_scan_roots(connection))


def test_a_root_that_does_not_exist_stops_before_any_root_is_scanned(tmp_path, connection):
    """後ろの root が無いと、前の root を走査し終えてから落ちていた（PR #75 のレビュー指摘1）。"""
    root = tmp_path / "Photo"
    write_image(root / "a.jpg")

    with pytest.raises(ValueError, match="ありません"):
        scan_directories([str(root), str(tmp_path / "missing")], connection, workers=1)

    assert _paths(connection) == []
    assert db.list_scan_roots(connection) == []


def test_a_sibling_with_a_common_prefix_is_not_taken_for_an_inner_root(connection):
    """`/mnt/Photo2` は `/mnt/Photo` の内側ではない（文字列の前方一致で判断しない）。"""
    db.record_scan_root(connection, "/mnt/Photo")

    assert db.record_scan_root(connection, "/mnt/Photo2") is True
    assert db.list_scan_roots(connection) == ["/mnt/Photo", "/mnt/Photo2"]


def test_an_aborted_scan_does_not_record_its_root(tmp_path, connection):
    empty = tmp_path / "unmounted"
    empty.mkdir()

    with pytest.raises(ScanAborted):
        scan_directory(str(empty), connection, workers=1)

    assert db.list_scan_roots(connection) == []


# ---------------------------------------------------------------------------
# 移行
# ---------------------------------------------------------------------------


def test_a_version_6_database_gains_the_root_table_and_keeps_its_faces(tmp_path):
    """**v6 → v7 で顔を1件も失わない**（CLAUDE.md §4）。root は推定しない。"""
    database = tmp_path / "v6.db"
    photo = write_image(tmp_path / "Photo" / "a.jpg")
    connection = db.ensure_database(str(database))
    person_id = db.add_person(connection, "父", "father", "")
    media_id = db.save_media(
        connection,
        {
            "path": str(photo),
            "filename": photo.name,
            "type": "image",
            "file_hash": "hash",
            "file_size": 1,
            "created_time": "2026-01-01T00:00:00",
        },
    )
    for _ in range(2):
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=person_id,
            assign_source=db.ASSIGN_MANUAL,
        )
    connection.commit()
    connection.close()
    raw = sqlite3.connect(str(database))
    raw.execute("DROP TABLE ScanRoot")
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()
    assert needs_migration(str(database))

    messages = []
    migrate_database(str(database), make_backup=False, log=messages.append)

    raw = sqlite3.connect(str(database))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 7
        assert raw.execute("SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'").fetchone()[0] == 2
        # **推定しない。** 次の scan が書く
        assert raw.execute("SELECT COUNT(*) FROM ScanRoot").fetchone()[0] == 0
    finally:
        raw.close()
    assert any("ScanRoot" in message for message in messages)
    assert needs_migration(str(database)) is False
    db.ensure_database(str(database)).close()


# ---------------------------------------------------------------------------
# GUI の表示と select の出力
# ---------------------------------------------------------------------------


def test_with_several_roots_the_folder_is_prefixed_with_the_root_name(tmp_path):
    """どちらの root にも `2021/` があるので、root の名前を付けないと別のフォルダが同じ名前になる。"""
    photo = tmp_path / "Photo"
    phone = tmp_path / "${PERSON_2}携帯"
    roots = [str(photo), str(phone)]

    assert photo_label(photo / "2021", roots) == "Photo/2021"
    assert photo_label(phone / "2021", roots) == "${PERSON_2}携帯/2021"
    assert photo_label(phone, roots) == "${PERSON_2}携帯"
    assert photo_label(tmp_path / "elsewhere", roots) == str(tmp_path / "elsewhere")


def test_with_one_root_the_folder_is_shown_as_before(tmp_path):
    photo = tmp_path / "Photo"

    assert photo_label(photo / "2021", [str(photo)]) == "2021"
    assert photo_label(photo / "2021", str(photo)) == "2021"
    assert photo_label(photo, [str(photo)]) == "（root 直下）"


def photo_label(folder, roots):
    return photoarchive_gui.format_folder(str(folder), roots)


def test_the_window_falls_back_to_the_roots_recorded_in_the_database(tmp_path):
    """設定に root が無くても、DB の記録で相対表示にする（設定を失っても読める）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    db.record_scan_root(connection, str(tmp_path / "Photo"))
    db.record_scan_root(connection, str(tmp_path / "phone"))
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    try:
        assert window.source_roots == [str(tmp_path / "Photo"), str(tmp_path / "phone")]
    finally:
        window.connection.close()


def test_select_copies_same_named_photos_from_different_roots(tmp_path):
    """#83: 出力は平らで元の名前を使わないので、別の root の同じ名前もぶつからない。"""
    photo = tmp_path / "Photo"
    phone = tmp_path / "phone"
    first = write_image(photo / "2021" / "a.jpg")
    second = write_image(phone / "2021" / "a.jpg", color=(10, 200, 30))
    output = tmp_path / "out"

    copied = copy_selected_media(
        [{"id": 1, "path": str(first)}, {"id": 2, "path": str(second)}],
        str(output),
        [str(photo), str(phone)],
    )

    assert copied == 2
    copies = sorted(output.iterdir())
    assert [copy.read_bytes() for copy in copies] == [first.read_bytes(), second.read_bytes()]


def test_select_resolves_a_relative_path_from_the_first_root(tmp_path):
    photo = tmp_path / "Photo"
    first = write_image(photo / "2021" / "b.jpg")
    output = tmp_path / "out"

    copied = copy_selected_media(
        [{"id": 1, "path": "2021/b.jpg"}], str(output), [str(photo), str(tmp_path / "phone")]
    )

    assert copied == 1
    [copy] = list(output.iterdir())
    assert copy.read_bytes() == first.read_bytes()


def test_select_copies_a_photo_outside_every_root(tmp_path):
    """どの root の外のメディアも、ほかと同じく直下にコピーする。

    以前（root からの相対パスを再現していた頃）はここで `str` に `as_posix()` を呼んで落ちていた。
    実データでは root の外が 5,323 件あった（#24）。
    """
    outside = write_image(tmp_path / "elsewhere" / "deep" / "c.jpg")
    output = tmp_path / "out"
    progress = []

    copied = copy_selected_media(
        [{"id": 9, "path": str(outside)}],
        str(output),
        [str(tmp_path / "Photo")],
        progress_callback=lambda current, total, detail: progress.append(detail),
    )

    assert copied == 1
    [copy] = list(output.iterdir())
    assert copy.read_bytes() == outside.read_bytes()
    assert progress == [copy.name]
