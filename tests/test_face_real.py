"""実物の dlib モデルが用意されている環境でだけ動かす確認。

Issue #10 の原因は `face_recognition_models/__init__.py` が `pkg_resources` に
依存していることだった(新しい setuptools では削除済み)。
`importlib.util.find_spec` はモジュールを実行しないので、この経路を踏まずに
モデルのパスだけ取り出せる。それをここで担保する。
"""

import numpy as np
import pytest

from photoarchive_ai import face

pytestmark = pytest.mark.models

if not face.has_dlib_models():  # pragma: no cover - 環境依存
    pytest.skip("dlib face models are not installed", allow_module_level=True)


def test_model_directory_is_resolved_without_pkg_resources():
    model_dir = face._resolve_model_dir()
    assert (model_dir / face.SHAPE_PREDICTOR_FILE).is_file()
    assert (model_dir / face.FACE_RECOGNITION_FILE).is_file()


def test_real_embedding_has_128_dimensions(monkeypatch):
    # conftest のフェイクを外して実物を読む
    monkeypatch.undo()
    face.reset_model_cache()
    rgb = (np.random.default_rng(0).random((400, 400, 3)) * 255).astype(np.uint8)
    embedding = face.compute_embedding(rgb, (100, 300, 300, 100))
    face.reset_model_cache()
    assert embedding is not None
    assert embedding.shape == (128,)
    assert embedding.dtype == np.float32


# `face_rect` の確認は tests/test_face_io.py にある。モデルを必要としないのに
# ここに置いていたため、回帰テスト(`-m "not models"`)では常に除外されていた。
