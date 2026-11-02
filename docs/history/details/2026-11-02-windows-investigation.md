# Windows 対応の調査（#124・ROADMAP 4.11.2）

2026-11-02。**調べただけで、コードは変えていない。** 移植の実装は ROADMAP 7。

問い（#124・利用者）:

1. Windows でもこのアプリを使えるようにできるか
2. Microsoft Store で配布できる形までできるか
3. 商用として使ってライセンス上問題ないか

**限界**: 公開されている文書・ライセンス本文・PyPI の配布物と、このリポジトリのコードを読んだだけ。
**Windows の実機では動かしていない**（この PC は Linux）。**法的な判断ではない**（商用にするなら、最後は専門家に確かめる）。

## 結論（先に）

| 問い | 答え | 一言 |
|---|---|---|
| 1. Windows で動くか | **動かせる見込みが高い。直す所はある** | 依存はすべて Windows 用の wheel がある（dlib を除く・退役済みなので外せる）。コードに Linux 前提が 4 か所（下の §2.2） |
| 2. Store で配布できるか | **形の上ではできる** | PyInstaller などで固めて MSIX にすれば出せる。データの置き場所を書き込める場所へ移す必要がある。LGPL（PySide6）の扱いは確認が要る |
| 3. 商用で問題ないか | **いまのままでは問題がある** | **顔の特徴量（ArcFace）と写真の良し悪し（MUSIQ・NIMA）の学習済みの重みが非商用に限られている。** HEIC の読み込みの wheel は GPLv2 |

**商用にしないなら**（家族で自分たちで使う・無償で配るのでもなく自分の PC で使う）、3 の問題はいまと変わらない。
**商用にする・Store で売るなら**、モデル 3 つの差し替えか商用ライセンスの購入が先に要る。どちらにするかは利用者が決める（§4）。

## 1. ライセンス（問い 3）

**コードのライセンスと、学習済みの重みのライセンスは別もの。** コードが MIT でも、重みが非商用ということがある（ArcFace がそれ）。

### 1.1 学習済みモデル（重み）

| 使い道 | モデル | 置き方 | 使用条件 | 商用 |
|---|---|---|---|---|
| 顔の特徴量（`match` の土台） | ArcFace `w600k_r50.onnx`（InsightFace `buffalo_l`） | `scripts/fetch_models.py` | InsightFace の README: コードは MIT だが「**The pretrained models provided with this library are for non-commercial research only**」。学習データ（WebFace 系）も研究目的に限る利用規約 | **不可**（InsightFace は商用ライセンスを別に売っている・条件と値段は未確認） |
| 写真の画質（`assess`） | MUSIQ（KonIQ で学習・pyiqa の重み） | `scripts/export_iqa_onnx.py` | pyiqa（IQA-PyTorch）は **PolyForm Noncommercial 1.0.0**。KonIQ-10k は「研究のために自由に使える」とだけある | **不可と見るのが安全** |
| 写真の美しさ（`assess`） | NIMA（AVA で学習・pyiqa の重み） | 同上 | 同上。AVA の利用条件は見つからなかった | **不可と見るのが安全** |
| 顔の検出（`scan`） | MediaPipe Face Detection（BlazeFace short range） | `mediapipe` に同梱 | モデルカードが Apache 2.0 | 可 |
| 顔の表情・5点・見え方 | MediaPipe Face Landmarker / FaceMesh | `fetch_models.py` / 同梱 | 中の Face Mesh V2・Blendshape V2・BlazeFace のモデルカードが Apache 2.0（束ねた `.task` そのもののカードは見つけていない） | 可（と見てよい） |
| 退役した特徴量 | dlib ResNet・5点（`face_recognition_models`） | pip | dlib は Boost。モデルは作者が公開。**もう使っていない** | 依存ごと外せる |

**ArcFace を差し替えると、ほぼ作り直しになる。** 特徴量が変わるので、全部の顔を `reembed` し、閾値（`embedding.ACTIVE`）を測り直す。
手本（手動の割り当て）は顔の行に付いているので**消えない**（`reembed` は `embedding` と `embed_version` だけを書く・CLAUDE.md §8）。
MUSIQ・NIMA を差し替えると `quality.MODEL_VERSION` を上げて `assess` を流し直し、重み（`scoring.Weights`）を見直す。

