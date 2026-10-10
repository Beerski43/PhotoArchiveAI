"""撮影日時の無い写真の撮影時期を、フォルダ名から起こす（Issue #65）。

**規約は EXIF が読める写真で照合して決めた**（`scripts/measure_folder_dates.py`）。
ここではその規約と、推測した日付が**どこに効いて、どこで EXIF と見分けられるか**を
固定する。

- **月までしか主張しない**（日付入りのフォルダ名は行事の初日で、撮影日ではない）
- **日付の無いサブフォルダは年だけ受け継ぐ**（`式前/` に行事より前の写真がある）
- **`Media.shooting_date` に書き戻さない**（EXIF 由来と推測を混ぜない）
- 年齢は**区間の両端で同じときだけ**決まる。画面・絞り込み・`match` で同じ規則
"""

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from photoarchive_ai import dates, db, gui
from photoarchive_ai.dates import Taken, age_at, folder_date_range, taken_at
from photoarchive_ai.matcher import _persons_alive_at
from photoarchive_ai.migration import migrate_database, needs_migration
from photoarchive_ai.selection import _get_media_year, _passes_date_filter

import importlib.util

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_folder_dates.py"


def _month(year, month):
    return (date(year, month, 1), date(year, month, 28 if month == 2 else 30 if month in (4, 6, 9, 11) else 31))


def _year(year):
    return (date(year, 1, 1), date(year, 12, 31))


# ---------------------------------------------------------------------------
# フォルダ名の読み方
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        # YYMM。**MMDD ではない**（照合で 400 件中 400 件が YYMM）
        ("/p/Photo/2012/1210/IMG_1.JPG", _month(2012, 10)),
        # YYYYMMDD の DD=00 は「日なし」。**Issue 本文の誤読2（年しか取れなかった）**
        ("/p/Photo/2026/20260100いろいろ/a.jpg", _month(2026, 1)),
        # YYMMDD の行事は**月まで**（行事の初日であって撮影日ではない）
        ("/p/Photo/2013/130816-18お盆/a.jpg", _month(2013, 8)),
        ("/p/Photo/2006/061231USJ/a.jpg", _month(2006, 12)),
        # YYYYMM-MM は月の範囲
        ("/p/Photo/2021/202101-03お正月/a.jpg", (date(2021, 1, 1), date(2021, 3, 31))),
        # MM だけ
        ("/p/Photo/2026/02/a.jpg", _month(2026, 2)),
        # **ファイル名は読まない。Issue 本文の誤読1（2010.jpg を YYMM と読んで 2020-10）**
        ("/p/Photo/2010/2010.jpg", _year(2010)),
        # 行事の下の MMDD。**親の区間に収まる読み方だけを残す**（YYMM の 2009年20月は無い）
        ("/p/Photo/2009/090920-22ツール・ド・のと/0920内灘-輪島/a.jpg", _month(2009, 9)),
        # 日付の無いフォルダの下でも、年が合わなければ MMDD（2008年の下の 0907）
        ("/p/Photo/2008/おなか/0907戌の日/a.jpg", _month(2008, 9)),
        # **日付の無いサブフォルダは年だけ受け継ぐ**（行事より前の写真が入っている）
        ("/p/Photo/2007/071222Wedding/式前/衣装合わせ/a.jpg", _year(2007)),
        ("/p/Photo/2011/110416結婚式/式場/前撮り/a.jpg", _year(2011)),
        # 形の決まらない名前（7桁）は読まない
        ("/p/natsu/2021/2021046-07クラス発表/a.jpg", _year(2021)),
        # 年と食い違う YYYYMMDD は読まない
        ("/p/natsu/2023/20230500いろいろ/20240925/a.jpg", _year(2023)),
        # 年のフォルダが無ければ起こさない
        ("/p/Photo/temp/a.jpg", None),
        ("a.jpg", None),
    ],
)
def test_the_folder_name_is_read_to_the_month_at_most(path, expected):
    assert folder_date_range(path) == expected


def test_a_range_never_leaves_its_calendar_year():
    """**SQL の年齢の判定はこれを前提にしている**（`db._folder_age_known`）。

    区間が年をまたぐと、その年の誕生日だけを見る判定が誤る。
    """
    for path in (
        "/p/2021/202101-03/a.jpg",
        "/p/2021/202111-12/a.jpg",
        "/p/2021/211231-0102/a.jpg",
        "/p/2021/x/a.jpg",
    ):
        start, end = folder_date_range(path)
        assert start.year == end.year == 2021


