"""Check that dupfinder finds exactly the duplicates the original pydupes finds.

Each test builds a small folder in pytest's tmp_path, runs both versions,
and compares the groups. Where the two versions are meant to differ
(symlinks, unreadable files, pipes) the test says so and checks the new
behaviour directly.
"""

import json
import os
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "original"))
sys.path.insert(0, str(REPO))

import duplicates  # noqa: E402  (the original pydupes, unchanged)
from dupfinder import core, find_duplicates, scan  # noqa: E402
from dupfinder.__main__ import main  # noqa: E402

MIB = 1024 * 1024


def original_groups(root: Path, recursive: bool = True) -> list[list[str]]:
    """Run the original and return its duplicates as sorted lists of full paths."""
    md5_dict, _ = duplicates.find_dup(str(root), 1 if recursive else 0)
    groups = []
    for names in md5_dict.values():
        if len(names) >= 2:
            # find_dup0 (non-recursive) returns bare file names, find_dup1 full
            # paths. os.path.join leaves a full path unchanged.
            groups.append(sorted(os.path.join(str(root), name) for name in names))
    return sorted(groups)


def write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def build_tree(root: Path) -> None:
    """A folder with every case from the task: nesting, copies, empties, traps."""
    rng = random.Random(42)
    a = rng.randbytes(5000)
    b = rng.randbytes(300)
    write(root / "a1.txt", a)
    write(root / "sub" / "a2.txt", a)  # pair across folders
    write(root / "b1.bin", b)
    write(root / "sub" / "deep" / "b2.bin", b)
    write(root / "sub" / "deep" / "deeper" / "b3.bin", b)  # triplicate
    write(root / "empty1", b"")
    write(root / "sub" / "empty2", b"")
    write(root / "sub" / "deep" / "empty3", b"")
    # Unique sizes: never opened by the new code.
    for i in range(5):
        write(root / "unique" / f"u{i}", rng.randbytes(100 + i))
    # Same size, different content (stage 2 separates them).
    write(root / "same_size_1", rng.randbytes(5000))
    # Trap: same size and same first 1 MiB, different last byte. Only a
    # full hash (stage 3) can tell these apart.
    big = rng.randbytes(MIB + 500)
    write(root / "trap" / "last_byte_1", big)
    write(root / "trap" / "last_byte_2", big[:-1] + bytes([big[-1] ^ 0xFF]))
    # Trap: same size, different first block.
    write(root / "trap" / "first_block_1", rng.randbytes(MIB + 900))
    write(root / "trap" / "first_block_2", rng.randbytes(MIB + 900))
    # A real duplicate bigger than one block, so it goes through stage 3.
    c = rng.randbytes(MIB + 1234)
    write(root / "c1", c)
    write(root / "sub" / "c2", c)


@pytest.mark.parametrize("hash_name", ["blake2b", "md5"])
def test_same_groups_as_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hash_name: str) -> None:
    # The choice of hash must not change which files are grouped.
    monkeypatch.setattr(core, "HASH_NAME", hash_name)
    build_tree(tmp_path)
    expected = original_groups(tmp_path)
    assert find_duplicates(str(tmp_path)) == expected
    # Sanity check that the tree really has the cases we think it has.
    names = [sorted(Path(p).name for p in group) for group in expected]
    assert ["a1.txt", "a2.txt"] in names
    assert ["b1.bin", "b2.bin", "b3.bin"] in names
    assert ["empty1", "empty2", "empty3"] in names
    assert ["c1", "c2"] in names
    assert len(expected) == 4  # the traps are not duplicates


def test_non_recursive_matches_find_dup0(tmp_path: Path) -> None:
    build_tree(tmp_path)
    write(tmp_path / "a_copy_at_top", (tmp_path / "a1.txt").read_bytes())
    expected = original_groups(tmp_path, recursive=False)
    result = find_duplicates(str(tmp_path), recursive=False)
    assert result == expected
    assert [Path(p).name for p in result[0]] == ["a1.txt", "a_copy_at_top"]


