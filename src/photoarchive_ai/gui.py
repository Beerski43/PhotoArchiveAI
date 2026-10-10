"""人物登録と、検出済みの顔の割り当てを行う GUI。

``scan`` が写真から顔を検出して貯めたあと、ここで人物を作り、顔サムネイルを
選んで人物へ割り当てる。割り当て済みの顔が ``match`` の手本になる。

顔の件数は数万件になりうるので、一覧は必ずページ単位で読む。サムネイルの
BLOB を全件読むと数百MBになり、画面が固まる。
"""

import argparse
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import clustering, db, embedding, face, matcher, migration, recommend
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
#: 報告。実データの未割当1ページ目は先頭が 101px・残りが 160px だった）。
#:
#: **枠を固定すれば、どの顔が先頭に来ても同じ見た目になる。**
#: 高さは「サムネイル＋文字2行」ぶん（自動割当の表示は
#: `13391 (自動 55) ひより (0歳)` のように長い）。
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

FILTER_UNASSIGNED = "未割当"
FILTER_AUTO = "自動割当"
FILTER_REJECTED = "除外済み"

#: 左の一覧が表す「いま何を見ているか」。
#:
#: **人物と同じ一覧に並べる。** 未割当の割り当てと、割り当て済みの見直しは
#: 同じ作業の表裏なので、画面を分けると開き直しの往復が要る（以前は
#: 「割り当て済みを確認」が別ウィンドウだった）。
SCOPE_UNASSIGNED = "unassigned"
SCOPE_AUTO = "auto"
SCOPE_REJECTED = "rejected"
SCOPE_PERSON = "person"
#: 人物を選び、種別を「未割当」にしたとき（#69）。**左の一覧の項目ではない**
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
        SCOPE_AUTO,
        FILTER_AUTO,
        "match が自動で割り当てた顔（全員ぶん）。"
        "確信度の低い順に見直すと、誤りに早く当たる。",
    ),
    (
        SCOPE_REJECTED,
        FILTER_REJECTED,
        "「家族の誰でもない顔」として外した顔。未割当に戻せる。",
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
    ("この人物に似た順", recommend.ORDER_SIMILAR),
    ("この人物に似ていない順", recommend.ORDER_DISSIMILAR),
)

#: 表示ごとの既定の並び。**その表示で何をするかで決まる。**
#:
#: - 未割当・除外済み: 撮影日時の新しい順。**同じ行事の写真が固まる**ので、
#:   まとめて選んで一度に割り当てられる（#53）
#: - 自動割当: 確信度の低い順。**誤りに早く当たる**
#: - 人物: 年齢の若い順。**成長の順に並ぶ**ので、年齢の入れ間違いや
#:   別人の混入に気づきやすい（#53）
#: - 人物の未割当: この人物に似た順。**本人を目で探さずに済む**（#69。
#:   2026-10-08 の測定で、本人の 94.0% が1ページ目に入った）
DEFAULT_ORDER = {
    SCOPE_UNASSIGNED: db.ORDER_SHOT_DESC,
    SCOPE_AUTO: db.ORDER_SCORE_ASC,
    SCOPE_REJECTED: db.ORDER_SHOT_DESC,
    SCOPE_PERSON: db.ORDER_AGE,
    SCOPE_PERSON_UNASSIGNED: recommend.ORDER_SIMILAR,
}

#: 表示ごとに**意味を持たない並び**と、その理由。押せなくして理由を出す。
#:
#: **隠さずに押せなくする**（隠すと「そんな機能は無い」と思われる）。
#: 未割当と除外済みの顔は人物が決まっていないので年齢を出せず、確信度も
#: 持たない（実データの未割当 31,275 件で、年齢は 2 件・確信度は 0 件）。
#: 選べると **id 順のまま何も変わらず**、並べ替えが壊れているように見える。
_NO_PERSON_TO_COMPARE = (
    "似ているかを比べる人物がいません。左で人物を選んでください"
    "（未割当の顔は、人物を選んで種別を「未割当」にすると似た順に並びます）"
)
ORDER_UNUSABLE = {
    SCOPE_UNASSIGNED: {
        db.ORDER_AGE: "未割当の顔は人物が決まっていないので、年齢を出せません",
        db.ORDER_AGE_DESC: "未割当の顔は人物が決まっていないので、年齢を出せません",
        db.ORDER_SCORE_ASC: "未割当の顔は自動割り当ての確信度を持ちません",
        db.ORDER_SCORE_DESC: "未割当の顔は自動割り当ての確信度を持ちません",
        recommend.ORDER_SIMILAR: _NO_PERSON_TO_COMPARE,
        recommend.ORDER_DISSIMILAR: _NO_PERSON_TO_COMPARE,
    },
    SCOPE_AUTO: {
        recommend.ORDER_SIMILAR: _NO_PERSON_TO_COMPARE,
        recommend.ORDER_DISSIMILAR: _NO_PERSON_TO_COMPARE,
    },
    SCOPE_REJECTED: {
        db.ORDER_AGE: "除外した顔は人物を持たないので、年齢を出せません",
        db.ORDER_AGE_DESC: "除外した顔は人物を持たないので、年齢を出せません",
        db.ORDER_SCORE_ASC: "除外した顔は自動割り当ての確信度を持ちません",
        db.ORDER_SCORE_DESC: "除外した顔は自動割り当ての確信度を持ちません",
        recommend.ORDER_SIMILAR: _NO_PERSON_TO_COMPARE,
        recommend.ORDER_DISSIMILAR: _NO_PERSON_TO_COMPARE,
    },
    SCOPE_PERSON_UNASSIGNED: {
        db.ORDER_SCORE_ASC: "未割当の顔は自動割り当ての確信度を持ちません",
        db.ORDER_SCORE_DESC: "未割当の顔は自動割り当ての確信度を持ちません",
    },
}

#: 右クリックのメニューに出す操作の名前。**用語は仕様書 §1.3 に揃える。**
ACTION_CONFIRM = "手本に確定"
ACTION_UNASSIGN = "未割当に戻す"
ACTION_DETACH = "割り当てを解除"
ACTION_NOT_THIS_PERSON = "この人物ではない"
ACTION_REJECT = "誰でもない顔として除外…"
ACTION_SET_AGE = "年齢を設定…"
ACTION_UNDO_REJECTION = "「この人物ではない」を取り消す"
ASSIGN_MENU = "人物に割り当て"
ASSIGN_MENU_OTHER = "別の人物に割り当て"


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


