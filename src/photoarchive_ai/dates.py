"""日付の読み取りと年齢の計算。

**「読める撮影日時か」の判断を、この1か所に持たせる。** 以前は `gui.py` に
あったが、利用者が3つになったので出した。

| 利用者 | 何に使うか |
|---|---|
| `gui.py` | 表示・年齢の計算・年齢の初期値・選択の知らせ |
| `db.py` | `SHOOTING_DATE_SORT_KEY`（**SQL 側の写し**。並び順のため） |
| `scripts/measure_embedding_models.py` | 行事（フォルダ×日）の判定 |
| `matcher.py` / `evaluation.py` | 誕生前の人物を外す・手本の年齢 |
| `similar.py` | 連写の候補（撮影日時を秒まで・`taken_moment`） |

**撮影日時が読めない写真は、フォルダ名から撮影時期を起こす**（#65・下の節）。
EXIF 由来の値とは**混ぜない**: `Media.shooting_date` には書き戻さず、
`Media.folder_date_from` / `folder_date_to` に区間で持つ。両方を見るときは
`taken_at` で `Taken` にしてから扱う。

**SQL 側の写しは消せない**（媒体が違う）。`db.SHOOTING_DATE_SORT_KEY` と
ここを**片方だけ直さないこと。**

**このリポジトリは、この判断を2か所に持ったせいで2度壊れている。**
`0000-00-00` だけを見ていたため `TTTT-TT-TTTTT:TT:TT` が素通りし、
撮影日時を持っているように見えてファイル日時のフォールバックまで消えた。
**`"TTTT-TT-TTTTT:TT:TT"[:10]` は10文字あるので、長さでは弾けない。**
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Tuple, Union


def parse_date(value: Optional[str]) -> Optional[date]:
    """`YYYY-MM-DD` で始まる文字列を日付にする。読めなければ ``None``。

    撮影日時（`2017-12-16T18:46:32`）も誕生日（`2011-05-03`）も先頭10文字が
    日付なので、同じ関数で扱える。

    **壊れた EXIF を弾くのがここの役目。** カメラが日付にならない値を書くことが
    あり、実データでは2種類あった（`0000-00-00T00:00:00` が Media 55件、
    `TTTT-TT-TTTTT:TT:TT` が 67件）。
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def calculate_age(
    birth_date: Optional[str],
    shooting_date: Optional[str],
    folder_from: Optional[str] = None,
    folder_to: Optional[str] = None,
) -> Optional[int]:
    """その写真が撮られた時点の年齢。誕生日を迎える前なら1引く。

    **どちらか一方でも欠けていれば計算しない**（仕様書 §8.4）。撮影日時は
    実データの 15.8% で欠けており、誕生日は登録するまで全員が未設定。

    撮影日が誕生日より前なら**負の数**を返す。行を消さずに「誕生前」と出して、
    **人物の選び間違いや日付の誤りに気づける**ようにするため。

    撮影日時が読めなければ、**フォルダ名から起こした区間**（``folder_from`` /
    ``folder_to``）で計算する。区間の途中に誕生日があれば ``None``（`age_at`）。
    """
    return age_at(birth_date, taken_at(shooting_date, folder_from, folder_to))


# ---------------------------------------------------------------------------
# フォルダ名から起こす撮影時期（#65）
# ---------------------------------------------------------------------------
#
# **フォルダ名の読み方は、この節の1か所だけに持つ。** `Media.folder_date_from` /
# `folder_date_to` はここの出力を保存したもので、`scan` と `migrate` が書く
# （`db.refresh_folder_dates`）。**SQL や画面に読み方を書き写さないこと。**
#
# 規約は EXIF が読める写真で照合して決めた（推測で決めない。Issue #65）。
# 照合は `scripts/measure_folder_dates.py` で再現できる。
#
# - **月までしか主張しない。** 日付入りのフォルダ名（`101201${EVENT}`）は
#   **行事の初日**で、撮影日ではない（出産の入院で +1〜5日、`061231USJ` は
#   元日に撮影）。日まで主張すると EXIF と 2,394 件食い違い、月までなら 469 件
# - **日付の無いサブフォルダは年だけ受け継ぐ。** `071222Wedding/式前/衣装合わせ`
#   のように、行事より**前**の写真が入っている（469 → 277 件）
# - 残る不一致は規則では消せない（EXIF の誤り・境目をまたぐ・別時期の写真の混入）。
#   分類は `docs/history/details/2026-10-10-folder-dates-measured.md`

