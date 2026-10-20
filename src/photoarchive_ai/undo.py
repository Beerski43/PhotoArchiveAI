"""GUI の顔の操作を元に戻す・やり直す（#116）。

**履歴は GUI を開いている間だけ**（利用者の決定）。DB には持たない（スキーマを変えない）。
Qt に依存させないのは、戻し方をテストで細かく確かめるため。

GUI の顔の操作が書き換えるのは、`Face` の割り当ての列（`FACE_COLUMNS`）と、
「この人物ではない」の記録（`FaceRejection`）だけ。操作の前後でそれを控え、
戻すときは前の控えを、やり直すときは後の控えを書く。

**戻す前に、いまの状態が「操作の直後」のままかを確かめる。** 違えば戻さない。
GUI から `match` を流したり、人物を削除したり、別の画面や CLI が書いたりした
あとで古い控えを書くと、**その後の結果を黙って上書きする**ため。
戻す先が指す人物が消えているときも戻さない（削除した人物の id を書き戻さない）。
"""

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

#: 顔の操作が書き換える `Face` の列。**操作が書く列を足したら、ここにも足す。**
FACE_COLUMNS = (
    "person_id",
    "assign_source",
    "assign_score",
    "assign_rule",
    "penalize",
    "age",
    "assigned_at",
)

#: 持つ操作の数。超えたら古いものから捨てる（利用者に示した値・#116）。
MAX_STEPS = 50

#: `IN (...)` に並べる数の上限。SQLite の変数の上限（古い版で 999）を超えないように分ける。
_CHUNK = 500


@dataclass(frozen=True)
class FaceState:
    """顔の割り当ての控え。``faces`` は ``{face_id: FACE_COLUMNS の値}``。"""

    faces: Dict[int, Tuple]
    #: ``(face_id, person_id, created_at)``。控えた顔に付いているものだけ。
    rejections: FrozenSet[Tuple[int, int, str]]

    def person_ids(self) -> set:
        """この控えが指す人物。**書き戻す前に、まだあるかを確かめる。**"""
        index = FACE_COLUMNS.index("person_id")
        ids = {values[index] for values in self.faces.values() if values[index] is not None}
        return ids | {person_id for _face_id, person_id, _created in self.rejections}


@dataclass(frozen=True)
class Step:
    """1つの操作。``label`` は「何をしたか」（画面に出す）。"""

    label: str
    face_ids: Tuple[int, ...]
    before: FaceState
    after: FaceState


class UndoConflict(Exception):
    """戻せない（やり直せない）。理由を利用者に見せる文で持つ。"""


def _chunks(face_ids: Sequence[int]):
    for start in range(0, len(face_ids), _CHUNK):
        yield face_ids[start : start + _CHUNK]


def capture(connection, face_ids: Sequence[int]) -> FaceState:
    """``face_ids`` の顔の割り当てと否定の記録を控える。消えた顔は控えに入らない。"""
    ids = sorted(set(int(face_id) for face_id in face_ids))
    faces: Dict[int, Tuple] = {}
    rejections = set()
    columns = ", ".join(FACE_COLUMNS)
    for chunk in _chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"SELECT id, {columns} FROM Face WHERE id IN ({placeholders})", chunk
        ):
            faces[int(row[0])] = tuple(row[1:])
        for row in connection.execute(
            "SELECT face_id, person_id, created_at FROM FaceRejection"
            f" WHERE face_id IN ({placeholders})",
            chunk,
        ):
            rejections.add((int(row[0]), int(row[1]), row[2]))
    return FaceState(faces, frozenset(rejections))


def _missing_persons(connection, state: FaceState) -> List[int]:
    wanted = sorted(state.person_ids())
    found = set()
    for chunk in _chunks(wanted):
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            int(row[0])
            for row in connection.execute(
                f"SELECT id FROM Person WHERE id IN ({placeholders})", chunk
            )
        )
    return [person_id for person_id in wanted if person_id not in found]


