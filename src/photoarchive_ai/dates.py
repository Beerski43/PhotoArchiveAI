"""日付の読み取りと年齢の計算。

**「読める撮影日時か」の判断を、この1か所に持たせる。** 以前は `gui.py` に
あったが、利用者が3つになったので出した。

| 利用者 | 何に使うか |
|---|---|
| `gui.py` | 表示・年齢の計算・年齢の初期値・選択の知らせ |
| `db.py` | `SHOOTING_DATE_SORT_KEY`（**SQL 側の写し**。並び順のため） |
| `scripts/measure_embedding_models.py` | 行事（フォルダ×日）の判定 |

**SQL 側の写しは消せない**（媒体が違う）。`db.SHOOTING_DATE_SORT_KEY` と
ここを**片方だけ直さないこと。**

**このリポジトリは、この判断を2か所に持ったせいで2度壊れている。**
`0000-00-00` だけを見ていたため `TTTT-TT-TTTTT:TT:TT` が素通りし、
撮影日時を持っているように見えてファイル日時のフォールバックまで消えた。
**`"TTTT-TT-TTTTT:TT:TT"[:10]` は10文字あるので、長さでは弾けない。**
"""

from __future__ import annotations

from datetime import date
from typing import Optional


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


def calculate_age(birth_date: Optional[str], shooting_date: Optional[str]) -> Optional[int]:
    """その写真が撮られた時点の年齢。誕生日を迎える前なら1引く。

    **どちらか一方でも欠けていれば計算しない**（仕様書 §8.4）。撮影日時は
    実データの 15.8% で欠けており、誕生日は登録するまで全員が未設定。

    撮影日が誕生日より前なら**負の数**を返す。行を消さずに「誕生前」と出して、
    **人物の選び間違いや日付の誤りに気づける**ようにするため。
    """
    born = parse_date(birth_date)
    taken = parse_date(shooting_date)
    if born is None or taken is None:
        return None
    return taken.year - born.year - ((taken.month, taken.day) < (born.month, born.day))
