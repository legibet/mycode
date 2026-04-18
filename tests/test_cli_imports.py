"""Tests for CLI import side effects."""

import importlib
import sys


def test_importing_cli_does_not_import_server_app() -> None:
    sys.modules.pop("mycode_cli.main", None)
    sys.modules.pop("mycode_cli.server.app", None)

    importlib.import_module("mycode_cli.main")

    assert "mycode_cli.server.app" not in sys.modules
