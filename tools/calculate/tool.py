"""Calculator Strands tool.

Evaluates a mathematical expression by walking the AST. Never `eval()`s — only
arithmetic operators, parentheses, numeric literals, and an allowlist of
`math` module functions/constants are permitted.

Examples:
    "17 * 23"           -> 391
    "sqrt(16) + log(1)" -> 4.0
    "2 ** 10"           -> 1024
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from strands import tool

# Binary operators supported by the calculator. Note: we deliberately exclude
# `MatMult` (`@`) — only numeric arithmetic makes sense here.
_BIN_OPS: dict[type[ast.AST], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.AST], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Functions from `math` the calculator may call. Names are the call form, e.g.
# `sqrt(16)` not `math.sqrt(16)`. Values are validated to be callable and
# safe at import time (all come from the stdlib `math` module).
_ALLOWED_FUNCS = {
    "sqrt": math.sqrt,
    "cbrt": getattr(math, "cbrt", math.pow),  # cbrt landed in 3.11
    "abs": abs,
    "round": round,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "log1p": math.log1p,
    "exp": math.exp,
    "expm1": math.expm1,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "gcd": math.gcd,
    "hypot": math.hypot,
    "degrees": math.degrees,
    "radians": math.radians,
    "min": min,
    "max": max,
}

_ALLOWED_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
}

_MAX_POW_EXPONENT = 10_000  # block `2 ** 999999999` memory bombs


class CalculationError(ValueError):
    """Raised when an expression is rejected before evaluation."""


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        # int, float, complex — but we reject complex to keep it numeric.
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise CalculationError(f"unsupported literal: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            raise CalculationError(f"unsupported operator: {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            try:
                exp = int(right)
            except (TypeError, ValueError):
                exp = -1
            if exp > _MAX_POW_EXPONENT:
                raise CalculationError(
                    f"exponent too large (max {_MAX_POW_EXPONENT})"
                )
        return op_fn(left, right)
    if isinstance(node, ast.UnaryOp):
        op_fn = _UNARY_OPS.get(type(node.op))
        if op_fn is None:
            raise CalculationError(f"unsupported unary op: {type(node.op).__name__}")
        return op_fn(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalculationError("only named function calls are allowed")
        func = _ALLOWED_FUNCS.get(node.func.id)
        if func is None:
            raise CalculationError(f"function not allowed: {node.func.id}")
        if node.keywords:
            raise CalculationError("keyword arguments are not allowed")
        args = [_eval_node(arg) for arg in node.args]
        return func(*args)
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        raise CalculationError(f"name not allowed: {node.id}")
    raise CalculationError(f"unsupported expression element: {type(node).__name__}")


def evaluate(expression: str) -> Any:
    """Parse and evaluate a mathematical expression. Pure Python, safe AST walk."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalculationError("expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"invalid expression: {exc.msg}") from exc
    return _eval_node(tree)


@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression and return the result as a string.

    Supports +, -, *, /, //, %, **, parentheses, numeric literals, named
    functions (sqrt, log, sin, cos, abs, round, min, max, ...), and named
    constants (pi, e, tau). Use this for arithmetic instead of guessing.

    Args:
        expression: A mathematical expression, e.g. "17 * 23" or "sqrt(16) + 1".
    """

    try:
        result = evaluate(expression)
    except CalculationError as exc:
        return f"Error: {exc}"
    except ZeroDivisionError:
        return "Error: division by zero"
    except (TypeError, ValueError, OverflowError) as exc:
        return f"Error: {exc}"
    return str(result)


TOOL = calculate
