"""行事の中で顔を束ねる計算（Issue #61）。

**ここで守っているのは「人が見て納得できる束になること」。** 束は判断の単位
なので、同じ行事を2回開いて違う束が出たり、1つの誤った近さで鎖のように
繋がったりすると、何を確認したのか分からなくなる。
"""

import numpy as np
import pytest

from photoarchive_ai import clustering, embedding


def _line(values):
    """1次元に並べた点。**ユークリッドで測れば距離がそのまま差になる。**"""
    return [np.array([float(value)], dtype=np.float32) for value in values]


EUCLIDEAN = embedding.METRIC_EUCLIDEAN


# ---------------------------------------------------------------------------
# 束ね方
# ---------------------------------------------------------------------------


def test_far_faces_stay_in_separate_clusters():
    clusters = clustering.cluster_faces(
        [1, 2, 3], _line([0.0, 0.1, 10.0]), threshold=0.5, metric=EUCLIDEAN
    )
    assert [cluster.face_ids for cluster in clusters] == [(1, 2), (3,)]


def test_average_linkage_does_not_chain():
    """**単連結にしない。** 等間隔に並んだ顔が鎖で1つに繋がらないこと。

    0 と 1、1 と 2 はどちらも距離 1.0。単連結だと閾値 1.0 で3つとも繋がるが、
    平均連結では {0,1} を作った時点で 2 までの距離が (2.0+1.0)/2 = 1.5 になり、
    閾値を超えて止まる。**この違いが、知らない他人を巻き込まない理由。**
    """
    clusters = clustering.cluster_faces(
        [1, 2, 3], _line([0.0, 1.0, 2.0]), threshold=1.0, metric=EUCLIDEAN
    )
    assert [cluster.face_ids for cluster in clusters] == [(1, 2), (3,)]


def test_weights_follow_the_size_of_each_cluster():
    """平均連結の重みは**件数**。大きい束の中心が小さい束に引っ張られない。

    0・0.2・0.4 の3つが先に1つになり（中心 0.2）、1.1 はそこから平均
    (1.1 + 0.9 + 0.7)/3 = 0.9 離れている。閾値 0.8 では入らない。
    """
    clusters = clustering.cluster_faces(
        [1, 2, 3, 4], _line([0.0, 0.2, 0.4, 1.1]), threshold=0.8, metric=EUCLIDEAN
    )
    assert [cluster.face_ids for cluster in clusters] == [(1, 2, 3), (4,)]


def test_bigger_clusters_come_first():
    """**大きい束から見せる。** 1件の束を先に見せても作業は減らない。"""
    clusters = clustering.cluster_faces(
        [1, 2, 3, 4], _line([0.0, 10.0, 10.1, 10.2]), threshold=0.5, metric=EUCLIDEAN
    )
    assert [cluster.face_ids for cluster in clusters] == [(2, 3, 4), (1,)]
    assert [cluster.size for cluster in clusters] == [3, 1]


def test_faces_keep_the_order_they_were_given():
    """束の中の並びは渡された順。**先頭が代表の顔**になるため。"""
    clusters = clustering.cluster_faces(
        [30, 10, 20], _line([0.0, 0.1, 0.2]), threshold=1.0, metric=EUCLIDEAN
    )
    assert clusters[0].face_ids == (30, 10, 20)


def test_the_same_input_always_gives_the_same_clusters():
    """**乱数を使わない。** 同じ行事を2回開いて違う束が出ると確認にならない。

    距離が完全に並んでいる（どの組も同じ距離）配置でも、結び方が決まること。
    """
    vectors = _line([0.0, 1.0, 2.0, 3.0])
    first = clustering.cluster_faces([1, 2, 3, 4], vectors, threshold=1.0, metric=EUCLIDEAN)
    second = clustering.cluster_faces([1, 2, 3, 4], vectors, threshold=1.0, metric=EUCLIDEAN)
    assert [cluster.face_ids for cluster in first] == [
        cluster.face_ids for cluster in second
    ]


def test_every_face_lands_in_exactly_one_cluster():
    """**顔を落とさない。** 束の合計が渡した顔の数と合うこと。

    合わないと「束ねたつもりの行事に未割当が残っている」ことに気づけない。
    """
    rng = np.random.default_rng(20261004)
    vectors = [rng.normal(size=8).astype(np.float32) for _ in range(40)]
    face_ids = list(range(100, 140))
    clusters = clustering.cluster_faces(face_ids, vectors, threshold=0.5)
    members = [face_id for cluster in clusters for face_id in cluster.face_ids]
    assert sorted(members) == face_ids


def test_a_single_face_is_a_cluster_of_one():
    clusters = clustering.cluster_faces([7], _line([0.0]), threshold=0.5, metric=EUCLIDEAN)
    assert [cluster.face_ids for cluster in clusters] == [(7,)]


def test_no_faces_means_no_clusters():
    assert clustering.cluster_faces([], [], threshold=0.5) == []


# ---------------------------------------------------------------------------
# 尺度と閾値はモデルから引く
# ---------------------------------------------------------------------------


def test_the_threshold_comes_from_the_active_model(monkeypatch):
    """**閾値を書き写さない。** 省略したら `embedding.ACTIVE` のものを使う。

    モデルを替えたときに古い数字が残らないようにするため。
    """
    seen = {}
    real = clustering.average_linkage_labels

    def spy(distances, threshold):
        seen["threshold"] = threshold
        return real(distances, threshold)

    monkeypatch.setattr(clustering, "average_linkage_labels", spy)
    clustering.cluster_faces([1, 2], _line([0.0, 5.0]))
    assert seen["threshold"] == embedding.ACTIVE.cluster_threshold


def test_the_metric_comes_from_the_active_model():
    """既定の尺度はモデルのもの。**コサインの顔をユークリッドで測らない。**

    長さだけが違う2つのベクトルは、コサインでは距離0・ユークリッドでは離れる。
    既定で1つの束になれば、コサインで測っている。
    """
    assert embedding.ACTIVE.metric == embedding.METRIC_COSINE
    vectors = [np.array([1.0, 0.0], dtype=np.float32), np.array([5.0, 0.0], dtype=np.float32)]
    clusters = clustering.cluster_faces([1, 2], vectors)
    assert [cluster.face_ids for cluster in clusters] == [(1, 2)]


# ---------------------------------------------------------------------------
# 入力の検査
# ---------------------------------------------------------------------------


def test_mismatched_lengths_are_refused():
    """**特徴量の無い顔を黙って落とさない**ための検査（呼び出し側で外す）。"""
    with pytest.raises(ValueError):
        clustering.cluster_faces([1, 2], _line([0.0]))


def test_too_many_faces_is_refused_instead_of_truncated(monkeypatch):
    """**上限を超えたら黙って捨てずに上げる。**

    先頭だけ束ねて残りを落とすと、画面に出ていない顔が未割当のまま残る。
    """
    monkeypatch.setattr(clustering, "MAX_FACES", 3)
    with pytest.raises(clustering.TooManyFacesError):
        clustering.cluster_faces([1, 2, 3, 4], _line([0.0, 1.0, 2.0, 3.0]))


def test_a_non_square_distance_matrix_is_refused():
    with pytest.raises(ValueError):
        clustering.average_linkage_labels(np.zeros((2, 3)), 0.5)
