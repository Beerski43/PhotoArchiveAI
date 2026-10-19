"""写真の良し悪しモデル（IQA）を試すスクリプト（Issue #105）。

**採るモデルは、このスクリプトの順位と見比べ画像を見て利用者が決めた。** 並べ方や
相関がおかしいと、間違ったモデルを選ぶことになる。PyTorch と pyiqa は要らない
（モデルを読む部分は測るときだけ import する）。
"""

import importlib.util
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image

from photoarchive_ai import db

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_iqa.py"


@pytest.fixture(scope="module")
def measure_module():
    spec = importlib.util.spec_from_file_location("measure_iqa", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_the_script_does_not_import_torch_at_load_time():
    """アプリの仮想環境（PyTorch なし）でも読み込めること。"""
    already = {name for name in ("torch", "pyiqa") if name in sys.modules}
    spec = importlib.util.spec_from_file_location("measure_iqa_load_check", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {name for name in ("torch", "pyiqa") if name in sys.modules} == already
    assert callable(module.measure)


def test_spearman_is_one_for_the_same_order_and_minus_one_for_reversed(measure_module):
    a = {"1": 1.0, "2": 2.0, "3": 3.0, "4": 4.0}
    assert measure_module.spearman(a, {"1": 10, "2": 20, "3": 30, "4": 40}) == pytest.approx(1.0)
    assert measure_module.spearman(a, {"1": 4, "2": 3, "3": 2, "4": 1}) == pytest.approx(-1.0)


def test_spearman_only_compares_photos_measured_by_both(measure_module):
    """片方で測れなかった写真を混ぜない（TOPIQ IAA のように途中で止まるモデルがある）。"""
    a = {"1": 1.0, "2": 2.0, "3": 3.0, "9": 100.0}
    b = {"1": 1.0, "2": 2.0, "3": 3.0}
    assert measure_module.spearman(a, b) == pytest.approx(1.0)
    assert measure_module.spearman(a, {"1": 1.0}) is None
    assert measure_module.spearman(a, {"1": 5.0, "2": 5.0, "3": 5.0}) is None


def test_ties_share_the_average_rank(measure_module):
    assert measure_module.ranks([3.0, 1.0, 3.0, 2.0]) == [3.5, 1.0, 3.5, 2.0]


def test_best_first_respects_models_where_lower_is_better(measure_module):
    scores = {"1": 0.2, "2": 0.9, "3": 0.5}
    assert measure_module.best_first(scores) == ["2", "3", "1"]
    assert measure_module.best_first(scores, lower_better=True) == ["1", "3", "2"]


def test_disagreements_put_the_largest_rank_gap_first(measure_module):
    first = ["a", "b", "c", "d"]   # a が最上位
    second = ["d", "c", "b", "a"]  # a が最下位
    high_low, low_high = measure_module.disagreements(first, second, 2)
    assert high_low == ["a", "b"]
    assert low_high == ["d", "c"]


def test_the_sample_is_family_photos_only_and_repeatable(tmp_path, measure_module):
    """手本か自動割り当ての顔がある画像だけ（未分類・動画は外す）。2回呼べば同じ写真を選ぶ。"""
    path = tmp_path / "photoarchive.db"
    connection = db.ensure_database(str(path))
    try:
        person = db.add_person(connection, "${PERSON_1}")
        rows = [
            ("/p/a.jpg", "image", db.ASSIGN_MANUAL),
            ("/p/b.jpg", "image", db.ASSIGN_AUTO),
            ("/p/c.jpg", "image", None),  # 未分類だけ
            ("/p/e.mp4", "video", db.ASSIGN_MANUAL),  # 動画は測らない
        ]
        expected = set()
        for media_path, media_type, source in rows:
            media_id = db.save_media(connection, {
                "path": media_path, "filename": Path(media_path).name, "type": media_type,
                "file_hash": media_path, "file_size": 100, "created_time": "2020-01-01T00:00:00",
            })
            db.add_face(
                connection, media_id=media_id, bbox=(0, 10, 10, 0),
                embedding=[0.0] * db.EMBEDDING_DIM, embed_version=db.embedding_model.ACTIVE.version,
                person_id=person if source else None, assign_source=source,
            )
            if media_type == "image" and source:
                expected.add(media_id)
        connection.commit()
    finally:
        connection.close()

    read_only = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        chosen = measure_module.family_photo_ids(read_only, 10)
        assert set(chosen) == expected
        assert measure_module.family_photo_ids(read_only, 10) == chosen
        assert len(measure_module.family_photo_ids(read_only, 1)) == 1
    finally:
        read_only.close()


def test_the_sample_does_not_depend_on_the_order_sqlite_returns(measure_module):
    """SQLite の返す順に依存しない。索引の無い表は挿入順で返すので、逆順に入れて確かめる。

    実データの DB は ``Face.media_id`` に索引があり ID 順で返すので、``ORDER BY`` を外しても
    上のテストは通ってしまう（PR #109 の指摘2）。
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE Media (id INTEGER PRIMARY KEY, type TEXT);"
        "CREATE TABLE Face (id INTEGER PRIMARY KEY, media_id INTEGER, assign_source TEXT);"
    )
    for media_id in range(1, 21):
        connection.execute("INSERT INTO Media VALUES (?, 'image')", (media_id,))
    for media_id in range(20, 0, -1):
        connection.execute("INSERT INTO Face (media_id, assign_source) VALUES (?, 'manual')", (media_id,))
    expected = list(range(1, 21))
    random.Random(0).shuffle(expected)
    assert measure_module.family_photo_ids(connection, 20) == expected


def test_sheets_are_written_for_each_model_and_each_pair(tmp_path, measure_module):
    photos = {}
    for key in ("1", "2", "3"):
        photo = tmp_path / f"{key}.jpg"
        Image.new("RGB", (64, 48), (int(key) * 60, 0, 0)).save(photo)
        photos[key] = photo
    photos["4"] = tmp_path / "missing.jpg"  # 読めない写真があっても止めない
    result = {
        "models": {"nima": {"lower_better": False}, "musiq": {"lower_better": False}},
        "scores": {
            "nima": {"1": 5.0, "2": 4.0, "3": 3.0, "4": 1.0},
            "musiq": {"1": 30.0, "2": 50.0, "3": 70.0, "4": 10.0},
        },
    }
    measure_module.write_sheets(result, photos, tmp_path)

    names = sorted(p.name for p in (tmp_path / "sheets").iterdir())
    assert names == sorted([
        "nima_top.jpg", "nima_bottom.jpg", "musiq_top.jpg", "musiq_bottom.jpg",
        "nima_high_musiq_low.jpg", "musiq_high_nima_low.jpg",
    ])
    json.dumps(result)  # 結果は JSON に書ける形のまま