def test_summary_counts_match_original(tmp_path: Path) -> None:
    """Total, distinct and duplicate counts equal what dup_summary would print."""
    build_tree(tmp_path)
    md5_dict, dup_count = duplicates.find_dup(str(tmp_path), 1)
    result = scan(str(tmp_path))
    assert result.total_files == sum(len(v) for v in md5_dict.values())
    assert result.distinct_files == len(md5_dict)
    assert result.duplicate_files == sum(dup_count.values())


def test_empty_files_grouped_like_original(tmp_path: Path) -> None:
    for name in ["e1", "e2", "dir/e3"]:
        write(tmp_path / name, b"")
    write(tmp_path / "not_empty", b"x")
    expected = original_groups(tmp_path)
    assert len(expected) == 1 and len(expected[0]) == 3
    assert find_duplicates(str(tmp_path)) == expected


def test_single_empty_file_is_not_a_duplicate(tmp_path: Path) -> None:
    write(tmp_path / "e1", b"")
    write(tmp_path / "x", b"x")
    assert find_duplicates(str(tmp_path)) == [] == original_groups(tmp_path)


@pytest.mark.parametrize("seed", range(5))
def test_random_trees_match_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    """Random trees with a tiny block size, so stage 3 runs on almost every group."""
    monkeypatch.setattr(core, "BLOCK_SIZE", 16)
    rng = random.Random(seed)
    contents = [rng.randbytes(rng.randint(0, 64)) for _ in range(40)]
    # Files that share the first 16 bytes but differ later.
    prefix = rng.randbytes(16)
    contents += [prefix + rng.randbytes(20) for _ in range(5)]
    for i in range(150):
        folder = tmp_path / f"d{rng.randint(0, 5)}" / f"e{rng.randint(0, 2)}"
        write(folder / f"f{i}", rng.choice(contents))
    assert find_duplicates(str(tmp_path)) == original_groups(tmp_path)


def test_threads_give_same_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Send every file to the thread pool, even tiny ones, so the threaded
    # code path really runs in this test.
    monkeypatch.setattr(core, "THREAD_MIN_SIZE", 0)
    build_tree(tmp_path)
    assert find_duplicates(str(tmp_path), workers=4) == original_groups(tmp_path)


def test_unique_sizes_are_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of stage 1: a file with a unique size is not read."""
    build_tree(tmp_path)
    opened: list[str] = []
    real_open = open

    def recording_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    # core.py calls open() by name, so a module-level "open" shadows the built-in.
    monkeypatch.setattr(core, "open", recording_open, raising=False)
    result = scan(str(tmp_path))
    # Compare relative paths, because tmp_path's own name contains "unique".
    opened = [os.path.relpath(path, tmp_path) for path in opened]
    assert not any(path.startswith("unique") for path in opened)
    assert not any("empty" in path for path in opened)
    # The first-block trap pair is opened once each and never reaches stage 3.
    assert sum("first_block" in path for path in opened) == 2
    assert result.stage3_files == 4  # c1, c2 and the last-byte trap pair


def test_symlink_is_not_a_duplicate(tmp_path: Path) -> None:
    """Difference from the original, on purpose.

    The original opens symlinks like normal files, so a link and its target
    show up as duplicates. Deleting the "duplicate" target would break the
    link, so the new code skips symlinks altogether.
    """
    data = b"some content"
    target = write(tmp_path / "real.txt", data)
    os.symlink(target, tmp_path / "link.txt")
    (tmp_path / "dir").mkdir()
    os.symlink(tmp_path / "dir", tmp_path / "dir_link")  # neither version follows this

    assert original_groups(tmp_path) == [[str(tmp_path / "link.txt"), target]]
    assert find_duplicates(str(tmp_path)) == []


def test_symlinks_removed_otherwise_same_as_original(tmp_path: Path) -> None:
    """With links in the tree, the new result is the original's minus the links."""
    build_tree(tmp_path)
    os.symlink(tmp_path / "a1.txt", tmp_path / "sub" / "link_to_a")
    os.symlink(tmp_path / "b1.bin", tmp_path / "link_to_b")
    expected = []
    for group in original_groups(tmp_path):
        group = [path for path in group if not os.path.islink(path)]
        if len(group) >= 2:
            expected.append(group)
    assert find_duplicates(str(tmp_path)) == sorted(expected)