# ---------------------------------------------------------------------------
# 年齢
# ---------------------------------------------------------------------------


def test_the_age_is_known_only_when_both_ends_agree():
    october = taken_at(None, "2012-10-01", "2012-10-31")
    assert october.inferred
    # 誕生日が区間の外なら1つに決まる
    assert age_at("2010-05-03", october) == 2
    # **区間の途中に誕生日があれば分からない**（推測で埋めない）
    assert age_at("2010-10-15", october) is None
    # 年までの区間はほとんどの誕生日で分からない
    assert age_at("2010-05-03", taken_at(None, "2012-01-01", "2012-12-31")) is None
    # 区間の終わりまでに生まれていないのは確か → 誕生前
    assert age_at("2013-05-03", taken_at(None, "2012-01-01", "2012-12-31")) < 0


def test_exif_wins_over_the_folder_and_broken_exif_falls_back_to_it():
    assert taken_at("2012-10-05T10:00:00", "2011-01-01", "2011-12-31") == Taken(
        date(2012, 10, 5), date(2012, 10, 5), inferred=False
    )
    # 壊れた EXIF は無いのと同じ（判断は `parse_date` の1か所）
    assert taken_at("0000-00-00T00:00:00", "2012-10-01", "2012-10-31").inferred
    assert taken_at(None, None, None) is None


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _media(connection, path, shooting_date=None, file_hash=None):
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": Path(path).name,
            "type": "image",
            "file_hash": file_hash or path,
            "file_size": 1,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": shooting_date,
        },
    )


def _face(connection, media_id, person_id=None, source=None):
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id,
        assign_source=source,
    )


@pytest.fixture
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def test_save_media_writes_the_folder_range_without_touching_the_exif_column(connection):
    media_id = _media(connection, "/p/2012/1210/a.jpg")
    row = connection.execute("SELECT * FROM Media WHERE id = ?", (media_id,)).fetchone()
    assert (row["folder_date_from"], row["folder_date_to"]) == ("2012-10-01", "2012-10-31")
    # **推測を EXIF の列に混ぜない**
    assert row["shooting_date"] is None


def test_refresh_replaces_ranges_left_by_an_older_reading(connection):
    """読み方を変えたとき、差分スキャンが書き直さない行にも新しい区間が入る。"""
    media_id = _media(connection, "/p/2012/1210/a.jpg")
    connection.execute(
        "UPDATE Media SET folder_date_from = '2012-12-10', folder_date_to = '2012-12-10'"
    )
    assert db.refresh_folder_dates(connection) == 1
    assert db.refresh_folder_dates(connection) == 0
    row = connection.execute("SELECT * FROM Media WHERE id = ?", (media_id,)).fetchone()
    assert row["folder_date_from"] == "2012-10-01"


def test_a_version_5_database_gains_the_folder_dates_and_keeps_its_faces(tmp_path):
    """**v5 → v6 で顔を1件も失わない**（CLAUDE.md §4）。列は移行の中で埋まる。

    パスしか読まない（NFS に触れない）ので、`scan` を流し直さなくても効く。
    """
    database = tmp_path / "v5.db"
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "ひより", birth_date="2010-12-08")
    media_id = _media(connection, "/p/2012/1210/a.jpg")
    _face(connection, media_id, person, db.ASSIGN_MANUAL)
    _face(connection, media_id)
    connection.commit()
    connection.close()

    raw = sqlite3.connect(str(database))
    raw.execute("ALTER TABLE Media DROP COLUMN folder_date_from")
    raw.execute("ALTER TABLE Media DROP COLUMN folder_date_to")
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()
    assert needs_migration(str(database))

    messages = []
    migrate_database(str(database), make_backup=False, log=messages.append)

    raw = sqlite3.connect(str(database))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
        assert raw.execute("SELECT COUNT(*) FROM Face").fetchone()[0] == 2
        assert raw.execute(
            "SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'"
        ).fetchone()[0] == 1
        assert raw.execute(
            "SELECT folder_date_from, folder_date_to FROM Media"
        ).fetchone() == ("2012-10-01", "2012-10-31")
    finally:
        raw.close()
    assert any("フォルダ名から撮影時期を起こしました" in message for message in messages)
    assert not needs_migration(str(database))


