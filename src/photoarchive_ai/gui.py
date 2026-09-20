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
from typing import List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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

from . import db, face, migration
from .config import find_settings_path

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


def parse_date(value: Optional[str]) -> Optional[date]:
    """`YYYY-MM-DD` で始まる文字列を日付にする。読めなければ ``None``。

    撮影日時（`2017-12-16T18:46:32`）も誕生日（`2011-05-03`）も先頭10文字が
    日付なので、同じ関数で扱える。

    **壊れた EXIF を弾くのがここの役目。** カメラが日付にならない値を書くことが
    あり、実データでは2種類あった（`0000-00-00T00:00:00` が Media 55件、
    `TTTT-TT-TTTTT:TT:TT` が 67件）。

    **「読める撮影日時か」の判断は、この関数1つに持たせる。** 表示
    （`_format_timestamp`）・年齢の計算（`calculate_age`）・年齢の初期値
    （`suggested_age`）・選択の知らせ（`summarize_selection`）が同じ答えを返さないと、
    「撮影日時: 不明」と出ている写真に年齢だけが出る、といった食い違いが起きる。
    並び順だけは SQL 側にあるので、`db.SHOOTING_DATE_SORT_KEY` に同じ判断を
    写してある（**片方だけ直さないこと**）。
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
    `/mnt/nfs/nanoPi-NEO2/suzuki/Photo/2011/...`）。`source_root` の外にある
    ものは絶対パスのまま出す。

    **プレビューの情報欄とフォルダ選択で、同じ規則を使う。** 別々に書くと、
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


class RegisteredFacesDialog(QDialog):
    """人物に割り当て済みの顔を確認し、確定・解除する。"""

    def __init__(self, parent, connection, person: dict):
        super().__init__(parent)
        self.connection = connection
        self.person = person
        self.setWindowTitle(f"割り当て済みの顔 - {person['name']}")
        self.resize(820, 600)

        self.min_age = QSpinBox()
        self.min_age.setRange(0, 150)
        self.min_age.setSpecialValueText("指定なし")
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 150)
        self.max_age.setSpecialValueText("指定なし")
        self.max_age.setValue(150)
        # 年齢の絞り込みも「指定なし」の文字が入っている。年齢の入力と
        # 同じ理由で、触ったときに打鍵で置き換えられるようにする。
        for spin in (self.min_age, self.max_age):
            spin.focusInEvent = _make_select_all_on_focus(spin)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        self.confirm_button = QPushButton("選択した顔を確定")
        self.unassign_button = QPushButton("割り当てを解除")
        self.age_button = QPushButton("年齢を設定")
        self.confirm_button.setToolTip("自動割当の顔を手動割当に昇格し、match の手本にする")
        self.confirm_button.clicked.connect(self._confirm_selected)
        self.unassign_button.clicked.connect(self._unassign_selected)
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
        age_filter.addWidget(QLabel("年齢"))
        age_filter.addWidget(self.min_age)
        age_filter.addWidget(QLabel("歳から"))
        age_filter.addWidget(self.max_age)
        age_filter.addWidget(QLabel("歳"))
        age_filter.addStretch(1)
        self.min_age.valueChanged.connect(self._reset_page)
        self.max_age.valueChanged.connect(self._reset_page)

        actions = QHBoxLayout()
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.unassign_button)
        actions.addWidget(self.age_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(age_filter)
        layout.addLayout(pager)
        layout.addWidget(self.face_list)
        layout.addLayout(actions)
        self.reload()

    def _age_range(self):
        return (self.min_age.value() or None, self.max_age.value() or None)

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
        minimum, maximum = self._age_range()
        self.total = db.count_faces(
            self.connection,
            person_id=self.person["id"],
            min_age=minimum,
            max_age=maximum,
        )
        pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        records = db.list_faces(
            self.connection,
            person_id=self.person["id"],
            with_thumbnail=True,
            limit=PAGE_SIZE,
            offset=self.page * PAGE_SIZE,
            min_age=minimum,
            max_age=maximum,
            # **年齢の若い順。** 成長の順に並ぶので、年齢の入れ間違いや、
            # 別人が混ざっているのに気づきやすい。未設定は最後。
            order=db.ORDER_AGE,
        )
        _fill_face_list(self.face_list, records)
        self.page_label.setText(f"{self.page + 1} / {pages} ページ（全 {self.total} 件）")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)

    def _selected_ids(self) -> List[int]:
        return [item.data(Qt.UserRole)["id"] for item in self.face_list.selectedItems()]

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


def format_folder_row(counts: dict, source_root: Optional[str] = None) -> str:
    """フォルダ選択の1行。**件数を先に、フォルダ名を後ろに置く。**

    フォルダ名は長さがまちまちなので、後ろに置かないと件数の桁が揃わず、
    どれが大きいのか見比べられない。
    """
    return (
        f"未割当 {counts['unassigned']:,} / 手本 {counts['manual']:,}"
        f" / 除外 {counts['rejected']:,}   "
        f"{format_folder(counts['folder'], source_root)}"
    )


class FolderPickerDialog(QDialog):
    """顔の一覧を絞り込むフォルダを選ぶ。

    **未割当の多い順に並べる。** まとめて除外して効き目が大きいフォルダが
    上に来る（実データの1位は結婚式の 1,357 件）。

    **ページャは置かない。** 「ページャの無い一覧を作らない」はサムネイルの
    BLOB を全件読まないための約束で、ここは文字だけ（実データで 1,042 行・
    0.099 秒）。目的のフォルダを探すにはページ送りより絞り込み欄が要る。
    """

    def __init__(self, parent, connection, source_root: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("フォルダを選ぶ")
        self.source_root = source_root
        with busy_cursor():
            self.rows = db.folder_face_counts(connection)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("フォルダ名で絞り込む（例: 結婚式）")
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.folder_list = QListWidget()
        self.folder_list.itemDoubleClicked.connect(lambda _item: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self.summary_label = QLabel("")
        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.folder_list)
        layout.addWidget(self.summary_label)
        layout.addWidget(buttons)
        self.resize(760, 520)
        self._apply_filter("")

    def _apply_filter(self, text: str) -> None:
        """絞り込み欄の文字を含むフォルダだけ出す。

        **画面に出している名前で照合する**（`source_root` からの相対）。
        絶対パスで照合すると、画面に見えていない部分に当たってしまう。
        """
        needle = text.strip()
        self.folder_list.clear()
        shown = 0
        for counts in self.rows:
            label = format_folder_row(counts, self.source_root)
            if needle and needle not in label:
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, counts)
            self.folder_list.addItem(item)
            shown += 1
        self.summary_label.setText(f"{shown} / {len(self.rows)} フォルダ")
        if shown:
            self.folder_list.setCurrentRow(0)

    def selected_folder(self) -> Optional[str]:
        """選ばれたフォルダ。**絞り込みの条件に使う絶対パスのほうを返す。**"""
        item = self.folder_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)["folder"]


def _fill_face_list(widget: QListWidget, records: List[dict]) -> None:
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
        _repopulate_face_list(widget, records)
    finally:
        widget.blockSignals(blocked)


def _repopulate_face_list(widget: QListWidget, records: List[dict]) -> None:
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
        if record.get("age") is not None:
            label = f"{label} {record['age']}歳"
        item.setText(label)
        item.setData(Qt.UserRole, record)
        widget.addItem(item)


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

        # --- フォルダで絞り、まとめて処理する ---------------------------
        # **未割当 58,212 件のうち、上位100フォルダで 51.5%（30,094件）を
        # 占める**（2026-09-20 実測）。結婚式や学校行事はほとんどが他人なので、
        # 1件ずつ判断させると総時間がそのぶん延びる。
        self.folder: Optional[str] = None
        self.folder_label = QLabel("")
        self.folder_label.setWordWrap(True)
        self.choose_folder_button = QPushButton("フォルダを選ぶ")
        self.clear_folder_button = QPushButton("解除")
        self.bulk_folder_button = QPushButton("")
        self.choose_folder_button.clicked.connect(self._choose_folder)
        self.clear_folder_button.clicked.connect(self._clear_folder)
        self.bulk_folder_button.clicked.connect(self._bulk_folder_action)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.choose_folder_button)
        folder_row.addWidget(self.clear_folder_button)
        folder_row.addWidget(self.bulk_folder_button)

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

        face_panel = QWidget()
        face_layout = QVBoxLayout(face_panel)
        face_layout.addLayout(pager)
        face_layout.addLayout(folder_row)
        face_layout.addWidget(face_area)
        face_layout.addLayout(face_actions)

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
        self._sync_folder_controls()
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
        # **フォルダ未指定なら `folder` を渡さない。** 渡すと `Media` を辿る
        # 条件が増え、撮影日時の索引を順に歩く経路（未割当 58,212 件を
        # 0.002 秒で1ページ分読む）から外れる。
        if self.folder is not None:
            filters["folder"] = self.folder
        return filters

    # ------------------------------------------------------------------
    # フォルダで絞る / まとめて処理する
    # ------------------------------------------------------------------

    def _choose_folder(self) -> None:
        dialog = FolderPickerDialog(self, self.connection, self.source_root)
        if dialog.exec() != QDialog.Accepted:
            return
        folder = dialog.selected_folder()
        if folder is None:
            return
        self.folder = folder
        self._reset_page()

    def _clear_folder(self) -> None:
        self.folder = None
        self._reset_page()

    def _sync_folder_controls(self) -> None:
        """フォルダ行の表示と、まとめて処理するボタンの意味をそろえる。

        **まとめて処理できるのはフォルダを選んでいるときだけ。** 選んでいないと
        対象が未割当 58,212 件全部になり、**一度の押し間違いで作業がすべて
        飛ぶ。** 押せない理由はツールチップに書く（隠さない）。
        """
        if self.folder is None:
            self.folder_label.setText("フォルダ: すべて")
        else:
            self.folder_label.setText(f"フォルダ: {format_folder(self.folder, self.source_root)}")
        self.clear_folder_button.setEnabled(self.folder is not None)

        label, tooltip = self._bulk_action_labels()
        self.bulk_folder_button.setText(label)
        self.bulk_folder_button.setEnabled(self.folder is not None)
        self.bulk_folder_button.setToolTip(
            tooltip
            if self.folder is not None
            else "先に「フォルダを選ぶ」でフォルダを指定してください。"
        )

    def _bulk_action_labels(self) -> tuple:
        """いま表示している一覧に対して、まとめて何ができるか。

        **表示を切り替えたらボタンの意味も変える。** 未割当を見ているときは
        「まとめて除外」、除外済みや自動割当を見ているときは「まとめて取り消す」。
        """
        selected = self.filter_box.currentText()
        if selected == FILTER_REJECTED:
            return (
                "このフォルダの除外をすべて取り消す",
                "このフォルダで除外した顔を、ページをまたいで未割当へ戻します。",
            )
        if selected == FILTER_AUTO:
            return (
                "このフォルダの自動割当をすべて取り消す",
                "このフォルダの自動割当を、ページをまたいで未割当へ戻します。",
            )
        return (
            "このフォルダの未割当をすべて除外",
            "このフォルダの未割当の顔を、ページをまたいでまとめて除外します"
            "（手動で割り当てた顔は触りません）。",
        )

    def _bulk_folder_action(self) -> None:
        """フォルダ単位のまとめ処理。**表示中のページではなくフォルダ全体に効く。**"""
        if self.folder is None:
            return
        filters = self._filter_arguments()
        with busy_cursor():
            face_ids = db.face_ids(self.connection, **filters)
        name = format_folder(self.folder, self.source_root)
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

    def _reset_page(self):
        self.page = 0
        self._sync_unassign_button()
        self._sync_folder_controls()
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