def test_hard_links_reported_like_original(tmp_path: Path) -> None:
    """A hard link is a second name for the same file. Both versions list it.

    Unlike a symlink, a hard link is a regular file to the directory walk,
    and deleting one name leaves the data reachable through the other.
    """
    write(tmp_path / "a", b"hello")
    os.link(tmp_path / "a", tmp_path / "b")
    expected = original_groups(tmp_path)
    assert expected == [[str(tmp_path / "a"), str(tmp_path / "b")]]
    assert find_duplicates(str(tmp_path)) == expected


def test_broken_symlink_does_not_crash(tmp_path: Path) -> None:
    write(tmp_path / "a", b"x")
    write(tmp_path / "b", b"x")
    os.symlink(tmp_path / "missing", tmp_path / "broken")
    with pytest.raises(FileNotFoundError):
        duplicates.find_dup(str(tmp_path), 1)  # the original crashes
    assert find_duplicates(str(tmp_path)) == [[str(tmp_path / "a"), str(tmp_path / "b")]]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_named_pipe_is_skipped(tmp_path: Path) -> None:
    # The original would open this pipe and wait forever for a writer,
    # so we only run the new code here.
    os.mkfifo(tmp_path / "pipe")
    write(tmp_path / "a", b"x")
    write(tmp_path / "b", b"x")
    result = scan(str(tmp_path))
    assert result.groups == [[str(tmp_path / "a"), str(tmp_path / "b")]]
    assert result.total_files == 2


def test_unreadable_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PermissionError on one file is counted and the scan carries on.

    chmod 000 does not stop root, so open() is replaced instead.
    """
    build_tree(tmp_path)
    blocked = str(tmp_path / "sub" / "deep" / "b2.bin")
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(core, "open", fake_open, raising=False)
    result = scan(str(tmp_path))
    assert result.skipped == [blocked]
    # The triplicate becomes a pair; everything else is unchanged.
    expected = [[p for p in g if p != blocked] for g in original_groups(tmp_path)]
    assert result.groups == sorted(expected)
    assert result.total_files == len(list(p for p in tmp_path.rglob("*") if p.is_file())) - 1


def test_file_deleted_after_listing_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that vanishes between the directory walk and hashing is skipped."""
    build_tree(tmp_path)
    expected = original_groups(tmp_path)
    victim = str(tmp_path / "sub" / "c2")
    real_list_files = core._list_files

    def list_then_delete(*args, **kwargs):
        files = real_list_files(*args, **kwargs)
        os.remove(victim)  # gone before stage 2 tries to open it
        return files

    monkeypatch.setattr(core, "_list_files", list_then_delete)
    result = scan(str(tmp_path))
    assert result.skipped == [victim]
    # c1 lost its only partner, so that group disappears.
    assert result.groups == [g for g in expected if victim not in g]


def test_unlistable_folder_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder we may not list is counted as skipped, and the rest is still scanned."""
    build_tree(tmp_path)
    blocked = str(tmp_path / "sub" / "deep")
    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(core.os, "scandir", fake_scandir)
    result = scan(str(tmp_path))
    assert result.skipped == [blocked]
    # b2, b3 and empty3 live under the blocked folder, so the b group is gone.
    assert [sorted(Path(p).name for p in g) for g in result.groups] == [
        ["a1.txt", "a2.txt"],
        ["c1", "c2"],
        ["empty1", "empty2"],
    ]


def test_cli_text_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_tree(tmp_path)
    main([str(tmp_path)])
    text = capsys.readouterr().out
    assert "3 identical files:" in text
    # 20 files. Extra copies: 1 (a) + 2 (b) + 2 (empty) + 1 (c) = 6.
    assert "Total Files    : 20" in text
    assert "Distinct Files : 14" in text
    assert "Duplicate Files:  6" in text
    assert "Skipped Files  :  0" in text

    main([str(tmp_path), "--json", "--no-recursive", "--workers", "1"])
    data = json.loads(capsys.readouterr().out)
    assert data["groups"] == original_groups(tmp_path, recursive=False)
    assert data["skipped_files"] == 0


def test_missing_root_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        scan(str(tmp_path / "nope"))
