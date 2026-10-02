"""日付の読み取りと年齢の計算（Issue #59 で `gui.py` から出した）。

**このリポジトリは、この判断を2か所に持ったせいで2度壊れている。**
`0000-00-00` だけを見ていたため `TTTT-TT-TTTTT:TT:TT` が素通りし、撮影日時を
持っているように見えてファイル日時のフォールバックまで消えた。
"""

from datetime import date

import pytest

from photoarchive_ai import dates, db, gui


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2017-12-16T18:46:32", date(2017, 12, 16)),
        ("2011-05-03", date(2011, 5, 3)),
        ("2011-05-03 10:00:00", date(2011, 5, 3)),
    ],
)
def test_readable_dates_are_parsed(value, expected):
    assert dates.parse_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "0000-00-00T00:00:00",
        # **10文字あるので長さでは弾けない。** 実データで Media 67件。
        "TTTT-TT-TTTTT:TT:TT",
        "2017-13-01",
        "2017-02-30",
        "not a date",
    ],
)
def test_broken_values_are_treated_as_missing(value):
    assert dates.parse_date(value) is None


def test_the_length_of_a_broken_value_is_not_a_safe_check():
    """**長さで弾こうとしないこと**を、数字で固定しておく。"""
    assert len("TTTT-TT-TTTTT:TT:TT"[:10]) == 10
    assert dates.parse_date("TTTT-TT-TTTTT:TT:TT") is None


@pytest.mark.parametrize(
    "birth, shot, expected",
    [
        ("2011-05-03", "2017-12-16T18:46:32", 6),
        # 誕生日の前日はまだ上がらない
        ("2011-05-03", "2017-05-02", 5),
        ("2011-05-03", "2017-05-03", 6),
        # 撮影日が誕生日より前なら負。**行を消さずに「誕生前」と出すため。**
        ("2011-05-03", "2010-01-01", -2),
    ],
)
def test_age_is_counted_from_the_birthday(birth, shot, expected):
    assert dates.calculate_age(birth, shot) == expected


@pytest.mark.parametrize(
    "birth, shot",
    [(None, "2017-12-16"), ("2011-05-03", None), (None, None), ("2011-05-03", "TTTT-TT-TT")],
)
def test_the_age_is_not_guessed_when_something_is_missing(birth, shot):
    """**どちらか一方でも欠けていれば計算しない**（仕様書 §8.4）。"""
    assert dates.calculate_age(birth, shot) is None


def test_the_gui_still_exposes_the_same_functions():
    """**`gui.parse_date` を参照している呼び出しとテストを壊さない。**

    出したあとも同じものが見えること。写しではなく同一であることを確かめる
    （別物になっていたら、片方だけ直したときに表示と並び順が食い違う）。
    """
    assert gui.parse_date is dates.parse_date
    assert gui.calculate_age is dates.calculate_age


def test_the_sql_side_keeps_the_same_judgement():
    """**SQL 側の写しと答えがそろっていること。**

    並び順だけは SQL でやるので写しが1つある（`db.SHOOTING_DATE_SORT_KEY`）。
    **片方だけ直すと、画面は「不明」なのに並び順は日付扱い**になる。
    """
    connection = db.connect(":memory:")
    try:
        readable = [
            "2017-12-16T18:46:32",
            "2011-05-03",
        ]
        broken = [
            "0000-00-00T00:00:00",
            "TTTT-TT-TTTTT:TT:TT",
            "",
        ]
        for value in readable + broken:
            row = connection.execute(
                f"SELECT {db.SHOOTING_DATE_SORT_KEY} FROM (SELECT ? AS shooting_date)",
                (value,),
            ).fetchone()
            sql_says_readable = row[0] is not None
            python_says_readable = dates.parse_date(value) is not None
            assert sql_says_readable == python_says_readable, value
    finally:
        connection.close()