def resolve_source_root(source_root: Optional[str]) -> Optional[str]:
    """設定に書かれた相対パスを、**設定ファイルの置き場所**を起点に解く。

    カレントディレクトリを起点にすると、リポジトリ直下以外から起動したときに
    相対化が静かに外れ、`GUI_USAGE.md` が約束している「`source_root` からの
    相対」ではなく、読めない NFS の絶対パスに戻る。`config` は設定ファイル
    自体を複数の場所から探しているのに、**その中の値だけ cwd 依存**という
    食い違いだった。

    `database_path` など他の相対値はここでは扱わない。設定の解釈を全体で
    変えるのは、明示的な指示が要る（CLAUDE.md §4）。
    """
    if not source_root or Path(source_root).is_absolute():
        return source_root
    settings_path = find_settings_path()
    if settings_path is None:
        return source_root
    # config/app_settings.yml → リポジトリ直下
    return str(settings_path.parent.parent / source_root)


def format_folder(folder: str, source_root: Optional[str] = None) -> str:
    """フォルダを、画面に出す形にする。**`source_root` からの相対。**

    絶対パスは長すぎて読めない（実データは
    `/mnt/nfs/nanoPi-NEO2/suzuki/Photo/2011/...`）。`source_root` の外にある
    ものは絶対パスのまま出す。

    **プレビューの情報欄と行事の選択で、同じ規則を使う。** 別々に書くと、
    同じフォルダが画面によって違う名前で出る。
    """
    path = Path(folder)
    if source_root:
        try:
            path = path.relative_to(Path(source_root).expanduser().resolve())
        except ValueError:
            # source_root の外にあるメディア。絶対パスのまま出す。
            pass
    if str(path) == ".":
        # source_root 直下。"." では何のことか読めない。
        return "（source_root 直下）"
    return str(path)


