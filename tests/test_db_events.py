"""行事（フォルダ×日）単位の絞り込みと集計（Issue #61）。

**取り下げた PR #56（フォルダ単位）から移してきたもの。** 束ねる単位が
フォルダから行事へ変わったので、日の条件を足して広げてある。

なぜフォルダだけでは粗いか: 同じフォルダでも日をまたぐと同一人物の距離が
開く（dlib で 0.480 → 0.578、ArcFace で 0.294 → 0.524）。
"""

from pathlib import Path

import pytest

from photoarchive_ai import db


@pytest.fixture()
def connection():
    """**メモリ上のDBで足りる。** この節は問い合わせだけを見る。

    ファイルに作ると1件ごとに DB と WAL を作ることになり、23件で 0.35 秒かかる
    （回帰テストは10秒以内に収める約束がある）。ファイルのパスが要るのは GUI の
    テストだけなので、そちらは `tmp_path` のまま。**リポジトリ内には何も書かない。**
    """
    connection = db.connect(":memory:")
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
        embedding=None,
        embed_version="test",
        thumbnail=b"",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 行事の取り出し方が、SQL と Python で一致していること
# ---------------------------------------------------------------------------

#: **片方だけ直すと絞り込みが黙って外れる。** 表示と比較は Python 側
#: (`db.folder_of`)、絞り込みは SQL 側 (`db.folder_expression`) で要るため、
#: 同じパスに同じ答えを返すことを固定しておく。
FOLDER_SAMPLES = [
    "/photos/2012/121006運動会/DSC_0001.JPG",
    "/photos/a.jpg",
    "a.jpg",
    "relative/dir/a.jpg",
    "/photos/2011/110416${EVENT}/式場/data/IMG.JPG",
    "/photos/ドット.混じり/a.b.c.jpg",
]


def test_folder_expression_and_folder_of_agree(connection):
    """SQL の式と Python の関数が、同じパスに同じフォルダを返す。"""
    for path in FOLDER_SAMPLES:
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


#: 壊れた撮影日時。**どれも「日が読めない」側へ落ちること。**
#: `"TTTT-TT-TTTTT:TT:TT"[:10]` は**10文字あるので長さでは弾けない。**
#: このリポジトリは同じ罠で2度壊れている（CLAUDE.md §8）。
BROKEN_DATES = ["TTTT-TT-TTTTT:TT:TT", "0000-00-00 00:00:00", "", None]


@pytest.mark.parametrize("shooting_date", BROKEN_DATES)
def test_day_expression_rejects_unreadable_dates(connection, shooting_date):
    media_id = _add_media(connection, "/photos/broken/a.jpg", shooting_date=shooting_date)
    row = connection.execute(
        f"SELECT {db.day_expression()} AS day FROM Media WHERE id = ?", (media_id,)
    ).fetchone()
    assert row["day"] is None


def test_day_expression_cuts_the_date_part(connection):
    media_id = _add_media(connection, "/photos/ok/a.jpg", shooting_date="2012-10-06T10:00:00")
    row = connection.execute(
        f"SELECT {db.day_expression()} AS day FROM Media WHERE id = ?", (media_id,)
    ).fetchone()
    assert row["day"] == "2012-10-06"


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def test_event_face_counts_splits_the_same_folder_by_day(connection):
    """**同じフォルダでも日が違えば別の行事。** これが PR #56 との違い。"""
    first = _add_media(connection, "/photos/trip/a.jpg", shooting_date="2012-10-06T09:00:00")
    second = _add_media(connection, "/photos/trip/b.jpg", shooting_date="2012-10-07T09:00:00")
    _add_face(connection, first)
    _add_face(connection, second)

    events = db.event_face_counts(connection)
    assert [(row["folder"], row["day"]) for row in events] == [
        ("/photos/trip", "2012-10-07"),
        ("/photos/trip", "2012-10-06"),
    ]


