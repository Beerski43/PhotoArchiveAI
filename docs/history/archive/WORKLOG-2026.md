# 作業履歴 2026

`WORKLOG.md` から切り出したもの。新しいものが上。

## 2026-09-06 — #18 顔への年齢登録、#17 GUI の起動エラー

PR [#20](https://github.com/Beerski43/PhotoArchiveAI/pull/20) と
[#19](https://github.com/Beerski43/PhotoArchiveAI/pull/19)（ともに merged）。

子どもの成長を扱うため、顔に撮影時の年齢を持たせた。**未設定と0歳は別の状態**として
区別する。GUI の起動バグを直し、操作手順を
[operation/GUI_USAGE.md](../operation/GUI_USAGE.md) に書いた。

## 2026-09-06 — #14 HEIC/HEIF の一括変換と、#10 の真因特定

PR [#21](https://github.com/Beerski43/PhotoArchiveAI/pull/21)（merged）。

`photoarchive convert-heic` を追加した。同名JPEGが同じ写真ならスキップし、違う
写真なら連番を付ける。元のHEICは変更しない。

`face_recognition_models/__init__.py` の `pkg_resources` 依存が setuptools 81 以降で
`ModuleNotFoundError` になるのが #10 の真因だった。`importlib.util.find_spec` なら
`__init__.py` を実行せずモデルのパスだけ取り出せる。`face_recognition` パッケージ
自体は不要なので依存から外した。

あわせて、解析結果がすべて0になる問題を追い込んだ。MediaPipe の矩形は
`relative_bounding_box` に入るのに空の `bounding_box` を見ていたこと、
`mp.solutions` API のために 0.10.21 への固定が必要だったことが原因。

詳細: [details/2026-09-06-heic-and-analyze.md](details/2026-09-06-heic-and-analyze.md)

## 2026-09-05 — #7 進捗表示、#10 の一次対応

PR [#11](https://github.com/Beerski43/PhotoArchiveAI/pull/11) と
[#12](https://github.com/Beerski43/PhotoArchiveAI/pull/12)、
[#13](https://github.com/Beerski43/PhotoArchiveAI/pull/13)、
[#16](https://github.com/Beerski43/PhotoArchiveAI/pull/16)（すべて merged）。

CLI に進捗バーを追加した。ANSI のカーソル移動で2行を書き換える方式。
読み込めないファイルで処理全体が止まらないようにし、`--log-level` を足した。
顔検出を `face_recognition` から MediaPipe へ置き換えた。

## 2026-08-02 — #4 テスト環境

PR [#6](https://github.com/Beerski43/PhotoArchiveAI/pull/6)（merged）。
pytest を導入し、システムテストを追加した。

## 2026-08-01 — #1 仕様設計、#2 初版実装

PR [#3](https://github.com/Beerski43/PhotoArchiveAI/pull/3) と
[#5](https://github.com/Beerski43/PhotoArchiveAI/pull/5)（ともに merged）。
要件・仕様・DB設計を [spec/Specification.md](../spec/Specification.md) に起こし、
スキャン・解析・抽出の初版を実装した。
