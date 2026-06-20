"""Hatch hooks for mycode-cli packaging."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # noqa: E402
from hatchling.metadata.plugin.interface import MetadataHookInterface  # noqa: E402


def _run_pnpm(args: list[str], *, cwd: Path) -> None:
    try:
        _ = subprocess.run(["pnpm", *args], cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pnpm is required to build the web assets") from exc
    except subprocess.CalledProcessError as exc:
        joined = " ".join(args)
        raise RuntimeError(f"pnpm {joined} failed with exit code {exc.returncode}") from exc


def _build_web_assets(project_root: Path) -> None:
    repo_root = project_root.parent
    web_dir = repo_root / "web"
    web_dist_dir = web_dir / "dist"
    static_dir = project_root / "src" / "mycode_cli" / "server" / "static"

    if not (web_dir / "package.json").is_file():
        if static_dir.is_dir():
            return
        raise RuntimeError(
            f"web sources not found at {web_dir}; the web/ submodule is not "
            "initialized. Run: git submodule update --init --recursive"
        )

    _run_pnpm(["install", "--frozen-lockfile"], cwd=web_dir)
    _run_pnpm(["build"], cwd=web_dir)

    if not web_dist_dir.is_dir():
        raise RuntimeError(f"web build output not found: {web_dist_dir}")

    if static_dir.exists():
        shutil.rmtree(static_dir)
    _ = shutil.copytree(web_dist_dir, static_dir)


def _readme_text(project_root: Path) -> str:
    candidates = [project_root.parent / "README.md", project_root / "README.md"]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    searched = ", ".join(str(path) for path in candidates)
    raise RuntimeError(f"README.md not found; checked: {searched}")


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict[str, Any]) -> None:
        metadata["readme"] = {
            "text": _readme_text(Path(self.root)),
            "content-type": "text/markdown",
        }


class CustomBuildHook(BuildHookInterface[Any]):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del build_data
        # Editable installs (uv sync) don't bundle web assets, so skip the build
        # and its web/ submodule + pnpm requirements. Wheel/sdist builds use "standard".
        if version == "editable":
            return
        _build_web_assets(Path(self.root))
