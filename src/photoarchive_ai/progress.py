"""端末の進捗表示（#78）。CLI の各コマンドと ``scripts/`` が共有する。

画面の形::

    Error: IMG_0002.jpg: Cannot read image      ← エラーが出たときだけ、上に残る
    Scanning: [######--------------]  30% (3/10)
    IMG_0004.jpg                                ← 最新のメッセージを1行だけ

**書き直す2行（バーと最新のメッセージ）は、端末の幅に収めて切る。** 以前はバーの行が
長いファイル名で折り返し、画面の上で3行以上になっていたのに、書き直すときは2行しか
戻らなかった。そのため書き直すたびに古い行が1つ取り残され、``Error: none`` が何行も
並んだ（#78）。日本語は1文字で2桁を使うので、文字数ではなく**表示幅**で切る。
**残す行は切らない。** 控えのパスや「触らなかったファイル」など、ほかに記録の無いものが
入る（PR #81 のレビュー指摘1）。書き直さない行なので、折り返しても戻る行数は狂わない。

**エラーが無いときは ``Error`` という語を出さない。** 端末によってはその語に
警告の色が付き、問題が起きたように見える（#78）。エラーは消さずに上へ残すので、
流れていく表示の中で一瞬しか見えない、ということも起きない（#25）。

**端末でない出力先**（ファイルへのリダイレクト・パイプ）には、エスケープを書かない。
残す行（エラーなど）と、各段の終わりのバーの行だけを出す。
"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from typing import Optional, TextIO

BAR_WIDTH = 20
#: 切ったことを示す印。幅が曖昧な "…" ではなく、どの端末でも3桁の ASCII にする。
ELLIPSIS = "..."


def display_width(text: str) -> int:
    """端末での表示幅。全角（East Asian Width が W/F）と**曖昧幅（A）**は2桁、結合文字は0桁。

    曖昧幅（``①`` ``※`` ``×`` ``…`` など）は端末の設定で1桁にも2桁にもなる。少なく
    見積もると2行目が折り返し、#78 と同じく古い行が残るので、多めに2桁と数える
    （PR #81 のレビュー指摘2）。ASCII に曖昧幅は無いので、英数字の幅は変わらない。
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
    return width


def fit(text: str, width: int) -> str:
    """表示幅 ``width`` に収める。改行は空白にし、はみ出すぶんは末尾を切って印を付ける。"""
    text = str(text).replace("\r", " ").replace("\n", " ")
    if display_width(text) <= width:
        return text
    limit = max(0, width - len(ELLIPSIS))
    kept = []
    used = 0
    for char in text:
        size = display_width(char)
        if used + size > limit:
            break
        kept.append(char)
        used += size
    return "".join(kept) + ELLIPSIS[: max(0, width - used)]


class ProgressDisplay:
    """バーと最新のメッセージの2行を書き直し、残す行はその上に積む。"""

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        interactive: Optional[bool] = None,
        width: Optional[int] = None,
    ) -> None:
        self._stream = stream
        self._interactive = interactive
        self._width = width
        #: いま画面に描いている行数（0 か 2）。書き直すときに戻る行数。
        self._drawn = 0
        self._last_error = ""
        self._block = ("", "")

    # -- 出力先 -------------------------------------------------------------

    @property
    def stream(self) -> TextIO:
        # 既定は呼ばれた時点の sys.stdout（pytest の capsys が差し替えるため、作った時点で固定しない）
        return self._stream if self._stream is not None else sys.stdout

    def _is_interactive(self) -> bool:
        if self._interactive is not None:
            return self._interactive
        try:
            return bool(self.stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _columns(self) -> int:
        if self._width is not None:
            return self._width
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    def _line_width(self) -> int:
        # 最後の桁まで書くと、端末によっては折り返しの待ちに入り、次の改行で1行余分に送る
        return max(10, self._columns() - 1)

    # -- 描画 ---------------------------------------------------------------

    def _erase(self) -> str:
        """描いている2行を消し、1行目の頭へ戻るエスケープ。"""
        if self._drawn == 0:
            return ""
        return "\r\033[K" + "\033[1A\r\033[K" * (self._drawn - 1)

    def _draw(self) -> None:
        width = self._line_width()
        bar, status = (fit(line, width) for line in self._block)
        self.stream.write(f"{self._erase()}{bar}\n{status}")
        self._drawn = 2
        self.stream.flush()

    def keep(self, message: str) -> None:
        """1行を残す。バーと最新のメッセージは、その下に描き直す。

        **残す行は切らない**（控えのパスなど、ほかに記録の無いものが入る。PR #81 の
        レビュー指摘1）。書く前に2行とも消し、書いたあとは ``_drawn = 0`` から描き直すので、
        折り返しても戻る行数は狂わない。
        """
        if self._is_interactive():
            line = str(message).replace("\r", " ").replace("\n", " ")
            self.stream.write(f"{self._erase()}{line}\033[K\n")
            self._drawn = 0
            if any(self._block):
                self._draw()
                return
        else:
            self.stream.write(f"{message}\n")
        self.stream.flush()

    def error(self, message: str) -> None:
        """エラーを1行残す。直前と同じエラーは繰り返さない。"""
        message = str(message).replace("\r", " ").replace("\n", " ").strip()
        if not message or message == self._last_error:
            return
        self._last_error = message
        self.keep(f"Error: {message}")

    def update(
        self,
        current: int,
        total: Optional[int],
        detail: str = "",
        prefix: str = "Progress",
        show_counts: bool = True,
    ) -> None:
        """進み具合を書き直す。``total`` が None なら総数の分からない段（件数だけ出す）。

        ``show_counts=False`` は件数の無い段（始めと終わりしか分からない段）のため。
        ``(0/1)`` と出しても意味が無い。
        """
        if total is None:
            head = f"{prefix}: {current:,} 件"
        else:
            percent = min(100, max(0, int(current * 100 / total))) if total > 0 else 100
            filled = min(BAR_WIDTH, int(BAR_WIDTH * current / total)) if total > 0 else BAR_WIDTH
            bar = "#" * filled + "-" * (BAR_WIDTH - filled)
            head = f"{prefix}: [{bar}] {percent:3d}%"
            if show_counts:
                head += f" ({current}/{total})"
        self._block = (head, str(detail))
        done = total is not None and current >= total
        if self._is_interactive():
            self._draw()
            if done:
                self.finish()
        elif done:
            self.stream.write(f"{head}\n")
            self.stream.flush()
            self._block = ("", "")

    def finish(self) -> None:
        """段を終える。バーの行は残し、最新のメッセージの行は消す。"""
        if self._drawn and self._is_interactive():
            self.stream.write("\r\033[K")
            self.stream.flush()
        self._drawn = 0
        self._block = ("", "")
