"""Parse/compile source in memory and resolve local imports without importing the app."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "app.py", ROOT / "career_app.py"]
for directory in ("src", "config", "scripts"):
    SOURCES.extend(sorted((ROOT / directory).rglob("*.py")))


def module_path(name: str):
    path = ROOT.joinpath(*name.split("."))
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py")
    if path.is_dir():
        return path
    return None


def main() -> int:
    errors = []
    count = 0
    for path in SOURCES:
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
            compile(tree, str(path), "exec")  # Does not execute application code or emit pyc.
            count += 1
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "execute" and len(node.args) >= 2
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                        and isinstance(node.args[1], ast.Tuple)
                        and not any(isinstance(a, ast.Starred) for a in node.args[1].elts)):
                    if node.args[0].value.count("?") != len(node.args[1].elts):
                        errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: SQL placeholder count differs from tuple")
                if isinstance(node, ast.Import):
                    for name in node.names:
                        if name.name.startswith(("src.", "config.")) and module_path(name.name) is None:
                            errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: missing {name.name}")
                        if path.parent.name == "assistant" and path.name != "ui.py" and name.name == "streamlit":
                            errors.append(f"{path.relative_to(ROOT)}: service imports UI framework")
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(("src.", "config.")):
                    target = module_path(node.module)
                    if target is None:
                        errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: missing {node.module}")
                    elif target.is_dir():
                        init = target / "__init__.py"
                        declared = init.read_text(encoding="utf-8-sig") if init.exists() else ""
                        for name in node.names:
                            if module_path(node.module + "." + name.name) is None and name.name not in declared:
                                errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: missing package member {name.name}")
                    else:
                        target_tree = ast.parse(target.read_text(encoding="utf-8-sig"))
                        declared = {n.name for n in ast.walk(target_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                        declared.update(n.id for n in ast.walk(target_tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
                        for imported in ast.walk(target_tree):
                            if isinstance(imported, (ast.Import, ast.ImportFrom)):
                                declared.update(a.asname or a.name.split(".")[0] for a in imported.names)
                        for name in node.names:
                            if name.name != "*" and name.name not in declared and "__getattr__" not in declared:
                                errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: missing symbol {node.module}.{name.name}")
        except (SyntaxError, OSError, UnicodeError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
    for error in errors:
        print(error)
    print(f"Static source check: {count} Python files, {len(errors)} errors. No application code executed.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