def test_the_age_filter_sees_the_same_age_as_the_screen(connection):
    """**絞り込み（SQL）と画面（`dates.age_at`）で年齢を一致させる。**

    SQL は年齢を計算しない（区間の途中に誕生日が来るかを文字列で見る）ので、
    両者がずれていないことを、誕生日の前後・区間の途中・誕生前・閏日で確かめる。
    """
    paths = [
        "/p/2012/1210/a.jpg",  # 2012年10月
        "/p/2012/1205/b.jpg",  # 2012年5月（誕生月）
        "/p/2012/x/c.jpg",  # 2012年
        "/p/2009/0903/d.jpg",  # 2009年3月（誕生前）
        "/p/misc/e.jpg",  # 起こせない
    ]
    faces = {path: _face(connection, _media(connection, path)) for path in paths}
    exif = _face(connection, _media(connection, "/p/2014/1401/f.jpg", "2014-01-02T00:00:00"))
    connection.commit()

    for birth in ("2010-05-03", "2008-02-29", "2010-10-31", "2012-01-01"):
        expected = {}
        for path, face_id in list(faces.items()) + [("exif", exif)]:
            takens = db.taken_by_face(connection, [face_id])
            expected[face_id] = age_at(birth, takens[face_id])
        for low, high in ((0, 1), (2, 2), (2, 5), (None, 3), (4, None)):
            for include_unknown in (False, True):
                found = {
                    row["id"]
                    for row in db.list_faces(
                        connection,
                        min_age=low,
                        max_age=high,
                        birth_date=birth,
                        include_unknown_age=include_unknown,
                    )
                }
                want = {
                    face_id
                    for face_id, age in expected.items()
                    if (age is None and include_unknown)
                    or (
                        age is not None
                        and (low is None or age >= low)
                        and (high is None or age <= high)
                    )
                }
                assert found == want, (birth, low, high, include_unknown)


def test_born_by_drops_only_photos_certainly_taken_before_the_birth(connection):
    before = _face(connection, _media(connection, "/p/2009/0903/a.jpg"))
    straddling = _face(connection, _media(connection, "/p/2010/x/b.jpg"))
    after = _face(connection, _media(connection, "/p/2011/1101/c.jpg"))
    connection.commit()
    found = {row["id"] for row in db.list_faces(connection, born_by="2010-05-03")}
    # 区間の途中で生まれた写真は**分からないので残す**
    assert found == {straddling, after}
    assert before not in found


def test_the_month_filter_takes_a_folder_range_only_when_it_fits_whole(connection):
    october = _face(connection, _media(connection, "/p/2012/1210/a.jpg"))
    whole_year = _face(connection, _media(connection, "/p/2012/x/b.jpg"))
    shot = _face(connection, _media(connection, "/p/2012/y/c.jpg", "2012-10-05T00:00:00"))
    connection.commit()

    def ids(**kwargs):
        return {row["id"] for row in db.list_faces(connection, **kwargs)}

    assert ids(month_from="2012-10", month_to="2012-10") == {october, shot}
    assert ids(month_from="2012-01", month_to="2012-12") == {october, whole_year, shot}
    # 「撮影日時なしのみ」は EXIF の無い顔（推測した区間があっても入る）
    assert ids(undated_only=True) == {october, whole_year}


def test_the_teacher_age_ignores_the_folder_range(connection):
    """**手本の年齢（閾値の上限）には、推測した日付を使わない**（利用者が決定）。

    使うと8歳以下と分かった手本の上限が締まり、実データの複製で**正しい自動割り当てが
    125 件外れた**（付いたのは 54 件）。`match` で使うのは誕生前の除外だけ。
    """
    person = db.add_person(connection, "旺志朗", birth_date="2013-10-09")
    folder_only = _face(
        connection, _media(connection, "/p/2016/1605/a.jpg"), person, db.ASSIGN_MANUAL
    )
    shot = _face(
        connection,
        _media(connection, "/p/2016/1605/b.jpg", "2016-05-05T00:00:00"),
        person,
        db.ASSIGN_MANUAL,
    )
    connection.commit()
    manual = db.load_manual_faces(connection)
    ages = dict(zip(manual.face_ids.tolist(), manual.ages.tolist()))
    assert np.isnan(ages[folder_only])
    assert ages[shot] == 2.0


