"""Build a source-only upload ZIP using an allowlist, never the whole workspace."""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ["app.py", "career_app.py", "pyproject.toml", "requirements.txt", "README.md",
              "SECURITY.md", "CONTRIBUTING.md", ".env.example", ".gitignore", ".editorconfig"]


def main():
    destination = ROOT / "dist" / "personal-agent-assistant-source.zip"
    destination.parent.mkdir(exist_ok=True)
    selected = [ROOT / name for name in ROOT_FILES]
    for directory, suffixes in [("src", {".py"}), ("config", {".py"}), ("scripts", {".py"}),
                                ("docs", {".md"}), (".github", {".yml", ".yaml"})]:
        selected.extend(p for p in (ROOT / directory).rglob("*") if p.suffix in suffixes and p.is_file())
    selected.append(ROOT / ".streamlit" / "config.toml")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(selected)):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError("Release contains a path outside the source workspace.")
            if path.is_file():
                archive.write(path, path.relative_to(ROOT).as_posix())
    print(destination)


if __name__ == "__main__":
    main()
