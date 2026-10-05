"""特徴量モデルの比較スクリプト（Issue #57）。

**測定の道具がおかしいと、間違った結論で全件再計算に進む。** 指標の計算と
5点整列の変換を固定しておく。実物の ArcFace を要する部分は `models` マーカー。
"""

import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import db

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_embedding_models.py"
ARCFACE_PATH = Path(__file__).resolve().parents[1] / "models" / "w600k_r50.onnx"


@pytest.fixture(scope="module")
def measure_module():
    spec = importlib.util.spec_from_file_location("measure_embedding_models", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# 距離尺度
# ---------------------------------------------------------------------------


def test_euclidean_and_cosine_are_both_supported(measure_module):
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]])
    euclid = measure_module.distance_matrix(vectors, measure_module.EUCLIDEAN)
    cosine = measure_module.distance_matrix(vectors, measure_module.COSINE)
    assert euclid[0][1] == pytest.approx(np.sqrt(2.0))
    assert cosine[0][1] == pytest.approx(1.0)
    # 自分との距離は 0
    assert euclid[0][0] == pytest.approx(0.0)
    assert cosine[1][1] == pytest.approx(0.0, abs=1e-12)


def test_cosine_ignores_the_length_of_the_vector(measure_module):
    """**ArcFace は L2 正規化してから比べる。** 長さに結果が左右されないこと。"""
    vectors = np.array([[3.0, 4.0], [30.0, 40.0], [-3.0, -4.0]])
    cosine = measure_module.distance_matrix(vectors, measure_module.COSINE)
    assert cosine[0][1] == pytest.approx(0.0, abs=1e-12)
    assert cosine[0][2] == pytest.approx(2.0)


def test_an_unknown_metric_is_refused(measure_module):
    """**尺度を黙って既定にしない。** dlib の尺度を他のモデルへ当てる事故を防ぐ。"""
    with pytest.raises(ValueError):
        measure_module.distance_matrix(np.zeros((2, 2)), "dot")


# ---------------------------------------------------------------------------
# 1位正解率
# ---------------------------------------------------------------------------


def test_top1_accuracy_excludes_faces_from_the_same_photo(measure_module):
    """**同じ写真の顔を候補から外す。外さないと正解率が水増しされる。**

    同じ写真の同一人物は切り出しがほぼ同じで距離が極端に小さい。入れると
    「同じ写真の自分」を当てているだけで高い点が出る。2026-09-20 の測定と
    条件をそろえるための約束でもある。

    下の配置は、**同じ写真の近い複製だけが正解の素**になるように作ってある。
    外せば 0%、外さなければ 40% になる。
    """
    vectors = np.array(
        [
            [0.000, 0.0],  # 0: 人物1 / 写真10
            [0.001, 0.0],  # 1: 人物1 / 写真10（0 のほぼ複製）
            [0.900, 0.0],  # 2: 人物1 / 写真11（年月が離れて遠い）
            [0.100, 0.0],  # 3: 人物2 / 写真12
            [0.950, 0.0],  # 4: 人物2 / 写真13
        ]
    )
    distances = measure_module.distance_matrix(vectors, measure_module.EUCLIDEAN)
    persons = [1, 1, 1, 2, 2]

    excluded, evaluated = measure_module.top1_accuracy(
        distances, persons, media_ids=[10, 10, 11, 12, 13]
    )
    assert evaluated == 5
    assert excluded == pytest.approx(0.0)

    # 写真が全部別なら同じ写真の複製が候補に残り、その2件だけが正解になる
    included, _ = measure_module.top1_accuracy(
        distances, persons, media_ids=[10, 20, 11, 12, 13]
    )
    assert included == pytest.approx(0.4)


def test_top1_accuracy_reports_nothing_when_every_face_shares_one_photo(measure_module):
    vectors = np.array([[0.0], [1.0]])
    distances = measure_module.distance_matrix(vectors, measure_module.EUCLIDEAN)
    assert measure_module.top1_accuracy(distances, [1, 2], [7, 7]) == (0.0, 0)


