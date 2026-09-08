"""Tests for CLI import side effects."""

import subprocess
import sys
import textwrap


def test_startup_imports_only_required_modules() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import sys
                import logging
                from mycode import Agent
                from mycode.providers import get_provider_adapter, list_supported_providers
                from mycode_cli.main import app

                assert "mycode_cli.server.app" not in sys.modules

                root = logging.getLogger()
                handlers, level = list(root.handlers), root.level
                from mycode_cli.server.app import create_api_app
                assert root.handlers == handlers and root.level == level
                from typer.testing import CliRunner

                for provider in list_supported_providers():
                    adapter = get_provider_adapter(provider)
                    adapter.api_key_from_env()
                    adapter.can_authenticate_from_env()
                    Agent(provider=provider, model="test-model", api_key="test-key")
                create_api_app()
                for option in ("--version", "--help"):
                    result = CliRunner().invoke(app, [option])
                    assert result.exit_code == 0, result.output
                loaded = {name for name in ("openai", "anthropic", "google.genai") if name in sys.modules}
                assert not loaded, loaded
            """),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
