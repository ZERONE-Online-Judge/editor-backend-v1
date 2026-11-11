"""Resolve backup metadata from the active release, never another deployment."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "backup" / "editor_backup.py"
SPEC = importlib.util.spec_from_file_location("editor_backup_slots", SOURCE)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


@pytest.fixture
def tool(tmp_path):
    root = tmp_path / "editor"
    (root / "config").mkdir(parents=True)
    return backup.Backup({"storage_root": str(root), "mount": str(tmp_path / "backup"),
                          "password_file": str(tmp_path / "password")})


@pytest.fixture
def inventory(monkeypatch):
    rows = []
    commands = []
    def docker(command):
        assert command[:2] == ["docker", "ps"]
        commands.append(command)
        filters = [command[index + 1] for index, value in enumerate(command) if value == "--filter"]
        assert set(filters) >= {"label=com.docker.compose.project=zoj-editor"}
        labels = [value.removeprefix("label=").split("=", 1) for value in filters]
        selected = [row["id"] for row in rows if row.get("running", True)
                    and all(row.get(key) == value for key, value in labels)]
        return ("\n".join(selected) + "\n").encode()
    monkeypatch.setattr(backup, "run", docker)
    def add(ident, service, project="zoj-editor", running=True):
        rows.append({"id": ident, "com.docker.compose.project": project,
                     "com.docker.compose.service": service, "running": running})
    return add, commands


@pytest.mark.parametrize("slot", ["blue", "green"])
def test_backup_selects_active_slot_with_both_releases_running(tool, inventory, slot):
    add, commands = inventory
    (tool.root / "config/release.env").write_text(
        f"EDITOR_API_IMAGE=zoj-editor-api:tested\nEDITOR_ACTIVE_SLOT={slot}\n")
    for service in ("api", "api-blue", "api-green"):
        add("editor-" + service, service)
        add("foreign-" + service, service, project="another-editor")
    add("stopped-active-api", "api-" + slot, running=False)
    assert tool.db_container("api") == "editor-api-" + slot
    assert f"label=com.docker.compose.service=api-{slot}" in commands[0]


@pytest.mark.parametrize("manifest", [None, "EDITOR_API_IMAGE=zoj-editor-api:legacy\n"])
def test_backup_preserves_legacy_api_fallback_without_active_slot(tool, inventory, manifest):
    add, _ = inventory
    if manifest is not None:
        (tool.root / "config/release.env").write_text(manifest)
    add("legacy-api", "api")
    add("inactive-blue-api", "api-blue")
    add("foreign-api", "api", project="another-editor")
    assert tool.db_container("api") == "legacy-api"


@pytest.mark.parametrize("value", ["", "legacy", "red", " blue", "green ", "../blue", "api-green"])
def test_invalid_active_slot_fails_before_querying_docker(tool, monkeypatch, value):
    (tool.root / "config/release.env").write_text(f"EDITOR_ACTIVE_SLOT={value}\n")
    docker = Mock()
    monkeypatch.setattr(backup, "run", docker)
    with pytest.raises(RuntimeError, match="Invalid active Editor release slot"):
        tool.db_container("api")
    docker.assert_not_called()


@pytest.mark.parametrize("second", ["blue", "green"])
def test_duplicate_active_slot_fails_even_when_values_match(tool, monkeypatch, second):
    (tool.root / "config/release.env").write_text(
        f"EDITOR_ACTIVE_SLOT=blue\nEDITOR_ACTIVE_SLOT={second}\n")
    docker = Mock()
    monkeypatch.setattr(backup, "run", docker)
    with pytest.raises(RuntimeError, match="Invalid active Editor release slot"):
        tool.db_container("api")
    docker.assert_not_called()


@pytest.mark.parametrize("count", [0, 2])
def test_backup_requires_exactly_one_running_api_in_selected_project(tool, inventory, count):
    add, _ = inventory
    (tool.root / "config/release.env").write_text("EDITOR_ACTIVE_SLOT=green\n")
    add("foreign-green", "api-green", project="another-editor")
    add("old-blue", "api-blue")
    for index in range(count):
        add(f"selected-green-{index}", "api-green")
    with pytest.raises(RuntimeError, match="Expected exactly one running Editor api-green container"):
        tool.db_container("api")


def test_database_container_selection_is_independent_of_release_slot(tool, inventory):
    add, commands = inventory
    (tool.root / "config/release.env").write_text("EDITOR_ACTIVE_SLOT=invalid\n")
    add("editor-database", "db")
    add("foreign-database", "db", project="another-editor")
    assert tool.db_container() == "editor-database"
    assert "label=com.docker.compose.service=db" in commands[0]
