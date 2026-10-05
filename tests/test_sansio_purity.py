"""Enforce the sans-io contract by AST-scanning the package.

No module under ``wire/``, ``packets/``, ``session/`` or ``devices/`` may import
``socket`` or ``asyncio``, or call ``time.time()`` / any other clock.  Time
enters the library exactly once, through ``DeviceConnection.advance(now)``.

``io/`` is exempt: it is the layer that exists to do I/O.  ``imaging/`` is a
separate leaf module with its own tests and is not covered here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import pyvisionect

PACKAGE_ROOT = Path(pyvisionect.__file__).parent
PURE_SUBPACKAGES = ("wire", "packets", "session", "devices")

FORBIDDEN_MODULES = {
    "socket",
    "asyncio",
    "ssl",
    "select",
    "selectors",
    "serial",
    "subprocess",
    "http",
    "urllib",
    "requests",
    "threading",
    "multiprocessing",
    "time",
    "datetime",
    "sched",
}

FORBIDDEN_CALLS = {
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("time", "sleep"),
    ("time", "time_ns"),
    ("time", "monotonic_ns"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
}


def pure_modules() -> list[Path]:
    paths: list[Path] = []
    for subpackage in PURE_SUBPACKAGES:
        paths.extend(sorted((PACKAGE_ROOT / subpackage).rglob("*.py")))
    assert paths, "found no modules to scan -- the layout must have changed"
    return paths


def test_the_pure_subpackages_exist() -> None:
    for subpackage in PURE_SUBPACKAGES:
        assert (PACKAGE_ROOT / subpackage / "__init__.py").is_file()
    assert len(pure_modules()) >= 12


@pytest.mark.parametrize("path", pure_modules(), ids=lambda p: p.name)
def test_no_io_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_MODULES, (
                    f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} imports "
                    f"{alias.name!r}, which breaks the sans-io contract"
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            assert root not in FORBIDDEN_MODULES, (
                f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} imports from "
                f"{node.module!r}, which breaks the sans-io contract"
            )


@pytest.mark.parametrize("path", pure_modules(), ids=lambda p: p.name)
def test_no_clock_calls(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            assert pair not in FORBIDDEN_CALLS, (
                f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} calls "
                f"{pair[0]}.{pair[1]}(); pass the clock in via advance(now)"
            )


def test_io_layer_is_the_only_place_with_io() -> None:
    """Positive control: the scan would actually catch an import."""
    tcp = (PACKAGE_ROOT / "io" / "tcp.py").read_text(encoding="utf-8")
    assert "import asyncio" in tcp
    assert "import time" in tcp
    tree = ast.parse(tcp)
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert roots & FORBIDDEN_MODULES, "the detector must flag io/tcp.py"
