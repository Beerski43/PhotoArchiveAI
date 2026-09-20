import os

import pytest

from tests.fakes import (
    FACE_MESH_STATE,
    _FakeRecognitionModel,
    _FakeShapePredictor,
    _install_fake_mediapipe_modules,
)

# 実物の mediapipe / dlib を読み込むと重く、環境にも左右されるため差し替える。
# ただし実装が読む属性と同じものを返すこと。以前のフェイクは
# `bounding_box` しか持たず、実装が先に見る `relative_bounding_box` を
# 通っていなかったため、検出経路が一度も検証されていなかった。
# **中身は tests/fakes.py にある。** scan のワーカープロセス(spawn)からも
# 同じフェイクを入れる必要があり、conftest は子から import できないため。

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_install_fake_mediapipe_modules()


class _FaceMeshControl:
    """FaceMesh のフェイクの戻り値を決める操作口。"""

    @staticmethod
    def set_landmarks(overrides) -> None:
        """``{ランドマーク番号: (x, y)}`` を与える。指定しない点は (0.5, 0.5)。

        x / y は MediaPipe と同じ 0.0-1.0 の相対座標。
        """
        FACE_MESH_STATE["landmarks"] = dict(overrides)

    @staticmethod
    def detect_nothing() -> None:
        """顔が1つも取れなかった場合を再現する。"""
        FACE_MESH_STATE["detected"] = False


@pytest.fixture(autouse=True)
def reset_fake_face_mesh():
    """FaceMesh のフェイクを毎回既定へ戻す。

    フェイクの状態はモジュール変数なので、戻さないと上書きしたテストの
    影響が実行順に応じて他のテストへ漏れる。
    """
    FACE_MESH_STATE["landmarks"] = {}
    FACE_MESH_STATE["detected"] = True
    yield
    FACE_MESH_STATE["landmarks"] = {}
    FACE_MESH_STATE["detected"] = True


@pytest.fixture()
def fake_face_mesh():
    return _FaceMeshControl



@pytest.fixture(autouse=True)
def fake_face_models(monkeypatch):
    """dlib のモデル読み込みを差し替える。実物の .dat は使わない。"""
    from photoarchive_ai import face, scoring

    monkeypatch.setattr(
        face, "_load_dlib_models", lambda: (_FakeShapePredictor(), _FakeRecognitionModel())
    )
    monkeypatch.setattr(face, "EMBED_MIN_FACE_PX", 4)
    face.reset_model_cache()
    scoring.reset_model_cache()
    yield
    face.reset_model_cache()
    scoring.reset_model_cache()


@pytest.fixture(autouse=True)
def isolate_app_settings(tmp_path, monkeypatch):
    """開発機の config/app_settings.json をテストから見えなくする。

    設定の探索は 環境変数 → カレントディレクトリ → リポジトリ直下 の順。
    最後の一段があるせいで、素のテストが実機の設定(NFS 上の source_root
    など)を読んでしまいうる。テストの結果が開発機の状態で変わらないよう、
    リポジトリ直下の探索先を空のディレクトリへ向ける。
    設定を使うテストは、自分でカレントディレクトリに置く。
    """
    from photoarchive_ai import config

    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-such-repo")
    yield


@pytest.fixture(autouse=True)
def reset_cli_progress_state():
    """cli._progress_started をテストごとに戻す。

    進捗表示は ANSI のカーソル移動で2行を書き換えるため、
    「1行目を出したか」をモジュール変数で持っている。テストが途中で
    終わると True のまま残り、次のテストの標準出力に \033[2A が
    混ざる。テストの実行順に依存した差が出るので、毎回戻す。
    """
    from photoarchive_ai import cli

    cli._reset_progress_state()
    yield
    cli._reset_progress_state()


# Make QMessageBox non-interactive in tests to avoid modal dialogs blocking execution.
try:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.information = lambda *a, **k: None
    QMessageBox.warning = lambda *a, **k: None
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
except Exception:
    # If PySide6 is unavailable in the collection stage, skip patching here.
    pass
