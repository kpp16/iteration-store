"""Git plumbing, verified against real git rather than assumed."""

from __future__ import annotations

from pathlib import Path

from iteration_store import gitutil

from .conftest import commit_all, git


class TestBlobHashing:
    def test_matches_git_hash_object(self, git_repo: Path):
        # The reimplementation is only correct if it agrees with git itself.
        target = git_repo / "services" / "auth.py"
        expected = git(git_repo, "hash-object", str(target))
        assert gitutil.git_blob_sha(target.read_bytes()) == expected

    def test_matches_git_for_empty_file(self, git_repo: Path):
        empty = git_repo / "empty.txt"
        empty.write_bytes(b"")
        assert gitutil.git_blob_sha(b"") == git(git_repo, "hash-object", str(empty))

    def test_matches_git_for_binary_content(self, git_repo: Path):
        blob = git_repo / "blob.bin"
        data = bytes(range(256))
        blob.write_bytes(data)
        assert gitutil.git_blob_sha(data) == git(git_repo, "hash-object", str(blob))

    def test_sha256_fallback_is_stable(self):
        assert gitutil.sha256_hex(b"abc") == gitutil.sha256_hex(b"abc")
        assert gitutil.sha256_hex(b"abc") != gitutil.sha256_hex(b"abd")


class TestRepoResolution:
    def test_finds_repository_root(self, git_repo: Path):
        info = gitutil.resolve_repo(git_repo / "services")
        assert info is not None
        assert info.main_root == git_repo.resolve()

    def test_returns_none_outside_a_repository(self, tmp_path: Path):
        loose = tmp_path / "loose"
        loose.mkdir()
        assert gitutil.resolve_repo(loose) is None

    def test_worktree_resolves_to_main_repository(self, git_repo: Path, tmp_path: Path):
        worktree = tmp_path / "wt"
        git(git_repo, "worktree", "add", "-b", "feature", str(worktree))

        info = gitutil.resolve_repo(worktree)
        assert info is not None
        assert info.worktree_root == worktree.resolve()
        assert info.main_root == git_repo.resolve()


class TestCommitsAndDiffs:
    def test_head_returns_current_commit(self, git_repo: Path):
        assert gitutil.head_commit(git_repo) == git(git_repo, "rev-parse", "HEAD")

    def test_head_is_none_before_any_commit(self, tmp_path: Path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        git(fresh, "init")
        assert gitutil.head_commit(fresh) is None

    def test_diff_reports_changes_since_a_commit(self, git_repo: Path):
        before = gitutil.head_commit(git_repo)
        target = git_repo / "services" / "billing.py"
        target.write_text("def charge(amount):\n    return amount * 2\n")
        commit_all(git_repo, "double")

        diff = gitutil.diff_since(git_repo, before, "services/billing.py")
        assert diff is not None
        assert "amount * 2" in diff

    def test_diff_is_none_when_nothing_changed(self, git_repo: Path):
        head = gitutil.head_commit(git_repo)
        assert gitutil.diff_since(git_repo, head, "services/auth.py") is None

    def test_diff_is_truncated(self, git_repo: Path):
        before = gitutil.head_commit(git_repo)
        (git_repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)))
        commit_all(git_repo, "big")

        diff = gitutil.diff_since(git_repo, before, "big.txt")
        assert len(diff) <= gitutil.MAX_DIFF_CHARS + 64
        assert "truncated" in diff
