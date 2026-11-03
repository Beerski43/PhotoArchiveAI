"""人物登録と、検出済みの顔の割り当てを行う GUI。

``scan`` が写真から顔を検出して貯めたあと、ここで人物を作り、顔サムネイルを
選んで人物へ割り当てる。割り当て済みの顔が ``match`` の手本になる。

顔の件数は数万件になりうるので、一覧は必ずページ単位で読む。サムネイルの
BLOB を全件読むと数百MBになり、画面が固まる。
"""

import argparse
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import db, face, matcher, migration, recommend, scoring, undo
# 写真の選定タブと同じ「フォーカスの無いときはホイールを受けない」部品（写しを作らない）。
# gui_select は gui を関数の中でしか import しないので、循環しない
from .gui_select import WheelSafeSlider, WheelSafeSpinBox
from .config import find_settings_path

# **日付の判断は `dates` に1つだけ持つ。** ここで再公開しているのは、
# `gui.parse_date` を参照している呼び出しとテストを壊さないため。
# **この module に写しを作らないこと**（`db.SHOOTING_DATE_SORT_KEY` が
# SQL 側の写しで、そちらと食い違うと表示と並び順がずれる）。
from .dates import Taken, age_at, as_taken, calculate_age, format_taken, parse_date, taken_at  # noqa: F401

PAGE_SIZE = 200
THUMBNAIL_SIZE = 120

#: 顔の一覧の1枠の大きさ。**サムネイルの寸法に任せない。**
#:
#: `setUniformItemSizes(True)` は**先頭の項目から枠の寸法を決める。** 保存して
#: あるサムネイルは大きさがまちまちなので（短辺の中央 160px・**112px 未満が
#: 7.3%**）、**先頭にたまたま小さい顔が来たページでは、枠がその顔に合わせて
#: 縮み、残りのサムネイルが切り詰められて下の文字も枠の外に出る**（利用者が
#: 報告。実データの未分類1ページ目は先頭が 101px・残りが 160px だった）。
#:
#: **枠を固定すれば、どの顔が先頭に来ても同じ見た目になる。**
#: 高さは「サムネイル＋文字2行」ぶん（人物の表示の自動割当は
#: `13391 (自動 55) (0歳)` のように長い）。
ITEM_WIDTH = THUMBNAIL_SIZE + 44
ITEM_HEIGHT = THUMBNAIL_SIZE + 48

#: 撮影年月の範囲で「端を決めない」を表す表示。
#:
#: **「指定なし」と具体的な年月を同じ値で表さない。** 下限だけ・上限だけの
#: 指定ができる必要がある（「2015-06 以降すべて」など）。
MONTH_ANY = "指定なし"

#: 撮影日時が読めない顔だけを見るときの表示。
#:
#: **EXIF の無い顔だけ**を見る指示（フォルダ名から推測した撮影時期がある顔も入る）。
#: **範囲とは別の問いなので併用しない。** 推測した区間が範囲にまるごと入る顔は、
#: 範囲の側にも出る（#65）。
MONTH_UNDATED = "撮影日時なしのみ"

FILTER_UNASSIGNED = "未分類"
FILTER_REJECTED = "顔認証外"
FILTER_PENALIZED = "他人(減点)"

#: 左の一覧が表す「いま何を見ているか」。
#:
#: **人物と同じ一覧に並べる。** 未分類の割り当てと、割り当て済みの見直しは
#: 同じ作業の表裏なので、画面を分けると開き直しの往復が要る（以前は
#: 「割り当て済みを確認」が別ウィンドウだった）。
SCOPE_UNASSIGNED = "unassigned"
SCOPE_REJECTED = "rejected"
#: 顔認証外にした顔のうち、`select` で減点する印を付けた顔（#110）。顔認証外の一部（#113）。
SCOPE_PENALIZED = "penalized"
SCOPE_PERSON = "person"
#: 人物を選び、種別を「未分類」にしたとき（#69）。**左の一覧の項目ではない**
#: （`_view_key` が作る）。並びの既定と、意味を持たない並びを決めるのに使う。
SCOPE_PERSON_UNASSIGNED = "person_unassigned"

#: 一覧の項目が持つ「表示」と「説明」。
#:
#: **人物の辞書と同じ役割に混ぜない。** 混ぜると「`scope` という鍵があるか」で
#: 見分けることになり、DBの列が増えた日に壊れる。
SCOPE_ROLE = Qt.UserRole + 1
DESCRIPTION_ROLE = Qt.UserRole + 2

#: 人物より上に置く表示。``(scope, 表示名, 説明)``。
VIEW_SCOPES = (
    (
        SCOPE_UNASSIGNED,
        FILTER_UNASSIGNED,
        "まだ誰にも割り当てていない顔。人物に割り当てるか、"
        "家族の誰でもない顔として外す。",
    ),
    (
        SCOPE_REJECTED,
        FILTER_REJECTED,
        "「家族の誰でもない顔」として外した顔。未分類に戻せる。"
        "他人(減点)にする印を付けた顔は、下の他人(減点)へ移る。",
    ),
    (
        SCOPE_PENALIZED,
        FILTER_PENALIZED,
        "顔認証外にした顔のうち、写っている写真を select で後ろへ回す顔。"
        "「顔認証外に戻す」で顔認証外へ戻る。",
    ),
)

#: 並び順の選択肢。``(表示名, 並び)``。
#:
#: **向きは対にして隣に置く**（#69 のコメント「高い順という選択があるなら
#: 低い順もあるべき」）。値を持たない顔はどちらの向きでも最後。
#: 「似た順」は db では並べられない（手本との距離が要る。`recommend`）。
ORDER_CHOICES = (
    ("撮影日時の新しい順", db.ORDER_SHOT_DESC),
    ("撮影日時の古い順", db.ORDER_SHOT_ASC),
    ("年齢の若い順", db.ORDER_AGE),
    ("年齢の高い順", db.ORDER_AGE_DESC),
    ("自動の確信度が低い順", db.ORDER_SCORE_ASC),
    ("自動の確信度が高い順", db.ORDER_SCORE_DESC),
    ("画質の高い順", db.ORDER_QUALITY),
    ("画質の低い順", db.ORDER_QUALITY_ASC),
    ("ボケている順", db.ORDER_SHARPNESS_ASC),
    ("くっきりした順", db.ORDER_SHARPNESS_DESC),
    ("この人物に似た順", recommend.ORDER_SIMILAR),
    ("この人物に似ていない順", recommend.ORDER_DISSIMILAR),
)

#: 表示ごとの既定の並び。**その表示で何をするかで決まる。**
#:
#: - 未分類・顔認証外・他人(減点): 撮影日時の新しい順。**同じ行事の写真が固まる**ので、
#:   まとめて選んで一度に割り当てられる（#53）
#: - 人物: 年齢の若い順。**成長の順に並ぶ**ので、年齢の入れ間違いや
#:   別人の混入に気づきやすい（#53）
#: - 人物の未分類: この人物に似た順。**本人を目で探さずに済む**（#69。
#:   2026-10-08 の測定で、本人の 94.0% が1ページ目に入った）
DEFAULT_ORDER = {
    SCOPE_UNASSIGNED: db.ORDER_SHOT_DESC,
    SCOPE_REJECTED: db.ORDER_SHOT_DESC,
    SCOPE_PENALIZED: db.ORDER_SHOT_DESC,
    SCOPE_PERSON: db.ORDER_AGE,
    SCOPE_PERSON_UNASSIGNED: recommend.ORDER_SIMILAR,
}

#: 表示ごとに**意味を持たない並び**と、その理由。押せなくして理由を出す。
#:
#: **隠さずに押せなくする**（隠すと「そんな機能は無い」と思われる）。
#: 未分類と顔認証外の顔は人物が決まっていないので年齢を出せず、確信度も
#: 持たない（実データの未分類 31,275 件で、年齢は 2 件・確信度は 0 件）。
#: 選べると **id 順のまま何も変わらず**、並べ替えが壊れているように見える。
_NO_PERSON_TO_COMPARE = (
    "似ているかを比べる人物がいません。左で人物を選んでください"
    "（未分類の顔は、人物を選んで種別を「未分類」にすると似た順に並びます）"
)
_REJECTED_UNUSABLE = {
    db.ORDER_AGE: "顔認証外にした顔は人物を持たないので、年齢を出せません",
    db.ORDER_AGE_DESC: "顔認証外にした顔は人物を持たないので、年齢を出せません",
    db.ORDER_SCORE_ASC: "顔認証外にした顔は自動割り当ての確信度を持ちません",
    db.ORDER_SCORE_DESC: "顔認証外にした顔は自動割り当ての確信度を持ちません",
    recommend.ORDER_SIMILAR: _NO_PERSON_TO_COMPARE,
    recommend.ORDER_DISSIMILAR: _NO_PERSON_TO_COMPARE,
}
ORDER_UNUSABLE = {
    SCOPE_UNASSIGNED: {
        db.ORDER_AGE: "未分類の顔は人物が決まっていないので、年齢を出せません",
        db.ORDER_AGE_DESC: "未分類の顔は人物が決まっていないので、年齢を出せません",
        db.ORDER_SCORE_ASC: "未分類の顔は自動割り当ての確信度を持ちません",
        db.ORDER_SCORE_DESC: "未分類の顔は自動割り当ての確信度を持ちません",
        recommend.ORDER_SIMILAR: _NO_PERSON_TO_COMPARE,
        recommend.ORDER_DISSIMILAR: _NO_PERSON_TO_COMPARE,
    },
    SCOPE_REJECTED: _REJECTED_UNUSABLE,
    # 他人(減点)は顔認証外の一部なので、押せない並びも同じ。
    SCOPE_PENALIZED: _REJECTED_UNUSABLE,
    SCOPE_PERSON_UNASSIGNED: {
        db.ORDER_SCORE_ASC: "未分類の顔は自動割り当ての確信度を持ちません",
        db.ORDER_SCORE_DESC: "未分類の顔は自動割り当ての確信度を持ちません",
    },
}

#: 右クリックのメニューに出す操作の名前。**用語は仕様書 §1.3 に揃える。**
ACTION_CONFIRM = "手本に確定"
ACTION_UNASSIGN = "未分類に戻す"
ACTION_DETACH = "割り当てを解除"
ACTION_NOT_THIS_PERSON = "この人物ではない"
#: 人物の表示では「この人物ではない」を行き先の小さなメニューにする（#113・利用者の指定）。
#: どの行き先でも「この人物ではない」と記録し、そのうえで顔を移す。
NOT_THIS_PERSON_MENU = ACTION_NOT_THIS_PERSON
ACTION_NOT_THIS_PERSON_TO_UNASSIGNED = "未分類へ"
ACTION_NOT_THIS_PERSON_STAY_UNASSIGNED = "未分類のまま"
ACTION_NOT_THIS_PERSON_TO_REJECTED = "顔認証外へ"
ACTION_NOT_THIS_PERSON_TO_PENALIZED = "他人(減点)へ"
ACTION_REJECT = "顔認証外にする…"
ACTION_SET_AGE = "年齢を設定…"
ACTION_UNDO_REJECTION = "「この人物ではない」を取り消す"
ACTION_PENALIZE = "他人(減点)にする"
ACTION_UNPENALIZE = "顔認証外に戻す"
#: 顔の操作を元に戻す・やり直す（#116）。**打鍵 `Z`（「この人物ではない」の取り消し）とは別物。**
ACTION_UNDO = "元に戻す"
ACTION_REDO = "やり直す"
UNDONE = "元に戻しました"
REDONE = "やり直しました"
UNDO_SHORTCUTS = ("Ctrl+Z",)
REDO_SHORTCUTS = ("Ctrl+Shift+Z", "Ctrl+Y")


def undo_write_failed(error: Exception) -> str:
    """戻す・やり直すで DB に書けなかったときの知らせ（PR #117 のレビュー指摘1）。**操作は履歴に残る。**"""
    return (
        f"データベースに書けませんでした（{error}）。\n\n"
        "ほかの処理（端末の match など）が書き込み中かもしれません。"
        "何も変えていないので、しばらくしてからもう一度押してください。"
    )
#: 他人(減点)の顔（#110）の一覧の印。
PENALIZE_MARK = "減点"
ASSIGN_MENU = "人物に割り当て"
ASSIGN_MENU_OTHER = "別の人物に割り当て"
#: 顔の割り当て画面の左（人物の一覧）の既定の幅（#118）。下の「自動割り当て（match）を実行…」が収まる幅。
PERSON_PANEL_WIDTH = 220


def _format_timestamp(value: Optional[str]) -> Optional[str]:
    """DB の ISO 文字列を "YYYY-MM-DD HH:MM:SS" にする。読めなければ ``None``。

    **カメラが壊れた撮影日時を書くことがある。** `scanner.extract_exif_datetime`
    は EXIF を機械的に整形するだけなので、そのまま保存される。実データでは
    2種類あった。

    | 保存されている値 | 件数(Media) |
    |---|---|
    | `0000-00-00T00:00:00` | 55 |
    | `TTTT-TT-TTTTT:TT:TT` | 67 |

    日付として読めないものをそのまま出すと、**撮影日時を持っているように見えて
    ファイル日時のフォールバックも消える**。いちばん手がかりが要る写真で
    手がかりが減るので、持っていないのと同じ扱いにする。

    **先頭の文字だけを見て弾かない。** `0000` だけを見ていたので
    `TTTT-TT-TTTTT:TT:TT` が素通りし、画面にそのまま出ていた。
    読めるかどうかの判断は `parse_date` に任せる（**同じ判断を2か所に
    書かない**。書くと、片方だけ直したときに表示と年齢が食い違う）。
    """
    if parse_date(value) is None:
        return None
    return str(value).replace("T", " ")[:19]


def format_age(age: Optional[int], inferred: bool = False) -> Optional[str]:
    """年齢を画面に出す形にする。計算できていなければ ``None``。

    ``inferred`` は**フォルダ名から起こした撮影時期で計算した**年齢（#65）。
    末尾に `?` を付けて、EXIF の撮影日時から計算したものと見分ける。
    """
    if age is None:
        return None
    text = "誕生前" if age < 0 else f"{age}歳"
    return f"{text}?" if inferred else text


def _taken_age(birth_date: Optional[str], taken: Optional[Taken]) -> Optional[str]:
    """``taken`` の時点の年齢を、推測かどうかを添えて出す形にする。"""
    return format_age(age_at(birth_date, taken), bool(taken and taken.inferred))


#: 年・月・日の入力欄で「未入力」を表す値。`QSpinBox` の最小値に置く。
BIRTH_DATE_UNSET = 0


def build_birth_date(year: int, month: int, day: int) -> Optional[str]:
    """年・月・日の3つの入力から、DB に入れる `YYYY-MM-DD` を作る。

    3つとも未入力なら未設定(``None``)。

    **年月日まで必須。** 古い写真では正確な日付が分からないことがあるが、
    **月日の分からない誕生日から年齢は出せない**ので、中途半端に持たない。

    Raises:
        ValueError: 一部だけ入っているとき、または存在しない日付のとき。
            画面にそのまま出す文面を持たせる。
    """
    parts = (year, month, day)
    if all(part == BIRTH_DATE_UNSET for part in parts):
        return None
    if any(part == BIRTH_DATE_UNSET for part in parts):
        raise ValueError(
            "誕生日は年・月・日をすべて入れてください。"
            "\n（3つとも空にすれば未設定になります）"
        )
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # 2月30日のような、暦に無い日。月と日を入れ替えた打ち間違いで起きる。
        raise ValueError(f"{year}年{month}月{day}日 は存在しない日付です。") from None


def split_birth_date(value: Optional[str]) -> tuple:
    """DB の `YYYY-MM-DD` を、入力欄に入れる (年, 月, 日) にする。

    未設定や読めない値は、3つとも未入力として返す。
    """
    parsed = parse_date(value)
    if parsed is None:
        return (BIRTH_DATE_UNSET, BIRTH_DATE_UNSET, BIRTH_DATE_UNSET)
    return (parsed.year, parsed.month, parsed.day)


def suggested_age(
    birth_date: Optional[str], shooting_dates: Sequence[Union[Taken, str, None]]
) -> Optional[int]:
    """年齢ダイアログの初期値。出せないなら ``None``（＝「未設定」で開く）。

    ``shooting_dates`` は**顔1件につき1件**（`db.taken_for_faces`）。
    撮影時期の分からない顔は ``None`` で入ってくる。EXIF の無い顔は
    フォルダ名から起こした区間で、**区間の途中に誕生日があれば分からない**扱い。

    **選択中の顔すべてが同じ年齢に落ちるときだけ**出す。1回の入力が選択中の
    全件に入る（`summarize_selection`）ので、年をまたいで選んでいるときに
    片方の年齢を初期値にすると、**黙って間違いが入る。**

    **「分からない」を捨てない。** 以前は `ages.discard(None)` していたため、
    10件のうち9件が EXIF 無しでも、残る1件の年齢が10件すべての初期値になった。
    **分からない顔が1件でもあれば出さない**（仕様書 §10.3 と `GUI_USAGE.md` が
    明文で約束していること）。

    誕生前（負の値）も出さない。初期値として意味を持たないうえ、
    `FaceAgeDialog` では負の値が「未設定」の席になっている。
    """
    ages = {age_at(birth_date, as_taken(value)) for value in shooting_dates}
    if len(ages) != 1 or None in ages:
        return None
    age = ages.pop()
    return None if age < 0 else age


#: root 1つか、root の並び（#24）。フォルダの表示を相対にするためだけに使う。
SourceRoots = Union[str, Sequence[str], None]


def resolve_root(source_roots: Optional[str]) -> Optional[str]:
    """設定に書かれた相対パスを、**設定ファイルの置き場所**を起点に解く。

    カレントディレクトリを起点にすると、リポジトリ直下以外から起動したときに
    相対化が静かに外れ、`GUI_USAGE.md` が約束している「`source_roots` からの
    相対」ではなく、読めない NFS の絶対パスに戻る。`config` は設定ファイル
    自体を複数の場所から探しているのに、**その中の値だけ cwd 依存**という
    食い違いだった。

    `database_path` など他の相対値はここでは扱わない。設定の解釈を全体で
    変えるのは、明示的な指示が要る（CLAUDE.md §4）。
    """
    from .config import resolve_source_root

    # 起点の決め方は設定の窓の検査と1つ（`config.resolve_source_root`・PR #138 のレビュー指摘1）
    return resolve_source_root(source_roots, find_settings_path())


