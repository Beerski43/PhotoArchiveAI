"""フォルダ単位の絞り込みと集計（Issue #55）。

**未割当 58,212 件のうち、上位100フォルダで 51.5% を占める**（2026-09-20 実測）。
結婚式や学校行事のフォルダはほとんどが他人なので、フォルダごとにまとめて
除外できると、1件ずつの判断がそのぶん減る。
"""

from pathlib import Path

import pytest

from photoarchive_ai import db


@pytest.fixture()
def connection(tmp_path: Path):
    connection = db.connect(str(tmp_path / "folders.db"))
    db.create_tables(connection)
    yield connection
    connection.close()


def _add_media(connection, path: str, shooting_date="2012-10-06T10:00:00") -> int:
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": Path(path).name,
            "type": "image",
            "file_hash": path,
            "file_size": 100,
            "created_time": "2012-10-06T10:00:00",
            "shooting_date": shooting_date,
        },
    )


def _add_face(connection, media_id: int, **kwargs) -> int:
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * 128,
        embed_version="test",
        thumbnail=b"",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# フォルダの取り出し方が、SQL と Python で一致していること
# ---------------------------------------------------------------------------

#: **片方だけ直すと絞り込みが黙って外れる。** 表示と比較は Python 側
#: (`db.folder_of`)、絞り込みは SQL 側 (`db.folder_expression`) で要るため、
#: 同じパスに同じ答えを返すことを固定しておく。
FOLDER_SAMPLES = [
    "/photos/2012/121006運動会/DSC_0001.JPG",
    "/photos/a.jpg",
    "a.jpg",
    "relative/dir/a.jpg",
    "/photos/2011/110416結婚式/式場/data/IMG.JPG",
    "/photos/ドット.混じり/a.b.c.jpg",
]


def test_folder_expression_and_folder_of_agree(connection):
    """SQL の式と Python の関数が、同じパスに同じフォルダを返す。"""
    for index, path in enumerate(FOLDER_SAMPLES):
        _add_media(connection, path)
    rows = connection.execute(
        f"SELECT path, {db.folder_expression('path')} FROM Media"
    ).fetchall()
    assert rows
    for path, folder in rows:
        assert folder == db.folder_of(path), path


def test_folder_of_handles_paths_without_a_folder():
    assert db.folder_of("/photos/2012/a.jpg") == "/photos/2012"
    # フォルダを持たないパス。**"" を返す**（"." や "/" にしない）。
    assert db.folder_of("a.jpg") == ""
    assert db.folder_of("/a.jpg") == ""


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def test_folder_face_counts_separates_unassigned_manual_and_rejected(connection):
    """未割当・手本・除外を別々に数える。

    **手本の件数が見えないと、まとめて除外を押せない。** 結婚式のフォルダは
    「ほぼ他人」であって「全部他人」ではなく、家族も写っている
    （実データの2位のフォルダに手本が4件ある）。
    """
    person_id = db.add_person(connection, name="なつ")
    wedding = _add_media(connection, "/photos/2007/wedding/a.jpg")
    home = _add_media(connection, "/photos/2007/home/b.jpg")
    for _ in range(3):
        _add_face(connection, wedding)
    manual = _add_face(connection, wedding)
    rejected = _add_face(connection, wedding)
    _add_face(connection, home)
    db.assign_faces(connection, [manual], person_id, age=3)
    db.reject_faces(connection, [rejected])

    counts = {row["folder"]: row for row in db.folder_face_counts(connection)}
    assert set(counts) == {"/photos/2007/wedding", "/photos/2007/home"}
    wedding_counts = counts["/photos/2007/wedding"]
    assert wedding_counts["unassigned"] == 3
    assert wedding_counts["manual"] == 1
    assert wedding_counts["rejected"] == 1
    assert wedding_counts["total"] == 5


def test_folder_face_counts_are_sorted_by_unassigned_desc(connection):
    """**未割当の多い順。** 効き目の大きいフォルダが上に来る。"""
    small = _add_media(connection, "/photos/small/a.jpg")
    big = _add_media(connection, "/photos/big/b.jpg")
    _add_face(connection, small)
    for _ in range(4):
        _add_face(connection, big)

    folders = [row["folder"] for row in db.folder_face_counts(connection)]
    assert folders == ["/photos/big", "/photos/small"]


def test_folder_face_counts_ignores_media_without_faces(connection):
    """顔の無いメディアだけのフォルダは出さない。選んでも何もできない。"""
    _add_media(connection, "/photos/empty/a.jpg")
    with_face = _add_media(connection, "/photos/withface/b.jpg")
    _add_face(connection, with_face)

    assert [row["folder"] for row in db.folder_face_counts(connection)] == [
        "/photos/withface"
    ]