def test_event_face_counts_separates_unassigned_manual_and_rejected(connection):
    """未割当・手本・除外を別々に数える。

    **手本の件数が見えないと、まとめて除外を押せない。** 結婚式のフォルダは
    「ほぼ他人」であって「全部他人」ではなく、家族も写っている。
    """
    person_id = db.add_person(connection, name="${PERSON_2}")
    wedding = _add_media(connection, "/photos/2007/wedding/a.jpg")
    home = _add_media(connection, "/photos/2007/home/b.jpg")
    for _ in range(3):
        _add_face(connection, wedding)
    manual = _add_face(connection, wedding)
    rejected = _add_face(connection, wedding)
    _add_face(connection, home)
    db.assign_faces(connection, [manual], person_id, age=3)
    db.reject_faces(connection, [rejected])

    counts = {(row["folder"], row["day"]): row for row in db.event_face_counts(connection)}
    assert set(counts) == {
        ("/photos/2007/wedding", "2012-10-06"),
        ("/photos/2007/home", "2012-10-06"),
    }
    wedding_counts = counts[("/photos/2007/wedding", "2012-10-06")]
    assert wedding_counts["unassigned"] == 3
    assert wedding_counts["manual"] == 1
    assert wedding_counts["rejected"] == 1
    assert wedding_counts["total"] == 5


def test_event_face_counts_are_sorted_by_unassigned_desc(connection):
    """**未割当の多い順。** 効き目の大きい行事が上に来る。"""
    small = _add_media(connection, "/photos/small/a.jpg")
    big = _add_media(connection, "/photos/big/b.jpg")
    _add_face(connection, small)
    for _ in range(4):
        _add_face(connection, big)

    folders = [row["folder"] for row in db.event_face_counts(connection)]
    assert folders == ["/photos/big", "/photos/small"]


def test_event_face_counts_keeps_faces_without_a_readable_day(connection):
    """**撮影日時が読めない顔を落とさない。** 実データで 6,190 件ある。

    落とすと、一覧に出ない顔が未割当のまま永久に残る。``day`` を ``None`` に
    して「フォルダだけが同じ集まり」として見せる。
    """
    broken = _add_media(connection, "/photos/broken/a.jpg", shooting_date="TTTT-TT-TTTTT:TT:TT")
    _add_face(connection, broken)

    events = {(row["folder"], row["day"]): row for row in db.event_face_counts(connection)}
    assert events[("/photos/broken", None)]["unassigned"] == 1


def test_event_face_counts_ignores_media_without_faces(connection):
    """顔の無いメディアだけの行事は出さない。選んでも何もできない。"""
    _add_media(connection, "/photos/empty/a.jpg")
    with_face = _add_media(connection, "/photos/withface/b.jpg")
    _add_face(connection, with_face)

    assert [row["folder"] for row in db.event_face_counts(connection)] == ["/photos/withface"]


# ---------------------------------------------------------------------------
# 絞り込み
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", [db.ORDER_QUALITY, db.ORDER_SHOT_DESC, db.ORDER_AGE])
def test_list_faces_filters_by_event_in_every_order(connection, order):
    """**並び順の経路が2つある。** どちらでも同じように絞り込めること。

    撮影日時順だけ `Media` と結合する別の問い合わせを通るので、片方にしか
    条件が足されていないと、並び替えただけで絞り込みが外れる。
    """
    wedding = _add_media(connection, "/photos/wedding/a.jpg", shooting_date="2007-12-22T10:00:00")
    next_day = _add_media(connection, "/photos/wedding/b.jpg", shooting_date="2007-12-23T10:00:00")
    home = _add_media(connection, "/photos/home/c.jpg")
    wedding_faces = [_add_face(connection, wedding) for _ in range(2)]
    _add_face(connection, next_day)
    _add_face(connection, home)

    records = db.list_faces(
        connection, unassigned=True, folder="/photos/wedding", day="2007-12-22", order=order
    )
    assert sorted(record["id"] for record in records) == sorted(wedding_faces)
    assert (
        db.count_faces(connection, unassigned=True, folder="/photos/wedding", day="2007-12-22")
        == 2
    )
    # 日を指定しなければフォルダ全体（翌日のぶんも入る）。
    assert db.count_faces(connection, unassigned=True, folder="/photos/wedding") == 3
    assert db.count_faces(connection, unassigned=True) == 4


def test_day_none_and_undated_are_different_instructions(connection):
    """``day=None`` は「日で絞らない」、`db.UNDATED` は「読めない顔だけ」。

    **同じ値で表すと、片方を頼んだつもりでもう片方が起きる**（CLAUDE.md §8 の
    `KEEP_AGE` と同じ理由）。
    """
    good = _add_media(connection, "/photos/mix/a.jpg", shooting_date="2012-10-06T10:00:00")
    broken = _add_media(connection, "/photos/mix/b.jpg", shooting_date="TTTT-TT-TTTTT:TT:TT")
    good_face = _add_face(connection, good)
    broken_face = _add_face(connection, broken)

    both = db.face_ids(connection, unassigned=True, folder="/photos/mix")
    assert both == sorted([good_face, broken_face])
    undated = db.face_ids(connection, unassigned=True, folder="/photos/mix", day=db.UNDATED)
    assert undated == [broken_face]
    dated = db.face_ids(
        connection, unassigned=True, folder="/photos/mix", day="2012-10-06"
    )
    assert dated == [good_face]