def write(connection, state: FaceState) -> None:
    """控えのとおりに書き戻す。**1つのトランザクションで**（途中で落ちても半端に戻さない）。"""
    assignments = ", ".join(f"{column} = ?" for column in FACE_COLUMNS)
    ids = sorted(state.faces)
    try:
        connection.executemany(
            f"UPDATE Face SET {assignments} WHERE id = ?",
            [(*state.faces[face_id], face_id) for face_id in ids],
        )
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM FaceRejection WHERE face_id IN ({placeholders})", chunk
            )
        connection.executemany(
            "INSERT INTO FaceRejection (face_id, person_id, created_at) VALUES (?, ?, ?)",
            sorted(state.rejections),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class History:
    """元に戻す・やり直すの履歴。**1つの GUI（`MainWindow`）に1つ。** 束ねる画面も同じものに積む。"""

    def __init__(self, max_steps: int = MAX_STEPS):
        self.max_steps = max_steps
        self.undo_steps: List[Step] = []
        self.redo_steps: List[Step] = []

    def record(self, connection, label: str, face_ids: Sequence[int], work: Callable[[], object]):
        """``work`` を流し、前後を控えて積む。``work`` の戻り値を返す。

        **``work`` が触る顔は ``face_ids`` の中に収まっていること**（GUI の操作は、選んだ顔
        だけを書く）。何も変わらなかった操作は積まない（戻しても何も起きないので）。
        """
        ids = tuple(sorted(set(int(face_id) for face_id in face_ids)))
        before = capture(connection, ids)
        result = work()
        after = capture(connection, ids)
        if before != after:
            self.undo_steps.append(Step(label, ids, before, after))
            del self.undo_steps[: -self.max_steps]
            self.redo_steps.clear()
        return result

    def can_undo(self) -> bool:
        return bool(self.undo_steps)

    def can_redo(self) -> bool:
        return bool(self.redo_steps)

    def next_undo(self) -> Optional[Step]:
        return self.undo_steps[-1] if self.undo_steps else None

    def next_redo(self) -> Optional[Step]:
        return self.redo_steps[-1] if self.redo_steps else None

    def undo(self, connection) -> Step:
        """いちばん新しい操作を戻す。戻せなければ `UndoConflict`（その操作は履歴から捨てる）。"""
        step = self._pop(self.undo_steps, "元に戻す")
        self._apply(connection, step, expected=step.after, target=step.before, verb="元に戻せません")
        self.redo_steps.append(step)
        return step

    def redo(self, connection) -> Step:
        """いちばん新しく戻した操作をやり直す。やり直せなければ `UndoConflict`。"""
        step = self._pop(self.redo_steps, "やり直す")
        self._apply(connection, step, expected=step.before, target=step.after, verb="やり直せません")
        self.undo_steps.append(step)
        return step

    @staticmethod
    def _pop(steps: List[Step], name: str) -> Step:
        if not steps:
            raise UndoConflict(f"{name}操作がありません。")
        return steps.pop()

    def _apply(self, connection, step: Step, expected: FaceState, target: FaceState, verb: str) -> None:
        """いまの状態が ``expected`` のままなら ``target`` を書く。

        **違えば書かず、その操作を捨てる。** 残しておいても、ずれた状態の上では二度と
        戻せない。それより古い操作は残す（別の顔の操作なら戻せる）。
        """
        if capture(connection, step.face_ids) != expected:
            raise UndoConflict(
                f"{verb}: 「{step.label}」のあとで、同じ顔の割り当てが変わっています"
                "（自動割り当ての実行や、ほかの画面での操作など）。この操作は履歴から外しました。"
            )
        missing = _missing_persons(connection, target)
        if missing:
            raise UndoConflict(
                f"{verb}: 「{step.label}」が指す人物（id {', '.join(map(str, missing))}）が"
                "削除されています。この操作は履歴から外しました。"
            )
        write(connection, target)
