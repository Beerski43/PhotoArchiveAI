"""実物のモデルが用意されている環境でだけ動かす確認。

Issue #10 の原因は `face_recognition_models/__init__.py` が `pkg_resources` に
依存していることだった(新しい setuptools では削除済み)。
`importlib.util.find_spec` はモジュールを実行しないので、この経路を踏まずに
モデルのパスだけ取り出せる。それをここで担保する。

**Issue #59 で特徴量が ArcFace(512次元・コサイン) になった。** 実物の ONNX は
174MB あるので、ここだけで読む（回帰テストの合否には含めない）。
"""

import numpy as np
import pytest

from photoarchive_ai import embedding, face

pytestmark = pytest.mark.models


def test_model_directory_is_resolved_without_pkg_resources():
    if not face.has_dlib_models():  # pragma: no cover - 環境依存
        pytest.skip("dlib face models are not installed")
    model_dir = face._resolve_model_dir()
    assert (model_dir / face.SHAPE_PREDICTOR_FILE).is_file()
    assert (model_dir / face.FACE_RECOGNITION_FILE).is_file()


def _real_rgb(seed: int = 0, size: int = 400) -> np.ndarray:
    return (np.random.default_rng(seed).random((size, size, 3)) * 255).astype(np.uint8)


def test_the_real_model_returns_what_the_description_promises(monkeypatch):
    """**記述と実物がそろっていること。** 次元数が違えば保存で弾かれる。"""
    monkeypatch.undo()  # conftest のフェイクを外して実物を読む
    face.reset_model_cache()
    try:
        if not face.embedding_available():  # pragma: no cover - 環境依存
            pytest.skip(f"{face.ARCFACE_FILE} が無い（scripts/fetch_models.py で取得する）")
        vector = face.compute_embedding(_real_rgb(), (100, 300, 300, 100))
        assert vector is not None
        assert vector.shape == (embedding.ACTIVE.dimensions,)
        assert vector.dtype == np.float32
    finally:
        face.reset_model_cache()


def test_the_real_model_also_works_from_a_thumbnail(monkeypatch):
    """**`reembed` が通る経路。** 切り出し済みの顔画像だけで特徴量が作れること。"""
    monkeypatch.undo()
    face.reset_model_cache()
    try:
        if not face.embedding_available():  # pragma: no cover - 環境依存
            pytest.skip(f"{face.ARCFACE_FILE} が無い（scripts/fetch_models.py で取得する）")
        vector = face.compute_embedding_from_face_image(_real_rgb(size=160))
        assert vector is not None
        assert vector.shape == (embedding.ACTIVE.dimensions,)
    finally:
        face.reset_model_cache()


def test_the_same_face_image_gives_the_same_vector(monkeypatch):
    """**同じ入力から同じ特徴量が出ること。**

    整列に RANSAC を使うと乱数で揺れる。`similarity_transform` は閉じた式に
    してあるので、ここが揺れたら整列の実装が変わった合図。
    """
    monkeypatch.undo()
    face.reset_model_cache()
    try:
        if not face.embedding_available():  # pragma: no cover - 環境依存
            pytest.skip(f"{face.ARCFACE_FILE} が無い（scripts/fetch_models.py で取得する）")
        rgb = _real_rgb(size=160)
        first = face.compute_embedding_from_face_image(rgb)
        second = face.compute_embedding_from_face_image(rgb)
        assert np.allclose(first, second)
    finally:
        face.reset_model_cache()


# `face_rect` の確認は tests/test_face_io.py にある。モデルを必要としないのに
# ここに置いていたため、回帰テスト(`-m "not models"`)では常に除外されていた。