# ---------------------------------------------------------------------------
# 絞り込み
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", [db.ORDER_QUALITY, db.ORDER_SHOT_DESC, db.ORDER_AGE])
def test_list_faces_filters_by_folder_in_every_order(connection, order):
    """**並び順の経路が2つある。** どちらでも同じように絞り込めること。

    撮影日時順だけ `Media` と結合する別の問い合わせを通るので、片方にしか
    条件が足されていないと、並び替えただけで絞り込みが外れる。
    """
    wedding = _add_media(connection, "/photos/wedding/a.jpg")
    home = _add_media(connection, "/photos/home/b.jpg")
    wedding_faces = [_add_face(connection, wedding) for _ in range(2)]
    _add_face(connection, home)

    records = db.list_faces(connection, unassigned=True, folder="/photos/wedding", order=order)
    assert sorted(record["id"] for record in records) == sorted(wedding_faces)
    assert db.count_faces(connection, unassigned=True, folder="/photos/wedding") == 2
    assert db.count_faces(connection, unassigned=True) == 3


def test_folder_filter_does_not_match_subfolders(connection):
    """**入れ子は別のフォルダとして扱う。** 前方一致にしない。

    実データの `2011/110416雄司明日美結婚式` は `式場/data` と `suzuki` に
    分かれていて、片方だけ除外したい場合がある。
    """
    parent = _add_media(connection, "/photos/wedding/a.jpg")
    child = _add_media(connection, "/photos/wedding/hall/b.jpg")
    parent_face = _add_face(connection, parent)
    _add_face(connection, child)

    records = db.list_faces(connection, unassigned=True, folder="/photos/wedding")
    assert [record["id"] for record in records] == [parent_face]


def test_face_ids_returns_every_match_beyond_one_page(connection):
    """**まとめて処理はページをまたぐ。** `list_faces` のページ単位では届かない。"""
    media_id = _add_media(connection, "/photos/wedding/a.jpg")
    expected = [_add_face(connection, media_id) for _ in range(250)]

    assert db.face_ids(connection, unassigned=True, folder="/photos/wedding") == expected
    # 1ページ分しか読まない `list_faces` と対比しておく。
    page = db.list_faces(
        connection, unassigned=True, folder="/photos/wedding", limit=200, offset=0
    )
    assert len(page) == 200


def test_face_ids_for_unassigned_leaves_manual_faces_alone(connection):
    """**手本を巻き込まない。** これがフォルダ一括除外でいちばん大事な一線。"""
    person_id = db.add_person(connection, name="なつ")
    media_id = _add_media(connection, "/photos/wedding/a.jpg")
    manual = _add_face(connection, media_id)
    unassigned = [_add_face(connection, media_id) for _ in range(3)]
    db.assign_faces(connection, [manual], person_id, age=3)

    targets = db.face_ids(connection, unassigned=True, folder="/photos/wedding")
    assert targets == unassigned

    db.reject_faces(connection, targets)
    kept = db.get_face(connection, manual)
    assert kept["assign_source"] == db.ASSIGN_MANUAL
    assert kept["person_id"] == person_id
    assert kept["age"] == 3
    assert db.count_faces(connection, assign_source=db.ASSIGN_REJECTED) == 3


def test_the_folder_filter_walks_its_index(connection):
    """**`idx_media_folder` を使うこと。** 使わないと Media を全件走査する。

    実データ（Media 70,297 件）で 1ページの読み出しが 0.407秒 → 0.017秒。
    `folder_expression` の式を書き写して綴りがずれると、索引は黙って使われなく
    なり、この検査だけが気づける。
    """
    _add_media(connection, "/photos/wedding/a.jpg")
    where, params = db._face_filter(None, None, True, None, None, folder="/photos/wedding")
    plan = "\n".join(
        row[-1]
        for row in connection.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM Face{where}", params
        )
    )
    assert "idx_media_folder" in plan


def test_face_filter_without_folder_keeps_the_previous_query(connection):
    """**フォルダ未指定のときは問い合わせを変えない。**

    `match` と `evaluate` も `list_faces` を通る。条件が1つ増えるだけで、
    58,547 件の未割当を撮影日時順に読む経路が索引から外れる（実測 0.002 秒 →
    0.199 秒）。
    """
    where, params = db._face_filter(None, None, True, None, None)
    assert "Media" not in where
    assert params == []

    with_folder, folder_params = db._face_filter(None, None, True, None, None, folder="/photos")
    assert "SELECT id FROM Media" in with_folder
    assert folder_params == ["/photos"]