def format_folder(folder: str, source_roots: SourceRoots = None) -> str:
    """フォルダを、画面に出す形にする。**それを含む root からの相対。**

    絶対パスは長すぎて読めない（実データは
    `${NFS_ROOT}/${SURNAME}/Photo/2011/...`）。どの root の外にある
    ものは絶対パスのまま出す。

    ``source_roots`` は root 1つか、root の並び（#24）。**root が2つ以上なら、相対の前に root の
    名前を付ける**（`Photo/2011/...`・`${PERSON_2}携帯/2021/...`）。どちらの root にも `2021/` が
    あるので、付けないと別のフォルダが同じ名前で並ぶ。root が1つなら今までどおり付けない。

    **プレビューの情報欄と写真の選定タブで、同じ規則を使う。** 別々に書くと、
    同じフォルダが画面によって違う名前で出る。
    """
    if isinstance(source_roots, (str, Path)):
        roots = [source_roots]
    else:
        roots = [root for root in (source_roots or []) if root]
    path = Path(folder)
    for root in roots:
        root_path = Path(root).expanduser().resolve()
        try:
            relative = path.relative_to(root_path)
        except ValueError:
            continue
        if len(roots) == 1:
            # root 直下は "." になる。それでは何のことか読めない。
            return "（root 直下）" if str(relative) == "." else str(relative)
        return root_path.name if str(relative) == "." else f"{root_path.name}/{relative}"
    # どの root の外にあるメディア。絶対パスのまま出す。
    return str(path)


def persons_alive_on(persons: List[dict], day: Union[Taken, str, None]) -> List[dict]:
    """その日にまだ生まれていない人物を外す。

    **束をまとめて割り当てるときに、選べてはいけない人物を消すため。**
    1件ずつの割り当てでは年齢欄が負の数になって気づけるが、まとめて押すときは
    画面に出るのが人物名だけなので、**選べると気づけない。**

    誕生日が未設定の人物は**残す**（分からないことを理由に消さない）。
    ``day`` が読めないときも全員残す。フォルダ名から起こした区間（`Taken`）なら、
    **区間の終わりまでに生まれていない**人物だけを外す。
    """
    taken = as_taken(day)
    if taken is None:
        return list(persons)
    alive = []
    for person in persons:
        born = parse_date(person.get("birth_date"))
        if born is not None and born > taken.latest:
            continue
        alive.append(person)
    return alive


def has_pre_birth_photo(
    birth_date: Optional[str], shooting_dates: Sequence[Union[Taken, str, None]]
) -> bool:
    """選んだ顔に、**その人物が生まれる前の写真**が混ざっているか。

    **誕生前への割り当ては、手本の誤りとしていちばん多い形。** `match` は
    誕生日で候補を外すので（仕様書 §8.3）、この矛盾を作れるのは手作業だけ。
    実データでも「赤ん坊の顔に大人のラベルが付いた手本」が誤一致の原因に
    なっていた（2026-10-06 の測定）。

    **読めない日付は数えない**（判断は `dates.parse_date` に1つだけ）。
    フォルダ名から起こした区間は、**区間の終わりが誕生日より前**のときだけ数える。
    """
    born = parse_date(birth_date)
    if born is None:
        return False
    return any(
        taken is not None and taken.latest < born
        for taken in (as_taken(value) for value in shooting_dates)
    )


def _assign_label(
    index: int, person: dict, shooting_dates: Sequence[Union[Taken, str, None]]
) -> str:
    """「人物に割り当て」の1行。**撮影時の年齢と、誕生前の警告を添える。**

    誰の顔かを決めるとき、いちばん効く手がかりが**撮影時の年齢**
    （仕様書 §10.3）。選んだ顔の年齢が揃うときだけ出す（`suggested_age`）。

    ``index`` は打鍵する数字。1〜9 までは打鍵でも割り当てられる。
    """
    text = f"{index}  {person['name']}"
    age = format_age(suggested_age(person.get("birth_date"), shooting_dates))
    if age:
        text = f"{text}（{age}）"
    if has_pre_birth_photo(person.get("birth_date"), shooting_dates):
        # **選べなくはしない。** 1件だけ混ざった選択を丸ごと止めると、
        # なぜ割り当てられないのかが画面から分からない。
        text = f"{text} ⚠誕生前の写真あり"
    return text


def format_media_info(
    media: dict,
    source_roots: SourceRoots = None,
    person: Optional[dict] = None,
    persons: Optional[List[dict]] = None,
) -> str:
    """プレビューの下に出す、撮影日時とフォルダと、撮影時の年齢。

    **年齢を入れるには、その写真がいつ撮られたか分からないといけない。**
    EXIF の撮影日時は実データの 15.8% で欠けているので、日付を持つことが多い
    フォルダ名も併せて出す。

    ``created_time`` は**撮影日時ではない**（コピーで変わる）。取り違えると
    年齢を間違えるので、EXIF が無いときだけ、別の名前で出す。

    ``person`` を渡すと、その人物の誕生日と撮影日時から**撮影時の年齢**を
    最後の行に出す。人物が未選択・誕生日が未設定・撮影日時が無いのいずれかなら
    **行そのものを出さない**（誤解を招く「不明」を並べるより、無いほうがよい）。

    ``persons`` は**人物を選んでいないとき**（未分類の表示）に渡す。
    その写真の時点で**各人が何歳だったか**を1行にまとめて出す。
    未分類の作業は「この顔は誰か」を決めることなので、**全員の年齢が並んで
    いるほうが効く。** まだ生まれていない人は出さない（`persons_alive_on`）。
    """
    lines = []
    taken = taken_at(
        media.get("shooting_date"), media.get("folder_date_from"), media.get("folder_date_to")
    )
    shooting_date = _format_timestamp(media.get("shooting_date"))
    if shooting_date:
        lines.append(f"撮影日時: {shooting_date}")
    else:
        lines.append("撮影日時: 不明（EXIFなし）")
        guessed = format_taken(taken)
        if guessed:
            # **推測だと分かる名前で出す。** EXIF の撮影日時と同じ行に入れない。
            lines.append(f"撮影時期: {guessed}（フォルダ名から推測）")
        file_time = _format_timestamp(media.get("created_time"))
        if file_time:
            lines.append(f"ファイル日時: {file_time}")

    path = Path(media.get("path", ""))
    # **フォルダの出し方を写さない。** `format_folder` の規則を通す
    # （別々に書くと、同じフォルダが画面によって違う名前で出る）。
    lines.append(f"フォルダ: {format_folder(str(path.parent), source_roots)}")
    lines.append(f"ファイル: {path.name}")

    if person:
        age = _taken_age(person.get("birth_date"), taken)
        if age:
            lines.append(f"{person.get('name') or '?'}: {age}")
    elif persons:
        ages = []
        for candidate in persons_alive_on(persons, taken):
            age = _taken_age(candidate.get("birth_date"), taken)
            if age:
                ages.append(f"{candidate.get('name') or '?'} {age}")
        if ages:
            lines.append("撮影時の年齢: " + " / ".join(ages))
    return "\n".join(lines)


#: 登録が済んだ顔のプレビューを、どこまで薄くするか。
DONE_PREVIEW_OPACITY = 0.35

#: これより短い作業では、進み具合の窓を出さない（ミリ秒）。
#: **一瞬だけ出て消える窓は、出さないより煩わしい。** `QProgressDialog` が
#: この時間を超えたときだけ自分で開く。
PROGRESS_POPUP_DELAY_MS = 400
#: 画面の接続が、ほかの接続（裏の `scan`・端末の CLI）の書き込みを待つ秒数（#129）
BUSY_TIMEOUT_MS = 30_000
#: 鮮明さの絞り込みの既定（#135）。`select` が「はっきりボケている」とみなす値と同じ。
BLURRY_DEFAULT = int(scoring.SHARPNESS_BLURRY)
#: 鮮明さの絞り込みの上限。くっきりした顔は数百〜千を超えるが、ボケた顔を探すのに要るのはこのあたりまで。
BLURRY_MAX = 300
#: 鮮明さの絞り込みの説明（#135）
BLURRY_TOOLTIP = (
    "鮮明さ（顔のサムネイルを 112px にそろえて測ったラプラシアン分散）がこの値以下の顔だけを出します。\n"
    "小さいほどボケ・ぶれています。select は 30 以下を「はっきりボケている」、150 以上を「くっきり」とみます。\n"
    "**未計測の顔は出ません**（「鮮明さを測る」で測ります）。\n"
    "未分類を見ているときは、右下のボタンで絞った顔をまとめて顔認証外にできます。"
)
#: 測るボタンの説明（#135）
MEASURE_TOOLTIP = (
    "未計測の顔（未分類・手動・自動）の鮮明さ・向きを、保存済みのサムネイルから測ります（元写真は読みません）。\n"
    "裏で流すので、測っているあいだも割り当てを続けられます。止めても、次は続きからになります。"
)
MATCH_BLOCKED_BY_JOB = "裏で処理（スキャン・鮮明さの計測）を流しているあいだは実行できません。終わるか、止めてから実行してください。"
#: まとめ処理を押せない理由（#135）
BULK_BLOCKED_BY_JOB = (
    "裏で処理（スキャン・鮮明さの計測）を流しているあいだは押せません。"
    "一覧に出ていない顔が増えていくため、終わってから押してください。"
)
BULK_NEEDS_FILTER = (
    "撮影年月・撮影日時なしのみ・鮮明さ（人物なら年齢も）で絞ってから押してください。"
    "絞らずに押すと、この表示の顔がすべて対象になります。"
)
WRITE_LOCKED = (
    "データベースがほかの処理（裏のスキャンや、端末で流している photoarchive など）で使われていて、"
    "30 秒待っても書き込めませんでした。この操作では何も変えていません。\n"
    "少し待ってから、もう一度やり直してください。"
)


@contextmanager
def busy_cursor():
    """処理のあいだ、カーソルを砂時計にする。

    **押したことが分かるようにするため。** 実測で、顔を1つ選ぶたびに元写真を
    NFS から読み直して 220ms かかる。その間まったく無反応なので、
    「クリックできたのか」が分からない。

    **例外が出ても必ず戻す。** 戻し忘れると、以後ずっと砂時計のままになる。
    """
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def visible_bounds(window: QWidget) -> QRect:
    """メニューを収める枠。**GUI のウィンドウ**（と Qt の画面の小さいほう）。

    **Qt が知っている画面は、見えている画面と一致するとは限らない**（#118）。
    Windows から X11 転送で開くと、Qt が見るのは X サーバーの root で、
    モニタの区切りやタスクバーが入らない。Qt は収まると思ってメニューを置き、
    実際には下が切れて選べなかった。**ウィンドウは利用者に見えている。**
    """
    window = window.window()
    bounds = window.frameGeometry()
    screen = window.screen()
    if screen is not None:
        bounds = bounds.intersected(screen.availableGeometry())
    return bounds


class KeepMenuInsideWindow(QObject):
    """メニューを GUI のウィンドウの中に収める（#118）。

    **高さはウィンドウまで、超えるぶんはスクロール。** 出たときにはみ出すぶんは
    内側へずらす。サブメニューの位置は Qt が決めるので、出た直後に直す。
    **右に入らないサブメニューは親の左へ出す**（押し戻すだけだと親に重なる）。
    """

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        #: 枠はこの部品の**いまの**トップレベルのウィンドウ（タブに入れても追う）。
        self.widget = widget

    def attach(self, menu: QMenu) -> QMenu:
        """``menu`` とそのサブメニューに掛ける。**サブメニューを足し終えてから呼ぶ。**"""
        # 列に折り返すと、上限の高さを超えたぶんが見えないまま残る（Qt は折り返しを
        # 自分の知っている画面の高さで決める）。
        menu.setStyleSheet("QMenu { menu-scrollable: 1; }")
        # 高さは出す前に決める（スクロールの量は出すときに計算される）。
        menu.aboutToShow.connect(
            lambda: menu.setMaximumHeight(visible_bounds(self.widget).height())
        )
        menu.installEventFilter(self)
        for action in menu.actions():
            if action.menu() is not None:
                self.attach(action.menu())
        return menu

    def eventFilter(self, menu, event) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(menu, QMenu):
            bounds = visible_bounds(self.widget)
            rect = QRect(menu.geometry())
            parent = menu.parentWidget()
            if (
                isinstance(parent, QMenu)
                and parent.isVisible()
                and rect.right() > bounds.right()
            ):
                # 右に入らないサブメニューは親の左へ（Qt が画面の端でするのと同じ）。
                # 押し戻すだけだと親の上に重なり、カーソルの真下の人物にクリック1つで
                # 割り当たった（PR #119 のレビュー指摘1）。
                rect.moveRight(parent.geometry().left() - 1)
            menu.move(clamp_into(rect, bounds))
        return False


def clamp_into(rect: QRect, bounds: QRect) -> QPoint:
    """``rect`` を ``bounds`` の内側へずらした左上。**収まらない向きは上端・左端に揃える。**"""
    x = max(bounds.left(), min(rect.left(), bounds.right() - rect.width() + 1))
    y = max(bounds.top(), min(rect.top(), bounds.bottom() - rect.height() + 1))
    return QPoint(x, y)


class WorkProgress:
    """件数の分かる作業の進み具合を出す。``db`` の ``progress`` に渡せる。

    **短い作業では何も出さない。** `QProgressDialog` は
    `setMinimumDuration` を超えて初めて自分を開くので、1件の割り当てのように
    すぐ終わるものでは窓が出ない。

    **取り消しボタンは置かない。** 書き込みの途中で止めると、半分だけ
    適用された状態になる。
    """

    def __init__(self, parent, label: str, total: int, delay_ms: int = PROGRESS_POPUP_DELAY_MS):
        # **自分が書いた文字を読み直さない。** `labelText()` から元の文言を
        # 取り出す作りだと、人物名に `（）` が入っていたときに名前が欠ける
        # （「父（実父） に割り当てています」→「父（0 / 120 件）」）。
        self.base_label = label
        self.dialog = QProgressDialog(label, "", 0, max(total, 1), parent)
        self.dialog.setWindowTitle("処理中")
        self.dialog.setCancelButton(None)
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(delay_ms)
        self.dialog.setValue(0)

    def __call__(self, done: int, total: int) -> None:
        """``db`` から呼ばれる通知口。"""
        self.dialog.setMaximum(max(total, 1))
        self.dialog.setLabelText(f"{self.base_label}（{done} / {total} 件）")
        self.dialog.setValue(done)
        # **描き直さないと、窓が白いままになる。** 同期処理の途中なので、
        # ここで明示的にイベントを回す。
        QApplication.processEvents()

    def step(self, label: str) -> None:
        """件数で測れない工程に移ったことを知らせる（一覧の作り直しなど）。"""
        self.base_label = label
        self.dialog.setLabelText(label)
        QApplication.processEvents()

    def finish(self) -> None:
        self.dialog.setValue(self.dialog.maximum())
        self.dialog.close()


def dim_pixmap(pixmap: QPixmap, opacity: float = DONE_PREVIEW_OPACITY) -> QPixmap:
    """画像を薄くした複製を返す。**元の画像は変えない。**

    登録の済んだ顔だと**一目で分かる**ようにするため。文字だけで知らせると、
    次の顔を選ぶまで前の顔がそのままの濃さで残り、「まだ選んでいる」ように
    見える。
    """
    if pixmap.isNull():
        return pixmap
    dimmed = QPixmap(pixmap.size())
    dimmed.fill(Qt.transparent)
    painter = QPainter(dimmed)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return dimmed


def _select_on_focus(spin: QSpinBox) -> QSpinBox:
    """特別な文字（「未設定」など）が入った QSpinBox を、打鍵で置き換えられるようにする。

    `setSpecialValueText` を使うと、入力欄には数字ではなく「未設定」という
    **文字**が入っている。その状態で数字を打つと "未設定5" という文字列になり、
    検証に落ちて**何も起きない。** 利用者からは「▲を押さないと入力できない」
    ように見える。

    開いた時点で全選択しておけば、打った数字がそのまま置き換わる。
    """
    spin.setFocus()
    spin.selectAll()
    return spin


def _make_select_all_on_focus(spin: QSpinBox):
    """フォーカスが入ったら全選択する `focusInEvent` を作る。

    常時フォーカスしてよい入力（ダイアログの主役）と違い、画面に並ぶ入力欄は
    **触られたときだけ**選択したい。
    """
    original = spin.__class__.focusInEvent

    def focus_in(event):
        original(spin, event)
        spin.selectAll()

    return focus_in



