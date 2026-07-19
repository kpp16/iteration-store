"""Project identity, store placement, and isolation between projects."""

from __future__ import annotations

from pathlib import Path

from iteration_store.projects import (
    list_projects,
    project_key,
    register,
    resolve_project,
    store_home,
    store_path,
)

from .conftest import git


class TestIdentity:
    def test_git_repository_resolves_to_its_root(self, git_repo: Path):
        project = resolve_project(git_repo / "services")
        assert project.root == git_repo.resolve()
        assert project.is_git

    def test_non_git_directory_resolves_to_itself(self, tmp_path: Path):
        loose = tmp_path / "loose"
        loose.mkdir()
        project = resolve_project(loose)
        assert project.root == loose.resolve()
        assert not project.is_git

    def test_worktree_shares_the_main_repository_key(self, git_repo: Path, tmp_path: Path):
        worktree = tmp_path / "wt"
        git(git_repo, "worktree", "add", "-b", "feature", str(worktree))
        assert resolve_project(worktree).key == resolve_project(git_repo).key


class TestKeys:
    def test_is_stable_for_the_same_root(self, tmp_path: Path):
        assert project_key(tmp_path) == project_key(tmp_path)

    def test_differs_for_same_named_directories_elsewhere(self, tmp_path: Path):
        first = tmp_path / "a" / "api"
        second = tmp_path / "b" / "api"
        assert project_key(first) != project_key(second)

    def test_keeps_a_readable_prefix(self, tmp_path: Path):
        assert project_key(tmp_path / "my-api").startswith("my-api-")


class TestStoreLocation:
    def test_lives_under_the_configured_home(self, git_repo: Path, isolated_home: Path):
        path = store_path(resolve_project(git_repo))
        assert path.is_relative_to(isolated_home)
        assert path.name == "store.db"

    def test_home_honours_the_environment_override(self, isolated_home: Path):
        assert store_home() == isolated_home

    def test_separate_projects_get_separate_files(self, git_repo: Path, tmp_path: Path):
        other = tmp_path / "other"
        other.mkdir()
        assert store_path(resolve_project(git_repo)) != store_path(resolve_project(other))


class TestRegistry:
    def test_records_projects_for_the_future_global_tool(self, git_repo: Path):
        register(resolve_project(git_repo))
        keys = [project.key for project in list_projects()]
        assert resolve_project(git_repo).key in keys

    def test_registering_twice_does_not_duplicate(self, git_repo: Path):
        project = resolve_project(git_repo)
        register(project)
        register(project)
        assert len([p for p in list_projects() if p.key == project.key]) == 1

    def test_empty_when_nothing_registered(self):
        assert list_projects() == []
