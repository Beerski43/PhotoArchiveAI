"""写真全体の良し悪し（#106）。`quality.py`・`PhotoQuality`・`photoarchive assess`・点の式。

**前処理は pyiqa（PyTorch）の処理を numpy に写したもの。** 正解値は PyTorch で作って埋め込んだ
（`F.interpolate(mode='bicubic', align_corners=False)` と pyiqa の位置の番号）。写真の点まで
一致することは `scripts/export_iqa_onnx.py` が実物のモデルで確かめる（ここでは PyTorch を使わない）。
"""

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import db, quality, scoring
from photoarchive_ai.migration import migrate_database, needs_migration


# ---------------------------------------------------------------------------
# 前処理
# ---------------------------------------------------------------------------


def test_cubic_resize_matches_pytorch_when_shrinking():
    """PyTorch の bicubic（平滑化なし・端は複製）と同じ値。MUSIQ はこれで学習している。"""
    image = (np.arange(35, dtype=np.float32).reshape(1, 5, 7) ** 1.5) / 10
    expected = [0.44622, 0.88436, 1.41635, 2.1262, 5.40521, 6.49962, 7.53113, 8.74812,
                13.65215, 15.12068, 16.47178, 18.0338]
    assert quality.resize_cubic(image, 3, 4).ravel() == pytest.approx(expected, abs=1e-4)


def test_cubic_resize_matches_pytorch_when_enlarging():
    image = np.array([[0.0, 1.0, 4.0], [2.0, 3.0, 9.0]], dtype=np.float32)[None]
    expected = [-0.26961, 0.01714, 0.82639, 2.57502, 3.82898, 0.904, 1.064, 2.0, 4.826, 6.932,
                2.07761, 2.11086, 3.17361, 7.07699, 10.03503]
    assert quality.resize_cubic(image, 3, 5).ravel() == pytest.approx(expected, abs=1e-4)


def test_hashed_positions_match_pyiqa():
    """位置の番号を grid × grid に畳む（pyiqa `get_hashed_spatial_pos_emb_index`）。"""
    assert quality.hashed_positions(10, 3, 4).tolist() == [0, 2, 5, 7, 30, 32, 35, 37, 60, 62, 65, 67]
    assert quality.hashed_positions(10, 13, 12).tolist()[:14] == [0, 0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9, 0, 0]


def test_musiq_input_has_two_padded_scales_and_every_original_patch():
    """224 は 7×7=49・384 は 12×12=144 に埋め、元の解像度は全パッチ（100×37 なら 4×2）。"""
    tokens = quality.musiq_input(np.full((100, 37, 3), 0.5, dtype=np.float32))
    assert tokens.shape == (1, 49 + 144 + 8, 32 * 32 * 3 + 3)
    scale, mask = tokens[0, :, -2], tokens[0, :, -1]
    # 縮めた 224 は 224×83 → 7×3=21 パッチ。残りは 0 で埋める（マスクも 0）
    assert mask[:49].sum() == 21 and scale[:21].tolist() == [0] * 21
    # 384 は 384×142 → 12×5=60 パッチ
    assert mask[49:193].sum() == 60 and set(scale[49:49 + 60].tolist()) == {1}
    assert mask[193:].tolist() == [1] * 8 and set(scale[193:].tolist()) == {2}
    # 画素は -1〜1 に直す（0.5 は 0）
    assert np.abs(tokens[0, 193:, :-3]).max() == pytest.approx(0.0, abs=1e-6)


def test_nima_input_is_the_centre_square_of_299():
    """短辺を 299 に縮め、中央を切り出す。左右の帯は落ちる。"""
    rgb = np.zeros((300, 600, 3), dtype=np.float32)
    rgb[:, 150:450] = 1.0  # 中央の帯だけ白
    pixels = quality.nima_input(rgb)
    assert pixels.shape == (1, 3, 299, 299)
    assert pixels[0, :, :, 10:-10].min() > 0.99


def test_clipping_counts_blown_and_crushed_pixels():
    rgb = np.full((10, 10, 3), 0.5, dtype=np.float32)
    rgb[:2] = 1.0
    rgb[2:5] = 0.0
    highlight, shadow = quality.clipping(rgb)
    assert (highlight, shadow) == (pytest.approx(0.2), pytest.approx(0.3))


