# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import math
import random
import logging
import os
import re
from datetime import datetime

# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('MathSolver')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(
    'log/math_solver.log', encoding='utf-8'
)
_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(_handler)

# ─── Pastel Colors ────────────────────────────────────────────────────────────
PASTEL = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF,
    0xB5EAD7, 0xFFDAB9, 0xFFF0A0, 0xC9C0D3,
    0xFFB7CE, 0xA8D8EA, 0xFDFD96, 0xE0BBE4,
]

# ─── Safe Math Namespace ──────────────────────────────────────────────────────
MATH_NS = {
    # Constants
    "pi": math.pi, "e": math.e, "tau": math.tau,
    "inf": math.inf, "nan": math.nan,
    # Builtins
    "abs": abs, "round": round, "pow": pow,
    "min": min, "max": max, "sum": sum,
    "int": int, "float": float, "bool": bool,
    "list": list, "tuple": tuple,
    "True": True, "False": False,
    # math functions
    "sqrt":    math.sqrt,    "cbrt":    math.cbrt,
    "exp":     math.exp,     "log":     math.log,
    "log2":    math.log2,    "log10":   math.log10,
    "floor":   math.floor,   "ceil":    math.ceil,
    "trunc":   math.trunc,   "fabs":    math.fabs,
    "fmod":    math.fmod,    "modf":    math.modf,
    "hypot":   math.hypot,   "dist":    math.dist,
    "prod":    math.prod,    "fsum":    math.fsum,
    "factorial": math.factorial,
    "gcd":     math.gcd,     "lcm":     math.lcm,
    "comb":    math.comb,    "perm":    math.perm,
    "isqrt":   math.isqrt,   "copysign": math.copysign,
    "sin":     math.sin,     "cos":     math.cos,
    "tan":     math.tan,     "asin":    math.asin,
    "acos":    math.acos,    "atan":    math.atan,
    "atan2":   math.atan2,   "degrees": math.degrees,
    "radians": math.radians,
    "sinh":    math.sinh,    "cosh":    math.cosh,
    "tanh":    math.tanh,    "asinh":   math.asinh,
    "acosh":   math.acosh,   "atanh":   math.atanh,
    "isinf":   math.isinf,   "isnan":   math.isnan,
    "isfinite":math.isfinite,"isclose": math.isclose,
    "erf":     math.erf,     "erfc":    math.erfc,
    "gamma":   math.gamma,   "lgamma":  math.lgamma,
}

BLOCKED = [
    "__", "import", "exec", "eval", "open", "os", "sys",
    "getattr", "setattr", "delattr", "globals", "locals",
    "vars", "dir", "compile", "input", "print", "quit", "exit",
]

def _preprocess(expr: str) -> str:
    """Normalize Unicode math symbols before evaluation."""
    expr = expr.replace("×", "*").replace("÷", "/")
    expr = expr.replace("−", "-").replace("–", "-")
    expr = expr.replace("²", "**2").replace("³", "**3")
    expr = expr.replace("⁴", "**4").replace("⁵", "**5")
    expr = re.sub(r'\^', '**', expr)          # ^ → **
    expr = re.sub(r'(\d)\s*\(', r'\1*(', expr) # 2(3) → 2*(3)
    return expr.strip()

def _safe_eval(expr: str):
    """Evaluate expression safely. Returns result or raises ValueError."""
    for word in BLOCKED:
        if word in expr:
            raise ValueError(f"Blocked keyword: `{word}`")
    try:
        return eval(expr, {"__builtins__": {}}, MATH_NS)
    except ZeroDivisionError:
        raise ValueError("Division by zero! ➗❌")
    except OverflowError:
        raise ValueError("Result is too large to compute! 🌌")
    except SyntaxError:
        raise ValueError("Invalid expression syntax ✏️")
    except Exception as e:
        raise ValueError(str(e))

def _format(result) -> str:
    """Pretty-print a result value."""
    if isinstance(result, tuple):
        return f"({', '.join(_format(v) for v in result)})"
    if isinstance(result, float):
        if math.isinf(result): return "∞" if result > 0 else "-∞"
        if math.isnan(result): return "NaN"
        if result == int(result): return str(int(result))
        return f"{result:.10g}"
    return str(result)

