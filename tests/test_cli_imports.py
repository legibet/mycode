"""Tests for CLI import side effects."""

import importlib
import sys


def test_importing_cli_does_not_import_server_app() -> None:
    sys.modules.pop("mycode.cli.main", None)
    sys.modules.pop("mycode.server.app", None)

    importlib.import_module("mycode.cli.main")

    assert "mycode.server.app" not in sys.modules