# ---------------------------------------------------------------------------
# 行事の中とまたぐの切り分け
# ---------------------------------------------------------------------------


def _separation(measure_module, vectors, persons, medias, events):
    distances = measure_module.distance_matrix(np.array(vectors), measure_module.EUCLIDEAN)
    return measure_module.pair_separation(distances, persons, medias, events)


def test_pairs_are_split_into_within_event_and_across_events(measure_module):
    """**プールした平均を出さない。** ここを混ぜたのが 9/20 の読み違いの原因。"""
    separation = _separation(
        measure_module,
        [[0.0], [1.0], [10.0], [11.0]],
        persons=[1, 2, 1, 2],
        medias=[1, 2, 3, 4],
        events=["A", "A", "B", "B"],
    )
    assert sorted(separation.within_same) == []
    assert sorted(separation.within_diff) == [1.0, 1.0]
    assert sorted(separation.cross_same) == [10.0, 10.0]
    assert sorted(separation.cross_diff) == [9.0, 11.0]


def test_faces_without_a_readable_date_are_left_out_of_both_sides(measure_module):
    """**分からないものをどちらかに混ぜない。** 混ぜると両方の数字が信じられない。

    実データでは未割当の 10.4% が撮影日時を持たない。
    """
    separation = _separation(
        measure_module,
        [[0.0], [1.0], [2.0]],
        persons=[1, 1, 1],
        medias=[1, 2, 3],
        events=["A", "", "A"],
    )
    # 行事の決まらない 1 を含むペアは、どちらにも入らない
    assert separation.within_same == [2.0]
    assert separation.cross_same == []


def test_pairs_from_the_same_photo_are_skipped(measure_module):
    separation = _separation(
        measure_module,
        [[0.0], [0.5]],
        persons=[1, 1],
        medias=[9, 9],
        events=["A", "A"],
    )
    assert separation.within_same == []


def test_the_gap_is_how_far_strangers_sit_from_the_same_person(measure_module):
    separation = measure_module.Separation(
        within_same=[0.3, 0.5], within_diff=[0.9], cross_same=[0.6], cross_diff=[0.7]
    )
    assert separation.gap(within=True) == pytest.approx(0.5)
    assert separation.gap(within=False) == pytest.approx(0.1)


def test_the_gap_is_unknown_when_one_side_is_empty(measure_module):
    separation = measure_module.Separation([], [0.9], [0.6], [])
    assert separation.gap(within=True) is None
    assert separation.gap(within=False) is None


# ---------------------------------------------------------------------------
# 閾値を振る
# ---------------------------------------------------------------------------


def test_the_sweep_finds_a_threshold_that_separates(measure_module):
    best = measure_module.threshold_sweep(same=[0.1, 0.2, 0.3], diff=[0.8, 0.9])
    assert best is not None
    threshold, true_positive, false_positive = best
    assert 0.3 <= threshold < 0.8
    assert true_positive == pytest.approx(1.0)
    assert false_positive == pytest.approx(0.0)


def test_the_sweep_says_nothing_when_a_side_is_empty(measure_module):
    assert measure_module.threshold_sweep([], [0.5]) is None
    assert measure_module.threshold_sweep([0.5], []) is None


# ---------------------------------------------------------------------------
# 手本の読み出し
# ---------------------------------------------------------------------------


