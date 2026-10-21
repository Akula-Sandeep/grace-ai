import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_release_artifact import is_forbidden, looks_like_placeholder


def test_release_forbids_secret_and_generated_paths():
    assert is_forbidden(".env")
    assert is_forbidden("Backend/.env")
    assert is_forbidden("Frontend/node_modules/react/index.js")
    assert is_forbidden("Frontend/dist/assets/app.js")
    assert is_forbidden("Backend/.venv/bin/python")
    assert not is_forbidden(".env.example")
    assert not is_forbidden("README.md")


def test_release_scanner_accepts_placeholders_and_ci_values():
    assert looks_like_placeholder("replace-with-a-long-random-secret")
    assert looks_like_placeholder("ci-test-secret-123")
    assert looks_like_placeholder("redis://:ci-password@redis:6379/0")
    assert looks_like_placeholder("postgresql+psycopg://user:password@localhost:5432/db")
    assert not looks_like_placeholder("sk-live-very-secret-value")


def test_release_builder_emits_real_zip_for_zip_output(tmp_path, monkeypatch):
    import zipfile

    import scripts.build_release_artifact as release

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(release, "run_git", lambda *args: b"abc123\n")
    output = tmp_path / "grace-source.zip"
    release.build_release(root, ["README.md"], output)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert names == ["Grace-Source-abc123/README.md"]
        assert archive.testzip() is None


def test_release_builder_emits_gzip_tar_for_non_zip_output(tmp_path, monkeypatch):
    import tarfile

    import scripts.build_release_artifact as release

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(release, "run_git", lambda *args: b"abc123\n")
    output = tmp_path / "grace-source.tar.gz"
    release.build_release(root, ["README.md"], output)

    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
        assert names == ["Grace-Source-abc123/README.md"]