def test_the_size_is_taken_before_the_exif_rotation(tmp_path):
    """**顔の矩形は EXIF の向きを直さない座標**（`face.read_rgb`）。見切れを測る大きさもそれに合わせる。"""
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # 90 度回して見る
    Image.new("RGB", (400, 200), "white").save(path, exif=exif)
    image, size = quality.load_photo(path)
    assert size == (400, 200)
    assert image.size == (200, 400)  # モデルには向きを直して渡す


def test_the_models_are_required_with_how_to_make_them(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTOARCHIVE_ONNX_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr("photoarchive_ai.face.find_model_file", lambda name: None)
    with pytest.raises(FileNotFoundError, match="export_iqa_onnx.py"):
        quality.Models.load()


# ---------------------------------------------------------------------------
# DB・assess
# ---------------------------------------------------------------------------


class FakeModels:
    """点をファイル名の数から作る（`a75.jpg` なら画質 75）。呼ばれた回数を数える。"""

    def __init__(self):
        self.calls = 0

    def technical(self, rgb):
        self.calls += 1
        return float(rgb.shape[1])  # 幅（ファイルごとに変えてある）

    def aesthetic(self, rgb):
        return 5.0


@pytest.fixture
def connection(tmp_path):
    conn = db.ensure_database(str(tmp_path / "quality.db"))
    yield conn
    conn.close()


def _photo(connection, tmp_path, name, *, width=40, source=db.ASSIGN_MANUAL, write=True, file_hash=None):
    path = tmp_path / name
    if write:
        Image.new("RGB", (width, 30), (120, 120, 120)).save(path)
    media_id = db.save_media(connection, {
        "path": str(path), "filename": name, "type": "image", "file_hash": file_hash or name,
        "file_size": 1, "created_time": "2020-01-01T00:00:00",
    })
    person = connection.execute("SELECT id FROM Person").fetchone()
    person_id = person[0] if person else db.add_person(connection, "${PERSON_1}")
    db.add_face(
        connection, media_id=media_id, bbox=(5, 20, 20, 5), embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id if source else None, assign_source=source,
    )
    connection.commit()
    return media_id


def test_assess_measures_only_family_photos_and_only_once(connection, tmp_path):
    family = _photo(connection, tmp_path, "family.jpg", width=41)
    _photo(connection, tmp_path, "stranger.jpg", source=None)
    models = FakeModels()

    first = quality.assess(connection, models)
    assert first == {"total": 1, "measured": 1, "unreadable": 0}
    stored = db.photo_quality(connection, quality.MODEL_VERSION)
    assert list(stored) == [family]
    assert stored[family]["technical"] == 41.0
    assert (stored[family]["width"], stored[family]["height"]) == (41, 30)

    assert quality.assess(connection, models)["total"] == 0
    assert models.calls == 1


def test_assess_continues_where_it_stopped(connection, tmp_path, monkeypatch):
    """塊ごとに書くので、途中で止めても続きから測る（一晩かかるため）。"""
    monkeypatch.setattr(quality, "CHUNK_SIZE", 2)
    ids = [_photo(connection, tmp_path, f"p{i}.jpg", width=40 + i) for i in range(5)]
    models = FakeModels()
    real_measure = quality.measure
    count = {"n": 0}

    def stop_after_three(models_, image, size):
        count["n"] += 1
        if count["n"] == 4:
            raise KeyboardInterrupt
        return real_measure(models_, image, size)

    monkeypatch.setattr(quality, "measure", stop_after_three)
    with pytest.raises(KeyboardInterrupt):
        quality.assess(connection, models)
    assert sorted(db.photo_quality(connection, quality.MODEL_VERSION)) == ids[:2]

    monkeypatch.setattr(quality, "measure", real_measure)
    assert quality.assess(connection, models)["total"] == 3
    assert sorted(db.photo_quality(connection, quality.MODEL_VERSION)) == ids


def test_an_unreadable_photo_is_recorded_and_not_read_again(connection, tmp_path):
    _photo(connection, tmp_path, "missing.jpg", write=False)
    assert quality.assess(connection, FakeModels()) == {"total": 1, "measured": 0, "unreadable": 1}
    assert db.photo_quality(connection, quality.MODEL_VERSION) == {}
    assert quality.assess(connection, FakeModels())["total"] == 0


def test_a_new_model_version_measures_everything_again(connection, tmp_path, monkeypatch):
    _photo(connection, tmp_path, "a.jpg")
    quality.assess(connection, FakeModels())
    monkeypatch.setattr(quality, "MODEL_VERSION", "musiq-koniq/2+nima-ava/2")
    assert quality.assess(connection, FakeModels())["total"] == 1


def test_a_changed_file_loses_its_old_measurement(connection, tmp_path):
    """**中身から測った値なので、ハッシュが変わったら消す**（`look_hash` と同じ）。"""
    media_id = _photo(connection, tmp_path, "a.jpg")
    quality.assess(connection, FakeModels())
    db.save_media(connection, {
        "path": str(tmp_path / "a.jpg"), "filename": "a.jpg", "type": "image",
        "file_hash": "changed", "file_size": 1, "created_time": "2020-01-01T00:00:00",
    })
    assert media_id not in db.photo_quality(connection, quality.MODEL_VERSION)
    # 属性だけ変わったとき（ハッシュは同じ）は消さない
    other = _photo(connection, tmp_path, "b.jpg")
    quality.assess(connection, FakeModels())
    db.save_media(connection, {
        "path": str(tmp_path / "b.jpg"), "filename": "b.jpg", "type": "image",
        "file_hash": "b.jpg", "file_size": 2, "created_time": "2020-01-02T00:00:00",
    })
    assert other in db.photo_quality(connection, quality.MODEL_VERSION)


def test_deleting_the_media_deletes_its_measurement(connection, tmp_path):
    media_id = _photo(connection, tmp_path, "a.jpg")
    quality.assess(connection, FakeModels())
    db.delete_media(connection, [media_id])
    assert connection.execute("SELECT COUNT(*) FROM PhotoQuality").fetchone()[0] == 0


def test_a_version_10_database_gains_the_table_and_keeps_its_faces(tmp_path):
    """テーブルを作るだけ。**手本も見え方も消えない**（CLAUDE.md §4）。"""
    database = tmp_path / "v10.db"
    connection = db.ensure_database(str(database))
    media_id = _photo(connection, tmp_path, "a.jpg")
    connection.execute("UPDATE Face SET aligned = 1, sharpness = 80.0")
    connection.execute("DROP TABLE PhotoQuality")
    connection.execute("PRAGMA user_version = 10")
    connection.commit()
    connection.close()
    assert needs_migration(str(database))

    migrate_database(str(database), make_backup=False)

    connection = db.ensure_database(str(database))
    try:
        assert db.get_schema_version(connection) == db.SCHEMA_VERSION
        row = connection.execute(
            "SELECT media_id, assign_source, aligned, sharpness FROM Face"
        ).fetchone()
        assert tuple(row) == (media_id, db.ASSIGN_MANUAL, 1, 80.0)
        assert connection.execute("SELECT COUNT(*) FROM PhotoQuality").fetchone()[0] == 0
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 点
# ---------------------------------------------------------------------------


def _row(left, top, right, bottom):
    return {"bbox_left": left, "bbox_top": top, "bbox_right": right, "bbox_bottom": bottom}


def test_a_face_touching_the_edge_is_cut_off():
    assert scoring.is_cut_off(_row(0, 50, 100, 150), 1000, 800)
    assert scoring.is_cut_off(_row(500, 50, 1000, 150), 1000, 800)
    assert not scoring.is_cut_off(_row(100, 100, 200, 200), 1000, 800)
    assert not scoring.is_cut_off(_row(0, 0, 10, 10), None, None)  # 大きさが分からなければ切れていない
    faces = [_row(0, 50, 100, 150), _row(100, 100, 200, 200)]
    assert scoring.cutoff_level(faces, {"width": 1000, "height": 800}) == 0.5


def test_unmeasured_photos_are_neither_good_nor_bad():
    """未計測は、画質・美しさは真ん中（`UNKNOWN`）、欠点は引かない（分からない欠点で引かない）。"""
    weights = scoring.Weights.default()._replace(technical=1, aesthetic=1, highlights=1, shadows=1, cutoff=1)
    parts = scoring.photo_parts([_row(0, 0, 10, 10)], None, weights)
    assert parts == {"technical": 50.0, "aesthetic": 50.0, "highlights": 0.0, "shadows": 0.0, "cutoff": 0.0}


def test_zero_photo_weights_give_the_score_from_before():
    rows = [dict(_row(0, 0, 10, 10), media_id=1, person_id=1, aligned=1, yaw=0.0, sharpness=200.0,
                 eye_blink=0.0, mouth_smile=0.3)]
    measured = {1: {"technical": 20.0, "aesthetic": 3.0, "highlight_clip": 0.5, "shadow_clip": 0.5,
                    "width": 100, "height": 100}}
    off = scoring.Weights.default()._replace(technical=0, aesthetic=0, highlights=0, shadows=0, cutoff=0)
    assert scoring.family_photo_scores(rows, weights=off, quality=measured) == scoring.family_photo_scores(
        rows, weights=off
    )


def test_the_default_weights_are_the_ones_the_user_chose():
    """既定は画質 0.3・美しさ 0.1、白飛び・黒つぶれ・見切れは 0（2026-10-16・利用者の決定）。"""
    weights = scoring.Weights.default()
    assert (weights.technical, weights.aesthetic) == (0.3, 0.1)
    assert (weights.highlights, weights.shadows, weights.cutoff) == (0.0, 0.0, 0.0)


def test_the_breakdown_adds_up_to_the_score_with_photo_weights():
    rows = [dict(_row(0, 0, 10, 10), media_id=1, person_id=1, aligned=1, yaw=0.0, sharpness=200.0,
                 eye_blink=0.0, mouth_smile=0.3)]
    measured = {"technical": 60.0, "aesthetic": 5.0, "highlight_clip": 0.1, "shadow_clip": 0.05,
                "width": 100, "height": 100}
    weights = scoring.Weights.default()._replace(technical=0.3, aesthetic=0.2, highlights=0.4, shadows=0.4, cutoff=0.1)
    parts = scoring.photo_breakdown(rows, 0.0, weights, measured)
    score = scoring.family_photo_scores(rows, weights=weights, quality={1: measured})[1]
    assert parts["total"] == pytest.approx(score)
    assert parts["highlights"] == pytest.approx(-100 * 0.4 * 0.1 / scoring.CLIP_FULL)
    assert parts["cutoff"] == pytest.approx(-10.0)  # 顔が角に接している


@pytest.mark.models
def test_the_real_models_give_scores_in_their_ranges():
    """実物の ONNX（`scripts/export_iqa_onnx.py` で書き出したもの）。無ければ飛ばす（環境依存・合否に含めない）。"""
    from photoarchive_ai import face

    if face.find_model_file(quality.MUSIQ_FILE) is None or face.find_model_file(quality.NIMA_FILE) is None:
        pytest.skip("IQA models are not exported")
    models = quality.Models.load()
    rgb = np.random.default_rng(0).random((240, 320, 3)).astype(np.float32)
    assert 0.0 <= models.technical(rgb) <= 100.0
    assert 1.0 <= models.aesthetic(rgb) <= 10.0


def test_match_writes_the_same_score_as_select_with_the_photo_quality(connection, tmp_path):
    """`match` の `family_score` も `PhotoQuality` を読む（`select` と同じ点・#106）。"""
    from photoarchive_ai import selection

    media_id = _photo(connection, tmp_path, "a.jpg")
    db.save_photo_quality(connection, [(media_id, (20.0, 3.0, 0.5, 0.5, 40, 30))], quality.MODEL_VERSION)
    db.recompute_family_scores(connection)
    stored = db.get_analysis_result(connection, media_id)["family_score"]
    scores, _ = selection.family_scores(connection)
    assert stored == pytest.approx(scores[media_id])
    faces = db.family_faces(connection)
    assert stored < scoring.family_photo_scores(faces)[media_id]  # 画質が悪いぶん、未計測より低い
