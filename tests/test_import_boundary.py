"""Agent-side code must never reach verifier capabilities or task checks."""

import ast
from pathlib import Path

import pytest

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


TASK_ONLY_NAMES = ("sensitive_actions", "TaskSpec")


def task_only_references(source: str) -> list[str]:
    """Identifiers, attributes, imported names, keyword and parameter names,
    and non-docstring string literals that name task-only fields."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            found.append(n.attr)
        elif isinstance(n, ast.Name):
            found.append(n.id)
        elif isinstance(n, ast.alias):
            found += [n.name.rsplit(".", 1)[-1], n.asname or ""]
        elif isinstance(n, ast.keyword):
            found.append(n.arg or "")
        elif isinstance(n, ast.arg):
            found.append(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            found += [w for w in TASK_ONLY_NAMES if w in n.value]
    return [f for f in found if f in TASK_ONLY_NAMES]


def test_agent_modules_never_read_task_sensitive_actions():
    """The sensitive-action policy is a general prompt rule; per-task lists
    are for the verifier only and must not reach agent-side code."""
    agent_dir = Path(harness.__file__).parent / "agent"
    paths = list(agent_dir.rglob("*.py"))
    assert paths
    for path in paths:
        assert task_only_references(path.read_text()) == [], path


@pytest.mark.parametrize("source", [
    "task.sensitive_actions",
    "sensitive_actions = []",
    "from harness.contracts import TaskSpec",
    "import harness.contracts.TaskSpec as T",
    "f(sensitive_actions=[])",
    "def f(sensitive_actions): pass",
    "getattr(task, 'sensitive_actions')",
    "x = 'read sensitive_actions here'",
])
def test_task_only_detector_catches_each_form(source):
    assert task_only_references(source)


def test_task_only_detector_ignores_docstrings():
    src = '"""Never read sensitive_actions or TaskSpec."""\n\ndef f():\n    """sensitive_actions stays out."""\n'
    assert task_only_references(src) == []
