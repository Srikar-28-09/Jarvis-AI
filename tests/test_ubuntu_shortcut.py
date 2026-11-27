import importlib.util
import json
import os
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ubuntu_shortcut", ROOT / "scripts" / "ubuntu-shortcut.py"
)
shortcut = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shortcut)


class GSettings:
    def __init__(self, paths=None):
        self.settings = {shortcut.SCHEMA: {"custom-keybindings": paths or []}}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        _, operation, *rest = argv
        if operation == "list-schemas":
            return SimpleNamespace(
                returncode=0,
                stdout=f"{shortcut.SCHEMA}\n",
                stderr="",
            )
        if operation == "list-relocatable-schemas":
            return SimpleNamespace(
                returncode=0,
                stdout=f"{shortcut.CUSTOM_SCHEMA}\n",
                stderr="",
            )
        schema, *args = rest
        if operation == "range":
            key_type = "as" if args[0] == "custom-keybindings" else "s"
            return SimpleNamespace(returncode=0, stdout=f"type {key_type}\n", stderr="")
        if ":" in schema:
            schema, path = schema.split(":", 1)
            scope = self.settings.setdefault((schema, path), {})
        else:
            scope = self.settings.setdefault(schema, {})
        if operation == "get":
            value = scope.get(args[0], "")
            if isinstance(value, list) and not value:
                output = "@as []\n"
            else:
                output = repr(value) + "\n"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        if operation == "set":
            key, raw_value = args
            try:
                value = shortcut.ast.literal_eval(raw_value)
            except (ValueError, SyntaxError):
                value = raw_value
            scope[key] = value
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if operation == "reset":
            scope.pop(args[0], None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def test_default_shortcut_maps_to_gnome_accelerator_and_invalid_input_is_rejected():
    assert shortcut.DEFAULT_SHORTCUT == "Ctrl+Alt+Shift+J"
    assert shortcut.accelerator(shortcut.DEFAULT_SHORTCUT) == "<Control><Alt><Shift>J"
    assert shortcut.accelerator("Shift+Enter") == "<Shift>Return"
    assert shortcut.accelerator("Ctrl+Alt+J") == "<Control><Alt>J"
    for invalid in ("", "Enter", "Shift+Bad Key", "Shift+Shift+L", "Shift+L;rm"):
        with pytest.raises(ValueError):
            shortcut.accelerator(invalid)


def test_install_registers_launcher_without_replacing_other_shortcuts(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    other_path = f"{shortcut.KEYBINDINGS_PATH}/other/"
    runner = GSettings([other_path])

    value = shortcut.install_shortcut(str(executable), runner=runner)

    assert value == "<Control><Alt><Shift>J"
    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"] == [
        other_path,
        shortcut.JARVIS_PATH,
    ]
    settings = runner.settings[(shortcut.CUSTOM_SCHEMA, shortcut.JARVIS_PATH)]
    assert settings["name"] == "Jarvis-AI"
    assert settings["command"] == f"{str(executable)} --toggle"
    assert settings["binding"] == "<Control><Alt><Shift>J"
    shortcut.install_shortcut(str(executable), runner=runner)
    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"].count(shortcut.JARVIS_PATH) == 1


def test_set_updates_only_installed_binding_and_reports_conflict():
    other_path = f"{shortcut.KEYBINDINGS_PATH}/other/"
    runner = GSettings([shortcut.JARVIS_PATH, other_path])
    runner.settings[(shortcut.CUSTOM_SCHEMA, shortcut.JARVIS_PATH)] = {
        "binding": "<Shift>Return",
    }
    runner.settings[(shortcut.CUSTOM_SCHEMA, other_path)] = {
        "binding": "<Shift>J",
    }

    assert shortcut.set_shortcut("Ctrl+Alt+J", runner=runner) == "<Control><Alt>J"
    runner.settings[(shortcut.CUSTOM_SCHEMA, other_path)]["binding"] = "<Control><Alt><Shift>J"
    with pytest.raises(RuntimeError, match="already assigned"):
        shortcut.set_shortcut(shortcut.DEFAULT_SHORTCUT, runner=runner)


def test_install_rejects_existing_conflict_without_adding_jarvis_binding(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    other_path = f"{shortcut.KEYBINDINGS_PATH}/other/"
    runner = GSettings([other_path])
    runner.settings[(shortcut.CUSTOM_SCHEMA, other_path)] = {
        "binding": "<Control><Alt><Shift>J",
    }

    with pytest.raises(RuntimeError, match="already assigned"):
        shortcut.install_shortcut(str(executable), runner=runner)
    assert shortcut.JARVIS_PATH not in runner.settings[shortcut.SCHEMA]["custom-keybindings"]


def test_remove_preserves_other_shortcuts():
    other_path = f"{shortcut.KEYBINDINGS_PATH}/other/"
    runner = GSettings([shortcut.JARVIS_PATH, other_path])

    shortcut.remove_shortcut(runner=runner)

    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"] == [other_path]
    assert len([call for call in runner.calls if call[1] == "reset"]) == 3


def test_desktop_launcher_resolves_binary_from_arbitrary_working_directory(tmp_path):
    app_root = tmp_path / "app"
    launcher = app_root / "desktop" / "bin" / "odysseus-voice"
    binary = app_root / "desktop" / "src-tauri" / "target" / "release" / "odysseus-voice"
    launcher.parent.mkdir(parents=True)
    binary.parent.mkdir(parents=True)
    launcher.write_text((ROOT / "desktop" / "bin" / "odysseus-voice").read_text())
    binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n")
    launcher.chmod(0o755)
    binary.chmod(0o755)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    capture = tmp_path / "args.txt"

    result = subprocess.run(
        [str(launcher), "--toggle"],
        cwd=unrelated,
        env={**os.environ, "CAPTURE_ARGS": str(capture)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text().strip() == "--toggle"


def test_backend_unit_uses_existing_compose_project_and_preserves_volumes():
    unit = (ROOT / "odysseus-backend.service").read_text()

    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "compose up -d" in unit
    assert "compose down" in unit
    assert "-v" not in unit


def test_installation_registers_shortcut_to_absolute_launcher_and_managed_units():
    installer = (ROOT / "install-ubuntu-desktop.sh").read_text()

    assert 'ln -sfn "${PROJECT_DIR}/desktop/bin/odysseus-voice" "${LAUNCHER}"' in installer
    assert 'install --launcher "${LAUNCHER}" --shortcut Ctrl+Alt+Shift+J' in installer
    assert "systemctl --user daemon-reload" in installer
    assert "npm run tauri build -- --bundles deb" in installer
    assert "trap cleanup EXIT" in installer
    assert "cleanup restored" in installer
    assert "MANAGED_BACKUPS" in installer
    assert "cleanup was incomplete" in installer


def test_tauri_bundle_uses_a_square_512_pixel_png_icon():
    config = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
    icon_paths = config["bundle"]["icon"]
    icon_path = ROOT / "desktop" / "src-tauri" / icon_paths[0]
    with icon_path.open("rb") as icon:
        header = icon.read(24)

    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", header[16:24])
    assert (width, height) == (512, 512)


@pytest.mark.parametrize(
    "output, expected",
    [
        ("@as []", []),
        ("  []  \n", []),
        (
            "@as ['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']\n",
            [f"{shortcut.KEYBINDINGS_PATH}/custom0/"],
        ),
        (
            " [ '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/' ] ",
            [f"{shortcut.KEYBINDINGS_PATH}/custom0/"],
        ),
    ],
)
def test_parses_gsettings_string_array_formats(output, expected):
    assert shortcut._parse_paths(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "",
        "@as",
        "@as 'not an array'",
        "['/some/other/schema/custom0/']",
        "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/../']",
        "[1]",
        "@invalid []",
    ],
)
def test_rejects_malformed_or_unexpected_custom_shortcut_output(output):
    with pytest.raises(RuntimeError, match="invalid custom-shortcut list"):
        shortcut._parse_paths(output)


def test_install_collapses_duplicate_jarvis_paths_and_preserves_other_shortcuts(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    other_path = f"{shortcut.KEYBINDINGS_PATH}/custom0/"
    runner = GSettings([shortcut.JARVIS_PATH, other_path, shortcut.JARVIS_PATH])

    shortcut.install_shortcut(str(executable), runner=runner)

    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"] == [
        shortcut.JARVIS_PATH,
        other_path,
    ]


def test_install_migrates_existing_jarvis_binding_to_new_default(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    other_path = f"{shortcut.KEYBINDINGS_PATH}/custom0/"
    runner = GSettings([other_path, shortcut.JARVIS_PATH])
    runner.settings[(shortcut.CUSTOM_SCHEMA, shortcut.JARVIS_PATH)] = {
        "name": "Jarvis-AI",
        "command": "/old/launcher --toggle",
        "binding": "<Shift>Return",
    }

    shortcut.install_shortcut(str(executable), runner=runner)

    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"] == [
        other_path,
        shortcut.JARVIS_PATH,
    ]
    assert runner.settings[(shortcut.CUSTOM_SCHEMA, shortcut.JARVIS_PATH)]["binding"] == (
        "<Control><Alt><Shift>J"
    )
    assert runner.settings[(shortcut.CUSTOM_SCHEMA, other_path)] == {}


def test_install_failure_rolls_back_new_jarvis_settings_and_path(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    class FailingSettings(GSettings):
        def __call__(self, argv, **kwargs):
            if argv[1:4] == ["set", shortcut.CUSTOM_SCHEMA + ":" + shortcut.JARVIS_PATH, "binding"]:
                raise RuntimeError("simulated GNOME write failure")
            return super().__call__(argv, **kwargs)

    runner = FailingSettings()
    with pytest.raises(RuntimeError, match="simulated GNOME write failure"):
        shortcut.install_shortcut(str(executable), runner=runner)

    assert runner.settings[shortcut.SCHEMA]["custom-keybindings"] == []
    assert runner.settings[(shortcut.CUSTOM_SCHEMA, shortcut.JARVIS_PATH)] == {}


def test_schema_type_mismatch_fails_before_any_settings_are_written(tmp_path):
    executable = tmp_path / "odysseus-voice"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    class WrongSchema(GSettings):
        def __call__(self, argv, **kwargs):
            if argv[1:4] == ["range", shortcut.SCHEMA, "custom-keybindings"]:
                return SimpleNamespace(returncode=0, stdout="type s\n", stderr="")
            return super().__call__(argv, **kwargs)

    runner = WrongSchema()
    with pytest.raises(RuntimeError, match="not the expected string-array type"):
        shortcut.install_shortcut(str(executable), runner=runner)

    assert not any(call[1] == "set" for call in runner.calls)


def test_installer_restores_existing_files_when_shortcut_registration_fails(tmp_path):
    project = tmp_path / "project"
    (project / "desktop" / "bin").mkdir(parents=True)
    binary = project / "desktop" / "src-tauri" / "target" / "release" / "odysseus-voice"
    binary.parent.mkdir(parents=True)
    binary.write_text("built binary")
    binary.chmod(0o755)
    launcher_script = project / "desktop" / "bin" / "odysseus-voice"
    launcher_script.write_text("#!/bin/sh\nexit 0\n")
    launcher_script.chmod(0o755)
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "ubuntu-shortcut.py").write_text(
        "import sys\nprint('simulated GNOME registration failure', file=sys.stderr)\nsys.exit(1)\n"
    )
    (project / "odysseus-host-bridge.service").write_text("new host unit\n")
    (project / "odysseus-backend.service").write_text("new backend unit\n")
    installer = project / "install-ubuntu-desktop.sh"
    installer.write_text((ROOT / "install-ubuntu-desktop.sh").read_text())

    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    libexec_dir = home / ".local" / "libexec"
    desktop_dir = home / ".local" / "share" / "applications"
    unit_dir = home / ".config" / "systemd" / "user"
    for path in (bin_dir, libexec_dir, desktop_dir, unit_dir):
        path.mkdir(parents=True)

    prior_launcher = tmp_path / "prior-launcher"
    prior_launcher.write_text("old launcher target")
    (bin_dir / "odysseus-voice").symlink_to(prior_launcher)
    prior_files = {
        libexec_dir / "odysseus-ubuntu-shortcut.py": "old helper\n",
        unit_dir / "odysseus-host-bridge.service": "old host unit\n",
        unit_dir / "odysseus-backend.service": "old backend unit\n",
        desktop_dir / "odysseus-voice.desktop": "old desktop entry\n",
    }
    for path, contents in prior_files.items():
        path.write_text(contents)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in ("gsettings", "systemctl", "docker"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(installer)],
        env={
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CURRENT_DESKTOP": "GNOME",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "simulated GNOME registration failure" in result.stderr
    assert "cleanup restored previous launcher and service files" in result.stderr
    assert (bin_dir / "odysseus-voice").is_symlink()
    assert os.readlink(bin_dir / "odysseus-voice") == str(prior_launcher)
    for path, contents in prior_files.items():
        assert path.read_text() == contents
