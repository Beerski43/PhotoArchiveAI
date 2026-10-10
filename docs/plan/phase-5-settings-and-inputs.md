# Phase 5 — 設定と入力の整理

親 Issue: **#73**。2026-10-10 に利用者の依頼で「進める順序」の5番から繰り上げた
（「#24 #26 #27 を早く着手してほしい」）。精度にも `select` の質にも直接は効かないが、
**設定を失ったときに DB から戻せない**（2026-10-02 の復旧・#24 のコメント）という
安全性の穴を含む。

## 子 Issue と順序

**利用者の決定**: 3本の PR を順に出す（1 Issue - 1 ブランチ - 1 PR）。後ろの PR は
前の PR のブランチに積み重ねる。ベースはどれも `develop`。

| 順 | Issue | やること | 利用者が決めたこと | 状態 |
|---|---|---|---|---|
| 1 | #27 | 設定ファイルとルールを YAML にする | **YAML だけを読む**。手元の JSON は YAML に変換する | PR 起票 |
| 2 | #24 | 検出元ディレクトリを配列で指定する | **走査した root を DB に記録する（スキーマ版7）** | PR 起票 |
| 3 | #26 | scan 以降の処理から HEIC を外す | **既存の HEIC の行は引き継がずに消す** | PR 起票 |

## #27 — YAML 化

- `config/app_settings.yml` と `config/rule.yml`。サンプルも `*.sample.yml`
- **古い `app_settings.json` は読まない。** 残っているだけなら WARNING で変換を促し、
  CLI と GUI は「DB のパスが要る」の代わりにそのことを言う
- `.json` のルールは止める
- 手元の設定は 2026-10-10 に変換した。#74 のマージ後、不要になった JSON（`config/` と控え）は
  利用者の指示で消した

## #24 — 検出元の複数指定

- 設定は `source_roots:`（配列）。**古い `source_root:` は読まずに WARNING**（PR #75 で利用者が決めた）。`--source` は何度でも書ける
- root ごとに走査し、消えた行の削除と2割の安全弁も root ごと。**入れ子の root は止める**
- `ScanRoot`（v7）に走査し終えた root を書く。年フォルダだけの走査は書かない。**記録済みの root を含む親は走査する前に止める**（PR #75 のレビューで利用者が決めた。記録は消さない）。無い root があればどの root も走査しない。設定に root が
  無ければ `scan` / `select` / GUI は記録を使う。**移行では推定しない**
- GUI は root が2つ以上なら `<root の名前>/<相対>`。`select` は含む root からの相対
- 実データの複製で v6 → v7 を流した（2026-10-10）: 2.3 秒・Media 70,297 / Face 58,606 / 手動 10,753 件がそのまま
- 手元の設定に root を2つ書いた（`source_roots`）。古い `source_root` の行は、読まなくすると決めた時点で消した
- **マージ後に利用者がすること**: `photoarchive migrate`（版7）→ `photoarchive scan`
  （root が記録される。差分スキャンなので読み直しは変わったファイルだけ）

実データの root は2つ（2026-10-02 に数えた。`Media.path` の接頭辞で数え直せる）。
`${NFS_ROOT}/${SURNAME}/Photo` と `${NFS_ROOT}/share/photo/person2Temp/${PERSON_2}携帯`。
**共通の親（`${NFS_ROOT}`）で走査すると他家の写真まで入る。** root は推定しない。

## #26 — HEIC を外す

- `scanner.IMAGE_EXTENSIONS` から `heic` / `heif` を外し、`face.py` の HEIF デコーダ登録もやめた
- 既存の HEIC の行は**対象外の拡張子として**消す（`scanner.prune_excluded_types`）。
  安全弁の母数にも分子にも入れない
- **`scan` は HEIC をファイル名も含めて一切見ない**（PR #76 のレビューで利用者が決めた）。同名の JPEG が無い
  HEIC は 2026-10-10 時点で 3 件（`IMG_6463〜6465.HEIC`）。知らせないので、先に `convert-heic` を流す
- **マージ後に利用者がすること**: 先に `photoarchive convert-heic`（JPEG の無い3件）→ `scan`
  （2026-10-10 時点で HEIC の行 1,761 件と顔 941 件が消える）

実データの HEIC はすべて `${PERSON_2}携帯` 側で、その root の 33%（2026-10-10。`sqlite3` で
`SELECT COUNT(*) FROM Media WHERE lower(path) LIKE '%.heic'`）。**2割の安全弁に掛けない
削除の経路が要る。**
