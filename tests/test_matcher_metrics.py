"""距離尺度がモデルの属性であること（Issue #59）。

**dlib の 0.4 を他のモデルへ持ち込むと意味が変わる。** dlib はユークリッド、
ArcFace は L2 正規化後のコサイン。尺度と閾値は一体なので、どちらかだけを
差し替えると**静かに誤った紐づけが増える。**
"""

import numpy as np
import pytest

from photoarchive_ai import embedding, matcher, scoring


def _vectors(*rows) -> np.ndarray:
    return np.asarray(rows, dtype=np.float64)


def test_the_defaults_come_from_the_active_model():
    """**閾値もマージンも書き写さない。** モデルの記述から引く。"""
    assert matcher.DEFAULT_THRESHOLD == embedding.ACTIVE.threshold
    assert matcher.DEFAULT_MARGIN == embedding.ACTIVE.margin
    assert scoring.SIMILARITY_REFERENCE_DISTANCE == embedding.ACTIVE.similarity_reference


def test_the_assign_score_floor_matches_the_documented_formula():
    """**仕様書の式と実装がずれないこと。**（PR #60 の指摘2）

    仕様書 §8.3 は `assign_score = (1 - 距離 / 基準距離) × 100` と書き、
    「閾値で切るので下限がある」と続ける。基準距離を書き写していたため、
    **既定の閾値を変えたときに式だけ 0.6 のまま残っていた。**
    """
    model = embedding.ACTIVE
    expected = (1.0 - model.threshold / model.similarity_reference) * 100.0
    assert scoring.distance_to_similarity(model.threshold) == pytest.approx(expected)
    # 閾値より遠い顔は割り当てられないので、これが下限
    assert scoring.distance_to_similarity(model.threshold * 1.01) < expected


def test_euclidean_distances_are_the_plain_geometry():
    distances = matcher._distances(
        _vectors([0.0, 0.0], [3.0, 4.0]),
        _vectors([0.0, 0.0]),
        embedding.METRIC_EUCLIDEAN,
    )
    assert distances[0][0] == pytest.approx(0.0)
    assert distances[1][0] == pytest.approx(5.0)


def test_cosine_distances_ignore_the_length_of_the_vector():
    """**L2 正規化してから比べる。** 省くとベクトルの長さが距離に混ざる。

    ArcFace の出力は長さがそろっていないので、正規化しないと「同じ向きの
    別の長さ」が別人として扱われる。
    """
    distances = matcher._distances(
        _vectors([3.0, 4.0], [30.0, 40.0], [-3.0, -4.0]),
        _vectors([3.0, 4.0]),
        embedding.METRIC_COSINE,
    )
    assert distances[0][0] == pytest.approx(0.0, abs=1e-12)
    assert distances[1][0] == pytest.approx(0.0, abs=1e-12)  # 10倍でも同じ向き
    assert distances[2][0] == pytest.approx(2.0)  # 真逆


def test_cosine_distances_stay_inside_the_expected_range():
    """**0 から 2 の間に収める。** 丸め誤差で負になると閾値の判断が崩れる。"""
    rng = np.random.default_rng(0)
    left = rng.normal(size=(20, 8))
    right = rng.normal(size=(5, 8))
    distances = matcher._distances(left, right, embedding.METRIC_COSINE)
    assert distances.min() >= 0.0
    assert distances.max() <= 2.0


def test_a_zero_vector_does_not_divide_by_zero():
    """長さ0のベクトルが来ても落ちないこと（特徴量が壊れている顔）。"""
    distances = matcher._distances(
        _vectors([0.0, 0.0]), _vectors([1.0, 0.0]), embedding.METRIC_COSINE
    )
    assert np.isfinite(distances).all()


def test_an_unknown_metric_is_refused():
    """**尺度を黙って既定にしない。** 取り違えると誤った紐づけが静かに増える。"""
    with pytest.raises(ValueError):
        matcher._distances(_vectors([1.0, 0.0]), _vectors([1.0, 0.0]), "dot")


def test_the_metric_defaults_to_the_active_model():
    """省略したら、いま使うモデルの尺度になること。"""
    same_direction = matcher._distances(_vectors([3.0, 4.0]), _vectors([30.0, 40.0]))
    if embedding.ACTIVE.metric == embedding.METRIC_COSINE:
        assert same_direction[0][0] == pytest.approx(0.0, abs=1e-12)
    else:
        assert same_direction[0][0] > 1.0


def test_the_histogram_cap_follows_the_metric():
    """**分布の刻みの上限も尺度で変わる。** 固定すると遠い顔が1つの桶に潰れる。"""
    expected = 2.0 if embedding.ACTIVE.metric == embedding.METRIC_COSINE else 1.5
    assert matcher.HISTOGRAM_MAX == expected


def test_every_known_model_has_a_usable_description():
    """**記述の取りこぼしを防ぐ。** 版から記述を引けないモデルを作らない。"""
    for version, model in embedding.KNOWN_MODELS.items():
        assert embedding.model_for_version(version) is model
        assert model.dimensions > 0
        assert model.metric in (embedding.METRIC_EUCLIDEAN, embedding.METRIC_COSINE)
        assert 0.0 < model.threshold < model.similarity_reference
        assert 0.0 < model.margin < model.threshold


def test_an_unknown_version_cannot_be_resolved():
    with pytest.raises(KeyError):
        embedding.model_for_version("whatever/v0")