### 1.2 ライブラリ

| パッケージ | ライセンス | 商用 | 気をつけること |
|---|---|---|---|
| PySide6 | LGPL-3.0（または GPL） | 可 | LGPL の義務（利用者が Qt を差し替えられること・ライセンス文の同梱）。**Store の MSIX で差し替えの自由をどう満たすかは未確認。** Qt の商用ライセンスを買えば義務は消える |
| pillow-heif | ソースは BSD-3 だが、**配布される wheel は GPLv2**（同梱の x265 が GPLv2。libheif・libde265 は LGPLv3） | **wheel のまま同梱すると、アプリ全体に GPLv2 が及ぶおそれ** | このアプリは HEIC を**読むだけ**（`convert-heic` は JPEG へ書く）。同じ作者の **`pi-heif`** は読むだけの版で、x265 を含まず LGPLv3（Windows 用 wheel あり）。差し替えは import 名を変えるだけ。HEVC の特許も論点になりうる（未調査） |
| mediapipe | Apache 2.0 | 可 | |
| onnxruntime | MIT | 可 | |
| opencv-python | Apache 2.0 | 可 | wheel に FFmpeg（LGPL）が入っている |
| numpy | BSD 系 | 可 | |
| Pillow | HPND（MIT 系） | 可 | |
| PyYAML | MIT | 可 | |
| dlib | Boost | 可 | 使っていない（§2.1） |

## 2. Windows で動くか（問い 1）

### 2.1 依存の配布物（PyPI・2026-11-02）

| パッケージ | Windows 用 wheel |
|---|---|
| mediapipe 0.10.21（固定している版） | あり（cp39〜cp312・win_amd64）。**Python 3.13 以降の wheel は無い**ので、Windows でも Python 3.12 までにする |
| onnxruntime・PySide6・opencv-python・numpy・pillow-heif・pi-heif | あり |
| **dlib** | **無い**（ソースだけ。CMake と Visual Studio の C++ でビルドが要る） |

**dlib は退役している**（2026-10-02 に ArcFace へ移った・`embedding.py`）。いまも `pyproject.toml` の依存に入っていて、
`face.py` に読み込みの経路（`_load_dlib_models`）が残っている。Windows で入れるのがいちばん面倒なので、移植では**依存から外す**のがよい
（外すと `-m models` のテストの扱いも見直しになる）。

### 2.2 コードの Linux 前提（読んで見つけたもの）

