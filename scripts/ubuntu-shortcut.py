#!/usr/bin/env python3
"""Install and update Jarvis' cold-start GNOME keyboard shortcut."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = f"{SCHEMA}.custom-keybinding"
KEYBINDINGS_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
JARVIS_PATH = f"{KEYBINDINGS_PATH}/jarvis/"
DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+J"

_MODIFIERS = {
    "shift": "<Shift>",
    "ctrl": "<Control>",
    "control": "<Control>",
    "alt": "<Alt>",
    "super": "<Super>",
    "win": "<Super>",
}
_NAMED_KEYS = {
    "enter": "Return",
    "return": "Return",
    "space": "space",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


def accelerator(shortcut: str) -> str:
    parts = [part.strip() for part in shortcut.split("+")]
    if len(parts) < 2 or any(not part for part in parts):
        raise ValueError("Use at least one modifier and one key, such as Ctrl+Alt+Shift+J")
    modifiers = []
    for modifier in parts[:-1]:
        value = _MODIFIERS.get(modifier.lower())
        if value is None or value in modifiers:
            raise ValueError(f"Unsupported or duplicate shortcut modifier: {modifier}")
        modifiers.append(value)
    key = parts[-1]
    normalized_key = _NAMED_KEYS.get(key.lower())
    if normalized_key is None:
        if re.fullmatch(r"[A-Za-z0-9]", key):
            normalized_key = key.upper() if key.isalpha() else key
        elif re.fullmatch(r"F(?:[1-9]|1[0-2])", key, re.IGNORECASE):
            normalized_key = key.upper()
        else:
            raise ValueError(f"Unsupported shortcut key: {key}")
    return "".join(modifiers) + normalized_key


def _run_gsettings(*args: str, runner=subprocess.run) -> str:
    try:
        result = runner(
            ["gsettings", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "Could not run gsettings; verify that GNOME Settings Daemon is installed "
            "and the user session is available."
        ) from error
    if result.returncode:
        message = (result.stderr or "").strip()
        raise RuntimeError(message or "gsettings could not update the GNOME shortcut")
    return result.stdout.strip()


def _validate_schema(runner=subprocess.run) -> None:
    if not _valid_path(JARVIS_PATH):
        raise RuntimeError("The configured Jarvis GNOME keybinding path is invalid.")
    schemas = set(_run_gsettings("list-schemas", runner=runner).splitlines())
    relocatable = set(
        _run_gsettings("list-relocatable-schemas", runner=runner).splitlines()
    )
    if SCHEMA not in schemas:
        raise RuntimeError(
            f"GNOME schema {SCHEMA} is unavailable; install or enable GNOME Settings Daemon."
        )
    if CUSTOM_SCHEMA not in relocatable:
        raise RuntimeError(
            f"GNOME custom-shortcut schema {CUSTOM_SCHEMA} is unavailable."
        )
    if _run_gsettings("range", SCHEMA, "custom-keybindings", runner=runner) != "type as":
        raise RuntimeError(
            f"GNOME setting {SCHEMA}.custom-keybindings is not the expected string-array type."
        )
    for key in ("name", "command", "binding"):
        if _run_gsettings("range", CUSTOM_SCHEMA, key, runner=runner) != "type s":
            raise RuntimeError(
                f"GNOME custom-shortcut key {key} is not the expected string type."
            )


def _valid_path(path: str) -> bool:
    prefix = f"{KEYBINDINGS_PATH}/"
    if not path.startswith(prefix) or not path.endswith("/"):
        return False
    components = path[len(prefix) : -1].split("/")
    return bool(components) and all(
        re.fullmatch(r"[A-Za-z0-9_]+", component) for component in components
    )


def _parse_paths(output: str) -> list[str]:
    value = output.strip()
    if value.startswith("@as"):
        value = re.sub(r"^@as\s+", "", value, count=1)
    elif value.startswith("@"):
        value = ""
    try:
        paths = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise RuntimeError(
            "GNOME returned an invalid custom-shortcut list; expected a GVariant "
            "string array such as '@as []'."
        ) from error
    if not isinstance(paths, list) or not all(
        isinstance(path, str) and _valid_path(path) for path in paths
    ):
        raise RuntimeError(
            "GNOME returned an invalid custom-shortcut list; paths must be "
            f"beneath {KEYBINDINGS_PATH}/ and end with '/'."
        )
    return paths


def _parse_string(output: str, key: str) -> str:
    value = output.strip()
    if value.startswith("@s"):
        value = re.sub(r"^@s\s+", "", value, count=1)
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise RuntimeError(f"GNOME returned an invalid value for custom shortcut {key}.") from error
    if not isinstance(parsed, str):
        raise RuntimeError(f"GNOME returned an invalid value for custom shortcut {key}.")
    return parsed


def _paths(runner=subprocess.run) -> list[str]:
    result = _run_gsettings("get", SCHEMA, "custom-keybindings", runner=runner)
    return _parse_paths(result)


def _custom_key(path: str) -> str:
    return f"{CUSTOM_SCHEMA}:{path}"


def _check_conflicts(target: str, paths: list[str], runner) -> None:
    for path in paths:
        if path == JARVIS_PATH:
            continue
        existing = _run_gsettings("get", _custom_key(path), "binding", runner=runner)
        if _parse_string(existing, "binding") == target:
            raise RuntimeError("That shortcut is already assigned to another GNOME custom shortcut.")


def set_shortcut(shortcut: str, *, runner=subprocess.run) -> str:
    target = accelerator(shortcut)
    _validate_schema(runner)
    paths = _paths(runner)
    if JARVIS_PATH not in paths:
        raise RuntimeError(
            "Jarvis' GNOME launcher is not installed. Run install-ubuntu-desktop.sh first."
        )
    _check_conflicts(target, paths, runner)
    schema_key = _custom_key(JARVIS_PATH)
    original = _run_gsettings("get", schema_key, "binding", runner=runner)
    _parse_string(original, "binding")
    try:
        _run_gsettings("set", schema_key, "binding", json.dumps(target), runner=runner)
        confirmed = _run_gsettings("get", schema_key, "binding", runner=runner)
        if _parse_string(confirmed, "binding") != target:
            raise RuntimeError("GNOME did not retain the requested shortcut.")
    except (OSError, RuntimeError) as error:
        try:
            _run_gsettings("set", schema_key, "binding", original, runner=runner)
        except (OSError, RuntimeError) as rollback_error:
            raise RuntimeError(
                f"{error}; restoring the previous shortcut also failed: {rollback_error}"
            ) from error
        raise
    return target


def install_shortcut(launcher: str, shortcut: str = DEFAULT_SHORTCUT, *, runner=subprocess.run) -> str:
    launcher = os.path.abspath(launcher)
    if not os.path.isfile(launcher) or not os.access(launcher, os.X_OK):
        raise ValueError("The Jarvis launcher must exist and be executable.")
    target = accelerator(shortcut)
    _validate_schema(runner)
    paths = _paths(runner)
    _check_conflicts(target, paths, runner)
    had_jarvis = JARVIS_PATH in paths
    updated_paths = []
    jarvis_added = False
    for path in paths:
        if path == JARVIS_PATH:
            if not jarvis_added:
                updated_paths.append(path)
                jarvis_added = True
        else:
            updated_paths.append(path)
    if not jarvis_added:
        updated_paths.append(JARVIS_PATH)

    original_values = {}
    if had_jarvis:
        for key in ("name", "command", "binding"):
            raw_value = _run_gsettings("get", _custom_key(JARVIS_PATH), key, runner=runner)
            _parse_string(raw_value, key)
            original_values[key] = raw_value

    command = f"{shlex.quote(launcher)} --toggle"
    try:
        for key, value in (("name", "Jarvis-AI"), ("command", command), ("binding", target)):
            _run_gsettings(
                "set", _custom_key(JARVIS_PATH), key, json.dumps(value), runner=runner
            )
            confirmed = _run_gsettings("get", _custom_key(JARVIS_PATH), key, runner=runner)
            if _parse_string(confirmed, key) != value:
                raise RuntimeError(f"GNOME did not retain the Jarvis shortcut {key}.")
        if updated_paths != paths:
            _run_gsettings(
                "set", SCHEMA, "custom-keybindings", repr(updated_paths), runner=runner
            )
            if _paths(runner) != updated_paths:
                raise RuntimeError("GNOME did not retain the Jarvis custom-shortcut path.")
    except (OSError, RuntimeError) as error:
        rollback_errors = []
        for key in ("name", "command", "binding"):
            try:
                if had_jarvis:
                    _run_gsettings(
                        "set", _custom_key(JARVIS_PATH), key, original_values[key], runner=runner
                    )
                else:
                    _run_gsettings("reset", _custom_key(JARVIS_PATH), key, runner=runner)
            except (OSError, RuntimeError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        if updated_paths != paths:
            try:
                _run_gsettings(
                    "set", SCHEMA, "custom-keybindings", repr(paths), runner=runner
                )
            except (OSError, RuntimeError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                f"{error}; rollback was incomplete: {'; '.join(rollback_errors)}"
            ) from error
        raise
    return target


def remove_shortcut(*, runner=subprocess.run) -> None:
    _validate_schema(runner)
    paths = _paths(runner)
    if JARVIS_PATH not in paths:
        return
    remaining_paths = [path for path in paths if path != JARVIS_PATH]
    _run_gsettings(
        "set", SCHEMA, "custom-keybindings", repr(remaining_paths), runner=runner
    )
    for key in ("name", "command", "binding"):
        _run_gsettings("reset", _custom_key(JARVIS_PATH), key, runner=runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("install")
    install.add_argument("--launcher", required=True)
    install.add_argument("--shortcut", default=DEFAULT_SHORTCUT)
    update = commands.add_parser("set")
    update.add_argument("--shortcut", required=True)
    commands.add_parser("remove")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            result = install_shortcut(args.launcher, args.shortcut)
            print(f"Registered Jarvis shortcut: {args.shortcut} ({result})")
        elif args.action == "set":
            result = set_shortcut(args.shortcut)
            print(f"Updated Jarvis shortcut: {args.shortcut} ({result})")
        else:
            remove_shortcut()
            print("Removed Jarvis GNOME shortcut.")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Jarvis shortcut setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