def _simple_op(a: float, op: str, b: float):
    """Perform a simple arithmetic operation from the operator choice."""
    match op:
        case "add":      return a + b
        case "subtract": return a - b
        case "multiply": return a * b
        case "divide":
            if b == 0: raise ValueError("Division by zero! ➗❌")
            return a / b
        case "floor_div":
            if b == 0: raise ValueError("Division by zero! ➗❌")
            return a // b
        case "modulo":
            if b == 0: raise ValueError("Division by zero! ➗❌")
            return a % b
        case "power":    return a ** b
        case _:          raise ValueError(f"Unknown operator: `{op}`")

OP_SYMBOLS = {
    "add":       "+",
    "subtract":  "−",
    "multiply":  "×",
    "divide":    "÷",
    "floor_div": "//",
    "modulo":    "%",
    "power":     "^",
}


# ══════════════════════════════════════════════════════════════════════════════
# 🧮 Math Solver Cog
# ══════════════════════════════════════════════════════════════════════════════
class MathSolverCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /math ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="math",
        description="Solve any math operation or expression! 🧮✨"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        expression = "Full expression (e.g. sqrt(144), sin(pi/2), 2**10+5) ✨",
        number_a   = "First number for simple arithmetic (e.g. 25) 🔢",
        operator   = "Arithmetic operator ➕➖✖️➗",
        number_b   = "Second number for simple arithmetic (e.g. 4) 🔢",
    )
    @app_commands.choices(operator=[
        app_commands.Choice(name="➕  Add          ( a + b )",        value="add"),
        app_commands.Choice(name="➖  Subtract      ( a − b )",        value="subtract"),
        app_commands.Choice(name="✖️  Multiply      ( a × b )",        value="multiply"),
        app_commands.Choice(name="➗  Divide        ( a ÷ b )",        value="divide"),
        app_commands.Choice(name="🔢  Floor Divide  ( a // b )",       value="floor_div"),
        app_commands.Choice(name="〰️  Modulo        ( a % b )",        value="modulo"),
        app_commands.Choice(name="⬆️  Power         ( a ^ b )",        value="power"),
    ])
    async def math_command(
        self,
        interaction: discord.Interaction,
        expression: str       = None,
        number_a:   str       = None,
        operator:   str       = None,
        number_b:   str       = None,
    ):
        await interaction.response.defer()
        color = random.choice(PASTEL)

        # ── Route: simple arithmetic mode ─────────────────────────────────────
        if operator is not None:
            # Both numbers required for simple mode
            if number_a is None or number_b is None:
                err = discord.Embed(
                    title="❌ Missing Numbers",
                    description=(
                        "When using an **operator**, you must also provide "
                        "**number_a** and **number_b**! 😊\n\n"
                        "Example: `number_a: 25` `operator: ÷ Divide` `number_b: 5`"
                    ),
                    color=0xFF6B6B
                )
                return await interaction.followup.send(embed=err)

            try:
                a = float(number_a.replace(",", "."))
                b = float(number_b.replace(",", "."))
            except ValueError:
                err = discord.Embed(
                    title="❌ Invalid Number",
                    description=f"`{number_a}` or `{number_b}` is not a valid number! 😊",
                    color=0xFF6B6B
                )
                return await interaction.followup.send(embed=err)

            symbol = OP_SYMBOLS[operator]
            expr_display = f"{_format(a)} {symbol} {_format(b)}"

            try:
                result = _simple_op(a, operator, b)
            except ValueError as e:
                err = discord.Embed(
                    title="❌ Math Error",
                    description=str(e),
                    color=0xFF6B6B
                )
                err.add_field(name="📝 Expression", value=f"`{expr_display}`", inline=False)
                return await interaction.followup.send(embed=err)

            result_str = _format(result)

            embed = discord.Embed(title="🧮 Math Solved! ✨", color=color, timestamp=datetime.now())
            embed.add_field(name="📝 Expression", value=f"```\n{expr_display}\n```", inline=False)
            embed.add_field(name="✅ Result",     value=f"```\n{result_str}\n```",   inline=False)
            embed.add_field(name="🔢 A",          value=f"`{_format(a)}`",           inline=True)
            embed.add_field(name="🔣 Operator",   value=f"`{symbol}`",               inline=True)
            embed.add_field(name="🔢 B",          value=f"`{_format(b)}`",           inline=True)
            embed.set_footer(
                text=f"Requested by {interaction.user.display_name} 😊",
                icon_url=interaction.user.display_avatar.url
            )

            logger.info(f"🧮 /math simple | {interaction.user} | {expr_display} = {result_str}")
            return await interaction.followup.send(embed=embed)

        # ── Route: expression mode ─────────────────────────────────────────────
        if expression is None:
            err = discord.Embed(
                title="❌ Nothing to Solve",
                description=(
                    "Please provide an **expression** or use **number_a + operator + number_b**! 😊\n\n"
                    "**Examples:**\n"
                    "`expression: sqrt(144)`\n"
                    "`expression: sin(pi/2) * 100`\n"
                    "`number_a: 25  operator: ÷  number_b: 5`"
                ),
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err)

        raw  = expression.strip()
        expr = _preprocess(raw)

        try:
            result     = _safe_eval(expr)
            result_str = _format(result)
        except ValueError as e:
            err = discord.Embed(
                title="❌ Math Error",
                description=str(e),
                color=0xFF6B6B,
                timestamp=datetime.now()
            )
            err.add_field(name="📝 Your Expression", value=f"`{raw}`", inline=False)
            err.set_footer(text="Try: sqrt(144), sin(pi/2), factorial(10) 😊")
            return await interaction.followup.send(embed=err)

        embed = discord.Embed(title="🧮 Math Solved! ✨", color=color, timestamp=datetime.now())
        embed.add_field(name="📝 Expression", value=f"```\n{raw}\n```",        inline=False)
        embed.add_field(name="✅ Result",     value=f"```\n{result_str}\n```", inline=False)

        # Extra info for special results
        if isinstance(result, float):
            if math.isinf(result):
                embed.add_field(name="ℹ️ Note", value="Result is **infinite** ∞", inline=False)
            elif math.isnan(result):
                embed.add_field(name="ℹ️ Note", value="Result is **NaN** (Not a Number)", inline=False)
        if isinstance(result, tuple):
            embed.add_field(name="ℹ️ Note", value=f"Result is a **tuple** with {len(result)} values", inline=False)

        embed.set_footer(
            text=f"Requested by {interaction.user.display_name} 😊",
            icon_url=interaction.user.display_avatar.url
        )

        logger.info(f"🧮 /math expr | {interaction.user} | '{raw}' = {result_str}")
        await interaction.followup.send(embed=embed)

    # ── /math-ref ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="math-ref",
        description="View all available math functions and constants! 📚✨"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def math_ref(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        color = random.choice(PASTEL)

        embed = discord.Embed(
            title="📚 Math Reference Guide ✨",
            description="Everything you can use in `/math` 🧮",
            color=color,
            timestamp=datetime.now()
        )
        embed.add_field(name="➕ Simple Operators",
            value="`+` Add  `−` Subtract  `×` Multiply  `÷` Divide\n`//` Floor Div  `%` Modulo  `^` Power",
            inline=False)
        embed.add_field(name="🔢 Constants",
            value="`pi` `e` `tau` `inf` `nan`", inline=True)
        embed.add_field(name="📐 Roots & Logs",
            value="`sqrt` `cbrt` `exp` `log` `log2` `log10`", inline=True)
        embed.add_field(name="🔺 Trigonometry",
            value="`sin` `cos` `tan` `asin` `acos` `atan` `atan2` `degrees` `radians` `hypot`",
            inline=True)
        embed.add_field(name="🌊 Hyperbolic",
            value="`sinh` `cosh` `tanh` `asinh` `acosh` `atanh`", inline=True)
        embed.add_field(name="🔣 Number Theory",
            value="`factorial` `gcd` `lcm` `comb` `perm` `isqrt`", inline=True)
        embed.add_field(name="🧪 Rounding",
            value="`floor` `ceil` `trunc` `round` `fabs` `fmod` `modf`", inline=True)
        embed.add_field(name="🌟 Special",
            value="`erf` `erfc` `gamma` `lgamma` `isclose` `isinf` `isnan` `isfinite`",
            inline=True)
        embed.add_field(name="📦 List Ops",
            value="`prod([a,b,c])` `fsum([a,b,c])` `dist([x1,y1],[x2,y2])`", inline=True)
        embed.add_field(name="💡 Examples",
            value=(
                "`sqrt(144)` → 12\n"
                "`sin(pi/2)` → 1\n"
                "`factorial(10)` → 3628800\n"
                "`log(e**3)` → 3\n"
                "`degrees(pi)` → 180\n"
                "`gcd(48, 18)` → 6\n"
                "`comb(10, 3)` → 120\n"
                "`hypot(3, 4)` → 5\n"
                "`2**10 + round(pi, 3)` → 1027.142\n"
            ),
            inline=False
        )
        embed.set_footer(text="Use /math to calculate! 🧮✨")
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(MathSolverCog(bot))
