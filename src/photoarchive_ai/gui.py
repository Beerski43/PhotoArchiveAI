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
from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
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
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import clustering, db, embedding, face, migration
from .config import find_settings_path

# **日付の判断は `dates` に1つだけ持つ。** ここで再公開しているのは、
# `gui.parse_date` を参照している呼び出しとテストを壊さないため。
# **この module に写しを作らないこと**（`db.SHOOTING_DATE_SORT_KEY` が
# SQL 側の写しで、そちらと食い違うと表示と並び順がずれる）。
from .dates import calculate_age, parse_date  # noqa: F401

PAGE_SIZE = 200
THUMBNAIL_SIZE = 120

FILTER_UNASSIGNED = "未割当"
FILTER_AUTO = "自動割当"
FILTER_REJECTED = "除外済み"


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


def format_age(age: Optional[int]) -> Optional[str]:
    """年齢を画面に出す形にする。計算できていなければ ``None``。"""
    if age is None:
        return None
    return "誕生前" if age < 0 else f"{age}歳"


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
    birth_date: Optional[str], shooting_dates: List[Optional[str]]
) -> Optional[int]:
    """年齢ダイアログの初期値。出せないなら ``None``（＝「未設定」で開く）。

    ``shooting_dates`` は**顔1件につき1件**（`db.shooting_dates_for_faces`）。
    撮影日時の無い顔・読めない顔は ``None`` で入ってくる。

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
    ages = {calculate_age(birth_date, shooting_date) for shooting_date in shooting_dates}
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
    # config/app_settings.json → リポジトリ直下
    return str(settings_path.parent.parent / source_root)


def format_folder(folder: str, source_root: Optional[str] = None) -> str:
    """フォルダを、画面に出す形にする。**`source_root` からの相対。**

    絶対パスは長すぎて読めない（実データは
    `${NFS_ROOT}/${SURNAME}/Photo/2011/...`）。`source_root` の外にある
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


def persons_alive_on(persons: List[dict], day: Optional[str]) -> List[dict]:
    """その日にまだ生まれていない人物を外す。

    **束をまとめて割り当てるときに、選べてはいけない人物を消すため。**
    1件ずつの割り当てでは年齢欄が負の数になって気づけるが、まとめて押すときは
    画面に出るのが人物名だけなので、**選べると気づけない。**

    誕生日が未設定の人物は**残す**（分からないことを理由に消さない）。
    ``day`` が読めないときも全員残す。
    """
    taken = parse_date(day)
    if taken is None:
        return list(persons)
    alive = []
    for person in persons:
        born = parse_date(person.get("birth_date"))
        if born is not None and born > taken:
            continue
        alive.append(person)
    return alive


def format_media_info(
    media: dict, source_root: Optional[str] = None, person: Optional[dict] = None
) -> str:
    """プレビューの下に出す、撮影日時とフォルダと、選択中の人物の年齢。

    **年齢を入れるには、その写真がいつ撮られたか分からないといけない。**
    EXIF の撮影日時は実データの 15.8% で欠けているので、日付を持つことが多い
    フォルダ名も併せて出す。

    ``created_time`` は**撮影日時ではない**（コピーで変わる）。取り違えると
    年齢を間違えるので、EXIF が無いときだけ、別の名前で出す。

    ``person`` を渡すと、その人物の誕生日と撮影日時から**撮影時の年齢**を
    最後の行に出す。人物が未選択・誕生日が未設定・撮影日時が無いのいずれかなら
    **行そのものを出さない**（誤解を招く「不明」を並べるより、無いほうがよい）。
    """
    lines = []
    shooting_date = _format_timestamp(media.get("shooting_date"))
    if shooting_date:
        lines.append(f"撮影日時: {shooting_date}")
    else:
        lines.append("撮影日時: 不明（EXIFなし）")
        file_time = _format_timestamp(media.get("created_time"))
        if file_time:
            lines.append(f"ファイル日時: {file_time}")

    path = Path(media.get("path", ""))
    # **フォルダの出し方を写さない。** 行事の選択と同じ規則を通す
    # （別々に書くと、同じフォルダが画面によって違う名前で出る）。
    lines.append(f"フォルダ: {format_folder(str(path.parent), source_root)}")
    lines.append(f"ファイル: {path.name}")

    if person:
        age = format_age(calculate_age(person.get("birth_date"), media.get("shooting_date")))
        if age:
            lines.append(f"{person.get('name') or '?'}: {age}")
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