# ---------------------------------------------------------------------------
# match・画面・select
# ---------------------------------------------------------------------------


def test_match_drops_a_person_only_when_the_whole_range_is_before_the_birth():
    person_ids = np.array([1, 2])
    births = {1: "2008-02-19", 2: "2010-12-08"}
    year_2009 = taken_at(None, "2009-01-01", "2009-12-31")
    year_2010 = taken_at(None, "2010-01-01", "2010-12-31")
    assert _persons_alive_at(person_ids, births, year_2009).tolist() == [True, False]
    # 2010年の途中で生まれた → 分からないので外さない
    assert _persons_alive_at(person_ids, births, year_2010).tolist() == [True, True]


def test_the_screen_marks_an_age_computed_from_the_folder():
    record = {"age": None}
    inferred = taken_at(None, "2012-10-01", "2012-10-31")
    assert gui.face_age_label(record, "2010-05-03", inferred) == "(2歳?)"
    assert gui.face_age_label(record, "2010-05-03", "2012-10-05") == "(2歳)"
    # 区間の途中に誕生日があれば出さない
    assert gui.face_age_label(record, "2010-10-15", inferred) is None


def test_the_preview_says_the_period_is_a_guess():
    media = {
        "path": "/p/2012/1210/a.jpg",
        "shooting_date": None,
        "created_time": "2020-01-01T00:00:00",
        "folder_date_from": "2012-10-01",
        "folder_date_to": "2012-10-31",
    }
    text = gui.format_media_info(media, person={"name": "ひより", "birth_date": "2010-12-08"})
    assert "撮影日時: 不明（EXIFなし）" in text
    assert "撮影時期: 2012年10月（フォルダ名から推測）" in text
    assert "ひより: 1歳?" in text


def test_bulk_age_entry_tells_how_many_dates_are_guesses():
    takens = [
        taken_at("2012-10-05"),
        taken_at(None, "2012-10-01", "2012-10-31"),
        None,
    ]
    summary = gui.summarize_selection(3, takens)
    assert "2012-10-01 〜 2012-10-31" in summary
    assert "うち 1 件はフォルダ名から推測した撮影時期です。" in summary
    assert "うち 1 件は撮影日時が分かりません。" in summary
    assert gui.suggested_age("2010-05-03", takens[:2]) == 2
    # 区間の途中に誕生日があれば初期値を出さない
    assert gui.suggested_age("2010-10-15", takens[1:2]) is None


def test_select_counts_the_year_from_the_folder_before_the_file_time():
    base = {"created_time": "2020-01-01T00:00:00", "folder_date_from": "2012-10-01",
            "folder_date_to": "2012-10-31"}
    assert _get_media_year({**base, "shooting_date": None}) == 2012
    # 壊れた EXIF もフォルダ名から起こす（Issue #65 の対象 67 件）
    assert _get_media_year({**base, "shooting_date": "0000-00-00T00:00:00"}) == 2012
    # フォルダ名から起こせなければ、撮影日時が空のときだけファイル日時
    no_folder = {"created_time": "2020-01-01T00:00:00", "folder_date_from": None,
                 "folder_date_to": None}
    assert _get_media_year({**no_folder, "shooting_date": None}) == 2020
    assert _get_media_year({**no_folder, "shooting_date": "0000-00-00T00:00:00"}) is None


def test_select_date_range_takes_a_folder_range_only_when_it_fits_whole():
    media = {"shooting_date": None, "created_time": "2020-01-01T00:00:00",
             "folder_date_from": "2012-10-01", "folder_date_to": "2012-10-31"}
    assert _passes_date_filter(media, {"date": {"start": "2012-01-01", "end": "2012-12-31"}})
    assert not _passes_date_filter(media, {"date": {"start": "2012-10-15", "end": "2012-12-31"}})


# ---------------------------------------------------------------------------
# 照合スクリプト
# ---------------------------------------------------------------------------


