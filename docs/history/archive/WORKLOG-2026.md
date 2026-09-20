# 作業履歴 2026

`WORKLOG.md` から切り出したもの。新しいものが上。

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
