"""Shared by the tests: the hygiene check, loaded from the action's script as it ships."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_hygiene():
    spec = importlib.util.spec_from_file_location(
        "check_repo_hygiene", ROOT / "actions" / "repo-hygiene" / "check-repo-hygiene.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
