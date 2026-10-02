"""Agent-side code must never reach verifier capabilities or task checks."""

import ast
from pathlib import Path

import harness

FORBIDDEN = ("harness.device.inspect", "harness.verify", "harness.tasks", "harness.seed")


def forbidden_imports(source: str, package: str = "harness.agent") -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        hits += [n for n in names if any(n == f or n.startswith(f + ".") for f in FORBIDDEN)]
    return hits


def test_agent_modules_do_not_import_verifier_side_modules():
    agent_dir = Path(harness.__file__).parent / "agent"
    assert agent_dir.is_dir()
    for path in agent_dir.rglob("*.py"):
        assert forbidden_imports(path.read_text()) == [], path


def test_detector_catches_each_import_form():
    assert forbidden_imports("import harness.device.inspect")
    assert forbidden_imports("from harness.device import inspect")
    assert forbidden_imports("from harness.verify.runner import run_verifier")
    assert forbidden_imports("from ..device import inspect")
    assert forbidden_imports("from ..tasks import registry")
    assert forbidden_imports("from .. import seed")
    assert not forbidden_imports("from harness.contracts import Device\nimport harness.device.adb")
    assert not forbidden_imports("from ..device import adb")
