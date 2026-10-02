# 作業履歴 2026

`WORKLOG.md` から切り出したもの。新しいものが上。

## 2026-09-19 — #28 scan と analyze の処理順を転換し、自動紐づけを match へ分離

PR [#29](https://github.com/Beerski43/PhotoArchiveAI/pull/29)（merged）。

旧 `analyze` は **人物が1人も登録されていない段階で人物への紐づけを行っていた**。
`scan`（登録＋顔検出）→ GUI（人物登録＋手動割り当て）→ `match`（残りを自動紐づけ）
の順に組み替え、`analyze` を廃止した。あわせて、閾値判定が無く必ず誰かに紐づいて
いた欠陥（#22）、手本が2枚以上あると人物が壊れる剰余バグ、個人識別能力の無い
特徴量（FaceMesh の x 座標を128個並べただけ）を直した。特徴量は dlib ResNet の
128次元に置き換えた。DBスキーマは `user_version = 2`。移行は `photoarchive migrate`。

実データ 70,297件で移行と部分スキャンを確認済み。2回目のスキャンが 29.9秒 → 0.87秒
になり、差分スキャンの欠陥（#9）も解消した。既定の閾値は実測に基づき 0.5 → 0.4。

次にやること: GUI で人物ごとに20〜50枚を割り当てたうえで `match` の**取りこぼし率**を
測る（誤一致率しか測れていない）。全件スキャンは未実施。

詳細: [details/2026-09-19-issue28-rework.md](details/2026-09-19-issue28-rework.md)

## 2026-09-19 — #30 作業ルール・実装プラン・回帰テスト環境の整備と、既知の欠陥修正

リポジトリ全体の立て直し。`docs/` 直下を `spec/` `plan/` `history/` `testing/`
`operation/` へ整理し、作業ルールを `CLAUDE.md` に一本化した
（`.github/copilot-instructions.md` はポインタへ縮退）。

**素の `pytest` が収集エラーで落ちていた**（`pythonpath` 未設定）。3つの文書が
そろってこの落ちるコマンドを案内していた。`pyproject.toml` に
`pythonpath = ["."]` と `--strict-markers` を足して直した。
`scripts/run_regression.sh` を PR 起票前の必須手順にした。

`docs/plan/ROADMAP.md`（フェーズと Issue の対応）と
`docs/history/WORKLOG.md`（この文書）を新設し、CLAUDE.md から必ず参照させる
ようにした。WORKLOG は `scripts/archive_worklog.py` が直近20件だけを残し、
古いものを年ごとに `archive/` へ切り出す。

仕様書を v1.0 へ改訂した。各章に 実装済み / 一部実装 / 未実装 を明示し、
CLI リファレンス・スキーマ版と移行・特徴量の生成規約・非機能要件を新設。
画質評価は2要素しか実装していないこと、重複検出はハッシュ一致だけであること、
用途プリセットが未実装であることを、仕様の側で認めた。

**実データ運用を壊す欠陥を10件修正した。** 特に、更新時刻だけ変わった
ファイルが恒久的に再ハッシュされる問題（441GB を毎回読み直す）と、
dlib モデルが読めないと顔が無言で全損する問題は、全件スキャンの前に
潰す必要があった。テストは 40件 → 136件。

解消済みの #9 / #22 / #23 / #25 は、PR 本文の `Closes #N` でこの PR のマージ時に
閉じる。エージェントが Issue を直接クローズしない方針はそのままで、
「解消したものは PR 本文に任せる」とルールを足した。

レビュー（PR #31）で6件の指摘を受けて修正した。**このPRで新設した仕組み自体の
欠陥が2件**あった。回帰テストの集計行が端末では必ず `0 passed / 0 failed` になり
（pytest の着色エスケープ）、それが成功のように見えていたこと。作業履歴の
切り出しが日付エントリ以外の節を黙って消していたこと。どちらも
「毎回実行する」と決めた仕組みなので、気づかないまま記録が壊れる形だった。
`--allow-missing-embeddings` の逃げ道にも、A3 が防ごうとした
「二度と回収されない」状態が残っていた。

次にやること: ROADMAP の Phase 3（全件スキャン → GUI で人物ごとに20〜50枚を
割り当て → `match` の**取りこぼし率**を実測して閾値を確定）。
これまで測れているのは誤一致率だけ。

詳細: [details/2026-09-19-issue30-foundation.md](details/2026-09-19-issue30-foundation.md)

## 2026-09-19 — #32 Phase 2: 未検証だった経路にテストを足す

PR [#33](https://github.com/Beerski43/PhotoArchiveAI/pull/33)。

`face.py` の入出力、`scoring.py` の計算式、`gui.py` の人物編集とプレビュー、
`migration.py` の残りに、テストを足した。`scoring.py` は既定のフェイクが常に
同じランドマークを返すため、計算式が実質未検証だった。

レビューで **`CLAUDE.md` と `README.md` に実装で変わる数字（テストの件数・
所要時間・成功件数）を置かない**という方針を受けた。関係のない変更のたびに
更新が要り、忘れればいちばんよく読まれる2つの文書が静かに嘘になるため。
数字は `docs/testing/` 側に置くか、実行して確かめる形にした。
`tests/test_docs_stay_stable.py` が番人として入っている。

## 2026-09-19 — #30 PRレビューの手順をスキル化

PR #31 のレビューで使った進め方を `.claude/skills/pr-review-comment/` に
スキルとして固定した。`/pr-review-comment` で呼ぶ。

**指摘を書く前に手元で再現する**ことを手順の核心に据えた。#31 のレビューでは、
実行して見つけた「集計行が端末では必ず 0 passed」は採用され、想像で書きかけた
「`EMBED_VERSION` を上げても再スキャンされない」は `DETECTOR_VERSION` の定義を
読んで取り下げた。**出してよい指摘と出してはいけない指摘を分けるのは検証だけ**
という経験を、そのまま手順にしている。

出力の形も決めた。重要度（🔴 必須 / 🟡 推奨 / 🟢 任意 / 🔵 判断）の表を先頭に置き、
詳細は後ろへ回す。指摘は新しい Issue に切り出さず、そのPRの中で片付ける前提で書く
（文脈が失われると直りが遅れるため）。判断が要るものは選択肢と推奨をセットで出す。

**レビューは白紙の文脈で始める**ことも前提に加えた。実装した本人が続けて
レビューすると、意図を覚えている分「動くはず」と思い込んで検証を省く。
#31 で最も効いた指摘も、PR 本文の実行結果を鵜呑みにせず走らせたから見つかった
もので、書いた本人なら見逃していた。実装したセッションで呼ばれたら、
`/clear` かまっさらなサブエージェント（`fork` は不可）を促して止まる。

次にやること: ROADMAP の Phase 2 残り（`face.py` の実物経路、`scoring.py` の
計算式、`gui.py` の未検証クラス）。着手時に Issue を起票する。

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