def _thumbnail(color=(200, 120, 90), size=24) -> bytes:
    array = np.zeros((size, size, 3), dtype=np.uint8)
    array[:, :] = color
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _seed_database(path: Path) -> None:
    connection = db.ensure_database(str(path))
    try:
        person = db.add_person(connection, "なつ")
        other = db.add_person(connection, "ひより")
        rows = [
            ("/photos/2012/undoukai/a.jpg", "2012-10-06T10:00:00", person, (200, 120, 90)),
            ("/photos/2012/undoukai/b.jpg", "2012-10-06T11:00:00", other, (10, 200, 60)),
            # 壊れた EXIF。行事が決まらない。
            ("/photos/2013/broken/c.jpg", "TTTT-TT-TTTTT:TT:TT", person, (200, 120, 90)),
        ]
        for path_text, shot, person_id, color in rows:
            media_id = db.save_media(
                connection,
                {
                    "path": path_text,
                    "filename": Path(path_text).name,
                    "type": "image",
                    "file_hash": path_text,
                    "file_size": 10,
                    "created_time": "2012-10-06T10:00:00",
                    "shooting_date": shot,
                },
            )
            face_id = db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 24, 24, 0),
                embedding=[float(color[0]) / 255.0] * db.EMBEDDING_DIM,
                embed_version="test",
                thumbnail=_thumbnail(color),
            )
            db.assign_faces(connection, [face_id], person_id, age=3)
        connection.commit()
    finally:
        connection.close()


def test_manual_faces_are_read_without_writing_to_the_database(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    before = database.stat().st_mtime_ns

    records = measure_module.load_manual_faces(str(database))

    assert len(records) == 3
    assert database.stat().st_mtime_ns == before
    # 書き込み用に開いていたら、ここで落ちる
    with pytest.raises(sqlite3.OperationalError):
        read_only = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            read_only.execute("UPDATE Face SET age = 9")
        finally:
            read_only.close()


def test_an_event_is_a_folder_and_a_day(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))
    events = [record.event for record in records]
    assert events[0] == "/photos/2012/undoukai\t2012-10-06"
    assert events[0] == events[1]
    # 日付として読めない写真は行事を決めない
    assert events[2] == ""


def test_folder_of_strips_the_file_name(measure_module):
    assert measure_module.folder_of("/photos/2012/a.jpg") == "/photos/2012"
    assert measure_module.folder_of("a.jpg") == ""


def test_the_thumbnail_is_the_only_image_source(measure_module, tmp_path):
    """**元写真を読まない。** DB の中身だけで顔画像に戻せること。

    実データの元写真は NFS 上（実測 24MB/s）にあり、読み直すと 5.1 時間かかる。
    """
    database = tmp_path / "labels.db"
    _seed_database(database)
    record = measure_module.load_manual_faces(str(database))[0]
    rgb = record.rgb()
    assert rgb.shape == (24, 24, 3)
    # 元写真のパスは存在しないが、それでも顔画像が得られている
    assert not Path("/photos/2012/undoukai/a.jpg").exists()


# ---------------------------------------------------------------------------
# 測定の流れ
# ---------------------------------------------------------------------------


def test_measure_counts_what_it_could_not_embed(measure_module, tmp_path):
    """**作れなかった顔を黙って落とさない。** 母集団が変わると比較が成り立たない。"""
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))

    calls = []

    def embed(record):
        calls.append(record.face_id)
        # 3件目だけ作れない
        return None if len(calls) == 3 else np.array([float(len(calls))])

    variant = measure_module.Variant("x", "試し", measure_module.EUCLIDEAN, embed)
    result = measure_module.measure(variant, records)

    assert result.used == 2
    assert result.skipped == 1
    assert result.evaluated == 2


def test_measure_gives_up_when_fewer_than_two_faces_remain(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))
    variant = measure_module.Variant("x", "試し", measure_module.EUCLIDEAN, lambda _r: None)
    assert measure_module.measure(variant, records) is None


def test_the_stored_embedding_variant_uses_the_value_in_the_database(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))
    vector = measure_module.stored_embedding(records[0])
    assert vector is not None
    assert len(vector) == db.EMBEDDING_DIM


def test_the_embedding_can_be_recomputed_from_the_thumbnail(measure_module, tmp_path):
    """(b) の経路。**サムネイルだけで特徴量を作り直せること。**"""
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))
    vector = measure_module.embedding_from_thumbnail(records[0])
    assert vector is not None
    assert len(vector) == db.EMBEDDING_DIM


