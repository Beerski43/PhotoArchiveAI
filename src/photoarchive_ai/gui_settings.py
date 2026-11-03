"""設定の窓（#131・Phase 9 の 9.3）。``config/app_settings.yml`` を画面で見て直す。

タブの左上の ``設定…`` から開く（タブにはしない・利用者の決定 2026-11-03）。
直すのは DB の場所・写真の置き場所（root）・出力先・ルールのファイル。キーと既定は
``docs/operation/CONFIG.md`` が正。

- **保存は ``config.write_settings``**（書く前に ``.bak`` に控える・コメントは消える・
  画面に出さないキーは残す）
- **保存したら、DB の場所以外は開いている画面にすぐ効かせる。DB の場所は次に起動したときから**
  （3枚のタブが同じ接続を持っているので、開いたまま差し替えない・利用者の決定）
- 保存の前に、root の入れ子と、出力先が root と重なるのを止める（`select` は出力先の
  ファイルを消す）。無いフォルダは止めずに確かめる（NFS が外れているだけのことがある）
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import config

EMPTY_DATABASE = (
    "DB の場所が空です。空にすると、次に --db なしで起動できなくなります。"
    "DB を使わない設定にするなら、設定ファイルを直接直してください。"
)

#: 窓の題名とボタンの名前
TITLE = "設定"
OPEN_LABEL = "設定…"


def _resolved(value: str) -> Path:
    return Path(value).expanduser().resolve()


def check_paths(
    roots: Sequence[str],
    output_root: Optional[str],
    settings_path: Optional[Path] = None,
) -> Tuple[List[str], List[str]]:
    """保存の前に確かめる。``(止める理由, 確かめること)`` を返す。

    止める: root の入れ子（`scan` が止める・`scanner.normalize_source_roots` と同じ判定）・
    出力先が root と同じ・root の内側・root を内側に含む（`select` は出力先の直下と年の
    フォルダのファイルを消すので、どの向きでも元写真が消えうる）。
    確かめる: 無いフォルダ（NFS が外れているだけのことがある）。

    **相対の root は ``settings_path``（書く先の設定ファイル）の置き場所から解く**（起動時の
    ``gui.resolve_root`` と同じ・``config.resolve_source_root``）。カレントから解くと、設定が
    カレントの外にあるとき実在する root を「見つからない」と言い、root の内側の出力先を
    止められなかった（PR #138 のレビュー指摘1）。
    """
    errors: List[str] = []
    warnings: List[str] = []
    resolved = []
    for value in roots:
        path = _resolved(config.resolve_source_root(value, settings_path))
        if not path.is_dir():
            warnings.append(f"写真の置き場所が見つかりません: {value}")
        if path not in resolved:
            resolved.append(path)
    for outer in resolved:
        for inner in resolved:
            if outer != inner and outer in inner.parents:
                errors.append(
                    f"写真の置き場所が入れ子になっています: {inner} は {outer} の内側です。どちらか一方にしてください。"
                )
    if output_root:
        output = _resolved(output_root)
        for root in resolved:
            if output == root or root in output.parents or output in root.parents:
                errors.append(
                    f"出力先が写真の置き場所と重なっています（{output} と {root}）。"
                    "出力先は実行のたびにファイルを消すので、写真の置き場所の外の、select 専用のフォルダにしてください。"
                )
        if not output.is_dir():
            warnings.append(f"出力先のフォルダはまだありません（出力するときに作ります）: {output_root}")
    return errors, warnings


class SettingsDialog(QDialog):
    """``config/app_settings.yml`` を直す窓。

    ``open_database`` はいま開いている DB（``--db`` か設定）。保存した DB の場所がそれと違えば、
    次に起動したときからと知らせる。
    """

    def __init__(self, open_database: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.open_database = open_database
        self.path = config.settings_write_path()
        #: 保存した中身（保存しなければ None）
        self.saved: Optional[Dict[str, Any]] = None
        #: 保存したとき、DB の場所が次に起動したときからになったか
        self.database_needs_restart = False
        try:
            self.settings = config.read_settings_for_write(self.path)
            self.read_error = ""
        except config.SettingsWriteError as error:
            self.settings = {}
            self.read_error = str(error)

        where = QLabel()
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if self.read_error:
            where.setText(f"{self.read_error}\nファイルを直してから開き直してください。")
        elif self.path.exists():
            where.setText(f"設定ファイル: {self.path}\n保存すると {self.path.name}.bak に控えます（ファイルのコメントは消えます）。")
        else:
            where.setText(f"設定ファイルがまだありません。保存すると作ります: {self.path}")

        self.database_edit = QLineEdit(str(self.settings.get("database_path") or ""))
        database_row = self._with_button(self.database_edit, "選ぶ…", self._choose_database)
        database_note = QLabel(
            f"いま開いている DB: {open_database}\nDB の場所を変えたら、次に起動したときから使います。"
            "（--db を付けて起動したときは --db が優先します）"
        )
        database_note.setWordWrap(True)

        self.roots_list = QListWidget()
        self.roots_list.addItems(config.get_source_roots(self.settings))
        self.roots_list.setMinimumHeight(90)
        add_root = QPushButton("追加…")
        add_root.clicked.connect(self._add_root)
        self.remove_root_button = QPushButton("外す")
        self.remove_root_button.clicked.connect(self.remove_selected_root)
        root_buttons = QVBoxLayout()
        root_buttons.addWidget(add_root)
        root_buttons.addWidget(self.remove_root_button)
        root_buttons.addStretch(1)
        roots_row = QHBoxLayout()
        roots_row.addWidget(self.roots_list, 1)
        roots_row.addLayout(root_buttons)
        roots_note = QLabel(
            "共通の親（たとえば NAS の直下）を書かないでください。関係の無いフォルダまで取り込みます。"
            "相対パスは設定ファイルの置き場所（config/ の1つ上）から解きます。"
        )
        roots_note.setWordWrap(True)

        self.output_edit = QLineEdit(str(self.settings.get("output_root") or ""))
        output_row = self._with_button(self.output_edit, "選ぶ…", self._choose_output)
        output_note = QLabel("select 専用のフォルダにしてください。出力のたびに、前回のファイルを消してからコピーします。")
        output_note.setWordWrap(True)

        self.rule_edit = QLineEdit(str(self.settings.get("rule_path") or ""))
        rule_row = self._with_button(self.rule_edit, "選ぶ…", self._choose_rule)

        form = QFormLayout()
        form.addRow("DB の場所", database_row)
        form.addRow("", database_note)
        form.addRow("写真の置き場所（root）", roots_row)
        form.addRow("", roots_note)
        form.addRow("出力先", output_row)
        form.addRow("", output_note)
        form.addRow("抽出ルール", rule_row)

        self.save_button = QPushButton("保存")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save_clicked)
        self.save_button.setEnabled(not self.read_error)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(where)
        layout.addLayout(form)
        layout.addLayout(buttons)
        self.setMinimumWidth(720)
        self.adjustSize()

    @staticmethod
    def _with_button(edit: QLineEdit, label: str, slot) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton(label)
        button.clicked.connect(slot)
        row.addWidget(button)
        return row

    # ------------------------------------------------------------------
    # 選ぶ
    # ------------------------------------------------------------------

    def _start_dir(self, value: str) -> str:
        return str(Path(value).expanduser().parent) if value else str(Path.cwd())

    def _choose_database(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "DB を選ぶ", self._start_dir(self.database_edit.text()), "SQLite (*.db *.sqlite);;すべて (*)"
        )
        if path:
            self.database_edit.setText(path)

    def _add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "写真の置き場所を選ぶ", str(Path.cwd()))
        if path:
            self.add_root(path)

    def add_root(self, path: str) -> None:
        if path not in self.roots():
            self.roots_list.addItem(path)

    def remove_selected_root(self) -> None:
        for item in self.roots_list.selectedItems():
            self.roots_list.takeItem(self.roots_list.row(item))

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "出力先を選ぶ", self._start_dir(self.output_edit.text()))
        if path:
            self.output_edit.setText(path)

    def _choose_rule(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "抽出ルールを選ぶ", self._start_dir(self.rule_edit.text()), "YAML (*.yml *.yaml);;すべて (*)"
        )
        if path:
            self.rule_edit.setText(path)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def roots(self) -> List[str]:
        return [self.roots_list.item(i).text() for i in range(self.roots_list.count())]

    def values(self) -> Dict[str, Any]:
        """画面の値。空の欄はキーを消す（`config.write_settings`）。"""
        return {
            "database_path": self.database_edit.text().strip() or None,
            "source_roots": self.roots(),
            "output_root": self.output_edit.text().strip() or None,
            "rule_path": self.rule_edit.text().strip() or None,
        }

    def _save_clicked(self) -> None:
        if self.save():
            self.accept()

    def save(self) -> bool:
        """確かめてから書く。書いたら True。"""
        values = self.values()
        errors, warnings = check_paths(values["source_roots"], values["output_root"], self.path)
        if not values["database_path"]:
            # 無いと次に --db なしで起動できない（PR #138 のレビュー指摘2・利用者の決定「保存させない」）
            errors.insert(0, EMPTY_DATABASE)
        if errors:
            QMessageBox.warning(self, "保存できません", "\n\n".join(errors))
            return False
        if warnings:
            answer = QMessageBox.question(
                self, TITLE, "\n".join(warnings) + "\n\nこのまま保存しますか？"
            )
            if answer != QMessageBox.Yes:
                return False
        try:
            self.saved = config.write_settings(self.path, values)
        except (OSError, config.SettingsWriteError) as error:
            QMessageBox.warning(self, "保存できません", str(error))
            return False
        # **値を変えたときだけ**知らせる（PR #138 のレビュー指摘3）。いま開いている DB と比べると、
        # --db で起動したときに DB に触れなくても出ていた（次も --db なら --db が優先する）
        before = self.settings.get("database_path")
        self.database_needs_restart = not before or _resolved(str(before)) != _resolved(values["database_path"])
        return True
