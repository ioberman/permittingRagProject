import pytest

from app.storage import LocalFileStorage


def test_save_dedups_identical_content(tmp_path):
    store = LocalFileStorage(root=str(tmp_path))
    uri1, hash1 = store.save(b"hello world", "a.txt")
    uri2, hash2 = store.save(b"hello world", "b.txt")

    assert uri1 == uri2
    assert hash1 == hash2
    assert len([f for f in tmp_path.rglob("*") if f.is_file()]) == 1


def test_save_different_content_writes_separate_files(tmp_path):
    store = LocalFileStorage(root=str(tmp_path))
    uri1, _ = store.save(b"content one", "a.txt")
    uri2, _ = store.save(b"content two", "b.txt")

    assert uri1 != uri2
    assert len([f for f in tmp_path.rglob("*") if f.is_file()]) == 2


def test_load_roundtrip(tmp_path):
    store = LocalFileStorage(root=str(tmp_path))
    uri, _ = store.save(b"some content", "file.txt")

    assert store.load(uri) == b"some content"


def test_load_blocks_path_traversal(tmp_path):
    store = LocalFileStorage(root=str(tmp_path / "storage"))
    store.save(b"secret", "f.txt")

    with pytest.raises(ValueError):
        store.load("../../etc/passwd")