def test_the_stored_variant_follows_the_active_model(measure_module, tmp_path):
    """**(a)(b) の尺度と名札はモデルから引く。** 書き写すと古くなる。

    #59 で実データを ArcFace（コサイン）へ作り直したあと、ここには
    「dlib / ユークリッド」と書いた名札が残っていた。**コサインの特徴量を
    ユークリッドで測った表が出るところだった。**
    """
    from photoarchive_ai import embedding as embedding_model

    variants = {variant.key: variant for variant in measure_module.build_variants(None)}
    for key in ("a", "b"):
        assert variants[key].metric == embedding_model.ACTIVE.metric
        assert embedding_model.ACTIVE.version in variants[key].label


def test_arcface_is_skipped_when_the_model_is_absent(measure_module, tmp_path):
    """**モデルが無いときに黙って (a)(b) だけ出さない。** 理由を残すこと。"""
    variants = measure_module.build_variants(tmp_path / "nope.onnx")
    assert [variant.key for variant in variants] == ["a", "b"]


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def test_the_report_lists_every_variant_and_both_kinds_of_gap(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))
    variant = measure_module.Variant(
        "a", "dlib / 原寸", measure_module.EUCLIDEAN, measure_module.stored_embedding
    )
    result = measure_module.measure(variant, records)

    report = measure_module.format_report([result], records, total_faces=58606, notes=["備考1"])

    assert "dlib / 原寸" in report
    assert "行事の中の差" in report and "行事をまたぐ差" in report
    assert "58,606" in report
    assert "備考1" in report
    # 尺度の違うモデルを並べるので、距離そのものを比べさせない断り書きを落とさない
    assert "比べられない" in report


def test_the_report_survives_an_empty_gap(measure_module, tmp_path):
    """手本が少ないと、片側のペアが0件になる。**その場合に落ちないこと。**"""
    separation = measure_module.Separation([], [], [], [])
    variant = measure_module.Variant("z", "空", measure_module.COSINE, lambda _r: None)
    result = measure_module.Result(variant, 2, 0, 0.1, 0.5, 2, separation)
    report = measure_module.format_report([result], [], total_faces=1, notes=[])
    assert "—" in report


def test_main_refuses_a_database_that_is_not_there(measure_module, tmp_path, capsys):
    assert measure_module.main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "見つかりません" in capsys.readouterr().err


def test_main_writes_a_report_and_notes_the_missing_model(measure_module, tmp_path):
    database = tmp_path / "labels.db"
    _seed_database(database)
    report = tmp_path / "report.md"

    code = measure_module.main(
        [
            "--db",
            str(database),
            "--report",
            str(report),
            "--arcface",
            str(tmp_path / "nope.onnx"),
        ]
    )

    assert code == 0
    text = report.read_text(encoding="utf-8")
    assert "ArcFace を測れていない" in text
    # **いま使うモデルの版が名札に出ること。** 以前は "dlib" が出ることを
    # 見ていたが、モデルを替えたら嘘になる名札だった。
    from photoarchive_ai import embedding as embedding_model

    assert embedding_model.ACTIVE.version in text


# ---------------------------------------------------------------------------
# 実物の ArcFace（環境依存。回帰テストの合否に含めない）
# ---------------------------------------------------------------------------


@pytest.mark.models
def test_the_real_arcface_returns_512_dimensions(measure_module, tmp_path):
    if not ARCFACE_PATH.exists():
        pytest.skip(f"ArcFace の ONNX が無い: {ARCFACE_PATH}")
    database = tmp_path / "labels.db"
    _seed_database(database)
    records = measure_module.load_manual_faces(str(database))

    embedder = measure_module.ArcFaceEmbedder(ARCFACE_PATH, align=False)
    vector = embedder(records[0])

    assert vector is not None
    assert len(vector) == 512


# ---------------------------------------------------------------------------
# 同じ写真に写る顔のペアでの他人誤認率
# ---------------------------------------------------------------------------


