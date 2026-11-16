import ast
import math
import operator
from agent.registry import register_tool

# Safe operators
_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCTIONS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "pi": math.pi,
    "e": math.e,
}


MAX_AST_DEPTH = 30


def _eval_node(node, depth: int = 0):
    if depth > MAX_AST_DEPTH:
        raise ValueError("Expression nesting too deep (max depth 30)")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant: {node.value}")
    elif isinstance(node, ast.BinOp):
        left = _eval_node(node.left, depth + 1)
        right = _eval_node(node.right, depth + 1)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 500:
                raise ValueError("Exponent too large (max allowed: 500)")
            if abs(left) > 1e6 and right > 20:
                raise ValueError("Calculation magnitude too large to evaluate safely")
        op = _SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op)}")
        return op(left, right)
    elif isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, depth + 1)
        op = _SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op)}")
        return op(operand)
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCTIONS:
            func = _SAFE_FUNCTIONS[node.func.id]
            args = [_eval_node(arg, depth + 1) for arg in node.args]
            return func(*args)
        raise ValueError(f"Unsupported function call: {ast.dump(node)}")
    elif isinstance(node, ast.Name) and node.id in _SAFE_FUNCTIONS:
        return _SAFE_FUNCTIONS[node.id]
    else:
        raise ValueError(f"Unsupported expression node: {type(node)}")


@register_tool(
    name="calculator",
    description="Perform high-precision math calculations, scientific formulas, or statistical operations.",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The mathematical expression to evaluate (e.g. 'sqrt(144) * 25 + 10^2')"
            }
        },
        "required": ["expression"]
    }
)
def calculator(expression: str) -> str:
    if not expression or not expression.strip():
        return "Calculation error: Empty expression."
    if len(expression) > 300:
        return "Calculation error: Expression too long (maximum 300 characters)."
    cleaned = expression.replace("^", "**").strip()
    try:
        parsed = ast.parse(cleaned, mode="eval")
        result = _eval_node(parsed.body, depth=0)
        return f"{result}"
    except Exception as e:
        return f"Calculation error: {e}"