def test_the_measurement_counts_mismatches_per_folder():
    spec = importlib.util.spec_from_file_location("measure_folder_dates", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.measure(
        [
            ("/p/2012/1210/a.jpg", "2012-10-05T00:00:00", 1),
            ("/p/2012/1210/b.jpg", "2012-11-01T00:00:00", 1),  # 境目をまたいだ
            ("/p/2012/x/c.jpg", "2012-03-01T00:00:00", 0),
            ("/p/2012/1210/d.jpg", None, 2),
            ("/p/misc/e.jpg", None, 1),
        ]
    )
    assert result["checked"]["month"][True] == 1
    assert result["checked"]["month"][False] == 1
    assert result["checked"]["year"][True] == 1
    assert result["mismatched"] == {"/p/2012/1210": ["2012-11-01"]}
    assert result["target"] == {"month": 1, "none": 1}


# ---------------------------------------------------------------------------
# PR #72 のレビュー対応
# ---------------------------------------------------------------------------


def test_select_date_range_includes_a_folder_range_that_ends_on_the_end_day():
    """**指摘1。** `end` の日に終わる区間が、まるごと範囲内なのに外れていた。

    `end: "2023-12-31"` を0時と読んでいたため、`2023/`（1年）と `2023/2312/`
    （12月）が `select` から落ちた（仕様書 §12.1 の例そのもの）。
    """
    rule = {"date": {"start": "2014-01-01", "end": "2023-12-31"}}
    for start, end in (("2023-01-01", "2023-12-31"), ("2023-12-01", "2023-12-31")):
        media = {"shooting_date": None, "created_time": "2020-01-01T00:00:00",
                 "folder_date_from": start, "folder_date_to": end}
        assert _passes_date_filter(media, rule), (start, end)


def test_select_date_end_includes_the_whole_last_day():
    """**指摘1（EXIF 側）。** 12月31日の昼に撮った写真も外れていた。時刻つきの `end` は時刻で比べる。"""
    media = {"shooting_date": "2023-12-31T10:00:00", "created_time": "2020-01-01T00:00:00"}
    assert _passes_date_filter(media, {"date": {"end": "2023-12-31"}})
    assert not _passes_date_filter(media, {"date": {"end": "2023-12-31T09:00:00"}})


def test_select_reads_unquoted_yaml_dates(tmp_path):
    """**指摘3。** YAML は引用符の無い日付を `datetime.date` で返し、`TypeError` で落ちていた。"""
    from photoarchive_ai.selection import load_rule

    path = tmp_path / "rule.yaml"
    path.write_text("date:\n  start: 2014-01-01\n  end: 2023-12-31\n", encoding="utf-8")
    rule = load_rule(str(path))
    inside = {"shooting_date": "2023-12-31T10:00:00", "created_time": "2020-01-01T00:00:00"}
    outside = {"shooting_date": "2013-12-31T10:00:00", "created_time": "2020-01-01T00:00:00"}
    assert _passes_date_filter(inside, rule)
    assert not _passes_date_filter(outside, rule)


def test_a_leap_day_birthday_gets_the_same_age_window_as_the_screen(connection):
    """**指摘4。** 2月29日生まれの窓の端を2月28日に寄せていたので、閏年でない年の
    2月28日の写真が画面（`age_at`）では0歳、絞り込みでは1歳だった。EXIF でも同じ。
    """
    birth = "2012-02-29"
    on_28th = _face(connection, _media(connection, "/p/x/a.jpg", "2013-02-28T12:00:00"))
    on_1st = _face(connection, _media(connection, "/p/x/b.jpg", "2013-03-01T12:00:00"))
    connection.commit()
    assert age_at(birth, taken_at("2013-02-28")) == 0
    assert age_at(birth, taken_at("2013-03-01")) == 1

    def ids(low, high):
        return {
            row["id"]
            for row in db.list_faces(
                connection, min_age=low, max_age=high, birth_date=birth,
                include_unknown_age=False,
            )
        }

    assert ids(0, 0) == {on_28th}
    assert ids(1, 1) == {on_1st}


def test_refreshing_folder_dates_leaves_the_commit_to_the_caller(tmp_path):
    """**指摘5。** 移行の取引の途中で確定しないこと（版を刻むまでが1つの取引）。"""
    path = tmp_path / "t.db"
    connection = db.ensure_database(str(path))
    _media(connection, "/p/2012/1210/a.jpg")
    connection.execute("UPDATE Media SET folder_date_from = NULL, folder_date_to = NULL")
    connection.commit()

    assert db.refresh_folder_dates(connection) == 1
    connection.rollback()
    row = connection.execute("SELECT folder_date_from FROM Media").fetchone()
    assert row[0] is None, "rollback で取り消せる＝自分で commit していない"
    connection.close()
