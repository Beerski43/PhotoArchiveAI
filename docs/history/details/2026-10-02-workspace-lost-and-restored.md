# 申し送り（2026-10-02〜03）— ワークスペースが `rm` で消え、復旧した

**実データは無事。** git 管理外のものだけが失われた。

## いま何が起きている状態か

| | 状態 |
|---|---|
| ブランチ | `feature/#59_arcface-embeddings`（**PR はまだ出していない**） |
| コード・文書 | **完全**。GitHub に push 済み |
| 回帰テスト | 440 passed / 0 failed（9.1秒） |
| `data/photoarchive.db` | 復旧済み。**ただし特徴量の作り直しが途中**（下記） |
| 残っている作業 | **複製で `reembed` を完走 → `evaluate` と他人誤認率を測る → PR 起票** |

### ⚠️ 実データの特徴量は作り直しの途中

```
Face 58,606 のうち
  arcface_w600k_r50/5pt/112   19,800   ← 作り直し済み
  dlib_resnet_v1/sp5/pad0.25/full  残り  ← 古い版
```

**`match` は版が一致する顔だけを見る**ので、この状態でも誤った照合は起きない
（`db.load_manual_embeddings` が `embed_version` で絞る）。ただし**手本の多くが
照合の対象外**なので、`match` を流しても意味のある結果は出ない。

**続けるには `photoarchive reembed` を実行する**（版の古い顔だけを拾うので、
そのまま続きから進む）。**`scan --force-rescan` を使わないこと**（手本が消える）。

## 何が失われたか

git 管理外で、複製が無かったもの。

- `data/photoarchive.db.bak-*` 3本（9/20 の移行前バックアップ2本、`bak-pre28` 773MB）
  - `bak-pre28` は Phase 3 手順8 で「消してよいか判断する」としていたもの。
    **判断の機会は失われたが、現DBは `integrity_check: ok` で実害は無い**
- `data/logs/`（scan / match のログ）
- `output/`、`mediaFiles/`（どちらも生成物・symlink。再作成済み）
- `.venv/`（再構築済み）

## 実データが助かった理由

**証明用に `data/` の外へ複製を作って `reembed` を走らせていた。** それが実データの
唯一の生き残りだった。**ジョブ用の一時ディレクトリは消える場所**なので、
durable な場所へ退避した。

```
/home/suu/photoarchive-recovery/
  MASTER-do-not-touch.db                    (読み取り専用・控え)
  photoarchive-rescued-20261002-232032.db   (読み取り専用・控え)
  buffalo_l.zip                             (ArcFace の配布物)
  before.json                               (作り直し前の手本の指紋)
  reembed-proof.log
```

**教訓: 長時間かかる作業の作業用複製を、消える場所に置かない。**

## 設定の再作成（確認が要る）

`config/` は git 管理外なので失われた。再作成したが、**DB から裏が取れた値と
取れなかった値がある。**

| キー | 値 | 根拠 |
|---|---|---|
| `database_path` | `data/photoarchive.db` | sample と同じ |
| `source_root` | `/mnt/nfs/nanoPi-NEO2/suzuki/Photo` | **下記のとおり判断が入っている** |
| `output_root` | `output` | **未確認**（既定値を入れた） |
| `rule_path` | `config/rule.json` | **未確認**（ひな形をコピーした） |

### `source_root` でやりかけた事故

**最初、共通接頭辞から `/mnt/nfs/nanoPi-NEO2` と推定して書いた。これは危険だった。**

実データは**2つの根を `source_root` で切り替えて2回スキャン**して作られている。

| 根 | Media | 顔 | 手本 | 除外 |
|---|---|---|---|---|
| `/mnt/nfs/nanoPi-NEO2/suzuki/Photo` | 64,974 | 55,805 | 122 | 252 |
| `/mnt/nfs/nanoPi-NEO2/share/photo/natsuTemp/な携帯` | 5,323 | 2,801 | 4 | 16 |
| 合計 | **70,297** | **58,606** | **126** | **268** |

共通接頭辞の配下には `katayama`（他家の写真）と `temp` があり、
**あの設定のまま `scan` すれば取り込んでいた。** 経緯と設計の論点は
**Issue #24 のコメント**に残した。

**消える側は安全**（`scanner.prune_missing_media` は `root` 配下だけを対象にする）。
危ないのは増える側だけ。

### `mediaFiles/` はアプリの動作に関与しない

`scan` は `Path(source_dir).resolve()` で根を解決し、`rglob("*")` で走査する。
**Python 3.12 の `rglob` はシンボリックリンクのディレクトリへ降りない**（実測）。
だから **DB の 70,297 件は `mediaFiles/` を一度も通っていない。**

symlink の名前は DB に残っていなかったが、**Issue #24 の 2026-09-20 のコメントに
`mediaFiles/photo_natsu` と書いてあり、それで復元できた。**

## 次の一手

1. **複製に対して `reembed` を完走させる**（残り約4万件・2時間強。
   **ほかの重い処理と同時に走らせない** — 推論が全コアを使うので数倍遅くなる）
2. 複製で2つを測る
   - `photoarchive evaluate` — `match` が実際に出す判定（CORRECT / MISSED / WRONG）
   - `measure_embedding_models.py --same-photo-pairs` — **他人誤認率**。
     ArcFace の閾値 0.60 は「家族内の混同だけ」で選んだ暫定値で、ここが未計測
3. 結果を PR 本文に貼って **#59 の PR を起票する**
4. 本体の DB へ適用するかは**ユーザーの判断を待つ**