def test_event_filter_does_not_match_subfolders(connection):
    """**入れ子は別のフォルダとして扱う。** 前方一致にしない。

    実データの `2011/110416雄司明日美結婚式` は `式場/data` と `${SURNAME}` に
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
    """**手本を巻き込まない。** これが行事の一括操作でいちばん大事な一線。"""
    person_id = db.add_person(connection, name="${PERSON_2}")
    media_id = _add_media(connection, "/photos/wedding/a.jpg")
    manual = _add_face(connection, media_id)
    unassigned = [_add_face(connection, media_id) for _ in range(3)]
    db.assign_faces(connection, [manual], person_id, age=3)

    targets = db.face_ids(
        connection, unassigned=True, folder="/photos/wedding", day="2012-10-06"
    )
    assert targets == unassigned

    db.reject_faces(connection, targets)
    kept = db.get_face(connection, manual)
    assert kept["assign_source"] == db.ASSIGN_MANUAL
    assert kept["person_id"] == person_id
    assert kept["age"] == 3
    assert db.count_faces(connection, assign_source=db.ASSIGN_REJECTED) == 3


def test_the_event_filter_walks_its_index(connection):
    """**`idx_media_event` を使うこと。** 使わないと Media を全件走査する。

    実データ（Media 70,297 件）で 1ページの読み出しが 0.407秒 → 0.017秒。
    `folder_expression` / `day_expression` の式を書き写して綴りがずれると、
    索引は黙って使われなくなり、この検査だけが気づける。
    """
    _add_media(connection, "/photos/wedding/a.jpg")
    where, params = db._face_filter(
        None, None, True, None, None, folder="/photos/wedding", day="2012-10-06"
    )
    plan = "\n".join(
        row[-1]
        for row in connection.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM Face{where}", params
        )
    )
    assert "idx_media_event" in plan


def test_face_filter_without_an_event_keeps_the_previous_query(connection):
    """**行事を指定しないときは問い合わせを変えない。**

    `match` と `evaluate` も `list_faces` を通る。条件が1つ増えるだけで、
    58,547 件の未割当を撮影日時順に読む経路が索引から外れる（実測 0.002 秒 →
    0.199 秒）。
    """
    where, params = db._face_filter(None, None, True, None, None)
    assert "Media" not in where
    assert params == []

    with_event, event_params = db._face_filter(
        None, None, True, None, None, folder="/photos", day="2012-10-06"
    )
    assert "SELECT id FROM Media" in with_event
    assert event_params == ["/photos", "2012-10-06"]


def test_load_faces_for_clustering_requires_a_day(connection):
    """**``day`` に既定値を置かない。** 1つの行事を束ねる関数なので、
    日で絞らない呼び出しは常に誤り。

    既定値があったせいで、束ねる画面が `None` をそのまま渡し、同じフォルダの
    別の日の顔まで束に入れていた（PR #62 のレビュー指摘1）。
    """
    with pytest.raises(TypeError):
        db.load_faces_for_clustering(connection, "/photos/mix")


def test_load_faces_for_clustering_separates_the_undated_faces(connection):
    """`UNDATED` を渡したら、日付の読めない顔だけを読むこと。"""
    broken = _add_media(connection, "/photos/mix/a.jpg", shooting_date="TTTT-TT-TTTTT:TT:TT")
    dated = _add_media(connection, "/photos/mix/b.jpg", shooting_date="2012-10-06T10:00:00")
    broken_face = db.add_face(
        connection,
        media_id=broken,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )
    db.add_face(
        connection,
        media_id=dated,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )

    records = db.load_faces_for_clustering(connection, "/photos/mix", db.UNDATED)
    assert [record["id"] for record in records] == [broken_face]
    both = db.load_faces_for_clustering(connection, "/photos/mix", None)
    assert len(both) == 2  # `None` は「日で絞らない」。だから既定値を置かない
