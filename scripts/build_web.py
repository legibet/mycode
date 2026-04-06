from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
WEB_DIST_DIR = WEB_DIR / "dist"
STATIC_DIR = ROOT_DIR / "mycode" / "server" / "static"


def build_web() -> None:
    if not WEB_DIR.is_dir():
        raise RuntimeError(f"web source directory not found: {WEB_DIR}")

    _run_pnpm(["install", "--frozen-lockfile"], cwd=WEB_DIR)
    _run_pnpm(["build"], cwd=WEB_DIR)

    if not WEB_DIST_DIR.is_dir():
        raise RuntimeError(f"web build output not found: {WEB_DIST_DIR}")

    if STATIC_DIR.exists():
        shutil.rmtree(STATIC_DIR)
    _ = shutil.copytree(WEB_DIST_DIR, STATIC_DIR)


def _run_pnpm(args: list[str], *, cwd: Path) -> None:
    try:
        _ = subprocess.run(["pnpm", *args], cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pnpm is required to build the web assets") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pnpm {' '.join(args)} failed with exit code {exc.returncode}") from exc


if __name__ == "__main__":
    build_web()
