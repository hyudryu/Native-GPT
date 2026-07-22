"""Tests for tools/calculate/tool.py — the `evaluate` function (no Strands deps).

The tool module is imported inside a fixture so its `from strands import tool`
side-effect doesn't leak into the global module cache (other tests in the
session assert strands is not imported at startup).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOL_DIR = Path(__file__).resolve().parent


@pytest.fixture()
def evaluate_module(monkeypatch: pytest.MonkeyPatch):
    """Load tool.py fresh for each test and remove it (and strands) on teardown."""
    spec = importlib.util.spec_from_file_location("calculate_tool_under_test", _TOOL_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    # Drop anything this import pulled in so other tests stay clean.
    for key in list(sys.modules.keys()):
        if key.startswith("calculate_tool_under_test") or key == "strands" or key.startswith("strands."):
            sys.modules.pop(key, None)


def test_basic_arithmetic(evaluate_module) -> None:
    assert evaluate_module.evaluate("2 + 3") == 5
    assert evaluate_module.evaluate("17 * 23") == 391
    assert evaluate_module.evaluate("100 - 42") == 58


def test_operator_precedence(evaluate_module) -> None:
    assert evaluate_module.evaluate("2 + 3 * 4") == 14
    assert evaluate_module.evaluate("(2 + 3) * 4") == 20
    assert evaluate_module.evaluate("2 ** 3 ** 2") == 512  # right-assoc
    assert evaluate_module.evaluate("10 / 2 / 5") == 1.0


def test_unary_minus(evaluate_module) -> None:
    assert evaluate_module.evaluate("-5") == -5
    assert evaluate_module.evaluate("--5") == 5
    assert evaluate_module.evaluate("-(-5)") == 5
    assert evaluate_module.evaluate("3 * -2") == -6


def test_floor_div_and_mod(evaluate_module) -> None:
    assert evaluate_module.evaluate("17 // 5") == 3
    assert evaluate_module.evaluate("17 % 5") == 2


def test_math_functions(evaluate_module) -> None:
    import math

    assert evaluate_module.evaluate("sqrt(16)") == 4.0
    assert evaluate_module.evaluate("abs(-7)") == 7
    assert evaluate_module.evaluate("round(3.14159, 2)") == 3.14
    assert evaluate_module.evaluate("min(1, 2, 3)") == 1
    assert evaluate_module.evaluate("max(1, 2, 3)") == 3
    assert math.isclose(evaluate_module.evaluate("log(e)"), 1.0)


def test_constants(evaluate_module) -> None:
    import math

    assert math.isclose(evaluate_module.evaluate("pi"), math.pi)
    assert math.isclose(evaluate_module.evaluate("2 * pi"), 2 * math.pi)
    assert math.isclose(evaluate_module.evaluate("e"), math.e)


def test_compound_expression(evaluate_module) -> None:
    import math

    assert math.isclose(evaluate_module.evaluate("sqrt(16) + log(1)"), 4.0)
    assert evaluate_module.evaluate("floor(3.7) + ceil(2.1)") == 6


def test_rejects_import(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("__import__('os').system('echo pwned')")


def test_rejects_attribute_access(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("os.system('ls')")
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("(1).real")


def test_rejects_assignment(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("x = 5")


def test_rejects_arbitrary_names(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("secret_value")


def test_rejects_disallowed_function(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("print('hi')")
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("open('/etc/passwd')")


def test_rejects_keyword_arguments(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("min([1,2], default=0)")


def test_rejects_empty_expression(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("")
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("   ")


def test_rejects_syntax_error(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("2 +")
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("2 **")


def test_power_exponent_cap(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("2 ** 999999")
    # Smaller (but still big) is allowed — we cap at 10000.
    assert evaluate_module.evaluate("2 ** 10") == 1024


def test_division_by_zero_raises_not_swallowed(evaluate_module) -> None:
    # The evaluate function lets ZeroDivisionError propagate; the tool wrapper
    # turns it into a friendly message, but the evaluator surfaces it raw.
    with pytest.raises(ZeroDivisionError):
        evaluate_module.evaluate("1 / 0")


def test_boolean_literal_rejected(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("True")


def test_complex_literal_rejected(evaluate_module) -> None:
    with pytest.raises(evaluate_module.CalculationError):
        evaluate_module.evaluate("1j")