def format_event(folder: str, day: Optional[str], source_root: Optional[str] = None) -> str:
    """行事（フォルダ×日）を、画面に出す形にする。

    ``day`` が ``None`` なのは**撮影日時が読めない顔の集まり**（実データで
    **6,190 件**。この画面は特徴量の無い顔も数えるので、束ねられる
    「特徴量あり」の 5,916 件より多い）。「不明」と出して、日付のある行事と
    見分けられるようにする。
    """
    label = format_folder(folder, source_root)
    return f"{day} {label}" if day else f"（撮影日時不明） {label}"


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
    source_root: Optional[str] = None,
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

    ``persons`` は**人物を選んでいないとき**（未割当の表示）に渡す。
    その写真の時点で**各人が何歳だったか**を1行にまとめて出す。
    未割当の作業は「この顔は誰か」を決めることなので、**全員の年齢が並んで
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
    # **フォルダの出し方を写さない。** 行事の選択と同じ規則を通す
    # （別々に書くと、同じフォルダが画面によって違う名前で出る）。
    lines.append(f"フォルダ: {format_folder(str(path.parent), source_root)}")
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
    """

    def __init__(self, parent, counts: dict, database_path: str):
        super().__init__(parent)
        self.setWindowTitle("自動割り当て（match）を実行")
        layout = QVBoxLayout(self)

        detail = QLabel(
            f"手本 {counts['manual']:,} 件をもとに、未割当と自動割当の顔を付け直します。\n"
            f"いまの自動割当 {counts['auto']:,} 件は、いったん外してから付け直します。\n\n"
            "変わらないもの: 手本（手動の割り当て）・除外・「この人物ではない」・年齢\n"
            "「割り当てを解除」しただけの顔は、また付くことがあります"
            "（判断を残すには「この人物ではない」を使います）。\n\n"
            f"閾値 {matcher.DEFAULT_THRESHOLD} / マージン {matcher.DEFAULT_MARGIN}"
            "（photoarchive match と同じ既定）"
        )
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.setStyleSheet("padding: 8px; border: 1px solid #999;")
        layout.addWidget(detail)

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


def format_match_summary(
    summary: dict, persons: List[dict], backup: Optional[Path]
) -> str:
    """`match` を流したあとに出す結果。**CLI の出力と同じ数字を出す。**"""
    names = {int(person["id"]): person["name"] for person in persons}
    lines = [
        f"手本 {summary['teachers']:,} 件をもとに、{summary['candidates']:,} 件を照合しました。",
        "",
        f"自動で割り当てた顔: {summary['assigned']:,} 件",
        f"未割当のまま: {summary['unassigned']:,} 件",
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

    def __init__(self, parent=None, name="", relation="", memo="", birth_date=""):
        super().__init__(parent)
        self.setWindowTitle("人物情報")
        self.name_input = QLineEdit(name)
        self.relation_input = QLineEdit(relation)
        self.memo_input = QTextEdit(memo)
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)

        form = QFormLayout()
        form.addRow("名前", self.name_input)
        form.addRow("続柄", self.relation_input)
        form.addRow("誕生日", self._build_birth_date_row(birth_date))
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

#: 種別の選択肢に足す「未割当」（#69）。**その人物の候補を探す入口。**
#: 人物を選んでいるので「この人物に似た順」に並べられる。誕生前の写真と、
#: この人物ではないと記録した顔は出さない（候補になりえない）。
#: `SOURCE_FILTERS` の `None`（＝種別で絞らない）と取り違えないよう別に持つ。
UNASSIGNED_FOR_PERSON_FILTER = "未割当"

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


def _fill_face_list(
    widget: QListWidget,
    records: List[dict],
    age_labels: Optional[Dict[int, Optional[str]]] = None,
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
        _repopulate_face_list(widget, records, age_labels)
    finally:
        widget.blockSignals(blocked)


def _repopulate_face_list(
    widget: QListWidget,
    records: List[dict],
    age_labels: Optional[Dict[int, Optional[str]]] = None,
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
        item.setText(label)
        item.setData(Qt.UserRole, record)
        # **1件ずつにも同じ大きさを持たせる。** `setUniformItemSizes` は
        # 先頭の項目を見るので、**明示しないと先頭のサムネイル次第で全体が縮む。**
        item.setSizeHint(QSize(ITEM_WIDTH, ITEM_HEIGHT))
        widget.addItem(item)


def event_filters(folder: str, day: Optional[str]) -> dict:
    """行事を、顔の絞り込みの引数にする。

    **`None` を `db.UNDATED` に直すのはここだけ。** 行事の一覧は日が読めない
    行事を ``day=None`` で表すが、絞り込みの `None` は「日で絞らない」という
    別の指示なので、渡す前に必ず直す必要がある。

    **変換を2か所に書いたせいで、片方が抜けていた。** 一覧は直していたが、
    束ねる画面は `None` のまま渡しており、日付不明の行事を開くと**同じフォルダの
    別の日の顔まで束に入っていた**（実データで日付つきの未割当 18,000 件が
    363 フォルダで巻き込まれる。最悪の例は「日付不明 2 件」の行事に 985 件。
    PR #62 のレビュー指摘1）。
    """
    return {"folder": folder, "day": day if day is not None else db.UNDATED}


def format_event_row(counts: dict, source_root: Optional[str] = None) -> str:
    """行事を選ぶ一覧の1行。**件数を先に、行事を後ろに置く。**

    フォルダ名は長さがまちまちなので、後ろに置かないと件数の桁が揃わず、
    どれが大きいのか見比べられない。
    """
    return (
        f"未割当 {counts['unassigned']:,} / 手本 {counts['manual']:,}"
        f" / 除外 {counts['rejected']:,}   "
        f"{format_event(counts['folder'], counts['day'], source_root)}"
    )


class EventPickerDialog(QDialog):
    """顔の一覧を絞り込む**行事（フォルダ×日）**を選ぶ。

    **未割当の多い順に並べる。** まとめて処理して効き目が大きい行事が上に来る
    （実データの1位は 2011-04-16 の結婚式・1,357 件）。

    **ページャは置かない。** 「ページャの無い一覧を作らない」はサムネイルの
    BLOB を全件読まないための約束で、ここは文字だけ（実データで 2,700 行・
    0.4 秒）。目的の行事を探すにはページ送りより絞り込み欄が要る。
    """

    def __init__(self, parent, connection, source_root: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("行事を選ぶ（フォルダ×日）")
        self.source_root = source_root
        with busy_cursor():
            self.rows = db.event_face_counts(connection)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("行事名や日付で絞り込む（例: 結婚式 / 2011-04）")
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.event_list = QListWidget()
        self.event_list.itemDoubleClicked.connect(lambda _item: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self.summary_label = QLabel("")
        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.event_list)
        layout.addWidget(self.summary_label)
        layout.addWidget(buttons)
        self.resize(780, 540)
        self._apply_filter("")

    def _apply_filter(self, text: str) -> None:
        """絞り込み欄の文字を含む行事だけ出す。

        **画面に出している文字で照合する**（`source_root` からの相対と日付）。
        絶対パスで照合すると、画面に見えていない部分に当たってしまう。
        """
        needle = text.strip()
        self.event_list.clear()
        shown = 0
        for counts in self.rows:
            label = format_event_row(counts, self.source_root)
            if needle and needle not in label:
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, counts)
            self.event_list.addItem(item)
            shown += 1
        self.summary_label.setText(f"{shown} / {len(self.rows)} 行事")
        if shown:
            self.event_list.setCurrentRow(0)

    def selected_event(self) -> Optional[tuple]:
        """選ばれた行事を ``(フォルダ, 日)`` で返す。

        日は ``None`` のことがある（撮影日時が読めない顔の集まり）。
        **絞り込みに使う絶対パスのほうを返す**（画面の相対表示ではない）。
        """
        item = self.event_list.currentItem()
        if item is None:
            return None
        counts = item.data(Qt.UserRole)
        return (counts["folder"], counts["day"])


#: 束を割り直すときに、連結の上限をどれだけ下げるか。
#:
#: **0.05 ずつ。** 実測（2026-10-04）では 0.45 → 0.40 で決定が 19,678 → 22,552 回、
#: 他人誤認率が 0.66% → 0.48% に動く。これより粗い刻みだと、1回押しただけで
#: 束が細かく散らばって決定が増える。
SPLIT_STEP = 0.05

#: 束の中身を1度に出す上限。**サムネイルの BLOB を読む枚数**なので、
#: 一覧と同じ `PAGE_SIZE` に合わせる（実データの最大の束は 406 件）。
CLUSTER_PAGE_SIZE = PAGE_SIZE


class EventClusterDialog(QDialog):
    """1つの行事の顔を束ね、**束ごとにまとめて**割り当て／除外する（Issue #61）。

    束ねる理由は実測にある: **日付の読める未割当 51,860 件**を1件ずつ選ぶ作業が、
    行事ごとに束ねると **19,678 回の決定**まで落ちる（2026-10-04。連結の上限 0.45 で
    他人誤認率 0.66%）。**撮影日時が読めない 5,910 件はフォルダ単位**で、
    3,139 回（他人誤認率 0.88%）。

    守っていること。

    - **束は提案で、確定ではない。** 押すのは人。自動では1件も割り当てない
    - **触るのは `assign_source IS NULL` の顔だけ。** 束に手本が混ざっていても
      巻き込まない（手本が消えると `match` の土台が崩れる）
    - **束は閉じたら捨てる。** DBに持たない（手本が増えれば束は変わる）
    - **サムネイルは選んだ束のぶんだけ読む。** 行事には最大 1,357 件あるので、
      全部読むと画面が固まる（CLAUDE.md §8）
    """

    def __init__(
        self,
        parent,
        connection,
        folder: str,
        day: Optional[str],
        source_root: Optional[str] = None,
        threshold: Optional[float] = None,
    ):
        super().__init__(parent)
        self.connection = connection
        self.folder = folder
        self.day = day
        self.source_root = source_root
        self.setWindowTitle(f"行事の顔を束ねる - {format_event(folder, day, source_root)}")

        self.threshold = (
            threshold if threshold is not None else embedding.ACTIVE.cluster_threshold
        )
        #: 束ごとに、いまどの近さで割ったか。**割った束だけ厳しくする。**
        self.cluster_thresholds: List[float] = []
        with busy_cursor():
            records = db.load_faces_for_clustering(
                connection, **event_filters(folder, day)
            )
            self.records = {record["id"]: record for record in records}
            self.clusters = clustering.cluster_faces(
                [record["id"] for record in records],
                [record["embedding"] for record in records],
                threshold=self.threshold,
            )
            self.cluster_thresholds = [self.threshold] * len(self.clusters)
        self.page = 0

        self.cluster_list = QListWidget()
        self.cluster_list.currentRowChanged.connect(self._on_cluster_selected)

        self.face_list = make_face_list(QListWidget.SelectionMode.NoSelection)

        # **人物はこのダイアログで選ぶ。** 親画面の選択に従うと、束を見てから
        # 「この人だ」と決める順序にならない。
        all_persons = db.list_persons(connection)
        self.person_names = {person["id"]: person["name"] for person in all_persons}
        self.person_box = QComboBox()
        # **その日に生まれていない人物は出さない。** まとめて押すときは画面に
        # 出るのが人物名だけなので、選べると気づけない。
        self.persons = persons_alive_on(all_persons, day)
        for person in self.persons:
            self.person_box.addItem(person["name"], person)

        self.assign_button = QPushButton("この束をまとめて割り当て")
        self.reject_button = QPushButton("この束をまとめて除外")
        self.split_button = QPushButton("この束を割る")
        self.assign_button.clicked.connect(self._assign_cluster)
        self.reject_button.clicked.connect(self._reject_cluster)
        self.split_button.clicked.connect(self._split_cluster)
        self.split_button.setToolTip(
            "選んだ束を、もっと厳しい近さで割り直します"
            "（大きい束に別人が混ざっているときに使います）。"
        )

        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.notice_label = QLabel("")
        self.notice_label.setWordWrap(True)

        pager = QHBoxLayout()
        pager.addStretch(1)
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("人物"))
        actions.addWidget(self.person_box)
        actions.addWidget(self.assign_button)
        actions.addWidget(self.reject_button)
        actions.addWidget(self.split_button)
        actions.addStretch(1)

        faces_panel = QWidget()
        faces_layout = QVBoxLayout(faces_panel)
        faces_layout.setContentsMargins(0, 0, 0, 0)
        faces_layout.addLayout(pager)
        faces_layout.addWidget(self.face_list)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.cluster_list)
        splitter.addWidget(faces_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.notice_label)
        layout.addLayout(actions)
        layout.addWidget(close_buttons)
        self.resize(980, 640)

        self._reload_clusters()

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    def _pending_ids(self, index: int) -> List[int]:
        """その束の、**まだ人が判断していない顔**の id。

        手本と、この画面で割り当て済みにした顔は外す。**ここが
        「手本を巻き込まない」の実装**なので、呼び出し側で数え直さない。
        """
        if not (0 <= index < len(self.clusters)):
            return []
        return [
            face_id
            for face_id in self.clusters[index].face_ids
            if self.records[face_id]["assign_source"] is None
        ]

    def _teacher_names(self, index: int) -> List[str]:
        """その束に混ざっている**手本の人物名。**

        **手本が混ざっていれば、それが答え。** 誰の束かを探す手間が消える。
        名前の対応表は `__init__` で1度だけ作る（束ごとに引き直すと、
        束の数だけ問い合わせが出る）。
        """
        found = []
        for face_id in self.clusters[index].face_ids:
            record = self.records[face_id]
            if record["assign_source"] == db.ASSIGN_MANUAL:
                found.append(self.person_names.get(record["person_id"], "?"))
        return sorted(set(found))

    def _cluster_label(self, index: int) -> str:
        cluster = self.clusters[index]
        pending = len(self._pending_ids(index))
        notes = []
        if pending != cluster.size:
            notes.append(f"未判断 {pending}")
        teachers = self._teacher_names(index)
        if teachers:
            notes.append("手本: " + "・".join(teachers))
        if pending == 0:
            notes.append("済")
        label = f"束 {index + 1} — {cluster.size} 件"
        return f"{label}（{'、'.join(notes)}）" if notes else label

    def _reload_clusters(self, keep_row: Optional[int] = None) -> None:
        row = self.cluster_list.currentRow() if keep_row is None else keep_row
        blocked = self.cluster_list.blockSignals(True)
        try:
            self.cluster_list.clear()
            for index in range(len(self.clusters)):
                item = QListWidgetItem(self._cluster_label(index))
                item.setData(Qt.UserRole, index)
                self.cluster_list.addItem(item)
        finally:
            self.cluster_list.blockSignals(blocked)

        pending_total = sum(len(self._pending_ids(index)) for index in range(len(self.clusters)))
        decisions = sum(
            1 for index in range(len(self.clusters)) if self._pending_ids(index)
        )
        self.summary_label.setText(
            f"{format_event(self.folder, self.day, self.source_root)}\n"
            f"顔 {len(self.records):,} 件を {len(self.clusters):,} 束にまとめました"
            f"（未判断 {pending_total:,} 件 / 残る決定 {decisions:,} 回）。"
        )
        if self.clusters:
            self.cluster_list.setCurrentRow(min(max(row, 0), len(self.clusters) - 1))
        self._on_cluster_selected(self.cluster_list.currentRow())

    def _on_cluster_selected(self, row: int) -> None:
        self.page = 0
        self._reload_faces()

    def _reload_faces(self) -> None:
        row = self.cluster_list.currentRow()
        if not (0 <= row < len(self.clusters)):
            _fill_face_list(self.face_list, [])
            self.page_label.setText("-")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.reject_button.setEnabled(False)
            self.notice_label.setText("束がありません。")
            return
        face_ids = list(self.clusters[row].face_ids)
        pages = max(1, (len(face_ids) + CLUSTER_PAGE_SIZE - 1) // CLUSTER_PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        start = self.page * CLUSTER_PAGE_SIZE
        with busy_cursor():
            records = db.faces_by_ids(
                self.connection,
                face_ids[start : start + CLUSTER_PAGE_SIZE],
                with_thumbnail=True,
            )
        _fill_face_list(self.face_list, records)
        self.page_label.setText(
            f"{self.page + 1} / {pages} ページ（束の {len(face_ids):,} 件）"
        )
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)

        pending = self._pending_ids(row)
        has_person = self.person_box.count() > 0
        self.assign_button.setEnabled(bool(pending) and has_person)
        self.reject_button.setEnabled(bool(pending))
        # 1件の束は割れない。**判断の済んだ束も割らない**（割っても決定は減らない）。
        self.split_button.setEnabled(
            self.clusters[row].size > 1
            and bool(pending)
            and self.cluster_thresholds[row] > SPLIT_STEP
        )
        self._refresh_notice(row, pending, has_person)

    def _refresh_notice(self, row: int, pending: List[int], has_person: bool) -> None:
        """**押せない理由と、押したら何件動くかを文字で出す。**

        隠すと「そんな操作は無い」と思われ、黙って無効だと「壊れている」と
        思われる（`_sync_unassign_button` と同じ考え方）。
        """
        cluster = self.clusters[row]
        lines = []
        if not pending:
            lines.append("この束は、もう人が判断した顔だけです。")
        else:
            lines.append(f"押すと、この束の未判断 {len(pending):,} 件に効きます。")
        if len(pending) != cluster.size:
            lines.append(
                f"手本など {cluster.size - len(pending):,} 件は触りません。"
            )
        if not has_person:
            lines.append(
                "割り当て先の人物がいません（この日より後に生まれた人物は出していません）。"
            )
        self.notice_label.setText(" ".join(lines))

    def _previous_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self._reload_faces()

    def _next_page(self) -> None:
        self.page += 1
        self._reload_faces()

    # ------------------------------------------------------------------
    # まとめて処理する
    # ------------------------------------------------------------------

    def _run_with_progress(self, label: str, face_ids: List[int], work) -> None:
        row = self.cluster_list.currentRow()
        with busy_cursor():
            progress = WorkProgress(self, label, len(face_ids))
            try:
                work(progress)
                progress.step("束を数え直しています")
                # **DBから読み直して、どの顔が判断済みかを更新する。**
                # 束そのものは作り直さない（同じ画面で束が組み替わると、
                # いま見ていたものがどこへ行ったのか分からなくなる）。
                for record in db.faces_by_ids(self.connection, face_ids):
                    self.records[record["id"]].update(
                        {
                            "assign_source": record["assign_source"],
                            "person_id": record["person_id"],
                        }
                    )
                self._reload_clusters(keep_row=row)
            finally:
                progress.finish()

    def _split_cluster(self) -> None:
        """選んだ束を、**もっと厳しい近さで割り直す。**

        実データでいちばん大きい行事（2011-04-16 の結婚式・1,357 件）では、
        既定の 0.45 で **380 件の束**ができる。人が見て「別人が混ざっている」と
        分かっても、**割る手段が無いとその束はまとめて押せない**（押すと
        誤って数百件に効く）。そこで、その束だけ連結の上限を下げて割り直す。

        **行事全体は作り直さない。** 他の束は人が見終わった結果なので、
        画面の中で勝手に組み替えない。
        """
        row = self.cluster_list.currentRow()
        if not (0 <= row < len(self.clusters)):
            return
        cluster = self.clusters[row]
        if cluster.size < 2:
            return
        tighter = round(max(self.cluster_thresholds[row] - SPLIT_STEP, SPLIT_STEP), 3)
        face_ids = list(cluster.face_ids)
        with busy_cursor():
            parts = clustering.cluster_faces(
                face_ids,
                [self.records[face_id]["embedding"] for face_id in face_ids],
                threshold=tighter,
            )
        if len(parts) == 1:
            # **これ以上割れないことを黙らない。** 押しても何も起きないと
            # 「壊れている」と見える。
            self.cluster_thresholds[row] = tighter
            # **知らせを先に書かない。** `_reload_faces` が同じ札を書き直すので、
            # 先に書くと消える。
            self._reload_faces()
            self.notice_label.setText(
                f"近さ {tighter:.2f} でも、この束は割れませんでした"
                "（同じ人物の可能性が高いか、もっと下げる必要があります）。"
            )
            return
        self.clusters[row : row + 1] = parts
        self.cluster_thresholds[row : row + 1] = [tighter] * len(parts)
        self._reload_clusters(keep_row=row)
        self.notice_label.setText(
            f"近さ {tighter:.2f} で {len(parts)} 束に割りました。"
        )

    def _assign_cluster(self) -> None:
        row = self.cluster_list.currentRow()
        face_ids = self._pending_ids(row)
        person = self.person_box.currentData()
        if not face_ids or person is None:
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
            f"{person['name']} に割り当てています",
            face_ids,
            lambda progress: db.assign_faces(
                self.connection,
                face_ids,
                person["id"],
                db.ASSIGN_MANUAL,
                age=age,
                progress=progress,
            ),
        )

    def _reject_cluster(self) -> None:
        row = self.cluster_list.currentRow()
        face_ids = self._pending_ids(row)
        if not face_ids:
            return
        answer = QMessageBox.question(
            self,
            "まとめて除外",
            f"この束の未判断 {len(face_ids):,} 件を除外します。\n\n"
            "手本は触りません。あとで表示を「除外済み」にすれば取り消せます。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_with_progress(
            "まとめて除外しています",
            face_ids,
            lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
        )


class MainWindow(QWidget):
    """1枚の画面で、人物の登録から割り当て・見直しまでを済ませる。

    **左の一覧が「いま何を見ているか」。** 未割当・自動割当・除外済みと、
    登録した人物が同じ一覧に並ぶ。人物を選ぶと、その人物に割り当て済みの顔が
    右に出る（**以前は別ウィンドウだった**。開き直しの往復が要り、未割当の
    割り当てと見直しが同じ作業の表裏なのに画面が分かれていた）。

    **顔への操作は右クリックのメニューに集めた。** 見ているものによって
    意味のある操作が変わるので、ボタンで並べると**モードごとに増え続ける。**
    メニューには打鍵も出るので、**使いながら覚えられる。**

    **手作業の量がこの製品の精度の上限**（他人の顔の 99.1% が家族の写真に
    混ざっていて、まとめて消せない。2026-10-08 実測）。この画面の作りは
    すべて「1件あたりの手数を減らす」ためにある。
    """

    def __init__(self, database_path: str, source_root: Optional[str] = None):
        super().__init__()
        self.db_path = database_path
        #: プレビューのフォルダ表示を相対パスにするためだけに使う。
        #: 無ければ絶対パスで出すので、渡さなくても動く。
        self.source_root = source_root
        self.connection = db.ensure_database(database_path)
        self.setWindowTitle("PhotoArchiveAI 人物登録と顔の割り当て")
        self.page = 0
        #: 絞り込んでいる行事（フォルダ×日）。`None` はすべて。
        self.event: Optional[tuple] = None
        #: 1〜9 の打鍵で割り当てる人物（左の一覧の並び順）。
        self.assign_actions: List[QAction] = []
        #: 「この人物に似た順」の距離。**ページ送りのあいだ持ち続ける**（#69）。
        #: 人物を選び直したら作り直す（`_similarity_for`）。
        self.similarity: Optional[recommend.PersonSimilarity] = None
        #: 直前に並べたとき、根拠にした手本の件数。0 なら似た順に並べられていない。
        self.similarity_teachers: Optional[int] = None

        person_panel = self._build_person_panel()
        face_panel = self._build_face_panel()
        self._build_face_actions()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(person_panel)
        splitter.addWidget(face_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([320, 860])

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(splitter)
        self.resize(1180, 760)

        # 一覧を作ると「未割当」が選ばれ、そのまま顔の一覧まで作られる。
        self._reload_person_list()

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
            "手本をもとに、未割当と自動割当の顔を付け直します（photoarchive match と同じ）。"
        )
        self.match_button.clicked.connect(self._run_match)

        person_buttons = QHBoxLayout()
        person_buttons.addWidget(self.add_person_button)
        person_buttons.addWidget(self.edit_person_button)
        person_buttons.addWidget(self.delete_person_button)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addLayout(person_buttons)
        layout.addWidget(self.person_list, 1)
        layout.addWidget(self.details_label)
        layout.addWidget(self.match_button)
        return panel

    def _build_face_panel(self) -> QWidget:
        """右: 絞り込み・顔の一覧・プレビュー・行事。"""
        # --- 共通の絞り込み（どの表示でも意味がある） -------------------
        self.order_box = QComboBox()
        for label, _ in ORDER_CHOICES:
            self.order_box.addItem(label)
        self.order_box.setToolTip(
            "並び順。**条件に当たる全件を並べてから、ページに分けて出す**"
            "（いま見えている200件の中だけで並べ替えるのではない）。\n"
            "年齢は**画面に出ている年齢**（確定値か、誕生日から計算した値）で並べる。\n"
            "表示を切り替えると、その表示に向いた順に戻る"
            "（未割当は撮影日時の新しい順、自動割当は確信度の低い順、"
            "人物は年齢の若い順、人物の未割当はこの人物に似た順）。\n"
            "「この人物に似た順」は、その人物の手本との距離で並べる"
            "（5点整列ができなかった手本は使わない）。"
        )
        self.order_box.currentIndexChanged.connect(self._reset_page)

        # **撮影年月の範囲で絞る。** 家族の写っていない行事（結婚式・旅行先の
        # 他人など）は時期でまとまっているので、**その期間だけを開いてまとめて
        # 除外できる。** 片方だけの指定（「2015-06 以降」など）もできる。
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

        # --- 行事で絞り、まとめて処理する ------------------------------
        # **日付の読める未割当 51,860 件は、2,122 の行事（フォルダ×日）に
        # 散っている**（2026-10-04 実測）。行事ごとに束ねると決定が 19,678 回まで
        # 落ちる。**撮影日時が読めない 5,910 件は 521 フォルダ**に散っている。
        self.event_label = QLabel("")
        self.event_label.setWordWrap(True)
        self.choose_event_button = QPushButton("行事を選ぶ")
        self.clear_event_button = QPushButton("解除")
        self.cluster_event_button = QPushButton("この行事の顔を束ねる")
        self.bulk_event_button = QPushButton("")
        self.choose_event_button.clicked.connect(self._choose_event)
        self.clear_event_button.clicked.connect(self._clear_event)
        self.cluster_event_button.clicked.connect(self._cluster_event)
        self.bulk_event_button.clicked.connect(self._bulk_event_action)

        event_row = QHBoxLayout()
        event_row.addWidget(self.event_label, 1)
        event_row.addWidget(self.choose_event_button)
        event_row.addWidget(self.clear_event_button)
        event_row.addWidget(self.cluster_event_button)
        event_row.addWidget(self.bulk_event_button)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addLayout(common_filters)
        layout.addWidget(self.person_filter_row)
        layout.addWidget(face_area, 1)
        layout.addWidget(self.hint_label)
        layout.addLayout(event_row)
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
        self.action_not_this_person.triggered.connect(self._reject_for_person_selected)

        self.action_reject = QAction(ACTION_REJECT, self)
        self.action_reject.setShortcut("X")
        self.action_reject.setToolTip(
            "家族の誰でもない顔として除外する。\n"
            "**どの人物にも自動で付かなくなる。**"
        )
        self.action_reject.triggered.connect(self._reject_selected)

        self.action_set_age = QAction(ACTION_SET_AGE, self)
        self.action_set_age.setShortcut("G")
        self.action_set_age.triggered.connect(self._set_age_selected)

        self.action_undo_rejection = QAction(ACTION_UNDO_REJECTION, self)
        self.action_undo_rejection.setShortcut("Z")
        self.action_undo_rejection.triggered.connect(self._undo_rejection_selected)

        self.face_action_set = (
            self.action_confirm,
            self.action_unassign,
            self.action_not_this_person,
            self.action_reject,
            self.action_set_age,
            self.action_undo_rejection,
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
                # **先頭の3つはドラッグで動かさない。** 人物の並び順だけを
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
        """一覧の中から、人物 id か表示を選ぶ。**見つからなければ「未割当」。**"""
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
            SCOPE_AUTO: counts["auto"],
            SCOPE_REJECTED: counts["rejected"],
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

        **人物の項目だけを数える。** 先頭の表示（未割当など）と区切り線が
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
        """いま見ているもの。選択が無ければ「未割当」。"""
        item = self.person_list.currentItem()
        if item is None:
            return SCOPE_UNASSIGNED
        return item.data(SCOPE_ROLE) or SCOPE_UNASSIGNED

    def _current_person(self) -> Optional[dict]:
        """選択中の人物。表示（未割当など）を選んでいるときは ``None``。"""
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
        # **並びの既定は「見ているもの」で決める**（種別の「未割当」も含む）。
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
                f"未割当 {counts['unassigned']:,} / 手本 {counts['manual']:,} /"
                f" 自動 {counts['auto']:,} / 除外 {counts['rejected']:,}"
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
        menu.exec(self.person_list.mapToGlobal(pos))

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
        person_id = db.add_person(self.connection, name, relation, memo, birth_date=birth_date)
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
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = self._validated_values(dialog)
        if values is None:
            return
        name, relation, memo, birth_date = values
        db.update_person(
            self.connection, person["id"], name, relation, memo, birth_date=birth_date
        )
        # 編集した人物を選び直す。**誕生日を入れたら、年齢の行がその場で出る。**
        self._reload_person_list(select_person_id=person["id"])

    def _delete_person(self):
        person = self._current_person()
        if person is None:
            return
        if QMessageBox.question(
            self, "削除確認", f"{person['name']} を削除しますか？\n割り当て済みの顔は未割当に戻ります。"
        ) != QMessageBox.Yes:
            return
        db.delete_person(self.connection, person["id"])
        self._reload_person_list(select_scope=SCOPE_UNASSIGNED)

    # ------------------------------------------------------------------
    # 自動割り当て（match）
    # ------------------------------------------------------------------

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
        dialog = MatchDialog(self, counts, self.db_path)
        if dialog.exec() != QDialog.Accepted:
            return

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
            # 「この人物の候補から外す」と「誰でもない顔」。
            return (self.action_not_this_person, self.action_reject)
        if scope == SCOPE_PERSON:
            return (
                self.action_confirm,
                self.action_unassign,
                self.action_not_this_person,
                self.action_reject,
                self.action_set_age,
            )
        if scope == SCOPE_AUTO:
            return (
                self.action_confirm,
                self.action_unassign,
                self.action_not_this_person,
                self.action_reject,
            )
        if scope == SCOPE_REJECTED:
            return (self.action_unassign,)
        return (self.action_reject,)

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
        for action in self._menu_actions():
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.action_previous_page)
        menu.addAction(self.action_next_page)
        return menu

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
            "いま表示しているのは未割当の顔です。戻す先がありません。"
            if unassigned
            else "選んだ顔を未割当に戻します（除外や自動割当を取り消せます）。"
        )

        # 「この人物ではない」は、割り当て先が分かる顔にしか記録できない。
        has_owner = scope == SCOPE_PERSON or any(
            record.get("person_id") is not None for record in records
        )
        self.action_not_this_person.setEnabled(has_selection and has_owner)
        self.action_reject.setEnabled(has_selection)
        self.action_set_age.setEnabled(
            has_selection and scope == SCOPE_PERSON and not unassigned
        )
        self.action_undo_rejection.setEnabled(has_selection)

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
        keys = [
            f"{action.shortcut().toString()} {action.text()}"
            for action in self._menu_actions()
            if action.isEnabled() and not action.shortcut().isEmpty()
        ]
        if self.assign_actions:
            upper = min(len(self.assign_actions), 9)
            keys.insert(0, f"1〜{upper} 人物に割り当て" if upper > 1 else "1 人物に割り当て")
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
        """人物を選び、種別を「未割当」にしているか（#69）。"""
        return (
            self._current_scope() == SCOPE_PERSON
            and self.source_box.currentText() == UNASSIGNED_FOR_PERSON_FILTER
        )

    def _viewing_unassigned(self) -> bool:
        """一覧の顔が**未割当**か。左の「未割当」と、人物の種別「未割当」の両方。

        **戻す先が無い・除外に確認が要らない**のはどちらも同じなので、
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

    def _source_changed(self) -> None:
        """種別を変えたとき。**メニューの中身も、並びの既定も変わる**
        （否定の一覧は別物。「未割当」は似た順で見る）。"""
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
        elif scope == SCOPE_AUTO:
            filters = {"assign_source": db.ASSIGN_AUTO}
        elif scope == SCOPE_REJECTED:
            filters = {"assign_source": db.ASSIGN_REJECTED}
        else:
            filters = {"unassigned": True}
        # **行事を選んでいないときは `folder` / `day` を渡さない。** 渡すと
        # `Media` を辿る条件が増え、撮影日時の索引を順に歩く経路（未割当
        # 58,212 件を 0.002 秒で1ページ分読む）から外れる。
        if self.event is not None:
            # **変換は `event_filters` に1つだけ。** `None` を渡すと
            # 「日で絞らない」になり、フォルダ全体が対象になってしまう。
            filters.update(event_filters(*self.event))
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
    # 行事で絞る / 束ねる / まとめて処理する
    # ------------------------------------------------------------------

    def _choose_event(self) -> None:
        dialog = EventPickerDialog(self, self.connection, self.source_root)
        if dialog.exec() != QDialog.Accepted:
            return
        event = dialog.selected_event()
        if event is None:
            return
        self.event = event
        self._reset_page()

    def _clear_event(self) -> None:
        self.event = None
        self._reset_page()

    def _event_name(self) -> str:
        if self.event is None:
            return "すべての行事"
        folder, day = self.event
        return format_event(folder, day, self.source_root)

    def _sync_event_controls(self) -> None:
        """行事の行の表示と、まとめて処理するボタンの意味をそろえる。

        **まとめて処理できるのは行事を選んでいるときだけ。** 選んでいないと
        対象が未割当 57,770 件全部になり、**一度の押し間違いで作業がすべて
        飛ぶ。** 押せない理由はツールチップに書く（隠さない）。
        """
        self.event_label.setText(f"行事: {self._event_name()}")
        self.clear_event_button.setEnabled(self.event is not None)

        self.cluster_event_button.setEnabled(self.event is not None)
        self.cluster_event_button.setToolTip(
            "この行事の顔を似たもの同士で束ね、束ごとにまとめて割り当て／除外します。"
            if self.event is not None
            else "先に「行事を選ぶ」で行事を指定してください。"
        )

        label, tooltip = self._bulk_action_labels()
        self.bulk_event_button.setText(label)
        self.bulk_event_button.setEnabled(self.event is not None and label != "")
        if label == "":
            self.bulk_event_button.setText("まとめて処理（この表示では無し）")
            self.bulk_event_button.setToolTip(
                "「この人物ではない」と、人物の「未割当」の一覧には、"
                "まとめて効く操作がありません。"
            )
        else:
            self.bulk_event_button.setToolTip(
                tooltip
                if self.event is not None
                else "先に「行事を選ぶ」で行事を指定してください。"
            )

    def _bulk_action_labels(self) -> tuple:
        """いま表示している一覧に対して、まとめて何ができるか。

        **表示を切り替えたらボタンの意味も変える。** 未割当を見ているときは
        「まとめて除外」、それ以外は「まとめて未割当へ戻す」。
        """
        if self._showing_rejections() or self._showing_unassigned_for_person():
            return ("", "")
        scope = self._current_scope()
        if scope == SCOPE_REJECTED:
            return (
                "この行事の除外をすべて取り消す",
                "この行事で除外した顔を、ページをまたいで未割当へ戻します。",
            )
        if scope == SCOPE_AUTO:
            return (
                "この行事の自動割当をすべて取り消す",
                "この行事の自動割当を、ページをまたいで未割当へ戻します。",
            )
        if scope == SCOPE_PERSON:
            person = self._current_person()
            name = "" if person is None else person["name"]
            return (
                f"この行事の{name}の割り当てをすべて解除",
                "いま表示している条件に当たる顔を、ページをまたいで未割当へ戻します。",
            )
        return (
            "この行事の未割当をすべて除外",
            "この行事の未割当の顔を、ページをまたいでまとめて除外します"
            "（手動で割り当てた顔は触りません）。",
        )

    def _cluster_event(self) -> None:
        """行事の顔を束ねる画面を開く。**束ねる計算はこの中で捨てる。**"""
        if self.event is None:
            return
        folder, day = self.event
        try:
            dialog = EventClusterDialog(
                self, self.connection, folder, day, source_root=self.source_root
            )
        except clustering.TooManyFacesError as error:
            # **黙って先頭だけ束ねない。** 出ていない顔が未割当のまま残る。
            QMessageBox.warning(self, "束ねられません", str(error))
            return
        if not dialog.clusters:
            QMessageBox.information(
                self,
                "対象なし",
                f"{self._event_name()} に、束ねられる顔はありません"
                "（特徴量を持つ未割当の顔がありません）。",
            )
            return
        dialog.exec()
        # 束ねる画面の中で割り当て・除外をしているので、親の一覧も作り直す。
        self.reload_faces()
        self._refresh_counts()
        self._refresh_details()
        self._sync_event_controls()

    def _bulk_event_action(self) -> None:
        """行事単位のまとめ処理。**表示中のページではなく行事全体に効く。**"""
        if self.event is None or self._bulk_action_labels()[0] == "":
            return
        filters = self._filter_arguments()
        with busy_cursor():
            face_ids = db.face_ids(self.connection, **filters)
        name = self._event_name()
        if not face_ids:
            QMessageBox.information(
                self, "対象なし", f"{name} に、まとめて処理できる顔はありません。"
            )
            return

        rejecting = self._current_scope() == SCOPE_UNASSIGNED
        if rejecting:
            question = (
                f"{name}\n\n未割当の顔 {len(face_ids):,} 件をまとめて除外します。\n\n"
                "手動で割り当てた顔は触りません。\n"
                "除外したあとは、表示を「除外済み」にして取り消せます。"
            )
            title = "まとめて除外"
        else:
            question = f"{name}\n\n{len(face_ids):,} 件をまとめて未割当へ戻します。"
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
                "まとめて除外しています",
                face_ids,
                lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
                f"完了 — {name} の {len(face_ids):,} 件を除外しました",
            )
        else:
            self._run_with_progress(
                "まとめて未割当に戻しています",
                face_ids,
                lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
                f"完了 — {name} の {len(face_ids):,} 件を未割当に戻しました",
            )
        self._sync_event_controls()

    # ------------------------------------------------------------------
    # 顔の一覧
    # ------------------------------------------------------------------

    def _reset_page(self):
        self.page = 0
        self._sync_event_controls()
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
        _fill_face_list(self.face_list, records, self._age_labels(records))
        # **桁を区切る。** 実データは万単位（未割当 31,275 件）で、
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
        実データの未割当で約3秒かかるので、進み具合を出す。
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

        **全員ぶんの表示（自動割当）では人物名も添える。** 誰に付いた顔かが
        分からないと見直せない。
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

        **操作ごとに同じ型を書かない。** 割り当て・除外・解除・確定・年齢の
        違いは「何をするか」と「完了に何と出すか」だけ。
        """
        with busy_cursor():
            progress = WorkProgress(self, label, len(face_ids))
            try:
                work(progress)
                progress.step("一覧を作り直しています")
                self.reload_faces()
                self._refresh_counts()
                self._refresh_details()
            finally:
                progress.finish()
        self._mark_preview_done(done_message)

    def _mark_preview_done(self, message: str) -> None:
        """プレビューを「済んだ」表示にする。帯を出し、顔写真を薄くする。

        **顔は一覧から消えるのに、プレビューだけが残る。** 割り当てても除外しても
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
        未割当の作業では「この顔は誰か」を決めるので、**その写真の時点で
        各人が何歳だったか**がいちばん効く手がかりになる（人物を選んで
        いるときは、その1人ぶんだけを出す）。
        """
        person = self._current_person()
        if person is not None:
            return format_media_info(media, self.source_root, person)
        return format_media_info(
            media, self.source_root, persons=db.list_persons(self.connection)
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
        **割り当て先は操作のほうが持つ。** 未割当を見ながら、人物を選び直さずに
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
        """選んだ顔を未割当へ戻す。**除外の取り消しと割り当ての解除がこれ。**"""
        face_ids = self._selected_face_ids()
        if not face_ids or self._viewing_unassigned():
            return
        self._run_with_progress(
            "未割当に戻しています",
            face_ids,
            lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を未割当に戻しました",
        )

    def _reject_for_person_selected(self):
        """**この人物ではない**、と記録する。ほかの人物には付きうる。

        **「割り当てを解除」では足りない。** `match` は手本と閾値だけで決まるので、
        未割当へ戻しただけだと**流すたびに同じ誤りが戻る**（実データでひよりの
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

        self._run_with_progress(
            "この人物ではない、と記録しています",
            face_ids,
            work,
            f"完了 — {len(face_ids)} 件を「この人物ではない」に記録しました",
        )

    def _reject_selected(self):
        """**家族の誰でもない顔**として除外する。どの人物にも自動で付かなくなる。

        **割り当て済みの顔に押すときだけ確認する。** 未割当では毎件通る操作で、
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
                    "誰でもない顔として除外",
                    f"{len(face_ids)} 件を『家族の誰でもない顔』として除外します。\n\n"
                    "**どの人物にも自動で付かなくなります。**\n"
                    "別の人物のものかもしれない顔は『この人物ではない』を使ってください。\n\n"
                    "進めますか？",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        # 除外でも顔は一覧から消える。**割り当てと同じ症状**なので同じ扱いにする。
        self._run_with_progress(
            "除外しています",
            face_ids,
            lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を除外しました",
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
        get_source_root,
        legacy_settings_stop_message,
        load_settings,
    )

    settings = load_settings()
    db_path = args.db or get_database_path(settings)
    # プレビューのフォルダを相対パスで出すためだけに使う。無くても動く。
    source_root = resolve_source_root(get_source_root(settings))
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
        window = MainWindow(db_path, source_root=source_root)
    except db.SchemaVersionError as error:
        # `ensure_migrated` を通っても開けないとき（版が新しすぎる、など）。
        # **黙って落とさない。** GUI は端末を見ずに起動されることがある。
        QMessageBox.critical(None, "データベースを開けません", str(error))
        raise SystemExit(str(error)) from error
    window.show()
    sys.exit(app.exec())