def summarize_selection(face_count: int, shooting_dates: List[Optional[str]]) -> str:
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
    readable = sorted(value for value in shooting_dates if parse_date(value))
    unknown = face_count - len(readable)

    if not readable:
        return f"{face_count} 件すべてに同じ年齢を入れます（撮影日時は不明）。"

    first, last = readable[0][:10], readable[-1][:10]
    if first == last:
        lines = [f"{face_count} 件すべてに同じ年齢を入れます（撮影日時 {first}）。"]
    else:
        # QLabel は Markdown を解釈しないので、装飾記号を書かない（そのまま出る）。
        lines = [
            f"{face_count} 件すべてに同じ年齢を入れます。",
            f"撮影日時が {first} 〜 {last} にまたがっています。",
        ]
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


class RegisteredFacesDialog(QDialog):
    """人物に割り当て済みの顔を確認し、確定・解除する。"""

    def __init__(self, parent, connection, person: dict):
        super().__init__(parent)
        self.connection = connection
        self.person = person
        self.setWindowTitle(f"割り当て済みの顔 - {person['name']}")
        self.resize(820, 600)

        # **確定済みと自動を見分けて絞れるようにする。** 自動割り当てを
        # 見直すときは自動だけを、手本を見直すときは確定済みだけを見たい。
        self.source_box = QComboBox()
        for label, _ in SOURCE_FILTERS:
            self.source_box.addItem(label)
        self.source_box.addItem(NOT_THIS_PERSON_FILTER)
        self.source_box.currentIndexChanged.connect(self._reset_page)

        # **最小値を -1 にして「指定なし」に割り当てる。**
        # `FaceAgeDialog` と同じ理由で、**0 を特別扱いにすると 0歳で絞れなくなる**
        # （QSpinBox の `specialValueText` は最小値のときに出る）。
        # 絞り込み側だけ 0 を「指定なし」にしていたため、0歳の顔だけを見ることが
        # できなかった。
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

        # **年齢を出せない顔をどうするか。** 既定は「含めない」。
        # 含めると、範囲を指定しても年齢不明の顔が常に混ざり、**絞り込みが
        # ほとんど効かない**（実データで割り当て済み 22,511 件のうち 1,931 件。
        # かつては `Face.age` の無い顔を全部残しており、入っているのは 126 件
        # だけだったので、絞り込みが何もしないのと同じだった）。
        self.include_unknown_age = QCheckBox("年齢不明も含める")
        self.include_unknown_age.setToolTip(
            "誕生日が未登録か、写真に撮影日時が無くて年齢を出せない顔も残す"
        )
        self.include_unknown_age.stateChanged.connect(self._reset_page)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        self.confirm_button = QPushButton("選択した顔を確定")
        self.unassign_button = QPushButton("割り当てを解除")
        # **除外は2種類ある。** 混ぜると取り返しがつかない。
        self.not_this_person_button = QPushButton("この人物ではない")
        self.not_this_person_button.setToolTip(
            "この人物ではない、と記録する。\n"
            "**ほかの人物には自動で付きうる。**\n"
            "解除と違い、match を流し直しても戻ってこない。"
        )
        self.reject_button = QPushButton("誰でもない顔")
        self.reject_button.setToolTip(
            "家族の誰でもない顔として除外する。\n"
            "**どの人物にも自動で付かなくなる。**"
        )
        self.age_button = QPushButton("年齢を設定")
        self.confirm_button.setToolTip(CONFIRM_TOOLTIP_READY)
        self.confirm_button.clicked.connect(self._confirm_selected)
        # **確定は自動割り当てにしか効かない。** 選び直すたびに押せるかを見直す。
        self.face_list.itemSelectionChanged.connect(self._update_confirm_button)
        self.unassign_button.clicked.connect(self._unassign_selected)
        self.not_this_person_button.clicked.connect(self._reject_for_person_selected)
        self.undo_rejection_button = QPushButton("「この人物ではない」を取り消す")
        self.undo_rejection_button.clicked.connect(self._undo_rejection_selected)
        self.reject_button.clicked.connect(self._reject_selected)
        self.age_button.clicked.connect(self._set_age_selected)

        # 割り当て済みの顔も数百件になりうる。ページ単位で読まないと、
        # 1ページ目より後ろの顔に手が届かなくなる。
        self.page = 0
        self.total = 0
        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        pager = QHBoxLayout()
        pager.addStretch(1)
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)

        age_filter = QHBoxLayout()
        age_filter.addWidget(QLabel("種別"))
        age_filter.addWidget(self.source_box)
        age_filter.addSpacing(16)
        age_filter.addWidget(QLabel("年齢"))
        age_filter.addWidget(self.min_age)
        age_filter.addWidget(QLabel("歳から"))
        age_filter.addWidget(self.max_age)
        age_filter.addWidget(QLabel("歳"))
        age_filter.addWidget(self.include_unknown_age)
        age_filter.addStretch(1)
        self.min_age.valueChanged.connect(self._reset_page)
        self.max_age.valueChanged.connect(self._reset_page)

        actions = QHBoxLayout()
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.unassign_button)
        actions.addWidget(self.not_this_person_button)
        actions.addWidget(self.undo_rejection_button)
        actions.addWidget(self.reject_button)
        actions.addWidget(self.age_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(age_filter)
        layout.addLayout(pager)
        layout.addWidget(self.face_list)
        layout.addLayout(actions)
        self.reload()

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

        **これだけは `assign_source` で絞れない**（否定は `FaceRejection` 表）ので、
        読み出しの経路を分ける。
        """
        return self.source_box.currentText() == NOT_THIS_PERSON_FILTER

    def _reset_page(self) -> None:
        self.page = 0
        self.reload()

    def _previous_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self.reload()

    def _next_page(self) -> None:
        if (self.page + 1) * PAGE_SIZE < self.total:
            self.page += 1
            self.reload()

    def reload(self) -> None:
        if self._showing_rejections():
            self._reload_rejections()
            return
        minimum, maximum = self._age_range()
        source = self._source_filter()
        # **誕生日を渡すと、画面に出ている計算年齢でも絞れる。**
        # `Face.age` は `match` が書かないので、これが無いと自動割り当ての顔は
        # 1件も年齢で絞れない。
        birth_date = self.person.get("birth_date")
        include_unknown = self.include_unknown_age.isChecked()
        self.total = db.count_faces(
            self.connection,
            assign_source=source,
            person_id=self.person["id"],
            min_age=minimum,
            max_age=maximum,
            birth_date=birth_date,
            include_unknown_age=include_unknown,
        )
        pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        records = db.list_faces(
            self.connection,
            assign_source=source,
            person_id=self.person["id"],
            with_thumbnail=True,
            limit=PAGE_SIZE,
            offset=self.page * PAGE_SIZE,
            min_age=minimum,
            max_age=maximum,
            birth_date=birth_date,
            include_unknown_age=include_unknown,
            # **年齢の若い順。** 成長の順に並ぶので、年齢の入れ間違いや、
            # 別人が混ざっているのに気づきやすい。未設定は最後。
            order=db.ORDER_AGE,
        )
        # **自動割り当ての顔は `Face.age` が未設定**（`match` は年齢を書かない）。
        # 人物の誕生日と撮影日時から計算して出す。これが「その割り当てが
        # 正しいか」を人が見るときのいちばんの手がかりになる。
        shooting_dates = db.shooting_dates_by_face(
            self.connection, [record["id"] for record in records]
        )
        birth_date = self.person.get("birth_date")
        age_labels = {
            record["id"]: face_age_label(
                record, birth_date, shooting_dates.get(record["id"])
            )
            for record in records
        }
        _fill_face_list(self.face_list, records, age_labels)
        self.page_label.setText(f"{self.page + 1} / {pages} ページ（全 {self.total} 件）")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)
        # 作り直した直後は何も選ばれていない。**信号を止めて作り直している**ので
        # `itemSelectionChanged` は出ない（`_fill_face_list`）。ここで呼ぶ。
        self._update_confirm_button()

    def _reload_rejections(self) -> None:
        """「この人物ではない」と記録した顔の一覧。**取り消せるようにするため。**

        この一覧の顔は**その人物に割り当たっていない**ので、人物での絞り込みが
        使えない。顔 id を直に引く。
        """
        face_ids = db.rejected_face_ids_for_person(self.connection, self.person["id"])
        self.total = len(face_ids)
        pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        start = self.page * PAGE_SIZE
        records = db.faces_by_ids(
            self.connection, face_ids[start : start + PAGE_SIZE], with_thumbnail=True
        )
        shooting_dates = db.shooting_dates_by_face(
            self.connection, [record["id"] for record in records]
        )
        birth_date = self.person.get("birth_date")
        age_labels = {
            record["id"]: face_age_label(
                record, birth_date, shooting_dates.get(record["id"])
            )
            for record in records
        }
        _fill_face_list(self.face_list, records, age_labels)
        self.page_label.setText(
            f"{self.page + 1} / {pages} ページ（「この人物ではない」 {self.total} 件）"
        )
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)
        self._update_confirm_button()

    def _undo_rejection_selected(self) -> None:
        """「この人物ではない」を取り消す。**押し間違いから戻れるように。**"""
        face_ids = self._selected_ids()
        if not face_ids:
            return
        self._run_with_progress(
            "「この人物ではない」を取り消しています",
            face_ids,
            lambda progress: db.clear_person_rejections(
                self.connection, face_ids, self.person["id"]
            ),
        )

    def _selected_ids(self) -> List[int]:
        return [item.data(Qt.UserRole)["id"] for item in self.face_list.selectedItems()]

    def _update_confirm_button(self) -> None:
        """**確定は、自動割り当てを選んでいるときだけ押せる。**

        確定は `assign_source` を `'auto'` → `'manual'` に上げる操作なので、
        **すでに手本の顔に押しても `assigned_at` が今の時刻に書き換わるだけ**で、
        意味のある変化が起きない。押せてしまうと「何かが起きた」と誤解する。

        何も選んでいないときも押せない（自動の顔が1件も入っていないため）。
        """
        has_auto = any(
            item.data(Qt.UserRole).get("assign_source") == db.ASSIGN_AUTO
            for item in self.face_list.selectedItems()
        )
        self.confirm_button.setEnabled(has_auto)
        # **押せない理由を出す。** 灰色のボタンだけでは、壊れているのか
        # 選び方が足りないのかが分からない。
        self.confirm_button.setToolTip(
            CONFIRM_TOOLTIP_READY if has_auto else CONFIRM_TOOLTIP_BLOCKED
        )

    def _run_with_progress(self, label: str, face_ids: List[int], work) -> None:
        """件数の分かる作業を、砂時計と進み具合つきで流す。

        **押したことが分かるようにするため。** 短い作業では窓は出ない
        （`WorkProgress` が自分で判断する）。
        """
        with busy_cursor():
            progress = WorkProgress(self, label, len(face_ids))
            try:
                work(progress)
                progress.step("一覧を作り直しています")
                self.reload()
            finally:
                progress.finish()

    def _confirm_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        self._run_with_progress(
            "手本に確定しています",
            face_ids,
            lambda progress: db.assign_faces(
                self.connection,
                face_ids,
                self.person["id"],
                db.ASSIGN_MANUAL,
                progress=progress,
            ),
        )

    def _unassign_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        self._run_with_progress(
            "割り当てを解除しています",
            face_ids,
            lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
        )

    def _reject_for_person_selected(self) -> None:
        """**この人物ではない**、と記録する。ほかの人物には付きうる。

        **「割り当てを解除」では足りない。** `match` は手本と閾値だけで決まるので、
        未割当へ戻しただけだと**流すたびに同じ誤りが戻る**（実データで${PERSON_4}の
        1,785 件で起きた）。
        """
        face_ids = self._selected_ids()
        if not face_ids:
            return
        self._run_with_progress(
            "この人物ではない、と記録しています",
            face_ids,
            lambda progress: db.reject_faces_for_person(
                self.connection, face_ids, self.person["id"], progress=progress
            ),
        )

    def _reject_selected(self) -> None:
        """**家族の誰でもない顔**として除外する。どの人物にも自動で付かなくなる。

        **押し間違えると、その顔は `match` の候補から丸ごと外れる。**
        別の人物のものかもしれない顔には「この人物ではない」のほうを使う。
        """
        face_ids = self._selected_ids()
        if not face_ids:
            return
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
        self._run_with_progress(
            "除外しています",
            face_ids,
            lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
        )

    def _set_age_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        shooting_dates = db.shooting_dates_for_faces(self.connection, face_ids)
        dialog = FaceAgeDialog(
            self,
            summary=summarize_selection(len(face_ids), shooting_dates),
            initial_age=suggested_age(self.person.get("birth_date"), shooting_dates),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        age = dialog.age()

        def work(progress):
            # **1件ずつコミットしていた。** 200件なら 200 回の fsync になる。
            # まとめて1回にし、そのぶん進み具合を知らせる。
            db.set_faces_age(self.connection, face_ids, age, progress=progress)

        self._run_with_progress("年齢を設定しています", face_ids, work)


def face_age_label(
    record: dict, birth_date: Optional[str], shooting_date: Optional[str]
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
    """
    if record.get("age") is not None:
        return format_age(record["age"])
    computed = format_age(calculate_age(birth_date, shooting_date))
    return None if computed is None else f"({computed})"


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

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)

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
        shooting_dates = db.shooting_dates_for_faces(self.connection, face_ids)
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
    def __init__(self, database_path: str, source_root: Optional[str] = None):
        super().__init__()
        self.db_path = database_path
        #: プレビューのフォルダ表示を相対パスにするためだけに使う。
        #: 無ければ絶対パスで出すので、渡さなくても動く。
        self.source_root = source_root
        self.connection = db.ensure_database(database_path)
        self.setWindowTitle("PhotoArchiveAI 人物登録と顔の割り当て")
        self.page = 0

        # --- 左: 人物 ---------------------------------------------------
        self.person_list = QListWidget()
        # **ドラッグで並べ替えられるようにする。** よく割り当てる人物を上に
        # 置けないと、人数が増えるほど毎回探すことになる。
        self.person_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.person_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.person_list.currentItemChanged.connect(self._on_person_selected)
        # 並べ替えは「落とした時点」で確定する。**保存ボタンを置かない**
        # （押し忘れたぶんが黙って消える）。
        self.person_list.model().rowsMoved.connect(self._save_person_order)
        self.add_person_button = QPushButton("人物追加")
        self.edit_person_button = QPushButton("編集")
        self.delete_person_button = QPushButton("削除")
        self.view_faces_button = QPushButton("割り当て済みを確認")
        self.details_label = QLabel("人物を選択してください。")
        self.details_label.setWordWrap(True)

        self.add_person_button.clicked.connect(self._add_person)
        self.edit_person_button.clicked.connect(self._edit_person)
        self.delete_person_button.clicked.connect(self._delete_person)
        self.view_faces_button.clicked.connect(self._view_assigned_faces)

        person_buttons = QHBoxLayout()
        person_buttons.addWidget(self.add_person_button)
        person_buttons.addWidget(self.edit_person_button)
        person_buttons.addWidget(self.delete_person_button)

        person_panel = QWidget()
        person_layout = QVBoxLayout(person_panel)
        person_layout.addLayout(person_buttons)
        person_layout.addWidget(self.person_list)
        person_layout.addWidget(self.view_faces_button)
        person_layout.addWidget(self.details_label)

        # --- 右: 顔 -----------------------------------------------------
        self.filter_box = QComboBox()
        self.filter_box.addItems([FILTER_UNASSIGNED, FILTER_AUTO, FILTER_REJECTED])
        self.filter_box.currentIndexChanged.connect(self._reset_page)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        self.assign_button = QPushButton("選択した顔を割り当て")
        self.reject_button = QPushButton("この顔を除外")
        # **除外を取り消せるようにする。** 除外した顔は「割り当て済みを確認」に
        # 出てこない（あちらは人物で絞るが、除外した顔は person_id を持たない）
        # ので、いったん除外すると**誰かに割り当てる以外に戻す手段が無かった。**
        # 「決めきれないので保留に戻す」ができない。
        self.unassign_button = QPushButton("未割当に戻す")
        self.assign_button.clicked.connect(self._assign_selected)
        self.reject_button.clicked.connect(self._reject_selected)
        self.unassign_button.clicked.connect(self._unassign_selected)

        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        pager = QHBoxLayout()
        pager.addWidget(self.filter_box)
        pager.addStretch(1)
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)

        face_actions = QHBoxLayout()
        face_actions.addWidget(self.assign_button)
        face_actions.addWidget(self.reject_button)
        face_actions.addWidget(self.unassign_button)
        face_actions.addStretch(1)

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
        self.face_list.itemSelectionChanged.connect(self._show_preview)

        face_area = QSplitter(Qt.Horizontal)
        face_area.addWidget(self.face_list)
        face_area.addWidget(preview_panel)
        face_area.setStretchFactor(0, 3)
        face_area.setStretchFactor(1, 2)

        # --- 行事で絞り、まとめて処理する ------------------------------
        # **日付の読める未割当 51,860 件は、2,122 の行事（フォルダ×日）に
        # 散っている**（2026-10-04 実測）。行事ごとに束ねると決定が 19,678 回まで
        # 落ちる。**撮影日時が読めない 5,910 件は 521 フォルダ**に散っている。
        # 結婚式や学校行事はほとんどが他人なので、1件ずつ判断させると
        # 総時間がそのぶん延びる。
        self.event: Optional[tuple] = None
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

        face_panel = QWidget()
        face_layout = QVBoxLayout(face_panel)
        face_layout.addLayout(pager)
        face_layout.addWidget(face_area)
        face_layout.addLayout(face_actions)
        face_layout.addLayout(event_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(person_panel)
        splitter.addWidget(face_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([280, 780])

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(splitter)
        self.resize(1100, 720)

        self._reload_person_list()
        self._sync_unassign_button()
        self._sync_event_controls()
        self.reload_faces()

    # ------------------------------------------------------------------
    # 人物
    # ------------------------------------------------------------------

    def _reload_person_list(self, select_person_id: Optional[int] = None):
        """人物一覧を作り直す。``select_person_id`` を渡すとその人物を選び直す。

        **選び直さないと、追加・編集した直後に選択が外れる。** `clear()` が
        選択を落とすので、詳細欄が「人物を選択してください。」に戻り、
        **プレビューの年齢の行も出ない。** 誕生日を登録した本人には、
        機能が効いていないように見える。
        """
        self.person_list.clear()
        for person in db.list_persons(self.connection):
            item = QListWidgetItem(f"{person['name']} ({person.get('relation') or '-'})")
            item.setData(Qt.UserRole, person)
            self.person_list.addItem(item)
            if select_person_id is not None and person["id"] == select_person_id:
                self.person_list.setCurrentRow(self.person_list.count() - 1)
        if self.person_list.currentItem() is None:
            self.details_label.setText("人物を選択してください。")
            self._refresh_preview_info()

    def _save_person_order(self, *args) -> None:
        """画面に並んでいる順を、そのまま `display_order` に書く。

        **一覧に出ている全員を渡す。** 一部だけ書くと、書かなかった人物の
        順序が古いままになって並びが混ざる。
        """
        person_ids = [
            self.person_list.item(row).data(Qt.UserRole)["id"]
            for row in range(self.person_list.count())
        ]
        if person_ids:
            db.set_person_order(self.connection, person_ids)

    def _current_person(self) -> Optional[dict]:
        item = self.person_list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _on_person_selected(self, current: QListWidgetItem, previous: QListWidgetItem = None):
        if current is None:
            self.details_label.setText("人物を選択してください。")
            # **前の人物の年齢を残さない。** 選択が外れているのに年齢の行が
            # 出ていると、誰の年齢なのか分からない。
            self._refresh_preview_info()
            return
        person = current.data(Qt.UserRole)
        manual = db.count_faces(
            self.connection, assign_source=db.ASSIGN_MANUAL, person_id=person["id"]
        )
        auto = db.count_faces(self.connection, assign_source=db.ASSIGN_AUTO, person_id=person["id"])
        self.details_label.setText(
            f"名前: {person['name']}\n"
            f"続柄: {person.get('relation') or '-'}\n"
            # 誕生日を出しておかないと、年齢が出ない理由が画面から分からない。
            f"誕生日: {person.get('birth_date') or '未設定'}\n"
            f"メモ: {person.get('memo') or '-'}\n"
            f"手動割当: {manual} 件 / 自動割当: {auto} 件"
        )
        # 人物が変わると年齢の行も変わる。元写真は読み直さない。
        self._refresh_preview_info()

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
        self._reload_person_list()
        self.reload_faces()

    def _view_assigned_faces(self):
        person = self._current_person()
        if person is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        if db.count_faces(self.connection, person_id=person["id"]) == 0:
            QMessageBox.information(self, "割当なし", "この人物に割り当てられた顔はありません。")
            return
        dialog = RegisteredFacesDialog(self, self.connection, person)
        dialog.exec()
        self._on_person_selected(self.person_list.currentItem())
        self.reload_faces()

    # ------------------------------------------------------------------
    # 顔一覧
    # ------------------------------------------------------------------

    def _filter_arguments(self) -> dict:
        selected = self.filter_box.currentText()
        if selected == FILTER_AUTO:
            filters = {"assign_source": db.ASSIGN_AUTO}
        elif selected == FILTER_REJECTED:
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
        return filters

    def _sync_unassign_button(self) -> None:
        """いま見ている一覧で「未割当に戻す」が意味を持つかを反映する。

        **隠さずに、押せなくする。** 隠すと「そんな操作は無い」と思われる。
        押せない理由はツールチップに書く。
        """
        showing_unassigned = self.filter_box.currentText() == FILTER_UNASSIGNED
        self.unassign_button.setEnabled(not showing_unassigned)
        self.unassign_button.setToolTip(
            "いま表示しているのは未割当の顔です。戻す先がありません。"
            if showing_unassigned
            else "選択した顔を未割当に戻します（除外や自動割当を取り消せます）。"
        )

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
        self.bulk_event_button.setEnabled(self.event is not None)
        self.bulk_event_button.setToolTip(
            tooltip
            if self.event is not None
            else "先に「行事を選ぶ」で行事を指定してください。"
        )

    def _bulk_action_labels(self) -> tuple:
        """いま表示している一覧に対して、まとめて何ができるか。

        **表示を切り替えたらボタンの意味も変える。** 未割当を見ているときは
        「まとめて除外」、除外済みや自動割当を見ているときは「まとめて取り消す」。
        """
        selected = self.filter_box.currentText()
        if selected == FILTER_REJECTED:
            return (
                "この行事の除外をすべて取り消す",
                "この行事で除外した顔を、ページをまたいで未割当へ戻します。",
            )
        if selected == FILTER_AUTO:
            return (
                "この行事の自動割当をすべて取り消す",
                "この行事の自動割当を、ページをまたいで未割当へ戻します。",
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
        self._on_person_selected(self.person_list.currentItem())
        self._sync_event_controls()

    def _bulk_event_action(self) -> None:
        """行事単位のまとめ処理。**表示中のページではなく行事全体に効く。**"""
        if self.event is None:
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

        rejecting = self.filter_box.currentText() == FILTER_UNASSIGNED
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

    def _reset_page(self):
        self.page = 0
        self._sync_unassign_button()
        self._sync_event_controls()
        self.reload_faces()

    def reload_faces(self):
        filters = self._filter_arguments()
        total = db.count_faces(self.connection, **filters)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        records = db.list_faces(
            self.connection,
            with_thumbnail=True,
            limit=PAGE_SIZE,
            offset=self.page * PAGE_SIZE,
            # **撮影日時の新しい順。** 同じ行事の写真が固まるので、まとめて
            # 選んで一度に割り当てられる。撮影日時の無い顔は最後に来る。
            order=db.ORDER_SHOT_DESC,
            **filters,
        )
        _fill_face_list(self.face_list, records)
        self.page_label.setText(f"{self.page + 1} / {pages} ページ（全 {total} 件）")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < pages - 1)

    def _previous_page(self):
        self.page = max(0, self.page - 1)
        self.reload_faces()

    def _next_page(self):
        self.page += 1
        self.reload_faces()

    def _selected_face_ids(self) -> List[int]:
        return [item.data(Qt.UserRole)["id"] for item in self.face_list.selectedItems()]

    def _run_with_progress(self, label: str, face_ids: List[int], work, done_message: str) -> None:
        """件数の分かる作業を、砂時計と進み具合つきで流し、済んだことを知らせる。

        **3か所に同じ型を書かない。** 割り当て・除外・未割当へ戻す、の違いは
        「何をするか」と「完了に何と出すか」だけ。`RegisteredFacesDialog` にも
        同じ形のものがある。
        """
        with busy_cursor():
            progress = WorkProgress(self, label, len(face_ids))
            try:
                work(progress)
                progress.step("一覧を作り直しています")
                self.reload_faces()
                self._on_person_selected(self.person_list.currentItem())
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

    def _refresh_preview_info(self) -> None:
        """情報欄だけを書き直す。**元写真は読み直さない。**

        人物を選び直すと年齢の行が変わる。ここで画像ごと取り直すと、
        人物を選ぶたびに NFS（実測 24MB/s）から元写真を1枚読むことになる。
        """
        _, media = self._selected_face_and_media()
        if media:
            self.preview_info.setText(
                format_media_info(media, self.source_root, self._current_person())
            )

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
        self.preview_info.setText(
            format_media_info(media, self.source_root, self._current_person())
        )
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

    def assign_faces(
        self, face_ids: List[int], person_id: int, age=db.KEEP_AGE, progress=None
    ) -> int:
        """選択した顔を人物へ手動で割り当てる。テストからも直接呼ぶ。

        ``age`` を省くと年齢は触らない。``None`` は「未設定」の指示。
        """
        return db.assign_faces(
            self.connection, face_ids, person_id, db.ASSIGN_MANUAL, age=age, progress=progress
        )

    def _assign_selected(self):
        person = self._current_person()
        if person is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        face_ids = self._selected_face_ids()
        if not face_ids:
            QMessageBox.information(self, "選択なし", "割り当てる顔を選択してください。")
            return
        # **まとめて選ぶのは、この割り当てのときがいちばん多い。** #41 で入れた
        # 「N件すべてに同じ年齢を入れます」の知らせが、ここには繋がっていなかった。
        shooting_dates = db.shooting_dates_for_faces(self.connection, face_ids)
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

    def _unassign_selected(self):
        """選んだ顔を未割当へ戻す。**除外の取り消しがこれ。**"""
        face_ids = self._selected_face_ids()
        if not face_ids:
            return
        self._run_with_progress(
            "未割当に戻しています",
            face_ids,
            lambda progress: db.unassign_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を未割当に戻しました",
        )

    def _reject_selected(self):
        face_ids = self._selected_face_ids()
        if not face_ids:
            return
        # 除外でも顔は一覧から消える。**割り当てと同じ症状**なので同じ扱いにする。
        self._run_with_progress(
            "除外しています",
            face_ids,
            lambda progress: db.reject_faces(self.connection, face_ids, progress=progress),
            f"完了 — {len(face_ids)} 件を除外しました",
        )


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(prog="photoarchive-gui")
    parser.add_argument(
        "--db",
        help="SQLite database path. 省略すると config/app_settings.json の database_path を使う。",
    )
    args = parser.parse_args()

    # CLI と同じ解決順にする。GUI だけ設定ファイルを読まないと、
    # 「アプリケーション内にDBパスをハードコードしない」という方針から外れる。
    from .config import get_database_path, get_source_root, load_settings

    settings = load_settings()
    db_path = args.db or get_database_path(settings)
    # プレビューのフォルダを相対パスで出すためだけに使う。無くても動く。
    source_root = resolve_source_root(get_source_root(settings))
    if not db_path:
        raise SystemExit(
            "データベースのパスが必要です。--db で指定するか、"
            "config/app_settings.json の database_path を設定してください。"
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
