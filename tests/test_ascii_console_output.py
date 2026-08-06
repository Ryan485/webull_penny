"""Runtime print()/logger.*() string literals must be plain ASCII.

CLAUDE.md's own hard-won rule: "Windows console is cp949 (Korean). No
em-dashes/fancy Unicode in anything logged -- it crashes console logging."
Codex NEW-CP949-CONSOLE-OUTPUT (round 8, 2026-08-05) found two live
violations of this rule (an em dash in backtest_viral.py's summary header
and trading/broker.py's PaperBroker startup log) that neither of those had
ever been exercised against a real cp949 stream. A third instance (also an
em dash, trading/broker.py's phantom-position warning) was found and fixed
in the same pass while checking for siblings of the same bug.

AST-based (not a one-off grep) so a FUTURE non-ASCII character added to any
print()/logger.*() call in these files fails the suite immediately, rather
than waiting for the specific string to be hit on a real cp949 console.

Scope: only files actually touched by this review's diff (main.py,
backtest_viral.py, config.py, debug_entries.py, trading/*.py,
strategies/*.py) -- other pre-existing scripts (backtest_1y.py,
capture_webull_token.py, setup_webull.py, etc.) are untouched by this diff
and out of scope, same convention as the disclosed-but-unfixed
backtesting/engine.py import error from round 4.

Run: py -3.12 -m pytest tests -q
"""
import ast
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SCOPED_FILES = [
    "main.py", "backtest_viral.py", "config.py", "debug_entries.py",
    "trading/broker.py", "trading/portfolio.py", "trading/risk_manager.py",
] + sorted(
    os.path.relpath(p, REPO).replace("\\", "/")
    for p in glob.glob(os.path.join(REPO, "strategies", "*.py"))
)

_LOG_METHODS = {"info", "warning", "error", "debug", "critical", "exception"}


def _call_target_name(node):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _string_literals(node):
    """Yield every plain string constant reachable in a call's arguments,
    including the literal segments of an f-string (ast.JoinedStr)."""
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.lineno, sub.value


def _find_non_ascii_console_calls(path):
    src = open(os.path.join(REPO, path), encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _call_target_name(node)
        is_print = target == "print" and isinstance(node.func, ast.Name)
        is_log = target in _LOG_METHODS and isinstance(node.func, ast.Attribute)
        if not (is_print or is_log):
            continue
        for lineno, text in _string_literals(node):
            bad = [c for c in text if ord(c) > 127]
            if bad:
                violations.append((lineno, text, bad))
    return violations


def test_no_non_ascii_characters_in_runtime_print_or_logger_calls():
    all_violations = {}
    for path in SCOPED_FILES:
        v = _find_non_ascii_console_calls(path)
        if v:
            all_violations[path] = v
    assert not all_violations, (
        "Non-ASCII character(s) in a runtime print()/logger.*() call -- "
        "this crashes console logging on the documented Windows cp949 "
        "console:\n" + "\n".join(
            f"  {path}:{lineno}: {bad!r} in {text!r}"
            for path, vs in all_violations.items()
            for lineno, text, bad in vs
        )
    )
