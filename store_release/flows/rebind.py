"""Set a ported module's app constants and recompute everything derived from them.

The ported scripts compute paths and tables from ROOT and their app constants at
import time (METADATA_DIR = ROOT / "store" / ..., SCREENSHOT_SETS = (...), default
arguments such as `root: Path = ROOT`). rebind() assigns the new values, then
re-runs, in source order, every top-level assignment, function definition and
class definition whose value, defaults or body read a name that changed, so the
module ends up exactly as if its source had been written with the new values.
Nothing else in the module is touched.
"""
from __future__ import annotations

import __future__
import ast
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

_FLAGS = __future__.annotations.compiler_flag


def _loaded(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                names.add(child.id)
    return names


def _targets(node: ast.stmt) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: set[str] = set()
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.add(child.id)
    return names


def _function_reads(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    defaults = [*args.defaults, *(d for d in args.kw_defaults if d is not None), *node.decorator_list]
    return _loaded(defaults)


def _class_reads(node: ast.ClassDef) -> set[str]:
    # Bases, decorators and class-level statements other than methods (methods read
    # module globals at call time); method defaults are evaluated at class creation.
    parts: list[ast.AST] = [*node.bases, *node.decorator_list, *(k.value for k in node.keywords)]
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.extend(item.args.defaults)
            parts.extend(d for d in item.args.kw_defaults if d is not None)
            parts.extend(item.decorator_list)
        else:
            parts.append(item)
    return _loaded(parts)


def rebind(module: ModuleType, overrides: Mapping[str, Any]) -> list[str]:
    """Assign overrides to module and recompute what depends on them; returns the names rebound."""
    unknown = sorted(name for name in overrides if name not in module.__dict__)
    if unknown:
        raise AttributeError(f"{module.__name__} has no {', '.join(unknown)} to configure")
    source_path = Path(module.__file__ or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    module.__dict__.update(overrides)
    dirty = set(overrides)
    rebound: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            targets = _targets(node)
            if targets & set(overrides):
                continue  # the configured value wins over the source's own assignment
            if not (_loaded([node.value]) & dirty):
                continue
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not (_function_reads(node) & dirty):
                continue
            targets = {node.name}
        elif isinstance(node, ast.ClassDef):
            if not (_class_reads(node) & dirty):
                continue
            targets = {node.name}
        else:
            continue
        code = compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec",
                       flags=_FLAGS, dont_inherit=True)
        exec(code, module.__dict__)  # noqa: S102 - the module's own source, re-run with its new constants
        dirty |= targets
        rebound.extend(sorted(targets))
    return rebound


def unconfigured_values(module: ModuleType, placeholders: tuple[object, ...]) -> list[str]:
    """Module globals (and function defaults) that still hold or derive from a placeholder."""
    found: list[str] = []
    texts = [str(p) for p in placeholders]

    def holds(value: Any, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(value, (str, Path)):
            return any(text in str(value) for text in texts)
        if isinstance(value, (tuple, list, set, frozenset)):
            return any(holds(item, depth + 1) for item in value)
        if isinstance(value, dict):
            return any(holds(k, depth + 1) or holds(v, depth + 1) for k, v in value.items())
        fields = getattr(value, "_fields", None) or getattr(value, "__dataclass_fields__", None)
        if fields:
            return any(holds(getattr(value, name, None), depth + 1) for name in fields)
        return False

    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if callable(value) and getattr(value, "__module__", None) == module.__name__:
            defaults = (getattr(value, "__defaults__", None) or ()) + tuple(
                (getattr(value, "__kwdefaults__", None) or {}).values())
            if any(holds(item) for item in defaults):
                found.append(f"{name} (a default argument)")
            continue
        if isinstance(value, ModuleType):
            continue
        if holds(value):
            found.append(name)
    return found


def module(name: str) -> ModuleType:
    return sys.modules[name]