| # | どこ | 何が起きるか | 重さ |
|---|---|---|---|
| a | `db.folder_expression` / `db.folder_of`・`db._is_within`・`scanner._in_scope` | **パスの区切りを `/` と決め打ちしている。** Windows の `str(Path)` は `\` なので、フォルダが取れず（全部「フォルダ無し」）、**似た写真の束ね（同じ行事＝フォルダ×日）が日だけで束ねる**ことになる。root の内外の判定も外れ、**消えたファイルの削除と HEIC の行の削除が効かない** | **高い**（`select` の結果が黙って変わる）。DB に入れるパスを `/` にそろえる（`as_posix`）のが素直だが、既存 DB との兼ね合いがある |
| b | `face._suppress_mediapipe_output` | `sys.stderr.fileno()` を使う。**画面だけのアプリ（`pythonw`・PyInstaller の windowed）では `sys.stderr` が `None`** で、顔検出の最初の1回で落ちる | 高い（GUI から `scan` を流すとき・ROADMAP 6） |
| c | `cli.py` のログ `Path("data/logs")`・DB の既定 `data/` | 置き場所が**作業フォルダからの相対**。Store のアプリ（MSIX）はインストール先に書けないので、`%LOCALAPPDATA%` などへ移す必要がある | Store に出すなら必須 |
| d | `scripts/*.sh`・`.githooks/` | bash。利用者が使うものではないが、Windows で開発するなら Git Bash か書き直しが要る | 低い（開発用） |

**問題にならないもの**: ワーカーは `spawn`（`scanner.WORKER_START_METHOD`）で起こしているので、Windows の既定と同じ。`dates.py` のフォルダ名の読み取りは `\` を `/` に直している。

**既存の DB をそのまま Windows へ持っていくことはできない。** `Media.path` が Linux のパス（NFS のマウント先）なので、
Windows 側で同じ写真を指すようにパスを書き換える手段（root の置き換え）が要る。書き換えずに `scan` すると、消えたファイルの削除の安全弁（2割）で止まる。
NFS は Windows では SMB（`\\server\share`）で見るのが普通で、読み出しの速さは測っていない。

## 3. Microsoft Store（問い 2）

- Store は MSIX のほか、**MSI / EXE のインストーラも受け付ける**（Win32 アプリ）。Python のアプリは PyInstaller などで固めてから包む
  - MSI / EXE で出す: インストーラと**中のすべての PE ファイル**を、Microsoft の信頼するルートにつながる証明書で署名する必要がある（自己署名は不可・Store は署名し直さない）。静かなインストール（サイレント）と、消し方の明示も要る（ポリシー 10.2.x）
  - **MSIX で出す: Store が署名するので証明書は要らない**（Microsoft が勧める経路）。そのかわりインストール先は読み取り専用なので §2.2 c が必須
- Partner Center の開発者登録（個人は有料・一度きり）が要る
- 学習済みモデル（`models/` の4つで計 482MB・2026-11-02 に `du`。ArcFace 174MB・MUSIQ 109MB・NIMA 217MB・Face Landmarker 4MB）を同梱するか、初回に取りに行くか（いまは `scripts/fetch_models.py`）を決める。取りに行くなら、配布元の使用条件が §1.1 のとおりなので**商用では同じ問題が残る**
- PySide6 の LGPL を MSIX でどう満たすか（§1.2）は未確認

## 4. 利用者に決めてもらうこと（7 に着手する前）

1. **商用にするか。** しないなら §1 の問題は今と同じ（個人で使うぶんには変わらない）。する・Store で有料にするなら 2 へ
2. 商用にするなら、**顔の特徴量（ArcFace）をどうするか**: 商用ライセンスを買う（InsightFace に問い合わせ）／商用可のモデルに替える（`reembed` と閾値の測り直し・精度は測るまで分からない）
3. **写真の良し悪し（MUSIQ・NIMA）をどうするか**: 商用可のモデルに替える／商用版では外す（白飛び・黒つぶれ・見切れは明るさから測るのでモデル不要）
4. HEIC の読み込みを `pi-heif` に替える（商用でなくても替えて困ることは無い）
5. Store に出すなら無償か有償か（無償でも商用の扱いになりうる。専門家に確認）

## 5. 7（移植）でやることの候補

決め方しだいで増減する。

- §2.2 a・b・c を直す（a はパスを `/` にそろえる移行が要るかを先に決める）
- `dlib`・`face_recognition_models` を依存から外す
- `pillow-heif` → `pi-heif`
- 既存 DB の root を置き換える手段（Linux から Windows へ持っていくとき）
- PyInstaller で固め、MSIX に包む。モデルの置き方を決める
- Windows の実機で回帰テストを流す（いまは Linux でしか流していない）

## 根拠（2026-11-02 に読んだもの）

- InsightFace の README（python-package）: <https://github.com/deepinsight/insightface/tree/master/python-package>
- IQA-PyTorch（pyiqa）の LICENSE（PolyForm Noncommercial 1.0.0）: <https://github.com/chaofengc/IQA-PyTorch>
- KonIQ-10k: <https://database.mmsp-kn.de/koniq-10k-database.html>
- pillow-heif の同梱ライブラリ一覧（LICENSES_bundled.txt）: <https://github.com/bigcat88/pillow_heif>
- pi-heif: <https://pypi.org/project/pi-heif/>
- MediaPipe Face Landmarker: <https://developers.google.com/mediapipe/solutions/vision/face_landmarker>
- Store の MSI / EXE の要件: <https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msi/create-app-submission>・<https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msi/package-validation-pre-check>
- PySide6 のライセンス: <https://github.com/pyside/pyside-setup/blob/dev/README.pyside6_essentials.md>
- 各パッケージの Windows 用 wheel: `https://pypi.org/pypi/<名前>/json` の `releases`