#: 年のフォルダとみなす名前。**名前全体が4桁の年**であること。
_YEAR_FOLDER = re.compile(r"(?:19[89]\d|20\d\d)")

#: フォルダ名の先頭の数字と、続く範囲（`-18` / `-03` / `-0922`）。
_LEADING_DIGITS = re.compile(r"(\d+)(?:-(\d+))?")


def _month_range(year: int, first: int, last: int) -> Optional[Tuple[date, date]]:
    """``first`` 月の1日から ``last`` 月の末日まで。月が無効なら ``None``。"""
    if not (1 <= first <= last <= 12):
        return None
    return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])


def _valid_day(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _folder_months(
    name: str, year: int, within: Tuple[date, date]
) -> Optional[Tuple[date, date]]:
    """1つのフォルダ名を、``year`` 年の月の区間として読む。読めなければ ``None``。

    ``within`` は親フォルダの区間。**親の区間に収まらない読み方は捨てる**
    （`090920-22${EVENT}/0920${EVENT}` の `0920` は、親が 2009年9月なので
    `YYMM`（2009年20月）ではなく `MMDD`（9月20日）と読む）。

    読み方が2つ以上残ったら読まない（**曖昧なものを推測で決めない**）。
    """
    found = _LEADING_DIGITS.match(name)
    if not found:
        return None
    digits, tail = found.group(1), found.group(2)
    short_year = year % 100
    readings = []
    if len(digits) == 8:  # YYYYMMDD。DD=00 は「日なし」、MM=00 は「月なし」
        whole_year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        if whole_year == year and month == 0:
            readings.append((1, 12))
        elif whole_year == year and (day == 0 or _valid_day(year, month, day)):
            readings.append((month, month))
    elif len(digits) == 6:  # YYYYMM か YYMMDD
        if int(digits[:4]) == year:
            readings.append((int(digits[4:]), int(digits[4:])))
        if int(digits[:2]) == short_year and _valid_day(year, int(digits[2:4]), int(digits[4:])):
            readings.append((int(digits[2:4]), int(digits[2:4])))
    elif len(digits) == 4:  # YYMM。年が合わなければ MMDD
        # **YYMM を先に見る。** `2012/1210/` は 2012年10月（照合で 400件中 400件）。
        # 年が合っても月として読めなければ MMDD に回す（`2009/…/0920${EVENT}`）。
        if int(digits[:2]) == short_year and 1 <= int(digits[2:]) <= 12:
            readings.append((int(digits[2:]), int(digits[2:])))
        elif _valid_day(year, int(digits[:2]), int(digits[2:])):
            readings.append((int(digits[:2]), int(digits[:2])))
    elif len(digits) == 2:  # MM
        readings.append((int(digits), int(digits)))
    # 7桁（`2021046-07`）など、形の決まらないものは読まない。

    ranges = [_month_range(year, first, last) for first, last in set(readings)]
    ranges = [r for r in ranges if r and within[0] <= r[0] and r[1] <= within[1]]
    if len(ranges) != 1:
        return None
    start, end = ranges[0]
    # 範囲の終わり。月の範囲（`202101-03`）と、月をまたぐ日の範囲（`-0102`）だけが
    # 区間を広げる。日の範囲（`130816-18`）は同じ月に収まる。
    if tail and start.month == end.month:
        last = None
        if len(tail) == 2 and len(digits) == 6 and int(digits[:4]) == year:
            last = int(tail)  # YYYYMM-MM
        elif len(tail) == 4:
            last = int(tail[:2])  # ...-MMDD
        widened = None if last is None else _month_range(year, start.month, last)
        if widened is not None and widened[1] <= within[1]:
            end = widened[1]
    return start, end


def folder_date_range(path: str) -> Optional[Tuple[date, date]]:
    """写真のパスのフォルダ名から、撮影時期の区間（両端を含む）を起こす。

    起こせなければ ``None``。区間は**月の単位か年の単位**で、日は主張しない。

    - ``Photo/2012/1210/a.jpg`` → 2012-10-01〜2012-10-31（`YYMM`）
    - ``Photo/2026/20260100いろいろ/a.jpg`` → 2026-01（`YYYYMMDD` の DD=00）
    - ``Photo/2021/202101-03${EVENT}/a.jpg`` → 2021-01〜2021-03
    - ``Photo/2007/071222Wedding/式前/a.jpg`` → 2007年（日付の無いサブフォルダ）
    - ``Photo/2010/2010.jpg`` → 2010年（**ファイル名は読まない**）

    **最初に出てくる4桁の年のフォルダ**が起点。それより上は読まない。
    """
    folders = str(path).replace("\\", "/").split("/")[:-1]
    year = None
    current: Optional[Tuple[date, date]] = None
    for name in folders:
        if year is None:
            if _YEAR_FOLDER.fullmatch(name):
                year = int(name)
                current = (date(year, 1, 1), date(year, 12, 31))
            continue
        months = _folder_months(name, year, current)
        # **読めない名前は年だけ受け継ぐ。** 行事の下の `式前/` `前撮り/` には
        # 行事より前の写真が入っている（照合で 469 → 277 件）。
        current = months if months is not None else (date(year, 1, 1), date(year, 12, 31))
    return current


@dataclass(frozen=True)
class Taken:
    """撮影した時期。**EXIF なら1日、フォルダ名から起こしたなら区間。**

    ``inferred`` はフォルダ名から起こしたもの。**画面では EXIF 由来と見分ける。**
    """

    earliest: date
    latest: date
    inferred: bool


def taken_at(
    shooting_date: Optional[str],
    folder_from: Optional[str] = None,
    folder_to: Optional[str] = None,
) -> Optional[Taken]:
    """撮影時期。**EXIF が読めればそれ、読めなければフォルダ名の区間**、どちらも無ければ ``None``。

    ``folder_from`` / ``folder_to`` は `Media.folder_date_from` / `folder_date_to`。
    """
    exif = parse_date(shooting_date)
    if exif is not None:
        return Taken(exif, exif, False)
    start, end = parse_date(folder_from), parse_date(folder_to)
    if start is None or end is None or end < start:
        return None
    return Taken(start, end, True)


def taken_moment(shooting_date: Optional[str]) -> Optional[datetime]:
    """EXIF の撮影日時を**秒まで**読む。日付だけ・読めない値は ``None``。

    連写を束ねる（`similar.candidate_runs`）のに使う。**読めるかどうかは
    `parse_date` に預ける**（壊れた値を弾くのはあちらの役目）。フォルダ名から
    起こした区間は秒を持たないので、ここでは見ない。
    """
    if parse_date(shooting_date) is None or len(str(shooting_date)) <= 10:
        return None
    try:
        return datetime.fromisoformat(str(shooting_date))
    except ValueError:
        return None


def _age_on(born: date, taken: date) -> int:
    return taken.year - born.year - ((taken.month, taken.day) < (born.month, born.day))


def age_at(birth_date: Optional[str], taken: Optional[Taken]) -> Optional[int]:
    """``taken`` の時点の年齢。**区間の両端で同じときだけ決まる。**

    区間の途中に誕生日があれば ``None``（**分からないものを推測で埋めない**）。
    ただし区間の**終わりまでに生まれていない**なら、それは確かなので負の数を返す
    （誕生前の顔を弾く・画面に「誕生前」と出すため）。
    """
    born = parse_date(birth_date)
    if born is None or taken is None:
        return None
    first, last = _age_on(born, taken.earliest), _age_on(born, taken.latest)
    if first == last:
        return first
    if last < 0:
        return last
    return None


def as_taken(value: Union[str, Taken, None]) -> Optional[Taken]:
    """撮影日時の文字列でも `Taken` でも受け取れるようにする。

    画面の関数には、EXIF の文字列だけを渡す呼び出し（プレビューの1枚）と、
    フォルダ名の区間まで引いた `Taken` を渡す呼び出し（`db.taken_by_face`）がある。
    **文字列の読み方は `parse_date` に預ける**（ここで日付を読まない）。
    """
    if value is None or isinstance(value, Taken):
        return value
    return taken_at(value)


def format_taken(taken: Optional[Taken]) -> Optional[str]:
    """推測した撮影時期を画面に出す形にする（`2012年10月` / `2012年1月〜3月` / `2012年`）。

    EXIF 由来（``inferred`` でない）や ``None`` には ``None`` を返す。
    EXIF の日時は呼び出し側がそのまま出す。
    """
    if taken is None or not taken.inferred:
        return None
    first, last = taken.earliest, taken.latest
    if (first.month, last.month) == (1, 12):
        return f"{first.year}年"
    if first.month == last.month:
        return f"{first.year}年{first.month}月"
    return f"{first.year}年{first.month}月〜{last.month}月"
