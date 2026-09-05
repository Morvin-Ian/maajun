from __future__ import annotations

import json

from maajun.discovery.toolchain import detect_checks, detect_formatters


def commands(root):
    return [check.command for check in detect_checks(root)]


def rewrites(root):
    return [formatter.write for formatter in detect_formatters(root)]


def test_ruff_from_tool_table_uses_the_lockfile_runner(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert commands(tmp_path) == ["uv run ruff check ."]


def test_poetry_runner_and_dev_group_dependency(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry.group.dev.dependencies]\nblack = \"^24\"\n", encoding="utf-8"
    )
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    assert commands(tmp_path) == ["poetry run black --check ."]


def test_dependency_group_declares_the_tool(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = ["ruff>=0.6", "pytest"]\n', encoding="utf-8"
    )
    assert commands(tmp_path) == ["ruff check ."]


def test_ruff_format_section_adds_a_format_check(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.ruff.format]\nquote-style = \"double\"\n", encoding="utf-8"
    )
    assert commands(tmp_path) == ["ruff check .", "ruff format --check ."]


def test_standalone_ruff_and_flake8_config_files(tmp_path):
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    (tmp_path / ".flake8").write_text("[flake8]\nmax-line-length = 88\n", encoding="utf-8")
    assert commands(tmp_path) == ["ruff check .", "flake8"]


def test_setup_cfg_without_a_flake8_section_is_not_a_signal(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = app\n", encoding="utf-8")
    assert commands(tmp_path) == []


def test_package_json_lint_script_wins_over_installed_tools(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"lint": "eslint .", "build": "tsc"},
            "devDependencies": {"eslint": "^9", "prettier": "^3"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert commands(tmp_path) == ["pnpm run lint"]


def test_package_json_without_a_script_falls_back_to_dev_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"prettier": "^3"}}), encoding="utf-8"
    )
    assert commands(tmp_path) == ["npx prettier --check ."]


def test_go_check_fails_when_gofmt_lists_a_file(tmp_path):
    (tmp_path / "go.mod").write_text("module app\n", encoding="utf-8")
    assert commands(tmp_path) == ['test -z "$(gofmt -l .)"']


def test_rust_and_python_in_one_repo_are_both_reported(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"app\"\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    assert commands(tmp_path) == ["ruff check .", "cargo fmt --check"]


def test_malformed_manifests_are_ignored(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    assert commands(tmp_path) == []


def test_a_project_with_no_tooling_yields_nothing(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["httpx"]\n', encoding="utf-8"
    )
    assert commands(tmp_path) == []


def test_missing_directory_is_not_an_error(tmp_path):
    assert detect_checks(tmp_path / "nope") == []
    assert detect_checks(None) == []


def test_formatters_are_detected_with_the_lockfile_runner(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    formatter = detect_formatters(tmp_path)[0]
    assert formatter.write == "uv run ruff format ."
    assert formatter.check == "uv run ruff format --check ."


def test_a_lint_only_tool_is_never_offered_as_a_formatter(tmp_path):
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    assert commands(tmp_path) == ["flake8"]
    assert rewrites(tmp_path) == []


def test_prettier_from_a_config_file_with_no_dependency_entry(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".prettierrc").write_text("{}", encoding="utf-8")
    assert rewrites(tmp_path) == ["npx prettier --write ."]


def test_prettier_is_not_assumed_from_package_json_alone(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint ."}}), encoding="utf-8"
    )
    assert rewrites(tmp_path) == []


def test_go_and_rust_formatters(tmp_path):
    (tmp_path / "go.mod").write_text("module app\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert rewrites(tmp_path) == ["gofmt -w .", "cargo fmt"]


def test_no_manifest_means_no_formatter(tmp_path):
    assert detect_formatters(tmp_path) == []
    assert detect_formatters(None) == []