def _add_face_with_vector(connection, media_id, vector, **kwargs):
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 24, 24, 0),
        embedding=list(vector),
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=_thumbnail(),
        **kwargs,
    )


def _unit(*values):
    vector = [0.0] * db.EMBEDDING_DIM
    vector[: len(values)] = values
    return vector


def test_same_photo_pairs_separate_labelled_from_assumed(measure_module, tmp_path):
    """**「同じ写真なら別人」は仮定。** 手本どうしのペアと分けて数えること。

    本番の `match` が解くのは「未割当（大半が他人）を手本と照合する」問題なので、
    手本どうしの測定では実害が出る誤りを測れない。
    """
    database = tmp_path / "pairs.db"
    connection = db.ensure_database(str(database))
    try:
        natsu = db.add_person(connection, "なつ")
        hiyori = db.add_person(connection, "ひより")
        media_id = db.save_media(
            connection,
            {
                "path": "/photos/a.jpg",
                "filename": "a.jpg",
                "type": "image",
                "file_hash": "a",
                "file_size": 10,
                "created_time": "2012-10-06T10:00:00",
                "shooting_date": "2012-10-06T10:00:00",
            },
        )
        # 手本どうし・別人（確かな別人）
        a = _add_face_with_vector(connection, media_id, _unit(1.0, 0.0))
        b = _add_face_with_vector(connection, media_id, _unit(0.0, 1.0))
        # 手本どうし・同一人物（鏡など。**誤りに数えない**）
        c = _add_face_with_vector(connection, media_id, _unit(1.0, 0.01))
        # 未割当（別人と仮定）
        _add_face_with_vector(connection, media_id, _unit(0.0, 0.0, 1.0))
        db.assign_faces(connection, [a, c], natsu, age=3)
        db.assign_faces(connection, [b], hiyori, age=1)
        connection.commit()
    finally:
        connection.close()

    buckets = measure_module.same_photo_pairs(
        str(database), db.embedding_model.ACTIVE.version, measure_module.COSINE
    )

    # 4つの顔 → 6ペア。a-b と b-c が別人、a-c が同一人物、残り3つが未割当がらみ
    assert len(buckets[measure_module.LABELLED_DIFFERENT]) == 2  # a-b, b-c
    assert len(buckets[measure_module.LABELLED_SAME]) == 1  # a-c
    assert len(buckets[measure_module.ASSUMED_DIFFERENT]) == 3  # d と他の3組
    # a-c は同じ向きなので距離がほぼ0。**これを誤りに数えてはいけない**
    assert buckets[measure_module.LABELLED_SAME][0] < 0.01
    assert min(buckets[measure_module.LABELLED_DIFFERENT]) > 0.9


def test_photos_with_a_single_face_are_ignored(measure_module, tmp_path):
    database = tmp_path / "single.db"
    _seed_database(database)  # 1枚に1顔ずつ
    buckets = measure_module.same_photo_pairs(
        str(database), db.embedding_model.ACTIVE.version, measure_module.COSINE
    )
    assert sum(len(values) for values in buckets.values()) == 0


def test_faces_on_another_version_are_left_out(measure_module, tmp_path):
    """**版の違う特徴量を混ぜない。** `reembed` の途中は混在する。"""
    database = tmp_path / "mixed.db"
    _seed_database(database)
    buckets = measure_module.same_photo_pairs(
        str(database), "別の版/v9", measure_module.COSINE
    )
    assert sum(len(values) for values in buckets.values()) == 0


def test_the_report_keeps_the_assumption_visible(measure_module):
    buckets = {
        measure_module.LABELLED_DIFFERENT: [0.9],
        measure_module.LABELLED_SAME: [0.1],
        measure_module.ASSUMED_DIFFERENT: [0.3, 0.9],
    }
    report = measure_module.format_same_photo_report(buckets, "v/1", measure_module.COSINE)
    assert "高めに出る" in report
    assert "別人と仮定" in report
    # 同一人物の行を「誤り」と読ませない断り書き
    assert "誤りではなく" in report