class MigrationDialog(QDialog):
    """起動時に「移行が要る」と分かったときに出す確認。

    **端末へ追い出さない。** これまでは「`photoarchive migrate` を実行して
    ください」と言って終了していたが、GUI しか使わない利用者にとっては
    そこで手が止まる。**その場で実行できるようにする。**

    何が残って何が消えるかは `migration.describe_for_operator` が作る。
    CLI と同じ文面を使う（2か所に書くと、片方だけ「破棄します」のまま残る）。
    """

    def __init__(self, parent=None, summary: str = "", backs_up: bool = True):
        super().__init__(parent)
        self.setWindowTitle("データベースの移行が必要です")
        layout = QVBoxLayout(self)

        headline = QLabel("このデータベースは、いまのアプリケーションより古い形です。")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        detail = QLabel(summary)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.setStyleSheet("padding: 8px; border: 1px solid #999;")
        layout.addWidget(detail)

        if backs_up:
            note = QLabel("実行する前に、自動でバックアップを取ります。")
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QDialogButtonBox()
        # **既定のボタンを「実行」にしない。** Enter の連打で、内容を読まないまま
        # 走り出すのを避ける。
        self.run_button = buttons.addButton("移行を実行", QDialogButtonBox.ButtonRole.AcceptRole)
        self.quit_button = buttons.addButton("終了", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._prefer_quit()

    def _prefer_quit(self) -> None:
        """既定のボタンを「終了」にする。

        `QDialogButtonBox` は表示のたびに既定を付け直すので、`showEvent` でも
        やり直す。ここを外すと、**Enter の連打で内容を読まないまま走り出す。**
        """
        self.run_button.setAutoDefault(False)
        self.run_button.setDefault(False)
        self.quit_button.setAutoDefault(True)
        self.quit_button.setDefault(True)

    def showEvent(self, event):
        super().showEvent(event)
        self._prefer_quit()


def _format_size(byte_count: int) -> str:
    """控えの大きさの目安。**空き容量を確かめてもらうため**に出す。"""
    if byte_count >= 1024 ** 3:
        return f"{byte_count / 1024 ** 3:.1f}GB"
    return f"{max(byte_count, 1) / 1024 ** 2:.0f}MB"


class MatchDialog(QDialog):
    """`match`（自動割り当て）を GUI から流す前の確認。

    **端末へ追い出さない。** GUI で手本を増やしたら、その場で `match` を流して
    結果を見たい。毎回端末へ移ると、そこで手が止まる。

    **控えを取るかは毎回選べる。既定は「取る」。** `match` は自動割り当てを
    いったん外して付け直すので、**控えがあれば流す前の状態へ戻せる**し、
    結果も後から再現できる（`match` は決定的。2026-10-08 に、利用者が解除した
    1,778 件を流す前の控えから特定できた）。毎回 DB と同じ大きさのファイルが
    できるので、外せるようにもしてある（利用者の選択・2026-10-09）。

    **左で人物を選んでいれば、その人物だけを付け直すこともできる**（#123。
    `photoarchive match --person` と同じ）。既定は全員（いままでどおり）。
    人物を選んでいないときは、選ぶ欄そのものを出さない。
    """

    def __init__(
        self,
        parent,
        counts: dict,
        database_path: str,
        person: Optional[dict] = None,
        person_auto: int = 0,
    ):
        super().__init__(parent)
        self.setWindowTitle("自動割り当て（match）を実行")
        self.counts = counts
        self.person = person
        self.person_auto = int(person_auto)
        layout = QVBoxLayout(self)

        self.everyone_button = QRadioButton("全員を付け直す", self)
        self.person_button = QRadioButton(
            f"{person['name']} だけを付け直す（ほかの人物の自動割当には触らない）"
            if person
            else "",
            self,
        )
        self.everyone_button.setChecked(True)
        if person is not None:
            layout.addWidget(self.everyone_button)
            layout.addWidget(self.person_button)
        else:
            self.everyone_button.hide()
            self.person_button.hide()

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet("padding: 8px; border: 1px solid #999;")
        layout.addWidget(self.detail)
        self._update_detail()
        self.everyone_button.toggled.connect(self._update_detail)

        source = Path(database_path)
        size = source.stat().st_size if source.exists() else 0
        self.backup_box = QCheckBox(
            f"実行する前に控えを取る（{source.name}.bak-<日時>・約 {_format_size(size)}）"
        )
        self.backup_box.setChecked(True)
        layout.addWidget(self.backup_box)

        note = QLabel("途中で止めることはできません（半分だけ付け直した状態になるため）。")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        # **既定のボタンを「実行」にしない**（`MigrationDialog` と同じ理由）。
        self.run_button = buttons.addButton("実行", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("やめる", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._prefer_cancel()

    def _prefer_cancel(self) -> None:
        self.run_button.setAutoDefault(False)
        self.run_button.setDefault(False)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)

    def showEvent(self, event):
        super().showEvent(event)
        self._prefer_cancel()

    def makes_backup(self) -> bool:
        return self.backup_box.isChecked()

    def chosen_person_id(self) -> Optional[int]:
        """付け直す人物。全員なら ``None``（`matcher.match_faces` の ``person_id``）。"""
        if self.person is not None and self.person_button.isChecked():
            return int(self.person["id"])
        return None

    def _update_detail(self, *_args) -> None:
        if self.chosen_person_id() is None:
            scope = (
                f"手本 {self.counts['manual']:,} 件をもとに、未分類と自動割当の顔を付け直します。\n"
                f"いまの自動割当 {self.counts['auto']:,} 件は、いったん外してから付け直します。\n\n"
            )
        else:
            name = self.person["name"]
            scope = (
                f"手本 {self.counts['manual']:,} 件（全員の手本を対抗馬として使います）をもとに、"
                f"未分類の顔のうち {name} にいちばん近い顔だけを割り当てます。\n"
                f"{name} のいまの自動割当 {self.person_auto:,} 件は、いったん外してから付け直します。\n"
                "ほかの人物の自動割当は外さず、ほかの人物に近い顔は未分類のまま残します。\n\n"
            )
        self.detail.setText(
            scope
            + "変わらないもの: 手本（手動の割り当て）・顔認証外・「この人物ではない」・年齢\n"
            "「割り当てを解除」しただけの顔は、また付くことがあります"
            "（判断を残すには「この人物ではない」を使います）。\n\n"
            f"閾値 {matcher.DEFAULT_THRESHOLD} / マージン {matcher.DEFAULT_MARGIN}"
            "（photoarchive match と同じ既定）"
        )


def format_match_summary(
    summary: dict, persons: List[dict], backup: Optional[Path]
) -> str:
    """`match` を流したあとに出す結果。**CLI の出力と同じ数字を出す。**"""
    names = {int(person["id"]): person["name"] for person in persons}
    lines = [
        f"手本 {summary['teachers']:,} 件をもとに、{summary['candidates']:,} 件を照合しました。",
        "",
        f"自動で割り当てた顔: {summary['assigned']:,} 件",
        f"未分類のまま: {summary['unassigned']:,} 件",
    ]
    if summary.get("no_candidate"):
        lines.append(
            f"（うち、誕生日で候補が1人も残らなかった顔: {summary['no_candidate']:,} 件）"
        )
    if summary.get("unusable_teachers"):
        lines.append(
            f"5点整列ができなかった手本 {summary['unusable_teachers']:,} 件は、"
            "割り当ての根拠にしていません（2位の対抗馬としては使います）。"
        )
    per_person = summary.get("per_person") or {}
    if per_person:
        lines.append("")
        lines.append("人物ごと:")
        for person_id, count in sorted(per_person.items(), key=lambda item: -item[1]):
            lines.append(f"  {names.get(int(person_id), person_id)}: {count:,} 件")
    if summary.get("person_id") is not None:
        name = names.get(int(summary["person_id"]), summary["person_id"])
        if not summary.get("person_teachers"):
            lines.append("")
            lines.append(
                f"{name} の根拠にできる手本がありません（5点整列ができない手本だけのときも"
                "含みます）。顔のよく写った写真を割り当ててください。"
            )
        if summary.get("left_for_others"):
            lines.append("")
            lines.append(
                f"1位がほかの人物だった顔 {summary['left_for_others']:,} 件は、"
                "割り当てずに残しました（全員を付け直すと付きます）。"
            )
    lines.append("")
    lines.append(f"控え: {backup}" if backup else "控え: 取っていません")
    return "\n".join(lines)


def ensure_migrated(database_path: str, parent=None) -> bool:
    """必要なら移行の確認を出し、実行する。**先へ進んでよいか**を返す。

    移行が要らなければ何も出さずに ``True``。利用者が「終了」を選んだとき、
    または移行に失敗したときは ``False``。
    """
    if not migration.needs_migration(database_path):
        return True
    try:
        # **確認を聞く前に。** 同名の人物がいると移行は止まる（#90）。CLI と同じ順
        migration.check_person_names(database_path)
    except migration.DuplicatePersonNames as error:
        QMessageBox.critical(parent, "移行できませんでした", str(error))
        return False

    dialog = MigrationDialog(parent, summary=migration.describe_for_operator(database_path))
    if dialog.exec() != QDialog.Accepted:
        return False

    messages: List[str] = []
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        migration.migrate_database(database_path, log=messages.append)
    except Exception as error:
        # **何が起きたかを画面に出す。** 端末を見ずに起動されることがある。
        detail = [str(error)]
        if messages:
            # どこまで進んでいたか（バックアップを取ったかどうかを含む）。
            detail += ["", "ここまでの記録:"] + messages
        QMessageBox.critical(parent, "移行できませんでした", "\n".join(detail))
        return False
    finally:
        QApplication.restoreOverrideCursor()

    QMessageBox.information(parent, "移行が完了しました", "\n".join(messages))
    return True


class PersonDialog(QDialog):
    """人物の追加・編集。誕生日は任意で、入れると撮影時の年齢を出せる。"""

    def __init__(
        self, parent=None, name="", relation="", memo="", birth_date="", close=False, weight=1.0
    ):
        super().__init__(parent)
        self.setWindowTitle("人物情報")
        self.name_input = QLineEdit(name)
        self.relation_input = QLineEdit(relation)
        self.memo_input = QTextEdit(memo)
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)
        # 区分と重み（#110）。親しい人だけの写真は select で家族の写真に数えない
        self.category_input = QComboBox()
        self.category_input.addItem("家族", False)
        self.category_input.addItem("親しい人（家族と一緒に写れば加点）", True)
        self.category_input.setCurrentIndex(1 if close else 0)
        self.weight_input = QDoubleSpinBox()
        self.weight_input.setDecimals(2)
        self.weight_input.setRange(0.0, 100.0)
        self.weight_input.setSingleStep(0.1)
        self.weight_input.setValue(float(weight))

        form = QFormLayout()
        form.addRow("名前", self.name_input)
        form.addRow("続柄", self.relation_input)
        form.addRow("誕生日", self._build_birth_date_row(birth_date))
        form.addRow("区分", self.category_input)
        form.addRow("select の重み", self.weight_input)
        form.addRow("メモ", self.memo_input)
        form.addWidget(self.ok_button)
        self.setLayout(form)

    def _build_birth_date_row(self, birth_date) -> QWidget:
        """`[2011]年 [5]月 [3]日` の入力欄。

        1つの欄に `YYYY-MM-DD` と打たせると、区切りの書き方（`/` か `-` か）を
        間違えただけで弾かれる。**年・月・日に分ければ、書式を間違えようがない。**

        `QDateEdit` は「未設定」を表せないので使わない。3つとも空（`0`）が未設定。
        """
        year, month, day = split_birth_date(birth_date)
        self.birth_year = QSpinBox()
        self.birth_year.setRange(BIRTH_DATE_UNSET, 2200)
        self.birth_year.setValue(year)
        self.birth_month = QSpinBox()
        self.birth_month.setRange(BIRTH_DATE_UNSET, 12)
        self.birth_month.setValue(month)
        self.birth_day = QSpinBox()
        self.birth_day.setRange(BIRTH_DATE_UNSET, 31)
        self.birth_day.setValue(day)

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for spin, unit, width in (
            (self.birth_year, "年", 80),
            (self.birth_month, "月", 60),
            (self.birth_day, "日", 60),
        ):
            # 未入力であることが分かるようにする。0 のままだと「0年0月0日」を
            # 入れたように見える。
            spin.setSpecialValueText("--")
            spin.setFixedWidth(width)
            # 「--」の文字が入った欄は、全選択しておかないと打鍵で置き換わらない
            # （年齢の入力と同じ。▲を押すしかなくなる）。
            spin.focusInEvent = _make_select_all_on_focus(spin)
            layout.addWidget(spin)
            layout.addWidget(QLabel(unit))
        layout.addStretch(1)
        return row

    def values(self):
        """入力された 名前 / 続柄 / メモ / 誕生日 `(年, 月, 日)`。

        誕生日は**打たれた数字のまま**返す。組み立てと検証は呼び出し側で行う。
        名前が空のときと同じ場所でまとめて弾きたいため。
        """
        return (
            self.name_input.text().strip(),
            self.relation_input.text().strip(),
            self.memo_input.toPlainText().strip(),
            (self.birth_year.value(), self.birth_month.value(), self.birth_day.value()),
        )

    def scoring_values(self):
        """区分（親しい人なら True）と `select` の重み（#110）。"""
        return bool(self.category_input.currentData()), float(self.weight_input.value())


def summarize_selection(
    face_count: int, shooting_dates: Sequence[Union[Taken, str, None]]
) -> str:
    """年齢ダイアログに出す「何に入れるのか」の1〜2行。

    **1回の入力が選択中の全件に入る**のに、プレビューに出ているのは最後に
    選んだ1枚の撮影日時だけ。撮影年をまたいで選ぶと、画面の日時を見て入れた
    年齢が別の年の顔にも入る。件数と、撮影日時の範囲を見せて気づけるようにする。

    ``shooting_dates`` は**顔1件につき1件**。読めない値（`0000-00-00` など）と
    撮影日時の無い顔を **`parse_date` で外してから**範囲を作る。外さないと、
    `_format_timestamp` が「撮影日時: 不明」と出している写真が、同じ画面で
    日付を持っているように見える。

    **撮影日時の分からない顔があれば、その件数も出す。** 初期値が入らない
    理由がこれなので、黙っていると「なぜ空欄なのか」が分からない。

    1件だけの選択なら、プレビューと食い違わないので出さない。
    """
    if face_count <= 1:
        return ""
    takens = [taken for taken in (as_taken(value) for value in shooting_dates) if taken]
    unknown = face_count - len(takens)

    if not takens:
        return f"{face_count} 件すべてに同じ年齢を入れます（撮影日時は不明）。"

    first = min(taken.earliest for taken in takens).isoformat()
    last = max(taken.latest for taken in takens).isoformat()
    if first == last:
        lines = [f"{face_count} 件すべてに同じ年齢を入れます（撮影日時 {first}）。"]
    else:
        # QLabel は Markdown を解釈しないので、装飾記号を書かない（そのまま出る）。
        lines = [
            f"{face_count} 件すべてに同じ年齢を入れます。",
            f"撮影日時が {first} 〜 {last} にまたがっています。",
        ]
    guessed = sum(1 for taken in takens if taken.inferred)
    if guessed:
        lines.append(f"うち {guessed} 件はフォルダ名から推測した撮影時期です。")
    if unknown:
        lines.append(f"うち {unknown} 件は撮影日時が分かりません。")
    return "\n".join(lines)


class FaceAgeDialog(QDialog):
    """撮影時の年齢を任意で入力する。未設定と0歳は区別する。

    ``initial_age`` を渡すと、その値を入れた状態で開く（誕生日と撮影日時から
    計算した値。`suggested_age`）。**あくまで初期値で、自動保存はしない。**
    利用者が OK を押して初めて `Face.age` に入る（Issue #48 の判断2）。
    """

    def __init__(self, parent=None, summary: str = "", initial_age: Optional[int] = None):
        super().__init__(parent)
        self.setWindowTitle("撮影時の年齢")
        self.age_input = QSpinBox()
        # 最小値を -1 にして「未設定」に割り当てる。0 を特別扱いにすると
        # 0歳の顔を登録できなくなる。
        self.age_input.setRange(-1, 150)
        self.age_input.setSpecialValueText("未設定")
        if initial_age is not None and 0 <= initial_age <= 150:
            self.age_input.setValue(initial_age)
            # **機械が入れた値だと分かるようにする。** 黙って数字が入っていると、
            # 利用者が確かめた年齢なのか計算値なのか、あとから区別できない。
            summary = "\n".join(
                part for part in (summary, "誕生日から計算した年齢を入れてあります。") if part
            )
        else:
            self.age_input.setValue(-1)
        self.summary = summary
        # 「未設定」の文字が入ったままなので、全選択しておかないと
        # キーボードから数字を入れられない（▲を押すしかなくなる）。
        _select_on_focus(self.age_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        if summary:
            summary_label = QLabel(summary)
            summary_label.setWordWrap(True)
            layout.addRow(summary_label)
        layout.addRow("撮影時の年齢", self.age_input)
        layout.addRow(buttons)

    def showEvent(self, event):
        """開くたびに入力欄を選択しておく。

        `__init__` での選択は、ダイアログが表示されるときに解除されることが
        ある。**開いた直後に数字を打てる**ことが大事なので、ここでもやり直す。
        """
        super().showEvent(event)
        _select_on_focus(self.age_input)

    def age(self) -> Optional[int]:
        value = self.age_input.value()
        return None if value < 0 else value


#: 「割り当て済みの顔」の種別の絞り込み。**(表示, `assign_source` に渡す値)**。
#:
#: `None` は「種別で絞らない」。**`db.ASSIGN_MANUAL` と同じ意味に使わない**
#: （`_face_filter` の `day` と同じで、「絞らない」と「この値だけ」を同じ値で
#: 表すと取り違える。CLAUDE.md §8）。
SOURCE_FILTERS = (
    ("すべて", None),
    ("確定済みのみ", db.ASSIGN_MANUAL),
    ("自動のみ", db.ASSIGN_AUTO),
)

#: 手本の無い人物で「似た順」を選んだときに、ページの表示に添える。
NO_TEACHERS_NOTICE = "※ この人物には手本が無いので、似た順に並べられません（id 順）"

#: 種別の選択肢に足す「未分類」（#69）。**その人物の候補を探す入口。**
#: 人物を選んでいるので「この人物に似た順」に並べられる。誕生前の写真と、
#: この人物ではないと記録した顔は出さない（候補になりえない）。
#: `SOURCE_FILTERS` の `None`（＝種別で絞らない）と取り違えないよう別に持つ。
UNASSIGNED_FOR_PERSON_FILTER = "未分類"

#: 種別の選択肢に足す「この人物ではない」。**`assign_source` の値ではない**
#: （`FaceRejection` 表に持つ否定）ので、`SOURCE_FILTERS` とは別に持つ。
#: 押し間違いを見直して取り消すための入口。
NOT_THIS_PERSON_FILTER = "この人物ではない"

#: 確定ボタンのツールチップ。**押せるときと押せないときの両方を1か所に置く。**
#: 文言を2か所に書くと、片方だけ直したときに説明と振る舞いが食い違う。
CONFIRM_TOOLTIP_READY = "自動割当の顔を手動割当に昇格し、match の手本にする"
CONFIRM_TOOLTIP_BLOCKED = (
    "自動割り当ての顔を選んでいるときだけ押せる"
    "（手本はすでに手動割り当てなので、確定しても何も変わらない）"
)


def face_age_label(
    record: dict, birth_date: Optional[str], shooting_date: Union[Taken, str, None]
) -> Optional[str]:
    """一覧の1件に出す年齢。**確定した年齢と、計算しただけの年齢を見分ける。**

    `Face.age` は人が確かめて入れた値で、**`match` は書かない。** そのため
    自動割り当ての顔はすべて未設定で、年齢が1件も出なかった。
    **自動割り当てが正しいかを人が見るとき、撮影時の年齢がいちばん効く手がかり**
    なので、未設定なら人物の誕生日と撮影日時から計算して出す。

    **計算した値は括弧で囲む。** 確定した年齢と同じ見た目にすると、
    どちらが人の確かめた値か分からなくなる（Issue #48 の判断2と同じ理由で、
    計算値を `Face.age` に書き戻すこともしない）。

    撮影日より前に生まれていなければ「誕生前」。**これは誤割り当ての強い
    手がかり**なので、負の数でも落とさずに出す。

    EXIF が無くフォルダ名から起こした撮影時期で計算したものは `(7歳?)` と出す（#65）。
    """
    if record.get("age") is not None:
        return format_age(record["age"])
    computed = _taken_age(birth_date, as_taken(shooting_date))
    return None if computed is None else f"({computed})"


def make_face_list(selection_mode) -> QListWidget:
    """顔のサムネイル一覧を作る。**設定を2か所に書かない。**

    メイン画面と束ねる画面で同じ見た目・同じ枠にする。`ITEM_WIDTH` /
    `ITEM_HEIGHT` の理由はそちらに書いてある。
    """
    widget = QListWidget()
    widget.setViewMode(QListWidget.ViewMode.IconMode)
    widget.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
    widget.setResizeMode(QListWidget.ResizeMode.Adjust)
    widget.setUniformItemSizes(True)
    # **枠を明示する。** 先頭の項目の大きさに引きずられないようにする。
    widget.setGridSize(QSize(ITEM_WIDTH, ITEM_HEIGHT))
    # 長い文字は折り返す（切り詰めるより、2行で読めるほうがよい）。
    widget.setWordWrap(True)
    widget.setSelectionMode(selection_mode)
    return widget


def format_sharpness(value: Optional[float]) -> str:
    """一覧とツールチップに出す鮮明さ（#135）。未計測は「未計測」。"""
    return "鮮明さ 未計測" if value is None else f"鮮明さ {value:.0f}"


def _fill_face_list(
    widget: QListWidget,
    records: List[dict],
    age_labels: Optional[Dict[int, Optional[str]]] = None,
    show_sharpness: bool = False,
) -> None:
    """一覧を作り直す。**作り直しているあいだ、信号を止める。**

    `clear()` は項目を1つずつ外すので、そのたびに `itemSelectionChanged` が
    出る。プレビューがそれに繋がっているため、**200件を選んで割り当てると
    元写真を NFS から100回読み直し、1回の操作に17秒かかっていた**
    （実測。1枚あたり 220ms）。

    止めても困らない。作り直したあとは何も選ばれていないので、
    プレビューを描き直す理由がそもそも無い。
    """
    blocked = widget.blockSignals(True)
    try:
        _repopulate_face_list(widget, records, age_labels, show_sharpness)
    finally:
        widget.blockSignals(blocked)


def _repopulate_face_list(
    widget: QListWidget,
    records: List[dict],
    age_labels: Optional[Dict[int, Optional[str]]] = None,
    show_sharpness: bool = False,
) -> None:
    widget.clear()
    for record in records:
        pixmap = QPixmap()
        pixmap.loadFromData(record.get("thumbnail") or b"")
        item = QListWidgetItem()
        if not pixmap.isNull():
            item.setIcon(pixmap)
        label = str(record["id"])
        if record.get("assign_source") == db.ASSIGN_AUTO:
            label = f"{label} (自動 {record.get('assign_score') or 0:.0f})"
        if record.get("penalize"):
            label = f"{label} [{PENALIZE_MARK}]"
        # **年齢の出し方は呼び出し側が決める。** 人物が決まっている画面だけが
        # 誕生日を持っているので、計算した年齢を出せるのもそこだけ。
        if age_labels is not None:
            age_text = age_labels.get(record["id"])
        elif record.get("age") is not None:
            age_text = format_age(record["age"])
        else:
            age_text = None
        if age_text:
            label = f"{label} {age_text}"
        # **鮮明さで絞る・並べるときだけ顔の下に出す**（#135。枠が狭いので常には出さない）。
        # 閾値を決める手がかりなので、境目の顔の値が読めるようにする。
        if show_sharpness:
            label = f"{label} {format_sharpness(record.get('sharpness'))}"
        if "sharpness" in record:
            item.setToolTip(format_sharpness(record.get("sharpness")))
        item.setText(label)
        item.setData(Qt.UserRole, record)
        # **1件ずつにも同じ大きさを持たせる。** `setUniformItemSizes` は
        # 先頭の項目を見るので、**明示しないと先頭のサムネイル次第で全体が縮む。**
        item.setSizeHint(QSize(ITEM_WIDTH, ITEM_HEIGHT))
        widget.addItem(item)


class MainWindow(QWidget):
    """1枚の画面で、人物の登録から割り当て・見直しまでを済ませる。

    **左の一覧が「いま何を見ているか」。** 未分類・顔認証外・他人(減点)と、
    登録した人物が同じ一覧に並ぶ。人物を選ぶと、その人物に割り当て済みの顔が
    右に出る（**以前は別ウィンドウだった**。開き直しの往復が要り、未分類の
    割り当てと見直しが同じ作業の表裏なのに画面が分かれていた）。

    **顔への操作は右クリックのメニューに集めた。** 見ているものによって
    意味のある操作が変わるので、ボタンで並べると**モードごとに増え続ける。**
    メニューには打鍵も出るので、**使いながら覚えられる。**

    **手作業の量がこの製品の精度の上限**（他人の顔の 99.1% が家族の写真に
    混ざっていて、まとめて消せない。2026-10-08 実測）。この画面の作りは
    すべて「1件あたりの手数を減らす」ためにある。
    """

    #: 「鮮明さを測る」を押した（#135）。裏の処理は `AppWindow` が持つので、そちらへ頼む。
    measure_requested = Signal()

    def __init__(
        self, database_path: str, source_roots: SourceRoots = None
    ):
        super().__init__()
        self.db_path = database_path
        self.connection = db.ensure_database(database_path)
        # **裏の `scan` などと同時に書くときは待つ**（#129）。既定の 5 秒だと、相手が
        # 書き込みロックを握っているあいだに割り当ての操作が落ちる。
        self.connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        #: プレビューのフォルダ表示を相対パスにするためだけに使う（root 1つか並び）。
        #: **渡されなければ DB に記録された root を使う**（#24。設定を失っても読める表示に
        #: なる）。それも無ければ絶対パスで出すので、渡さなくても動く。
        self.source_roots = source_roots or db.list_scan_roots(self.connection) or None
        self.setWindowTitle("PhotoArchiveAI 人物登録と顔の割り当て")
        self.page = 0
        #: 裏で `scan` や鮮明さの計測を流しているか（#135。まとめ処理を止める）。
        self._job_running = False
        #: 1〜9 の打鍵で割り当てる人物（左の一覧の並び順）。
        self.assign_actions: List[QAction] = []
        #: 「この人物に似た順」の距離。**ページ送りのあいだ持ち続ける**（#69）。
        #: 人物を選び直したら作り直す（`_similarity_for`）。
        self.similarity: Optional[recommend.PersonSimilarity] = None
        #: 直前に並べたとき、根拠にした手本の件数。0 なら似た順に並べられていない。
        self.similarity_teachers: Optional[int] = None
        #: 顔の操作を元に戻す・やり直す履歴（#116）。**開いている間だけ持つ**（利用者の決定）。
        self.history = undo.History()
        #: 右クリックのメニューをウィンドウの中に収める（#118）。
        self.menu_keeper = KeepMenuInsideWindow(self)

        person_panel = self._build_person_panel()
        face_panel = self._build_face_panel()
        self._build_face_actions()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(person_panel)
        splitter.addWidget(face_panel)
        # **左は狭く、広げたぶんは顔の一覧へ**（#118・利用者「真ん中のサムネ一覧を
        # 広くしたい」）。左は「見るもの」を選ぶだけなので、名前が読めれば足りる。
        # 分割線を動かせば広げられる。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([PERSON_PANEL_WIDTH, 1180 - PERSON_PANEL_WIDTH])

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(splitter)
        self.resize(1180, 760)

        # 一覧を作ると「未分類」が選ばれ、そのまま顔の一覧まで作られる。
        self._reload_person_list()
        self.refresh_measure_count()

    # ------------------------------------------------------------------
    # 画面の組み立て
    # ------------------------------------------------------------------

    def _build_person_panel(self) -> QWidget:
        """左: 「見るもの」の一覧。**上に3つの表示、下に人物。**"""
        self.person_list = QListWidget()
        # **ドラッグで並べ替えられるようにする。** よく割り当てる人物を上に
        # 置けないと、人数が増えるほど毎回探すことになる。
        self.person_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.person_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.person_list.currentItemChanged.connect(self._on_view_selected)
        # 並べ替えは「落とした時点」で確定する。**保存ボタンを置かない**
        # （押し忘れたぶんが黙って消える）。
        self.person_list.model().rowsMoved.connect(self._save_person_order)
        self.person_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.person_list.customContextMenuRequested.connect(self._show_person_menu)

        self.add_person_button = QPushButton("人物追加")
        self.edit_person_button = QPushButton("編集")
        self.delete_person_button = QPushButton("削除")
        self.add_person_button.clicked.connect(self._add_person)
        self.edit_person_button.clicked.connect(self._edit_person)
        self.delete_person_button.clicked.connect(self._delete_person)

        self.details_label = QLabel("")
        self.details_label.setWordWrap(True)

        # **手本を増やしたら、その場で `match` を流せるようにする。**
        self.match_button = QPushButton("自動割り当て（match）を実行…")
        self.match_button.setToolTip(
            "手本をもとに、未分類と自動割当の顔を付け直します（photoarchive match と同じ）。"
        )
        self.match_button.clicked.connect(self._run_match)

        person_buttons = QHBoxLayout()
        for button in (
            self.add_person_button,
            self.edit_person_button,
            self.delete_person_button,
        ):
            # Qt の既定ではボタン1つが 80px 以上を取り、3つ並ぶと左が狭くならない（#118）。
            button.setMinimumWidth(
                button.fontMetrics().horizontalAdvance(button.text()) + 16
            )
            person_buttons.addWidget(button)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addLayout(person_buttons)
        layout.addWidget(self.person_list, 1)
        layout.addWidget(self.details_label)
        layout.addWidget(self.match_button)
        return panel

    def _build_face_panel(self) -> QWidget:
        """右: 絞り込み・顔の一覧・プレビュー・まとめ処理。"""
        # --- 共通の絞り込み（どの表示でも意味がある） -------------------
        self.order_box = QComboBox()
        for label, _ in ORDER_CHOICES:
            self.order_box.addItem(label)
        self.order_box.setToolTip(
            "並び順。**条件に当たる全件を並べてから、ページに分けて出す**"
            "（いま見えている200件の中だけで並べ替えるのではない）。\n"
            "年齢は**画面に出ている年齢**（確定値か、誕生日から計算した値）で並べる。\n"
            "表示を切り替えると、その表示に向いた順に戻る"
            "（未分類は撮影日時の新しい順、"
            "人物は年齢の若い順、人物の未分類はこの人物に似た順）。\n"
            "「この人物に似た順」は、その人物の手本との距離で並べる"
            "（5点整列ができなかった手本は使わない）。"
        )
        self.order_box.currentIndexChanged.connect(self._reset_page)

        # **撮影年月の範囲で絞る。** 家族の写っていない行事（結婚式・旅行先の
        # 他人など）は時期でまとまっているので、**その期間だけを開いてまとめて
        # 顔認証外にできる。** 片方だけの指定（「2015-06 以降」など）もできる。
        self.month_from_box = QComboBox()
        self.month_to_box = QComboBox()
        for box, tip in (
            (self.month_from_box, "この年月から（含む）。指定なしなら下限を決めない"),
            (self.month_to_box, "この年月まで（含む）。指定なしなら上限を決めない"),
        ):
            box.setToolTip(tip)
        self.undated_only_box = QCheckBox(MONTH_UNDATED)
        self.undated_only_box.setToolTip(
            "撮影日時が読めない写真の顔だけを見る。\n"
            "EXIF の無い写真と、カメラが壊れた日時を書いた写真が入る。\n"
            "**範囲の指定とは併用できない**（読めない日付はどの範囲にも入らない）。"
        )
        self._reload_months()
        self.month_from_box.currentIndexChanged.connect(self._month_changed)
        self.month_to_box.currentIndexChanged.connect(self._month_changed)
        self.undated_only_box.stateChanged.connect(self._month_changed)

        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        common_filters = QHBoxLayout()
        common_filters.addWidget(QLabel("並び"))
        common_filters.addWidget(self.order_box)
        common_filters.addSpacing(12)
        common_filters.addWidget(QLabel("撮影"))
        common_filters.addWidget(self.month_from_box)
        common_filters.addWidget(QLabel("〜"))
        common_filters.addWidget(self.month_to_box)
        common_filters.addWidget(self.undated_only_box)
        common_filters.addStretch(1)
        common_filters.addWidget(self.prev_button)
        common_filters.addWidget(self.page_label)
        common_filters.addWidget(self.next_button)

        # --- 鮮明さで絞る（#135） ----------------------------------------
        # **ボケ・ぶれた顔を閾値で絞り、まとめて顔認証外にする。** 閾値は一覧を
        # 「ボケている順」で見ながら決める（利用者「ぶれ方の閾値を調整して選定したい」）。
        self.blur_check = QCheckBox("鮮明さが")
        self.blur_check.setToolTip(BLURRY_TOOLTIP)
        # **ホイールはフォーカスのあるときだけ**（`WheelSafeSpinBox`）。上を通っただけで閾値が動かない
        self.blur_spin = WheelSafeSpinBox()
        self.blur_spin.setRange(0, BLURRY_MAX)
        self.blur_spin.setValue(BLURRY_DEFAULT)
        self.blur_spin.setSuffix(" 以下")
        self.blur_spin.setToolTip(BLURRY_TOOLTIP)
        self.blur_spin.setKeyboardTracking(False)
        self.blur_check.stateChanged.connect(self._blur_changed)
        self.blur_spin.valueChanged.connect(self._blur_value_changed)
        # **ゲージでも動かせる**（PR #136 の再レビュー指摘1・利用者「鮮明さもゲージで調整したい」）。
        # 値の正は数値欄で、ゲージはその写し。**離したときだけ確定する**（`setTracking(False)`）:
        # 動かすたびに一覧を読み直すと、ドラッグ1回で DB とサムネイルを数十回読む。
        # 動かしている間は数値欄の表示だけを追わせる（`sliderMoved`）。
        self.blur_slider = WheelSafeSlider(Qt.Horizontal)
        self.blur_slider.setRange(0, BLURRY_MAX)
        self.blur_slider.setValue(BLURRY_DEFAULT)
        self.blur_slider.setTracking(False)
        self.blur_slider.setToolTip(BLURRY_TOOLTIP)
        self.blur_slider.setMinimumWidth(160)
        self.blur_slider.sliderMoved.connect(self._show_blur_value)
        self.blur_slider.valueChanged.connect(self._blur_slider_changed)
        self.measure_button = QPushButton("鮮明さを測る")
        self.measure_button.setToolTip(MEASURE_TOOLTIP)
        self.measure_button.clicked.connect(self.measure_requested.emit)
        self.measure_label = QLabel("")
        self.measure_label.setStyleSheet("color: #555;")

        blur_filters = QHBoxLayout()
        blur_filters.addWidget(self.blur_check)
        blur_filters.addWidget(self.blur_slider)
        blur_filters.addWidget(self.blur_spin)
        blur_filters.addWidget(QLabel("（ボケ・ぶれた顔）"))
        blur_filters.addSpacing(12)
        blur_filters.addWidget(self.measure_button)
        blur_filters.addWidget(self.measure_label)
        blur_filters.addStretch(1)

        # --- 人物を選んでいるときだけの絞り込み ------------------------
        # **確定済みと自動を見分けて絞れるようにする。** 自動割り当てを
        # 見直すときは自動だけを、手本を見直すときは確定済みだけを見たい。
        self.source_box = QComboBox()
        for label, _ in SOURCE_FILTERS:
            self.source_box.addItem(label)
        self.source_box.addItem(UNASSIGNED_FOR_PERSON_FILTER)
        self.source_box.addItem(NOT_THIS_PERSON_FILTER)
        self.source_box.currentIndexChanged.connect(self._source_changed)

        # **最小値を -1 にして「指定なし」に割り当てる。**
        # `FaceAgeDialog` と同じ理由で、**0 を特別扱いにすると 0歳で絞れなくなる**
        # （QSpinBox の `specialValueText` は最小値のときに出る）。
        self.min_age = QSpinBox()
        self.min_age.setRange(-1, 150)
        self.min_age.setSpecialValueText("指定なし")
        self.min_age.setValue(-1)
        self.max_age = QSpinBox()
        self.max_age.setRange(-1, 150)
        self.max_age.setSpecialValueText("指定なし")
        self.max_age.setValue(-1)
        # 年齢の絞り込みも「指定なし」の文字が入っている。年齢の入力と
        # 同じ理由で、触ったときに打鍵で置き換えられるようにする。
        for spin in (self.min_age, self.max_age):
            spin.focusInEvent = _make_select_all_on_focus(spin)
        self.min_age.valueChanged.connect(self._reset_page)
        self.max_age.valueChanged.connect(self._reset_page)

        # **年齢を出せない顔をどうするか。** 既定は「含めない」。
        # 含めると、範囲を指定しても年齢不明の顔が常に混ざり、**絞り込みが
        # ほとんど効かない**（実データで割り当て済み 22,511 件のうち 1,931 件）。
        self.include_unknown_age = QCheckBox("年齢不明も含める")
        self.include_unknown_age.setToolTip(
            "誕生日が未登録か、写真に撮影日時が無くて年齢を出せない顔も残す"
        )
        self.include_unknown_age.stateChanged.connect(self._reset_page)

        person_filters = QHBoxLayout()
        person_filters.addWidget(QLabel("種別"))
        person_filters.addWidget(self.source_box)
        person_filters.addSpacing(12)
        person_filters.addWidget(QLabel("年齢"))
        person_filters.addWidget(self.min_age)
        person_filters.addWidget(QLabel("歳から"))
        person_filters.addWidget(self.max_age)
        person_filters.addWidget(QLabel("歳"))
        person_filters.addWidget(self.include_unknown_age)
        person_filters.addStretch(1)
        # **人物のときだけ出す。** 年齢は人物の誕生日が無いと計算できないので、
        # 全体の表示では意味を持たない（`Face.age` は実データ 22,511 件中
        # 126 件しか入っていない）。
        self.person_filter_row = QWidget()
        self.person_filter_row.setLayout(person_filters)

        # --- 顔の一覧 ---------------------------------------------------
        self.face_list = make_face_list(QListWidget.SelectionMode.ExtendedSelection)
        self.face_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.face_list.customContextMenuRequested.connect(self._show_face_menu)
        self.face_list.itemSelectionChanged.connect(self._on_face_selection_changed)

        self.preview_label = QLabel("顔を選ぶと元写真から切り出して表示します。")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumWidth(320)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("border: 1px solid #999; padding: 8px;")

        # 撮影日時とフォルダ。**年齢を入れるのに要る。**
        # QLabel は画像か文字のどちらかしか持てないので、もう1枚に分ける。
        self.preview_info = QLabel("")
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_info.setStyleSheet("padding: 4px;")

        # 登録が済んだことを知らせる札。**顔写真に重ねて中央に出す。**
        # 画像の外に置くと、見ているところ（顔）から目を離さないと気づけない。
        self.preview_status = QLabel("", self.preview_label)
        self.preview_status.setAlignment(Qt.AlignCenter)
        self.preview_status.setStyleSheet(
            "padding: 10px 18px; font-size: 16px; font-weight: bold;"
            " color: #1b5e20; background: rgba(232, 245, 233, 235);"
            " border: 2px solid #2e7d32; border-radius: 6px;"
        )
        self.preview_status.hide()
        # `preview_label` の中央に置く。レイアウトに入れると、画像は
        # `QLabel` 自身が描き、子ウィジェットがその上に載る。
        status_layout = QVBoxLayout(self.preview_label)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.addWidget(self.preview_status, 0, Qt.AlignCenter)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.preview_info, 0)

        face_area = QSplitter(Qt.Horizontal)
        face_area.addWidget(self.face_list)
        face_area.addWidget(preview_panel)
        face_area.setStretchFactor(0, 3)
        face_area.setStretchFactor(1, 2)

        # **右クリックは目に見えない。** 操作がメニューの中にあることと、
        # いま効く打鍵を1行で出す。
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #555;")

        # --- いまの絞り込みに当たる顔をまとめて処理する ----------------
        # **以前は「行事（フォルダ×日）を選ぶ」が要った**（#61）。利用者が使っておらず
        # 「これいらない」（2026-11-03・#135）ので行事の行ごと消した。代わりに、
        # **何かで絞っているときだけ**押せる（`_narrowed`）。
        self.bulk_button = QPushButton("")
        self.bulk_button.clicked.connect(self._bulk_action)

        bulk_row = QHBoxLayout()
        bulk_row.addStretch(1)
        bulk_row.addWidget(self.bulk_button)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addLayout(common_filters)
        layout.addLayout(blur_filters)
        layout.addWidget(self.person_filter_row)
        layout.addWidget(face_area, 1)
        layout.addWidget(self.hint_label)
        layout.addLayout(bulk_row)
        return panel

    def _build_face_actions(self) -> None:
        """顔への操作を `QAction` で1組だけ作る。

        **メニューと打鍵で同じものを使う。** 別々に作ると、片方だけ増えたり
        押せる条件がずれたりする。打鍵は**顔の一覧に焦点があるときだけ**効く
        （`WidgetWithChildrenShortcut`）。年齢の入力欄に数字を打っているときに
        割り当てが走っては困る。
        """
        self.action_confirm = QAction(ACTION_CONFIRM, self)
        self.action_confirm.setShortcut("C")
        self.action_confirm.triggered.connect(self._confirm_selected)

        self.action_unassign = QAction(ACTION_UNASSIGN, self)
        self.action_unassign.setShortcut("U")
        self.action_unassign.triggered.connect(self._unassign_selected)

        self.action_not_this_person = QAction(ACTION_NOT_THIS_PERSON, self)
        self.action_not_this_person.setShortcut("N")
        self.action_not_this_person.setToolTip(
            "この人物ではない、と記録する。\n"
            "**ほかの人物には自動で付きうる。**\n"
            "解除と違い、match を流し直しても戻ってこない。"
        )
        self.action_not_this_person.triggered.connect(lambda: self._reject_for_person_selected())

        # 人物の表示の「この人物ではない ▸」の残りの行き先（#113）。**X と P は
        # 顔認証外の表示などの操作と同じ打鍵だが、押せる表示が重ならない**ので取り合わない
        # （押せない `QAction` の打鍵は効かない）。
        self.action_not_this_person_reject = QAction(ACTION_NOT_THIS_PERSON_TO_REJECTED, self)
        self.action_not_this_person_reject.setShortcut("X")
        self.action_not_this_person_reject.setToolTip(
            "この人物ではない、と記録して顔認証外にする。\n**どの人物にも自動で付かなくなる。**"
        )
        self.action_not_this_person_reject.triggered.connect(
            lambda: self._reject_for_person_selected(SCOPE_REJECTED)
        )
        self.action_not_this_person_penalize = QAction(ACTION_NOT_THIS_PERSON_TO_PENALIZED, self)
        self.action_not_this_person_penalize.setShortcut("P")
        self.action_not_this_person_penalize.setToolTip(
            "この人物ではない、と記録して顔認証外にし、他人(減点)の印も付ける。"
        )
        self.action_not_this_person_penalize.triggered.connect(
            lambda: self._reject_for_person_selected(SCOPE_PENALIZED)
        )
        self.not_this_person_actions = (
            self.action_not_this_person,
            self.action_not_this_person_reject,
            self.action_not_this_person_penalize,
        )

        self.action_reject = QAction(ACTION_REJECT, self)
        self.action_reject.setShortcut("X")
        self.action_reject.setToolTip(
            "家族の誰でもない顔として顔認証外にする。\n"
            "**どの人物にも自動で付かなくなる。**"
        )
        self.action_reject.triggered.connect(self._reject_selected)

        self.action_set_age = QAction(ACTION_SET_AGE, self)
        self.action_set_age.setShortcut("G")
        self.action_set_age.triggered.connect(self._set_age_selected)

        self.action_undo_rejection = QAction(ACTION_UNDO_REJECTION, self)
        self.action_undo_rejection.setShortcut("Z")
        self.action_undo_rejection.triggered.connect(self._undo_rejection_selected)

        # 顔認証外にした顔のうち、select で点を引く顔を選ぶ（#110）。印の無い顔認証外の顔は引かない
        self.action_penalize = QAction(ACTION_PENALIZE, self)
        self.action_penalize.setShortcut("P")
        self.action_penalize.setToolTip(
            "この顔が大きく写った写真を select で後ろへ回す（家族の顔と比べた大きさ × 減点）。\n"
            "印の無い顔認証外の顔と未分類の顔は、点に入りません。"
        )
        self.action_penalize.triggered.connect(lambda: self._set_penalty_selected(True))
        self.action_unpenalize = QAction(ACTION_UNPENALIZE, self)
        self.action_unpenalize.setShortcut("Shift+P")
        self.action_unpenalize.triggered.connect(lambda: self._set_penalty_selected(False))

        self.face_action_set = (
            self.action_confirm,
            self.action_unassign,
            self.action_not_this_person,
            self.action_not_this_person_reject,
            self.action_not_this_person_penalize,
            self.action_reject,
            self.action_set_age,
            self.action_undo_rejection,
            self.action_penalize,
            self.action_unpenalize,
        )
        for action in self.face_action_set:
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.face_list.addAction(action)

        # ページ送りも打鍵で。**一覧の矢印キーと取り合わない組み合わせにする。**
        self.action_next_page = QAction("次のページ", self)
        self.action_next_page.setShortcut("Ctrl+Right")
        self.action_next_page.triggered.connect(self._next_page)
        self.action_previous_page = QAction("前のページ", self)
        self.action_previous_page.setShortcut("Ctrl+Left")
        self.action_previous_page.triggered.connect(self._previous_page)
        for action in (self.action_next_page, self.action_previous_page):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.face_list.addAction(action)

        # 元に戻す・やり直す（#116）。**顔の一覧に焦点が無くても効く**（左の一覧で人物を
        # 選び直したあとにも戻したい）。文字の欄では、欄が先に受け取って文字の取り消しになる。
        self.action_undo = QAction(ACTION_UNDO, self)
        self.action_undo.setShortcuts(list(UNDO_SHORTCUTS))
        self.action_undo.triggered.connect(lambda: self._undo_or_redo(redo=False))
        self.action_redo = QAction(ACTION_REDO, self)
        self.action_redo.setShortcuts(list(REDO_SHORTCUTS))
        self.action_redo.triggered.connect(lambda: self._undo_or_redo(redo=True))
        for action in (self.action_undo, self.action_redo):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._update_undo_actions()

    # ------------------------------------------------------------------
    # 左の一覧（見るもの）
    # ------------------------------------------------------------------

    def _reload_person_list(
        self,
        select_person_id: Optional[int] = None,
        select_scope: Optional[str] = None,
    ) -> None:
        """左の一覧を作り直す。**選択を復元し、顔の一覧まで作り直す。**

        ``select_person_id`` か ``select_scope`` を渡すとそれを選ぶ。
        **選び直さないと、追加・編集した直後に選択が外れる**（`clear()` が
        選択を落とす）。何も指定が無ければ、いま選んでいたものを保つ。

        **作り直しているあいだ信号を止める。** 止めないと、項目が増えるたびに
        選択が動いて**顔の一覧を何度も読み直す。**
        """
        if select_person_id is None and select_scope is None:
            person = self._current_person()
            select_person_id = None if person is None else person["id"]
            select_scope = self._current_scope()

        blocked = self.person_list.blockSignals(True)
        try:
            self.person_list.clear()
            for scope, label, description in VIEW_SCOPES:
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, None)
                item.setData(SCOPE_ROLE, scope)
                item.setData(DESCRIPTION_ROLE, description)
                # **先頭の表示はドラッグで動かさない。** 人物の並び順だけを
                # 保存するので、混ざると並びの意味が崩れる。
                item.setFlags(
                    (item.flags() | Qt.ItemFlag.ItemIsSelectable)
                    & ~Qt.ItemFlag.ItemIsDragEnabled
                    & ~Qt.ItemFlag.ItemIsDropEnabled
                )
                self.person_list.addItem(item)

            separator = QListWidgetItem("─" * 16)
            separator.setFlags(Qt.ItemFlag.NoItemFlags)
            separator.setData(SCOPE_ROLE, None)
            self.person_list.addItem(separator)

            for person in db.list_persons(self.connection):
                item = QListWidgetItem(person["name"])
                item.setData(Qt.UserRole, person)
                item.setData(SCOPE_ROLE, SCOPE_PERSON)
                # **旗は既定のまま**（掴める）。並べ替えは落とした時点で
                # `rowsMoved` が出て `_save_person_order` が受ける。
                self.person_list.addItem(item)

            self._select_view(select_person_id, select_scope)
        finally:
            self.person_list.blockSignals(blocked)
        self._refresh_counts()
        self._rebuild_assign_actions()
        self._on_view_selected(self.person_list.currentItem())

    def _select_view(
        self, person_id: Optional[int], scope: Optional[str]
    ) -> None:
        """一覧の中から、人物 id か表示を選ぶ。**見つからなければ「未分類」。**"""
        if person_id is not None:
            for row in range(self.person_list.count()):
                person = self.person_list.item(row).data(Qt.UserRole)
                if person is not None and person["id"] == person_id:
                    self.person_list.setCurrentRow(row)
                    return
        if scope is not None and scope != SCOPE_PERSON:
            for row in range(self.person_list.count()):
                if self.person_list.item(row).data(SCOPE_ROLE) == scope:
                    self.person_list.setCurrentRow(row)
                    return
        self.person_list.setCurrentRow(0)

    def _refresh_counts(self) -> None:
        """左の一覧の件数を書き直す。**項目は作り直さない。**

        作り直すと選択が動いて顔の一覧まで読み直すので、文字だけを差し替える。
        件数は `db.face_counts` が**1回の問い合わせ**で全部返す。

        **残りがどれだけあるかを出すためにある。** 手作業が精度の上限なので、
        減っていくのが見えること自体が作業の支えになる。
        """
        counts = db.face_counts(self.connection)
        totals = {
            SCOPE_UNASSIGNED: counts["unassigned"],
            # 他人(減点)に移った顔は顔認証外に出さない（#113）ので、件数からも引く。
            SCOPE_REJECTED: counts["rejected"] - counts["penalized"],
            SCOPE_PENALIZED: counts["penalized"],
        }
        by_person = counts["by_person"]
        for row in range(self.person_list.count()):
            item = self.person_list.item(row)
            scope = item.data(SCOPE_ROLE)
            person = item.data(Qt.UserRole)
            if scope in totals:
                label = dict((s, l) for s, l, _ in VIEW_SCOPES)[scope]
                item.setText(f"{label}　{totals[scope]:,}")
            elif person is not None:
                entry = by_person.get(person["id"], {"manual": 0, "auto": 0})
                item.setText(
                    f"{person['name']} ({person.get('relation') or '-'})\n"
                    f"　手本 {entry['manual']:,} / 自動 {entry['auto']:,}"
                )
        self._counts = counts

    def _save_person_order(self, *args) -> None:
        """画面に並んでいる順を、そのまま `display_order` に書く。

        **一覧に出ている全員を渡す。** 一部だけ書くと、書かなかった人物の
        順序が古いままになって並びが混ざる。

        **人物の項目だけを数える。** 先頭の表示（未分類など）と区切り線が
        同じ一覧に居るので、行番号をそのまま使うと順序がずれる。
        落とした先が区切り線より上でも、**作り直しで必ず下へ戻る。**
        """
        person_ids = [
            self.person_list.item(row).data(Qt.UserRole)["id"]
            for row in range(self.person_list.count())
            if self.person_list.item(row).data(Qt.UserRole) is not None
        ]
        if person_ids:
            db.set_person_order(self.connection, person_ids)
            # 落とした位置が表示の側に食い込んでいることがあるので、
            # 正しい形（表示 → 区切り → 人物）に作り直す。
            self._reload_person_list()

    def _current_scope(self) -> str:
        """いま見ているもの。選択が無ければ「未分類」。"""
        item = self.person_list.currentItem()
        if item is None:
            return SCOPE_UNASSIGNED
        return item.data(SCOPE_ROLE) or SCOPE_UNASSIGNED

    def _current_person(self) -> Optional[dict]:
        """選択中の人物。表示（未分類など）を選んでいるときは ``None``。"""
        item = self.person_list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _on_view_selected(self, current=None, previous=None) -> None:
        """左で選び直したとき。**絞り込みと並びを合わせ、一覧を読み直す。**"""
        self._sync_view_controls()
        self._refresh_details()
        self._reset_page()

    def _sync_view_controls(self) -> None:
        """見ているものに合わせて、絞り込みの出し方と並びの既定を決める。

        **人物向けの絞り込みは人物のときだけ出す。** 年齢は人物の誕生日から
        計算するので、全体の表示では意味を持たない。

        **並びは表示を切り替えたときだけ既定へ戻す。** 同じ表示を見ている
        あいだに戻すと、選んだ順が勝手に変わる。
        """
        scope = self._current_scope()
        self.person_filter_row.setVisible(scope == SCOPE_PERSON)
        if scope != getattr(self, "_synced_scope", None):
            self._synced_scope = scope
            if scope == SCOPE_PERSON:
                # 人物を選び直したら種別は「すべて」から見る。
                blocked = self.source_box.blockSignals(True)
                try:
                    self.source_box.setCurrentIndex(0)
                finally:
                    self.source_box.blockSignals(blocked)
        # **並びの既定は「見ているもの」で決める**（種別の「未分類」も含む）。
        view = self._view_key()
        # **意味を持たない並びは押せなくする**（`ORDER_UNUSABLE`）。
        unusable = ORDER_UNUSABLE.get(view, {})
        choices = self.order_box.model()
        for index, (_, value) in enumerate(ORDER_CHOICES):
            item = choices.item(index)
            item.setEnabled(value not in unusable)
            item.setToolTip(unusable.get(value, ""))
        if view != getattr(self, "_synced_view", None):
            self._synced_view = view
            order = DEFAULT_ORDER[view]
            index = next(
                (i for i, (_, value) in enumerate(ORDER_CHOICES) if value == order), 0
            )
            blocked = self.order_box.blockSignals(True)
            try:
                self.order_box.setCurrentIndex(index)
            finally:
                self.order_box.blockSignals(blocked)
        self._update_face_actions()

    def _refresh_details(self) -> None:
        """左下の説明。**人物なら内訳、表示なら何を見ているかを書く。**"""
        person = self._current_person()
        if person is None:
            item = self.person_list.currentItem()
            description = "" if item is None else (item.data(DESCRIPTION_ROLE) or "")
            counts = getattr(self, "_counts", None) or db.face_counts(self.connection)
            self.details_label.setText(
                f"{description}\n\n"
                f"未分類 {counts['unassigned']:,} / 手本 {counts['manual']:,} /"
                f" 自動 {counts['auto']:,} / 顔認証外 {counts['rejected']:,}"
            )
        else:
            counts = getattr(self, "_counts", None) or db.face_counts(self.connection)
            entry = counts["by_person"].get(person["id"], {"manual": 0, "auto": 0})
            rejected = db.count_person_rejections(self.connection, person["id"])
            self.details_label.setText(
                f"名前: {person['name']}\n"
                f"続柄: {person.get('relation') or '-'}\n"
                # 誕生日を出しておかないと、年齢が出ない理由が画面から分からない。
                f"誕生日: {person.get('birth_date') or '未設定'}\n"
                f"メモ: {person.get('memo') or '-'}\n"
                f"手本: {entry['manual']:,} 件 / 自動割当: {entry['auto']:,} 件\n"
                f"この人物ではない: {rejected:,} 件"
            )
        # 人物が変わると年齢の行も変わる。元写真は読み直さない。
        self._refresh_preview_info()

    # ------------------------------------------------------------------
    # 人物の登録
    # ------------------------------------------------------------------

    def _show_person_menu(self, pos) -> None:
        """左の一覧の右クリック。**顔の操作と同じ入口に揃える。**"""
        item = self.person_list.itemAt(pos)
        if item is not None and item.data(Qt.UserRole) is not None:
            # **右クリックした人物を選び直す。** 選択と違う人物を編集しては困る。
            self.person_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("人物を追加…", self._add_person)
        edit = menu.addAction("この人物を編集…", self._edit_person)
        delete = menu.addAction("この人物を削除…", self._delete_person)
        has_person = self._current_person() is not None
        edit.setEnabled(has_person)
        delete.setEnabled(has_person)
        self.menu_keeper.attach(menu).exec(self.person_list.mapToGlobal(pos))

    def _validated_values(self, dialog: "PersonDialog") -> Optional[tuple]:
        """人物ダイアログの入力を検証して返す。落ちたら知らせて ``None``。

        名前の検証と誕生日の検証を同じ場所に置く。片方がダイアログの中、
        もう片方が外にあると、**どこで弾かれたのかを追うのに両方読む**ことになる。
        """
        name, relation, memo, birth_parts = dialog.values()
        if not name:
            QMessageBox.warning(self, "入力エラー", "名前は必須です。")
            return None
        try:
            birth_date = build_birth_date(*birth_parts)
        except ValueError as error:
            QMessageBox.warning(self, "入力エラー", str(error))
            return None
        return name, relation, memo, birth_date

    def _add_person(self):
        dialog = PersonDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = self._validated_values(dialog)
        if values is None:
            return
        name, relation, memo, birth_date = values
        # **人物と区分・重みを1つのトランザクションで書く**（PR #133 のレビュー指摘1）。
        # 別々に commit すると、2つ目が書き込みロックで落ちたときに人物だけが残り、
        # 「何も変えていません」の知らせ（`report_unhandled_error`）が事実と違っていた。
        try:
            person_id = db.add_person(
                self.connection, name, relation, memo, birth_date=birth_date, commit=False
            )
            db.set_person_scoring(self.connection, person_id, *dialog.scoring_values(), commit=False)
        except db.DuplicatePersonName as error:
            # 名前は重複不可（#90）。同名の2人目を作ると、どちらに割り当てたのか見分けられない
            self.connection.rollback()
            QMessageBox.warning(self, "入力エラー", str(error))
            return
        self.connection.commit()
        self._reload_person_list(select_person_id=person_id)

    def _edit_person(self):
        person = self._current_person()
        if person is None:
            return
        dialog = PersonDialog(
            self,
            person["name"],
            person.get("relation") or "",
            person.get("memo") or "",
            person.get("birth_date") or "",
            close=person.get("category") == db.CATEGORY_CLOSE,
            weight=db.DEFAULT_PERSON_WEIGHT if person.get("weight") is None else person["weight"],
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = self._validated_values(dialog)
        if values is None:
            return
        name, relation, memo, birth_date = values
        # 追加と同じく、1つのトランザクションで書く（PR #133 のレビュー指摘1）
        try:
            db.update_person(
                self.connection, person["id"], name, relation, memo, birth_date=birth_date, commit=False
            )
            db.set_person_scoring(self.connection, person["id"], *dialog.scoring_values(), commit=False)
        except db.DuplicatePersonName as error:
            self.connection.rollback()
            QMessageBox.warning(self, "入力エラー", str(error))
            return
        self.connection.commit()
        # 編集した人物を選び直す。**誕生日を入れたら、年齢の行がその場で出る。**
        self._reload_person_list(select_person_id=person["id"])

    def _delete_person(self):
        person = self._current_person()
        if person is None:
            return
        if QMessageBox.question(
            self, "削除確認", f"{person['name']} を削除しますか？\n割り当て済みの顔は未分類に戻ります。"
        ) != QMessageBox.Yes:
            return
        db.delete_person(self.connection, person["id"])
        self._reload_person_list(select_scope=SCOPE_UNASSIGNED)

    # ------------------------------------------------------------------
    # 自動割り当て（match）
    # ------------------------------------------------------------------

    def set_job_running(self, running: bool) -> None:
        """裏で `scan` などを流しているあいだは `match` を押せなくする（#129）。

        `scan` が顔を足し消ししている途中の手本で照合しないため。`match` は長く書き込み
        ロックを握るので、裏の処理も待たせる。顔の割り当てなどの操作は続けられる。
        """
        self._job_running = running
        self._sync_bulk_controls()
        self.measure_button.setEnabled(not running)
        self.match_button.setEnabled(not running)
        self.match_button.setToolTip(
            MATCH_BLOCKED_BY_JOB
            if running
            else "手本をもとに、未分類と自動割当の顔を付け直します（photoarchive match と同じ）。"
        )

    def _run_match(self) -> None:
        """`match` を流す。**進み具合を出し、終わったら結果と件数を出し直す。**

        **別のスレッドに出さない。** このリポジトリの重い処理と同じく、
        `WorkProgress` が通知のたびにイベントを回す（実データで約 26 秒）。
        スレッドに出すと SQLite の接続を2本持つことになり、その間に画面から
        書き込めてしまう（`WorkProgress` は窓を塞ぐので、それも起きない）。
        """
        counts = db.face_counts(self.connection)
        if counts["manual"] == 0:
            QMessageBox.information(
                self,
                "手本がありません",
                "手本（手動で割り当てた顔）が1件もありません。\n"
                "先に顔を人物へ割り当ててから実行してください。",
            )
            return
        person = self._current_person()
        dialog = MatchDialog(
            self,
            counts,
            self.db_path,
            person=person,
            person_auto=(
                db.count_faces(
                    self.connection, assign_source=db.ASSIGN_AUTO, person_id=int(person["id"])
                )
                if person
                else 0
            ),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        person_id = dialog.chosen_person_id()

        backup: Optional[Path] = None
        progress = WorkProgress(self, "自動割り当ての準備をしています", 1, delay_ms=0)
        try:
            with busy_cursor():
                if dialog.makes_backup():
                    progress.step("控えを取っています")

                    def copied(status, remaining, total):
                        progress.step(
                            f"控えを取っています（{(total - remaining) * 100 // max(total, 1)}%）"
                        )

                    backup = db.backup_to(
                        self.connection,
                        migration.default_backup_path(self.db_path),
                        progress=copied,
                    )
                progress.step("自動割当をいったん外し、手本を読み込んでいます")
                progress.base_label = "顔を照合しています"
                summary = matcher.match_faces(
                    self.connection,
                    person_id=person_id,
                    # 件数の分からない工程（取り消しの直後）は、文言だけ変える
                    progress_callback=lambda done, total, detail: (
                        progress(done, total)
                        if total
                        else progress.step("自動割当を外しました。手本を読み込んでいます")
                    ),
                )
                progress.step("一覧を作り直しています")
                self._reload_person_list()
        except Exception as error:  # noqa: BLE001 — 画面に理由を出して止まる
            progress.finish()
            QMessageBox.critical(
                self,
                "自動割り当てに失敗しました",
                f"{error}\n\n"
                + (f"控え: {backup}" if backup else "控えは取っていません。"),
            )
            return
        progress.finish()
        QMessageBox.information(
            self,
            "自動割り当てが終わりました",
            format_match_summary(summary, db.list_persons(self.connection), backup),
        )

    # ------------------------------------------------------------------
    # 右クリックのメニューと打鍵
    # ------------------------------------------------------------------

    def _rebuild_assign_actions(self) -> None:
        """「人物に割り当て」の項目を作り直す。**1〜9 の打鍵もここで決まる。**

        **左の一覧に並んでいる順**に 1 から振る。よく割り当てる人物を上へ
        動かせば、押す数字も前に来る（並べ替えが打鍵に効く）。

        **10人目からは数字を振らない。** 打鍵は1桁に収める（2桁にすると
        「1」を押した時点で確定できず、待ちが生まれる）。メニューからは選べる。
        """
        for action in self.assign_actions:
            self.face_list.removeAction(action)
        self.assign_actions = []
        for index, person in enumerate(db.list_persons(self.connection), start=1):
            action = QAction(_assign_label(index, person, []), self)
            action.setData(person)
            if index <= 9:
                action.setShortcut(str(index))
                action.setShortcutContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut
                )
                self.face_list.addAction(action)
            action.triggered.connect(
                lambda checked=False, target=person: self._assign_selected(target)
            )
            self.assign_actions.append(action)

    def _menu_actions(self) -> tuple:
        """いまの表示で意味のある操作。**関係のないものは出さない。**

        ボタンで並べていたころは、モードが増えるたびにボタンが増え、
        押せないボタンが画面に残っていた。
        """
        scope = self._current_scope()
        if self._showing_rejections():
            return (self.action_undo_rejection,)
        if self._showing_unassigned_for_person():
            # **割り当て先は「人物に割り当て」（1〜9）から選ぶ。** ここに並ぶのは
            # 「この人物の候補から外す」（行き先つき・#113）。
            return self.not_this_person_actions
        if scope == SCOPE_PERSON:
            # 「顔認証外にする」は置かない。「この人物ではない ▸ 顔認証外へ」が
            # 同じ行き先で、否定の記録も残る（#113）。
            return (
                self.action_confirm,
                self.action_unassign,
                *self.not_this_person_actions,
                self.action_set_age,
            )
        # **顔認証外と他人(減点)は印の有無で分かれている**（#113）ので、
        # それぞれ効く側の操作だけを出す。
        if scope == SCOPE_REJECTED:
            return (self.action_penalize, self.action_unassign)
        if scope == SCOPE_PENALIZED:
            return (self.action_unpenalize, self.action_unassign)
        # 未分類から他人(減点)へ一手で（#113・利用者の指定）。
        return (self.action_reject, self.action_penalize)

    def _build_face_menu(self) -> QMenu:
        """顔の右クリックメニュー。**テストからも中身を見られるように分ける。**"""
        menu = QMenu(self)
        # 押せない理由をメニューの中で読めるようにする（灰色の項目だけでは
        # 壊れているのか選び方が足りないのかが分からない）。
        menu.setToolTipsVisible(True)
        person = self._current_person()
        if self.assign_actions:
            submenu = menu.addMenu(
                ASSIGN_MENU_OTHER
                if person is not None and not self._showing_unassigned_for_person()
                else ASSIGN_MENU
            )
            submenu.setToolTipsVisible(True)
            for action in self.assign_actions:
                submenu.addAction(action)
            submenu.setEnabled(bool(self.face_list.selectedItems()))
        else:
            empty = menu.addAction("先に人物を追加してください")
            empty.setEnabled(False)
        menu.addSeparator()
        grouped = self._groups_not_this_person()
        not_this_person_menu = None
        for action in self._menu_actions():
            if grouped and action in self.not_this_person_actions:
                # 人物の表示では「この人物ではない ▸ 行き先」にまとめる（#113）。
                if not_this_person_menu is None:
                    not_this_person_menu = menu.addMenu(NOT_THIS_PERSON_MENU)
                    not_this_person_menu.setToolTipsVisible(True)
                not_this_person_menu.addAction(action)
                continue
            menu.addAction(action)
        if not_this_person_menu is not None:
            not_this_person_menu.setEnabled(
                any(action.isEnabled() for action in self.not_this_person_actions)
            )
        menu.addSeparator()
        self._update_undo_actions()
        menu.addAction(self.action_undo)
        menu.addAction(self.action_redo)
        menu.addSeparator()
        menu.addAction(self.action_previous_page)
        menu.addAction(self.action_next_page)
        return self.menu_keeper.attach(menu)

    def _update_undo_actions(self) -> None:
        """元に戻す・やり直すを、**何が戻るかの名前つき**で出す（#116）。"""
        for action, name, step in (
            (self.action_undo, ACTION_UNDO, self.history.next_undo()),
            (self.action_redo, ACTION_REDO, self.history.next_redo()),
        ):
            action.setEnabled(step is not None)
            action.setText(name if step is None else f"{name}: {step.label}")

    def _undo_or_redo(self, redo: bool) -> None:
        """いちばん新しい操作を元に戻す（やり直す）。**戻せなければ理由を出す**（`undo.History`）。"""
        name = ACTION_REDO if redo else ACTION_UNDO
        try:
            with busy_cursor():
                step = (self.history.redo if redo else self.history.undo)(self.connection)
        except undo.UndoConflict as error:
            QMessageBox.information(self, name, str(error))
            self._update_undo_actions()
            return
        except sqlite3.Error as error:
            # 書けなかっただけ。操作は履歴に残っている（`undo.History._apply_or_keep`）
            QMessageBox.warning(self, name, undo_write_failed(error))
            self._update_undo_actions()
            return
        self.reload_faces()
        self._refresh_counts()
        self._refresh_details()
        self._update_undo_actions()
        self._mark_preview_done(f"{REDONE if redo else UNDONE} — {step.label}")

    def _groups_not_this_person(self) -> bool:
        """「この人物ではない」を行き先のメニューにまとめる表示か（人物の表示・#113）。"""
        return self._current_scope() == SCOPE_PERSON and not self._showing_rejections()

    def _show_face_menu(self, pos) -> None:
        self._select_under_cursor(pos)
        self._build_face_menu().exec(self.face_list.mapToGlobal(pos))

    def _select_under_cursor(self, pos) -> None:
        """右クリックした顔を選び直す。**選択と違う顔を処理しては困る。**

        すでに選ばれている顔を右クリックしたときは、選択を崩さない
        （**まとめて選んでから右クリック**が、いちばん多い使い方）。
        """
        item = self.face_list.itemAt(pos)
        if item is None or item.isSelected():
            return
        self.face_list.clearSelection()
        item.setSelected(True)
        self.face_list.setCurrentItem(item)

    def _on_face_selection_changed(self) -> None:
        self._update_face_actions()
        self._show_preview()

    def _update_face_actions(self) -> None:
        """選んだ顔に対して、何が押せるかを決める。

        **押せない理由をツールチップに出す。** 灰色の項目だけでは、壊れて
        いるのか選び方が足りないのかが分からない。
        """
        records = self._selected_records()
        has_selection = bool(records)
        scope = self._current_scope()
        # **確定は自動割り当てにしか効かない。** `assign_source` を
        # `'auto'` → `'manual'` へ上げる操作なので、手本に押しても
        # `assigned_at` が今の時刻に書き換わるだけで意味のある変化が起きない。
        has_auto = any(
            record.get("assign_source") == db.ASSIGN_AUTO for record in records
        )
        self.action_confirm.setEnabled(has_auto)
        self.action_confirm.setToolTip(
            CONFIRM_TOOLTIP_READY if has_auto else CONFIRM_TOOLTIP_BLOCKED
        )

        unassigned = self._viewing_unassigned()
        self.action_unassign.setText(
            ACTION_DETACH if scope == SCOPE_PERSON else ACTION_UNASSIGN
        )
        self.action_unassign.setEnabled(has_selection and not unassigned)
        self.action_unassign.setToolTip(
            "いま表示しているのは未分類の顔です。戻す先がありません。"
            if unassigned
            else "選んだ顔を未分類に戻します（顔認証外や自動割当を取り消せます）。"
        )

        # 「この人物ではない」は、割り当て先が分かる顔にしか記録できない。
        has_owner = scope == SCOPE_PERSON or any(
            record.get("person_id") is not None for record in records
        )
        self.action_not_this_person.setEnabled(has_selection and has_owner)
        grouped = self._groups_not_this_person()
        self.action_not_this_person.setText(
            ACTION_NOT_THIS_PERSON
            if not grouped
            else ACTION_NOT_THIS_PERSON_STAY_UNASSIGNED
            if unassigned
            else ACTION_NOT_THIS_PERSON_TO_UNASSIGNED
        )
        # 人物の表示では X は「この人物ではない ▸ 顔認証外へ」。同じ打鍵の
        # 「顔認証外にする」は押せなくする（押せる2つが重なると打鍵が効かない）。
        self.action_reject.setEnabled(has_selection and not grouped)
        self.action_not_this_person_reject.setEnabled(has_selection and grouped)
        self.action_not_this_person_penalize.setEnabled(has_selection and grouped)
        self.action_set_age.setEnabled(
            has_selection and scope == SCOPE_PERSON and not unassigned
        )
        self.action_undo_rejection.setEnabled(has_selection)
        # 減点の印は顔認証外の顔にだけ付く（`db.set_face_penalty`）
        marked = [bool(record.get("penalize")) for record in records]
        rejected_view = scope in (SCOPE_REJECTED, SCOPE_PENALIZED)
        # 左の「未分類」からも押せる（顔認証外にしてから印を付ける・#113）。人物の種別「未分類」は
        # 「この人物ではない ▸ 他人(減点)へ」が P を持つので押せなくする（打鍵が取り合う）。
        self.action_penalize.setEnabled(
            bool(records)
            and not all(marked)
            and (rejected_view or scope == SCOPE_UNASSIGNED)
        )
        self.action_unpenalize.setEnabled(rejected_view and any(marked))

        # 割り当て先の人物に、**選んだ顔の撮影時の年齢**を添える。
        shooting_dates = (
            db.taken_for_faces(
                self.connection, [record["id"] for record in records]
            )
            if records
            else []
        )
        for index, action in enumerate(self.assign_actions, start=1):
            action.setText(_assign_label(index, action.data(), shooting_dates))
            action.setEnabled(has_selection)
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        """一覧の下の1行。**右クリックと打鍵の案内。**

        **右クリックは目に見えない。** メニューに操作があることと、いま効く
        打鍵を出しておかないと、ボタンを消したぶんが「機能が無くなった」に見える。
        """
        selected = len(self.face_list.selectedItems())
        grouped = self._groups_not_this_person()
        keys = [
            f"{action.shortcut().toString()} "
            + (
                f"{NOT_THIS_PERSON_MENU}→{action.text()}"
                if grouped and action in self.not_this_person_actions
                else action.text()
            )
            for action in self._menu_actions()
            if action.isEnabled() and not action.shortcut().isEmpty()
        ]
        if self.assign_actions:
            upper = min(len(self.assign_actions), 9)
            keys.insert(0, f"1〜{upper} 人物に割り当て" if upper > 1 else "1 人物に割り当て")
        if self.history.can_undo():
            keys.append(f"{UNDO_SHORTCUTS[0]} {ACTION_UNDO}")
        head = f"顔を右クリックで操作（{selected:,} 件選択中）"
        self.hint_label.setText(head + ("　｜　" + " / ".join(keys) if keys else ""))

    # ------------------------------------------------------------------
    # 絞り込み
    # ------------------------------------------------------------------

    def _reload_months(self) -> None:
        """撮影年月の選択肢を作り直す。**いま選んでいる年月は保つ。**

        `scan` のあとなどに月が増えるので、選び直しを強いないようにする。
        **撮影日時が読めない顔が1件も無ければ、その選択肢は出さない。**
        """
        months = db.available_months(self.connection)
        for box in (self.month_from_box, self.month_to_box):
            keep = box.currentText() if box.count() else MONTH_ANY
            blocked = box.blockSignals(True)
            try:
                box.clear()
                box.addItem(MONTH_ANY)
                box.addItems(months)
                index = box.findText(keep)
                box.setCurrentIndex(index if index >= 0 else 0)
            finally:
                box.blockSignals(blocked)
        has_undated = bool(db.count_undated_faces(self.connection))
        self.undated_only_box.setVisible(has_undated)
        if not has_undated:
            self.undated_only_box.setChecked(False)

    def _month_changed(self) -> None:
        """範囲を触ったとき。**上下が逆転したまま空の一覧を見せない。**

        下限を上限より後ろにしたら、上限を押し上げる（その逆も同じ）。
        黙って0件にすると、絞り込みが壊れているように見える。
        """
        if not self.undated_only_box.isChecked():
            start, end = self._month_texts()
            if start != MONTH_ANY and end != MONTH_ANY and start > end:
                mover = (
                    self.month_to_box
                    if self.sender() is self.month_from_box
                    else self.month_from_box
                )
                target = start if mover is self.month_to_box else end
                blocked = mover.blockSignals(True)
                try:
                    mover.setCurrentIndex(mover.findText(target))
                finally:
                    mover.blockSignals(blocked)
        # 「撮影日時なしのみ」は範囲と排他。見た目でも分かるようにする。
        enabled = not self.undated_only_box.isChecked()
        self.month_from_box.setEnabled(enabled)
        self.month_to_box.setEnabled(enabled)
        self._reset_page()

    def _month_texts(self):
        return (self.month_from_box.currentText(), self.month_to_box.currentText())

    def _month_range(self):
        """``(開始, 終了, 読めない顔だけか)``。端の「指定なし」は ``None``。"""
        if self.undated_only_box.isChecked():
            return None, None, True
        start, end = self._month_texts()
        return (
            None if start in ("", MONTH_ANY) else start,
            None if end in ("", MONTH_ANY) else end,
            False,
        )

    def _age_range(self):
        """絞り込みの下限と上限。「指定なし」は ``None``。

        **`value() or None` と書かない。** 0 が偽なので、**0歳が「指定なし」に
        化ける**（`FaceAgeDialog.age` と同じ罠）。
        """
        minimum = self.min_age.value()
        maximum = self.max_age.value()
        return (None if minimum < 0 else minimum, None if maximum < 0 else maximum)

    def _source_filter(self) -> Optional[str]:
        """選ばれている種別。「すべて」なら ``None``（＝種別で絞らない）。"""
        index = self.source_box.currentIndex()
        if index >= len(SOURCE_FILTERS):
            return None
        return SOURCE_FILTERS[index][1]

    def _showing_rejections(self) -> bool:
        """「この人物ではない」の一覧を見ているか。

        **これだけは `assign_source` で絞れない**（否定は `FaceRejection` 表）。
        絞り込みの引数が変わるので、ここで見分ける。
        """
        return (
            self._current_scope() == SCOPE_PERSON
            and self.source_box.currentText() == NOT_THIS_PERSON_FILTER
        )

    def _showing_unassigned_for_person(self) -> bool:
        """人物を選び、種別を「未分類」にしているか（#69）。"""
        return (
            self._current_scope() == SCOPE_PERSON
            and self.source_box.currentText() == UNASSIGNED_FOR_PERSON_FILTER
        )

    def _viewing_unassigned(self) -> bool:
        """一覧の顔が**未分類**か。左の「未分類」と、人物の種別「未分類」の両方。

        **戻す先が無い・顔認証外にするのに確認が要らない**のはどちらも同じなので、
        操作の可否はこれで決める。
        """
        return (
            self._current_scope() == SCOPE_UNASSIGNED
            or self._showing_unassigned_for_person()
        )

    def _view_key(self) -> str:
        """並びの既定と、意味を持たない並びを引く鍵。"""
        if self._showing_unassigned_for_person():
            return SCOPE_PERSON_UNASSIGNED
        return self._current_scope()

    def _blur_filtering(self) -> bool:
        """鮮明さで絞っているか（#135）。"""
        return self.blur_check.isChecked()

    def _narrowed(self) -> bool:
        """**何かで絞っているか**（まとめ処理を押せる条件・#135）。

        撮影年月・撮影日時なしのみ・鮮明さ、人物なら年齢の範囲。絞らずに押せると、
        **一度の押し間違いでその表示の顔が全部動く**（未分類なら 2.9 万件）。
        """
        start, end, undated_only = self._month_range()
        if start is not None or end is not None or undated_only or self._blur_filtering():
            return True
        if self._current_scope() == SCOPE_PERSON:
            minimum, maximum = self._age_range()
            return minimum is not None or maximum is not None
        return False

    def _filter_summary(self) -> str:
        """いまの絞り込みを、確認の窓に出す1行にする。"""
        parts = []
        start, end, undated_only = self._month_range()
        if undated_only:
            parts.append("撮影日時なし")
        elif start is not None or end is not None:
            parts.append(f"撮影 {start or ''}〜{end or ''}")
        if self._current_scope() == SCOPE_PERSON:
            minimum, maximum = self._age_range()
            if minimum is not None or maximum is not None:
                parts.append(f"年齢 {'' if minimum is None else minimum}〜{'' if maximum is None else maximum}歳")
        if self._blur_filtering():
            parts.append(f"鮮明さ {self.blur_spin.value()} 以下")
        return "・".join(parts) or "絞り込みなし"

    def _blur_changed(self, *_args) -> None:
        self._reset_page()

    def _show_blur_value(self, value: int) -> None:
        """ゲージを動かしている間、数値欄に値だけ写す（一覧は読み直さない）。"""
        blocked = self.blur_spin.blockSignals(True)
        try:
            self.blur_spin.setValue(value)
        finally:
            self.blur_spin.blockSignals(blocked)

    def _blur_slider_changed(self, value: int) -> None:
        """ゲージを離した（またはキーで動かした）とき。数値欄に写して確定する。"""
        self._show_blur_value(value)
        self._apply_blur_value()

    def _blur_value_changed(self, *_args) -> None:
        """数値欄の値を変えたとき。ゲージを合わせて確定する。"""
        blocked = self.blur_slider.blockSignals(True)
        try:
            self.blur_slider.setValue(self.blur_spin.value())
        finally:
            self.blur_slider.blockSignals(blocked)
        self._apply_blur_value()

    def _apply_blur_value(self) -> None:
        """**絞っていなければ一覧に効かないので、ページを動かさない**
        （PR #136 のレビュー指摘2。数値欄の上でホイールを回しただけで先頭へ飛んでいた）。"""
        if self._blur_filtering():
            self._reset_page()

    def refresh_measure_count(self) -> None:
        """「鮮明さを測る」の横に、未計測の未分類の件数を出す（#135）。"""
        remaining = db.count_unassigned_without_sharpness(self.connection)
        self.measure_label.setText(
            "未分類はすべて測ってあります" if remaining == 0
            else f"未分類の未計測 {remaining:,} 件（絞り込みに出ません）"
        )

    def _source_changed(self) -> None:
        """種別を変えたとき。**メニューの中身も、並びの既定も変わる**
        （否定の一覧は別物。「未分類」は似た順で見る）。"""
        self._sync_view_controls()
        self._reset_page()

    def _current_order(self) -> str:
        index = max(0, self.order_box.currentIndex())
        return ORDER_CHOICES[index][1]

    def _filter_arguments(self) -> dict:
        """いま見ているものを、顔の絞り込みの引数にする。

        **「見るもの」と「共通の絞り込み」を足し合わせるのはここだけ。**
        一覧（`list_faces`）・件数（`count_faces`）・まとめて処理
        （`face_ids`）が同じ辞書を使うので、**画面に出ている顔と処理の対象が
        ずれない。**
        """
        scope = self._current_scope()
        person = self._current_person()
        if scope == SCOPE_PERSON and person is not None:
            if self._showing_unassigned_for_person():
                # **この人物の候補になりえない顔は出さない**（#69）。
                # 誕生前の写真には写れず、「この人物ではない」は人が決めた。
                filters = {
                    "unassigned": True,
                    "born_by": person.get("birth_date"),
                    "not_rejected_for_person": person["id"],
                }
            elif self._showing_rejections():
                # **この一覧の顔はその人物に割り当たっていない。**
                # `person_id` で絞ると1件も出ない。
                filters = {"rejected_for_person": person["id"]}
            else:
                filters = {
                    "person_id": person["id"],
                    "assign_source": self._source_filter(),
                }
            minimum, maximum = self._age_range()
            # **誕生日を渡すと、画面に出ている計算年齢でも絞れる。**
            # `Face.age` は `match` が書かないので、これが無いと自動割り当ての顔は
            # 1件も年齢で絞れない。
            filters.update(
                min_age=minimum,
                max_age=maximum,
                birth_date=person.get("birth_date"),
                include_unknown_age=self.include_unknown_age.isChecked(),
            )
        elif scope == SCOPE_REJECTED:
            # 他人(減点)に移った顔は出さない（#113・利用者の指定）。
            filters = {"assign_source": db.ASSIGN_REJECTED, "penalized": False}
        elif scope == SCOPE_PENALIZED:
            filters = {"penalized": True}
        else:
            filters = {"unassigned": True}
        if self._blur_filtering():
            filters["max_sharpness"] = self.blur_spin.value()
        start, end, undated_only = self._month_range()
        if undated_only:
            filters["undated_only"] = True
        else:
            if start is not None:
                filters["month_from"] = start
            if end is not None:
                filters["month_to"] = end
        return filters

    # ------------------------------------------------------------------
    # いまの絞り込みに当たる顔をまとめて処理する
    # ------------------------------------------------------------------

    def _sync_bulk_controls(self) -> None:
        """まとめ処理のボタンの意味と、押せるかをそろえる。

        **押せるのは何かで絞っているときだけ**（`_narrowed`）。絞らずに押せると
        一度の押し間違いでその表示の顔が全部動く。**裏で処理を流しているあいだも押せない**
        （PR #136 のレビュー指摘1。計測や `scan` が 500 件ずつ書くので、押した時点の対象に
        **一覧に出ていない顔**が混ざる）。押せない理由はツールチップに書く（隠さない）。
        """
        label, tooltip = self._bulk_action_labels()
        if label == "":
            self.bulk_button.setText("まとめて処理（この表示では無し）")
            self.bulk_button.setEnabled(False)
            self.bulk_button.setToolTip(
                "「この人物ではない」と、人物の「未分類」の一覧には、"
                "まとめて効く操作がありません。"
            )
            return
        self.bulk_button.setText(label)
        if self._job_running:
            self.bulk_button.setEnabled(False)
            self.bulk_button.setToolTip(BULK_BLOCKED_BY_JOB)
        elif not self._narrowed():
            self.bulk_button.setEnabled(False)
            self.bulk_button.setToolTip(BULK_NEEDS_FILTER)
        else:
            self.bulk_button.setEnabled(True)
            self.bulk_button.setToolTip(tooltip)

    def _bulk_action_labels(self) -> tuple:
        """いま表示している一覧に対して、まとめて何ができるか。

        **表示を切り替えたらボタンの意味も変える。** 未分類を見ているときは
        「まとめて顔認証外にする」、他人(減点)では「減点の印だけを外す」（顔認証外は残す・#113）、
        それ以外は「まとめて未分類へ戻す」。
        """
        if self._showing_rejections() or self._showing_unassigned_for_person():
            return ("", "")
        scope = self._current_scope()
        if scope == SCOPE_PENALIZED:
            # **未分類へは戻さない。** 戻すと顔認証外まで消える。減点の印だけを外す。
            return (
                "絞り込んだ他人(減点)をすべて顔認証外に戻す",
                "いまの絞り込みに当たる他人(減点)の顔の印を、ページをまたいで外します（顔認証外のまま残ります）。",
            )
        if scope == SCOPE_REJECTED:
            return (
                "絞り込んだ顔認証外をすべて取り消す",
                "いまの絞り込みに当たる顔認証外の顔を、ページをまたいで未分類へ戻します。",
            )
        if scope == SCOPE_PERSON:
            person = self._current_person()
            name = "" if person is None else person["name"]
            return (
                f"絞り込んだ{name}の割り当てをすべて解除",
                "いまの絞り込みに当たる顔を、ページをまたいで未分類へ戻します。",
            )
        if self._blur_filtering():
            return (
                f"ボケた未分類（鮮明さ {self.blur_spin.value()} 以下）をすべて顔認証外にする",
                "鮮明さで絞った未分類の顔を、ページをまたいでまとめて顔認証外にします"
                "（割り当て済みの顔と、鮮明さが未計測の顔は触りません）。",
            )
        return (
            "絞り込んだ未分類をすべて顔認証外にする",
            "いまの絞り込みに当たる未分類の顔を、ページをまたいでまとめて顔認証外にします"
            "（手動で割り当てた顔は触りません）。",
        )

    def _bulk_action(self) -> None:
        """まとめ処理。**表示中のページではなく、いまの絞り込みに当たる全件に効く。**"""
        if self._bulk_action_labels()[0] == "" or self._job_running or not self._narrowed():
            return
        filters = self._filter_arguments()
        with busy_cursor():
            face_ids = db.face_ids(self.connection, **filters)
        name = self._filter_summary()
        if not face_ids:
            QMessageBox.information(
                self, "対象なし", f"{name} に、まとめて処理できる顔はありません。"
            )
            return

        rejecting = self._current_scope() == SCOPE_UNASSIGNED
        unpenalizing = self._current_scope() == SCOPE_PENALIZED
        if rejecting:
            question = (
                f"{name}\n\n未分類の顔 {len(face_ids):,} 件をまとめて顔認証外にします。\n\n"
                "手動で割り当てた顔は触りません。\n"
                "顔認証外にしたあとは、Ctrl+Z か、表示を「顔認証外」にして取り消せます。"
            )
            title = "まとめて顔認証外にする"
        elif unpenalizing:
            question = (
                f"{name}\n\n他人(減点)の顔 {len(face_ids):,} 件の印をまとめて外します。\n\n"
                "顔認証外のまま残ります。"
            )
            title = "まとめて顔認証外に戻す"
        else:
            question = f"{name}\n\n{len(face_ids):,} 件をまとめて未分類へ戻します。"
            title = "まとめて取り消し"
        answer = QMessageBox.question(
            self,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if rejecting:
            self._run_with_progress(
                "まとめて顔認証外にしています",
                face_ids,
                lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
                f"完了 — {name} の {len(face_ids):,} 件を顔認証外にしました",
            )
        elif unpenalizing:
            self._run_with_progress(
                "まとめて減点の印を外しています",
                face_ids,
                lambda progress: db.set_face_penalty(self.connection, face_ids, False),
                f"完了 — {name} の {len(face_ids):,} 件の減点をやめました",
            )
        else:
            self._run_with_progress(
                "まとめて未分類に戻しています",
                face_ids,
                lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
                f"完了 — {name} の {len(face_ids):,} 件を未分類に戻しました",
            )
        self._sync_bulk_controls()

    # ------------------------------------------------------------------
    # 顔の一覧
    # ------------------------------------------------------------------

    def _reset_page(self):
        self.page = 0
        self._sync_bulk_controls()
        self.reload_faces()

    def reload_faces(self):
        filters = self._filter_arguments()
        order = self._current_order()
        self.similarity_teachers = None
        if order in recommend.ORDERS and self._current_person() is not None:
            # **似た順は SQL で並べられない。** 条件に当たる id を全件取り
            # （サムネイル抜き）、手本との距離で並べてから1ページ分だけ読む。
            ordered = self._ids_by_similarity(
                db.face_ids(self.connection, **filters),
                descending=order == recommend.ORDER_DISSIMILAR,
            )
            total = len(ordered)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            self.page = max(0, min(self.page, pages - 1))
            start = self.page * PAGE_SIZE
            records = db.faces_by_ids(
                self.connection, ordered[start : start + PAGE_SIZE], with_thumbnail=True
            )
        else:
            total = db.count_faces(self.connection, **filters)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            self.page = max(0, min(self.page, pages - 1))
            records = db.list_faces(
                self.connection,
                with_thumbnail=True,
                limit=PAGE_SIZE,
                offset=self.page * PAGE_SIZE,
                order=order,
                **filters,
            )
        _fill_face_list(
            self.face_list,
            records,
            self._age_labels(records),
            show_sharpness=self._blur_filtering()
            or order in (db.ORDER_SHARPNESS_ASC, db.ORDER_SHARPNESS_DESC),
        )
        # **桁を区切る。** 実データは万単位（未分類 31,275 件）で、
        # 区切らないと桁が読み取れない。
        text = f"{self.page + 1} / {pages:,} ページ（全 {total:,} 件）"
        if self.similarity_teachers == 0:
            # **黙って id 順に並べない。** 似た順のつもりで見てしまう。
            text += f"　{NO_TEACHERS_NOTICE}"
        self.page_label.setText(text)
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < pages - 1)
        # 作り直した直後は何も選ばれていない。**信号を止めて作り直している**ので
        # `itemSelectionChanged` は出ない（`_fill_face_list`）。ここで合わせる。
        self._update_face_actions()

    def _similarity_for(self, person_id: int) -> "recommend.PersonSimilarity":
        """選んでいる人物の距離の控え。**人物が変わったら作り直す。**"""
        if self.similarity is None or self.similarity.person_id != int(person_id):
            self.similarity = recommend.PersonSimilarity(person_id)
        return self.similarity

    def _ids_by_similarity(self, face_ids: List[int], descending: bool) -> List[int]:
        """顔の id を、選んでいる人物の手本に似た順（``descending`` なら似ていない順）に。

        **測っていない顔の分だけ測る**（`recommend.PersonSimilarity`）。初回は
        実データの未分類で約3秒かかるので、進み具合を出す。
        """
        person = self._current_person()
        similarity = self._similarity_for(person["id"])
        with busy_cursor():
            progress = WorkProgress(self, f"{person['name']} に似た顔を探しています", len(face_ids))
            try:
                self.similarity_teachers = similarity.update(
                    self.connection, face_ids, progress=progress
                )
            finally:
                progress.finish()
        return recommend.rank(face_ids, similarity.distances, descending=descending)

    def _age_labels(self, records: List[dict]) -> Dict[int, Optional[str]]:
        """一覧の1件ごとに添える文字。**撮影時の年齢**と、必要なら人物名。

        **自動割り当ての顔は `Face.age` が未設定**（`match` は年齢を書かない）。
        人物の誕生日と撮影日時から計算して出す。これが「その割り当てが
        正しいか」を人が見るときのいちばんの手がかりになる。

        **人物を選んでいない表示では、顔に付いている人物名も添える。** 誰に付いた顔かが
        分からないと見直せない（左の「自動割当」は #113 でやめたので、いまは人物の付いた
        顔が出ない）。
        """
        if not records:
            return {}
        shooting_dates = db.taken_by_face(
            self.connection, [record["id"] for record in records]
        )
        person = self._current_person()
        owners = (
            {}
            if person is not None
            else {row["id"]: row for row in db.list_persons(self.connection)}
        )
        labels: Dict[int, Optional[str]] = {}
        for record in records:
            owner = person or owners.get(record.get("person_id"))
            birth_date = None if owner is None else owner.get("birth_date")
            label = face_age_label(record, birth_date, shooting_dates.get(record["id"]))
            if person is None and owner is not None:
                label = f"{owner['name']} {label}" if label else owner["name"]
            labels[record["id"]] = label
        return labels

    def _previous_page(self):
        self.page = max(0, self.page - 1)
        self.reload_faces()

    def _next_page(self):
        self.page += 1
        self.reload_faces()

    def _selected_records(self) -> List[dict]:
        return [item.data(Qt.UserRole) for item in self.face_list.selectedItems()]

    def _selected_face_ids(self) -> List[int]:
        return [record["id"] for record in self._selected_records()]

    def _run_with_progress(self, label: str, face_ids: List[int], work, done_message: str) -> None:
        """件数の分かる作業を、砂時計と進み具合つきで流し、済んだことを知らせる。

        **操作ごとに同じ型を書かない。** 割り当て・顔認証外・解除・確定・年齢の
        違いは「何をするか」と「完了に何と出すか」だけ。
        """
        with busy_cursor():
            progress = WorkProgress(self, label, len(face_ids))
            try:
                # **ここを通る操作は、すべて元に戻せる**（#116）。完了の文が操作の名前になる。
                self.history.record(
                    self.connection, done_message.removeprefix("完了 — "), face_ids,
                    lambda: work(progress),
                )
                progress.step("一覧を作り直しています")
                self.reload_faces()
                self._refresh_counts()
                self._refresh_details()
            finally:
                progress.finish()
        self._update_undo_actions()
        self._mark_preview_done(done_message)

    def _mark_preview_done(self, message: str) -> None:
        """プレビューを「済んだ」表示にする。帯を出し、顔写真を薄くする。

        **顔は一覧から消えるのに、プレビューだけが残る。** 割り当てても顔認証外にしても
        同じで、そのままだと「まだ選んでいる」ように見え、**同じ顔をもう一度
        登録しようとする。**
        """
        self.preview_status.setText(message)
        self.preview_status.show()
        pixmap = self.preview_label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self.preview_label.setPixmap(dim_pixmap(pixmap))

    def _clear_preview_done(self) -> None:
        """「済んだ」表示を解く。**次の顔を選んだら必ず通る。**"""
        if self.preview_status.isVisible() or self.preview_status.text():
            self.preview_status.clear()
            self.preview_status.hide()

    def _selected_face_and_media(self):
        """プレビューの対象。選択が無ければ ``(None, None)``。

        複数選んでいるときは**最後に選んだ顔**を出す。
        """
        items = self.face_list.selectedItems()
        if not items:
            return None, None
        record = db.get_face(self.connection, items[-1].data(Qt.UserRole)["id"])
        if not record:
            return None, None
        return record, db.get_media_by_id(self.connection, record["media_id"])

    def _preview_info_text(self, media: dict) -> str:
        """プレビューの下に出す文字。

        **人物を選んでいないときは、登録した全員の撮影時の年齢を出す。**
        未分類の作業では「この顔は誰か」を決めるので、**その写真の時点で
        各人が何歳だったか**がいちばん効く手がかりになる（人物を選んで
        いるときは、その1人ぶんだけを出す）。
        """
        person = self._current_person()
        if person is not None:
            return format_media_info(media, self.source_roots, person)
        return format_media_info(
            media, self.source_roots, persons=db.list_persons(self.connection)
        )

    def _refresh_preview_info(self) -> None:
        """情報欄だけを書き直す。**元写真は読み直さない。**

        人物を選び直すと年齢の行が変わる。ここで画像ごと取り直すと、
        人物を選ぶたびに NFS（実測 24MB/s）から元写真を1枚読むことになる。
        """
        _, media = self._selected_face_and_media()
        if media:
            self.preview_info.setText(self._preview_info_text(media))

    def _show_preview(self) -> None:
        """選択中の顔を元写真から切り出して大きく表示する。

        サムネイルは160pxまで縮めてあるので、確認には元画像から取り直す。
        """
        record, media = self._selected_face_and_media()
        if not record or not media:
            return
        # 別の顔を選んだのだから、前の「完了」は消す。**残すと、いま選んでいる
        # 顔が済んでいるように見える。**
        self._clear_preview_done()
        # 情報は画像より先に出す。**元写真が開けないときこそ、
        # どのフォルダのどのファイルなのかが要る。**
        self.preview_info.setText(self._preview_info_text(media))
        bbox = (
            record["bbox_top"],
            record["bbox_right"],
            record["bbox_bottom"],
            record["bbox_left"],
        )
        try:
            # **元写真は NFS 上にあり、1枚あたり実測 220ms かかる。**
            # その間まったく無反応なので、押したことが分かるようにする。
            with busy_cursor():
                data = face.load_face_image_bytes(Path(media["path"]), bbox)
        except Exception as error:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"元写真を開けません:\n{media['path']}\n{error}")
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if pixmap.isNull():
            # **前の写真の画像を残さない。** 情報欄は先に新しい写真で
            # 上書きしているので、残すと上下で別の写真になる。
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"画像を表示できません:\n{media['path']}")
            return
        self.preview_label.setText("")
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    # ------------------------------------------------------------------
    # 顔への操作
    # ------------------------------------------------------------------

    def assign_faces(
        self, face_ids: List[int], person_id: int, age=db.KEEP_AGE, progress=None
    ) -> int:
        """選択した顔を人物へ手動で割り当てる。テストからも直接呼ぶ。

        ``age`` を省くと年齢は触らない。``None`` は「未設定」の指示。
        """
        return db.assign_faces(
            self.connection, face_ids, person_id, db.ASSIGN_MANUAL, age=age, progress=progress
        )

    def _assign_selected(self, person: Optional[dict] = None):
        """選んだ顔を人物へ割り当てる。**割り当て先はメニュー（1〜9）で選ぶ。**

        以前は左で選択中の人物へ割り当てていた。左が「見るもの」になったので、
        **割り当て先は操作のほうが持つ。** 未分類を見ながら、人物を選び直さずに
        1件ずつ別の人へ振れる（**往復がそのまま手数だった**）。
        """
        if person is None:
            person = self._current_person()
        if person is None:
            QMessageBox.information(
                self, "選択なし", "割り当てる人物を、右クリックのメニューから選んでください。"
            )
            return
        face_ids = self._selected_face_ids()
        if not face_ids:
            QMessageBox.information(self, "選択なし", "割り当てる顔を選択してください。")
            return
        # **まとめて選ぶのは、この割り当てのときがいちばん多い。**
        shooting_dates = db.taken_for_faces(self.connection, face_ids)
        dialog = FaceAgeDialog(
            self,
            summary=summarize_selection(len(face_ids), shooting_dates),
            initial_age=suggested_age(person.get("birth_date"), shooting_dates),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        age = dialog.age()
        self._run_with_progress(
            f"{person['name']} に割り当てています",
            face_ids,
            lambda progress: self.assign_faces(
                face_ids, person["id"], age, progress=progress
            ),
            f"完了 — {len(face_ids)} 件を {person['name']} に登録しました",
        )

    def _confirm_selected(self):
        """自動割り当てを**手本に昇格**する。`match` が手本にするのは手動だけ。"""
        face_ids = [
            record["id"]
            for record in self._selected_records()
            if record.get("assign_source") == db.ASSIGN_AUTO
        ]
        if not face_ids:
            return
        # **その顔に付いている人物へ確定する。** 全員ぶんの表示では、選んだ顔が
        # 別々の人物に付いていることがある。
        groups: Dict[int, List[int]] = {}
        for record in self._selected_records():
            if record.get("assign_source") != db.ASSIGN_AUTO:
                continue
            person_id = record.get("person_id")
            if person_id is not None:
                groups.setdefault(int(person_id), []).append(record["id"])

        def work(progress):
            for person_id, ids in groups.items():
                db.assign_faces(
                    self.connection, ids, person_id, db.ASSIGN_MANUAL, progress=progress
                )

        self._run_with_progress(
            "手本に確定しています",
            face_ids,
            work,
            f"完了 — {len(face_ids)} 件を手本に確定しました",
        )

    def _unassign_selected(self):
        """選んだ顔を未分類へ戻す。**顔認証外の取り消しと割り当ての解除がこれ。**"""
        face_ids = self._selected_face_ids()
        if not face_ids or self._viewing_unassigned():
            return
        self._run_with_progress(
            "未分類に戻しています",
            face_ids,
            lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を未分類に戻しました",
        )

    def _reject_for_person_selected(self, destination: Optional[str] = None):
        """**この人物ではない**、と記録する。ほかの人物には付きうる。

        ``destination`` は記録したあとの行き先（#113）。``None`` は未分類、
        `SCOPE_REJECTED` は顔認証外、`SCOPE_PENALIZED` は顔認証外にして減点の印も付ける。
        顔認証外にすると、ほかの人物にも自動で付かなくなる（否定の記録は残る）。

        **「割り当てを解除」では足りない。** `match` は手本と閾値だけで決まるので、
        未分類へ戻しただけだと**流すたびに同じ誤りが戻る**（実データで${PERSON_4}の
        1,785 件で起きた）。

        全員ぶんの表示では、選んだ顔が別々の人物に付いていることがある。
        **その顔に付いている人物ごとに記録する。**
        """
        person = self._current_person()
        groups: Dict[int, List[int]] = {}
        for record in self._selected_records():
            person_id = person["id"] if person is not None else record.get("person_id")
            if person_id is not None:
                groups.setdefault(int(person_id), []).append(record["id"])
        if not groups:
            return
        face_ids = [face_id for ids in groups.values() for face_id in ids]

        def work(progress):
            for person_id, ids in groups.items():
                db.reject_faces_for_person(self.connection, ids, person_id, progress=progress)
            if destination in (SCOPE_REJECTED, SCOPE_PENALIZED):
                db.reject_faces(self.connection, face_ids)
            if destination == SCOPE_PENALIZED:
                db.set_face_penalty(self.connection, face_ids, True)

        moved = {
            SCOPE_REJECTED: f"、{FILTER_REJECTED}へ移しました",
            SCOPE_PENALIZED: f"、{FILTER_PENALIZED}へ移しました",
        }.get(destination, "")
        self._run_with_progress(
            "この人物ではない、と記録しています",
            face_ids,
            work,
            f"完了 — {len(face_ids)} 件を「この人物ではない」に記録しました{moved}",
        )

    def _set_penalty_selected(self, penalize: bool) -> None:
        """顔認証外にした顔に「他人(減点)にする」の印を付ける／外す（#110）。

        **どちらの向きでも顔は一覧から消える**（#113）。顔認証外や未分類で印を付けた顔は
        他人(減点)へ（未分類の顔は顔認証外にしてから）、他人(減点)で印を外した顔は顔認証外へ移る。
        """
        face_ids = self._selected_face_ids()
        if not face_ids:
            return

        def work(progress):
            if penalize:
                # 未分類の顔は先に顔認証外にする（印は顔認証外の顔にしか付かない・#113）。
                # すでに顔認証外の顔に重ねても印は消えない（`db.reject_faces`）。
                not_rejected = [
                    record["id"]
                    for record in self._selected_records()
                    if record.get("assign_source") != db.ASSIGN_REJECTED
                ]
                db.reject_faces(self.connection, not_rejected)
            db.set_face_penalty(self.connection, face_ids, penalize)

        self._run_with_progress(
            "減点の印を付けています" if penalize else "減点の印を外しています",
            face_ids,
            work,
            # 完了の文が「元に戻す: 〈何を〉」の名前になるので「〜しました」にそろえる（PR #117 のレビュー指摘2）
            f"完了 — {len(face_ids)} 件" + ("を他人(減点)にしました" if penalize else "の減点をやめました"),
        )

    def _reject_selected(self):
        """**家族の誰でもない顔**として顔認証外にする。どの人物にも自動で付かなくなる。

        **割り当て済みの顔に押すときだけ確認する。** 未分類では毎件通る操作で、
        そこに確認を挟むと**作業そのものが遅くなる**（1件あたりの手数が総時間）。
        割り当て済みの顔は、押し間違えると手作業の結果が消える。
        """
        face_ids = self._selected_face_ids()
        if not face_ids:
            return
        if not self._viewing_unassigned():
            if (
                QMessageBox.question(
                    self,
                    "顔認証外にする",
                    f"{len(face_ids)} 件を『家族の誰でもない顔』として顔認証外にします。\n\n"
                    "**どの人物にも自動で付かなくなります。**\n"
                    "別の人物のものかもしれない顔は『この人物ではない』を使ってください。\n\n"
                    "進めますか？",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        # 顔認証外でも顔は一覧から消える。**割り当てと同じ症状**なので同じ扱いにする。
        self._run_with_progress(
            "顔認証外にしています",
            face_ids,
            lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を顔認証外にしました",
        )

    def _set_age_selected(self):
        """選んだ顔に撮影時の年齢を入れる。**人物を選んでいるときだけ。**"""
        person = self._current_person()
        face_ids = self._selected_face_ids()
        if person is None or not face_ids:
            return
        shooting_dates = db.taken_for_faces(self.connection, face_ids)
        dialog = FaceAgeDialog(
            self,
            summary=summarize_selection(len(face_ids), shooting_dates),
            initial_age=suggested_age(person.get("birth_date"), shooting_dates),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        age = dialog.age()
        self._run_with_progress(
            "年齢を設定しています",
            face_ids,
            # **1件ずつコミットしない。** 200件なら 200 回の fsync になる。
            lambda progress: db.set_faces_age(
                self.connection, face_ids, age, progress=progress
            ),
            f"完了 — {len(face_ids)} 件の年齢を設定しました",
        )

    def _undo_rejection_selected(self):
        """「この人物ではない」を取り消す。**押し間違いから戻れるように。**"""
        person = self._current_person()
        face_ids = self._selected_face_ids()
        if person is None or not face_ids:
            return
        self._run_with_progress(
            "「この人物ではない」を取り消しています",
            face_ids,
            lambda progress: db.clear_person_rejections(
                self.connection, face_ids, person["id"]
            ),
            f"完了 — {len(face_ids)} 件の「この人物ではない」を取り消しました",
        )


class AppWindow(QTabWidget):
    """起動したときの窓（#104）。**1枚目は今までの顔の割り当て、2枚目は写真の選定、
    3枚目は取り込み**（#129・`scan` を裏で流す）。

    1枚目（`MainWindow`）の中身と操作は変えない。2枚目（`gui_select.SelectionPanel`）は
    同じ DB の接続を使う（`select` の重みを動かして、結果をサムネイルで見る）。
    裏の処理の進み具合と「止める」は、どのタブからも見えるようタブの右上に置く。
    """

    def __init__(
        self,
        database_path: str,
        source_roots: SourceRoots = None,
        rule_path: Optional[str] = None,
        output_root: Optional[str] = None,
    ):
        super().__init__()
        from .gui_scan import JobController, JobStatusBar, ScanPanel
        from .gui_select import SelectionPanel

        self.assignment = MainWindow(database_path, source_roots=source_roots)
        roots = self.assignment.source_roots
        if isinstance(roots, str):
            roots = [roots]
        self.selection = SelectionPanel(
            self.assignment.connection, rule_path, output_root, roots or []
        )
        self.jobs = JobController(self)
        settings_roots = [source_roots] if isinstance(source_roots, str) else list(source_roots or [])
        self.scan_panel = ScanPanel(self.jobs, database_path, self.assignment.connection, settings_roots)
        self.job_status = JobStatusBar(self.jobs)
        self.setCornerWidget(self.job_status, Qt.TopRightCorner)
        # 設定の窓（#131）。右上は裏の処理の進み具合なので左上に置く。どのタブからも開ける
        from .gui_settings import OPEN_LABEL

        self.settings_button = QPushButton(OPEN_LABEL)
        self.settings_button.setToolTip("DB の場所・写真の置き場所・出力先・抽出ルール（config/app_settings.yml）")
        self.settings_button.clicked.connect(self.open_settings)
        self.setCornerWidget(self.settings_button, Qt.TopLeftCorner)
        self.database_path = database_path
        self.jobs.started.connect(lambda _name: self.assignment.set_job_running(True))
        self.jobs.finished.connect(self._job_finished)
        self.assignment.measure_requested.connect(self.start_measure)
        #: 子プロセスの入口で呼ぶもの（テストがフェイクを入れる）
        self.measure_initializer = None
        #: 鮮明さの計測の終わりの知らせ（テスト用）
        self.last_measure: Optional[dict] = None
        self.addTab(self.assignment, "顔の割り当て")
        self.addTab(self.selection, "写真の選定")
        self.addTab(self.scan_panel, "取り込み")
        self.setWindowTitle("PhotoArchiveAI")
        self.resize(1240, 820)

    def open_settings(self) -> Optional[dict]:
        """設定の窓を開く（#131）。保存したら、DB の場所以外をすぐ効かせる。保存した中身を返す。"""
        from .gui_settings import SettingsDialog

        dialog = SettingsDialog(self.database_path, self)
        if dialog.exec() != QDialog.Accepted or dialog.saved is None:
            return None
        self.apply_settings(dialog.saved)
        if dialog.database_needs_restart:
            QMessageBox.information(
                self,
                "設定",
                "保存しました。DB の場所は、次に起動したときから使います。",
            )
        return dialog.saved

    def apply_settings(self, settings: dict) -> None:
        """保存した設定を開いている画面に効かせる（#131）。**DB の場所は差し替えない。**

        root の解き方は起動したときと同じ（`main`・相対の root は設定ファイルの置き場所から）。
        """
        from .config import get_output_root, get_rule_path, get_source_roots

        roots = [resolve_root(root) for root in get_source_roots(settings)]
        self.assignment.source_roots = roots or db.list_scan_roots(self.assignment.connection) or None
        shown = self.assignment.source_roots
        shown = [shown] if isinstance(shown, str) else list(shown or [])
        self.selection.set_paths(get_rule_path(settings), get_output_root(settings), shown)
        self.scan_panel.settings_roots = roots
        self.scan_panel.reload_roots()

    def start_measure(self) -> None:
        """未計測の顔の鮮明さを裏で測る（#135・`jobs.run_measure_faces`）。"""
        from . import jobs

        if self.jobs.is_running():
            QMessageBox.information(self, "鮮明さを測る", MATCH_BLOCKED_BY_JOB)
            return
        job = jobs.Job(
            "measure_faces",
            jobs.run_measure_faces,
            initializer=self.measure_initializer,
            database_path=self.assignment.db_path,
        )
        self.jobs.start(job)

    def _job_finished(self, name: str, event: dict) -> None:
        """裏の処理が終わったら、顔の一覧を読み直す（`scan` が顔を足し消しした）。"""
        from .gui_scan import describe_final

        self.assignment.set_job_running(False)
        self.assignment._reload_person_list()
        self.assignment.refresh_measure_count()
        if name == "measure_faces":
            self.last_measure = event
            # 取り込みタブのログには出ないので、ここで知らせる
            if event["kind"] in ("aborted", "failed"):
                QMessageBox.warning(self, "鮮明さを測る", describe_final(name, event))
            else:
                self.assignment.measure_label.setText(
                    describe_final(name, event).splitlines()[0]
                )

    def closeEvent(self, event) -> None:
        """**裏の処理を流したまま閉じない。** 止めるなら、書きかけを保存して終わるのを待つ。"""
        from .gui_scan import wait_for_stop

        if self.jobs.is_running():
            answer = QMessageBox.question(
                self,
                "処理を流しています",
                "裏で処理を流しています。止めて閉じますか？\n"
                "済んだぶんは保存され、次に始めると続きからになります。",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            wait_for_stop(self.jobs, self)
        super().closeEvent(event)


def report_unhandled_error(connection, parent, error: BaseException) -> str:
    """画面の操作が例外で落ちたとき（#129）。**書きかけを捨て、何が起きたかを出す。**

    書き込みの途中で落ちると、書きかけが接続に残り、次の操作の commit に混ざる。
    いちばん起きやすいのは、裏の `scan` や端末の CLI が書き込みロックを握っていて
    待ちきれなかったとき（``database is locked``）。**黙って失わない**（利用者の決定）。
    出した文を返す（テスト用）。
    """
    try:
        connection.rollback()
    except sqlite3.Error:
        pass
    if isinstance(error, sqlite3.OperationalError) and (
        "locked" in str(error) or "busy" in str(error)
    ):
        message = WRITE_LOCKED
    else:
        message = f"操作が失敗しました。この操作の書きかけは捨てました。\n{type(error).__name__}: {error}"
    QMessageBox.warning(parent, "書き込めませんでした", message)
    return message


def install_error_handler(window: "AppWindow") -> None:
    """``sys.excepthook`` を差し替える。もとの出力（端末のトレースバック）も残す。"""
    import sys

    previous = sys.excepthook

    def hook(kind, error, trace):
        previous(kind, error, trace)
        if isinstance(error, Exception):
            report_unhandled_error(window.assignment.connection, window, error)

    sys.excepthook = hook


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(prog="photoarchive-gui")
    parser.add_argument(
        "--db",
        help="SQLite database path. 省略すると config/app_settings.yml の database_path を使う。",
    )
    args = parser.parse_args()

    # CLI と同じ解決順にする。GUI だけ設定ファイルを読まないと、
    # 「アプリケーション内にDBパスをハードコードしない」という方針から外れる。
    from .config import (
        find_legacy_settings_path,
        get_database_path,
        get_output_root,
        get_rule_path,
        get_source_roots,
        legacy_settings_stop_message,
        load_settings,
    )

    settings = load_settings()
    db_path = args.db or get_database_path(settings)
    # プレビューのフォルダを相対パスで出すためだけに使う。無くても動く
    # （無ければ MainWindow が DB に記録された root を使う）。
    source_roots = [resolve_root(root) for root in get_source_roots(settings)]
    if not db_path:
        legacy = find_legacy_settings_path()
        if legacy is not None:
            raise SystemExit(legacy_settings_stop_message(legacy))
        raise SystemExit(
            "データベースのパスが必要です。--db で指定するか、"
            "config/app_settings.yml の database_path を設定してください。"
        )

    from PySide6.QtCore import QLibraryInfo

    # OpenCV may register its own Qt plugins first; use the PySide6 plugins for this GUI.
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.path(
        QLibraryInfo.LibraryPath.PluginsPath
    )
    app = QApplication([])
    if not ensure_migrated(db_path):
        raise SystemExit("移行していないため、起動できません。")
    try:
        window = AppWindow(
            db_path,
            source_roots=source_roots,
            rule_path=get_rule_path(settings),
            output_root=get_output_root(settings),
        )
    except db.SchemaVersionError as error:
        # `ensure_migrated` を通っても開けないとき（版が新しすぎる、など）。
        # **黙って落とさない。** GUI は端末を見ずに起動されることがある。
        QMessageBox.critical(None, "データベースを開けません", str(error))
        raise SystemExit(str(error)) from error
    install_error_handler(window)
    window.show()
    sys.exit(app.exec())
