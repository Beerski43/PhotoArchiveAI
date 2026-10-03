"""モデルの取得スクリプト（Issue #59）。

**ネットワークには触らない。** 取得そのものは差し替えて、
**sha256 の検証と「置かない」判断**を確かめる。すり替わったモデルで照合が
走ると、版の文字列は同じままなので `reembed` も作り直さず、**静かに結果だけが
変わる。**
"""

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_models.py"


@pytest.fixture(scope="module")
def fetch_models():
    spec = importlib.util.spec_from_file_location("fetch_models", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _model(fetch_models, payload: bytes, *, member=None, name="model.onnx"):
    return fetch_models.ModelFile(
        name=name,
        sha256=hashlib.sha256(payload).hexdigest(),
        url="https://example.invalid/model",
        archive_member=member,
    )


def _serve(fetch_models, monkeypatch, payload: bytes, *, member=None):
    """ダウンロードを差し替える。**実際の通信はしない。**"""
    calls = []

    def fake_download(url, destination, log=print):
        calls.append(url)
        if member:
            with zipfile.ZipFile(destination, "w") as bundle:
                bundle.writestr(member, payload)
        else:
            destination.write_bytes(payload)

    monkeypatch.setattr(fetch_models, "_download", fake_download)
    return calls


def test_the_sha256_of_a_file_is_computed_in_blocks(fetch_models, tmp_path):
    """174MB を一度にメモリへ載せないこと（塊で読む）。"""
    payload = b"x" * (fetch_models.CHUNK_BYTES * 2 + 7)
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    assert fetch_models.sha256_of(path) == hashlib.sha256(payload).hexdigest()


def test_a_matching_file_is_left_alone(fetch_models, tmp_path, monkeypatch):
    payload = b"model-bytes"
    (tmp_path / "model.onnx").write_bytes(payload)
    calls = _serve(fetch_models, monkeypatch, payload)

    assert fetch_models.fetch(_model(fetch_models, payload), tmp_path, log=lambda *_: None)
    assert calls == []  # 取得しない


def test_a_file_with_the_wrong_contents_is_reported_not_replaced(
    fetch_models, tmp_path, monkeypatch
):
    """**勝手に取り直さない。** 置き換えると、利用者が置いたものが黙って消える。"""
    (tmp_path / "model.onnx").write_bytes(b"something-else")
    calls = _serve(fetch_models, monkeypatch, b"model-bytes")
    messages = []

    ok = fetch_models.fetch(
        _model(fetch_models, b"model-bytes"), tmp_path, log=messages.append
    )

    assert ok is False
    assert calls == []
    assert any("--force" in message for message in messages)
    assert (tmp_path / "model.onnx").read_bytes() == b"something-else"


def test_force_replaces_the_file(fetch_models, tmp_path, monkeypatch):
    payload = b"model-bytes"
    (tmp_path / "model.onnx").write_bytes(b"something-else")
    _serve(fetch_models, monkeypatch, payload)

    assert fetch_models.fetch(
        _model(fetch_models, payload), tmp_path, force=True, log=lambda *_: None
    )
    assert (tmp_path / "model.onnx").read_bytes() == payload


def test_a_download_that_does_not_match_is_never_placed(fetch_models, tmp_path, monkeypatch):
    """**いちばん大事。** 期待と違うものを `models/` へ置かない。

    置いてしまうと、別物のモデルで照合が走る。版の文字列は変わらないので
    `reembed` も作り直さず、**結果だけが静かに変わる。**
    """
    _serve(fetch_models, monkeypatch, b"tampered")
    messages = []

    ok = fetch_models.fetch(
        _model(fetch_models, b"expected"), tmp_path, log=messages.append
    )

    assert ok is False
    assert not (tmp_path / "model.onnx").exists()
    assert any("sha256 が一致しません" in message for message in messages)


def test_a_member_is_taken_out_of_the_archive(fetch_models, tmp_path, monkeypatch):
    """buffalo_l の ZIP から認識用の1本だけを取り出すこと。"""
    payload = b"arcface-bytes"
    _serve(fetch_models, monkeypatch, payload, member="w600k_r50.onnx")

    ok = fetch_models.fetch(
        _model(fetch_models, payload, member="w600k_r50.onnx", name="w600k_r50.onnx"),
        tmp_path,
        log=lambda *_: None,
    )

    assert ok
    assert (tmp_path / "w600k_r50.onnx").read_bytes() == payload


def test_check_only_never_downloads(fetch_models, tmp_path, monkeypatch):
    calls = _serve(fetch_models, monkeypatch, b"model-bytes")
    messages = []

    ok = fetch_models.fetch(
        _model(fetch_models, b"model-bytes"), tmp_path, check_only=True, log=messages.append
    )

    assert ok is False
    assert calls == []
    assert any("ありません" in message for message in messages)


def test_main_reports_a_failure_with_a_non_zero_code(fetch_models, tmp_path, capsys):
    code = fetch_models.main(["--models-dir", str(tmp_path), "--check"])
    assert code == 1
    assert "用意できていない" in capsys.readouterr().err


def test_the_arcface_entry_matches_what_face_py_looks_for(fetch_models):
    """**取得するファイル名と、本体が探す名前をそろえる。**

    片方だけ直すと「取得できたのに見つからない」になる。
    """
    from photoarchive_ai import face

    assert fetch_models.ARCFACE.name == face.ARCFACE_FILE
    assert len(fetch_models.ARCFACE.sha256) == 64
