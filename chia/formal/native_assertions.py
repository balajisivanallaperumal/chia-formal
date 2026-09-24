"""Grade a design's own in-tree assertions on the same mutant banks.

Papers on machine-generated assertions report a kill rate with nothing to
compare it against. The obvious reference point -- what a production
verification team's assertions catch on the same faults -- is rarely measured,
and lowRISC's ibex makes it available: 185 assertions across the core, written
and reviewed over years, shipped in the RTL.

Getting them to the solver is not free, and the obstacles are themselves worth
reporting:

* ``prim_assert.sv`` dispatches ``VERILATOR -> SYNTHESIS -> YOSYS``, and slang
  predefines ``SYNTHESIS``. The dummy macros therefore win and every
  ``\\`ASSERT`` compiles to nothing, whatever ``-DYOSYS`` says. Undefining
  ``SYNTHESIS`` is what makes the Yosys branch reachable.
* ``ASSERT_KNOWN``'s Yosys macro body is empty by design: X-propagation checks
  are meaningful in simulation and vacuous in a 2-state formal model. 39% of
  ibex's assertions are of this kind and cannot be graded by mutation
  coverage at all.
* The Yosys ``\\`ASSERT`` macro expands to an *immediate* assert, but 28% of
  ibex's assertions use ``|->``, which only exists in concurrent properties.
  Those are hard parse errors until rewritten.

The rewrite in :func:`rewrite_implications` handles the last of these. It is
sound here precisely because none of ibex's assertions use ``##`` or
``s_eventually``: every implication is same-cycle, so ``A |-> B`` and
``!(A) || (B)`` agree.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("NativeAssertions")

# Defines that make a lowRISC-style design's own assertions reachable.
# "!NAME" undefines; see check_formal_proof's `defines` argument.
LOWRISC_FORMAL_DEFINES = ("YOSYS", "!SYNTHESIS", "!VERILATOR")

# Constructs no same-cycle rewrite can preserve.
TEMPORAL = re.compile(r"##|s_eventually|throughout|within|\bintersect\b|\buntil\b")


@dataclass
class AssertionInventory:
    """What a design's in-tree assertions consist of.

    Attributes:
        total: Assertions found.
        x_checks: X-propagation checks. Vacuous under 2-state formal, and
            compiled to nothing by lowRISC's own Yosys macros -- excluded from
            grading rather than counted as missed.
        implications: Same-cycle ``|->`` / ``|=>``, rewritable.
        temporal: Multi-cycle properties, not rewritable.
        plain: Boolean properties that compile as-is.
    """
    total: int = 0
    x_checks: int = 0
    implications: int = 0
    temporal: int = 0
    plain: int = 0
    detail: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def gradeable(self) -> int:
        """Assertions that can reach the solver after rewriting."""
        return self.plain + self.implications

    def summary(self) -> str:
        if not self.total:
            return "no in-tree assertions"
        return (f"{self.total} assertions: {self.gradeable} gradeable "
                f"({self.plain} plain + {self.implications} rewritten), "
                f"{self.x_checks} X-checks excluded, {self.temporal} temporal")


def classify_assertions(rtl_text: str) -> AssertionInventory:
    """Inventory a design's in-tree assertions by what can be graded.

    Args:
        rtl_text: Design source.

    Returns:
        An :class:`AssertionInventory`.
    """
    inv = AssertionInventory()
    # Macro invocations continue across escaped newlines.
    body = re.sub(r"\\\s*\n", " ", rtl_text)
    for macro, arg in re.findall(r"`(ASSERT\w*)\(([^\n]*)", body):
        inv.total += 1
        if "KNOWN" in macro or "$isunknown" in arg:
            inv.x_checks += 1
            kind = "x_check"
        elif TEMPORAL.search(arg):
            inv.temporal += 1
            kind = "temporal"
        elif "|->" in arg or "|=>" in arg:
            inv.implications += 1
            kind = "implication"
        else:
            inv.plain += 1
            kind = "plain"
        inv.detail.append((kind, arg.strip()[:90]))
    return inv


def _split_top_level(text: str, token: str) -> Optional[Tuple[str, str]]:
    """Split on ``token`` at paren/bracket depth zero.

    A naive ``str.split`` would cut inside a nested expression such as
    ``f(a |-> b)``, producing a rewrite that does not parse.
    """
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text.startswith(token, i):
            return text[:i], text[i + len(token):]
        i += 1
    return None


def rewrite_implications(rtl_text: str) -> Tuple[str, int]:
    """Rewrite same-cycle ``|->`` / ``|=>`` into boolean implication.

    lowRISC's Yosys ``\\`ASSERT`` macro places its property inside an immediate
    ``assert(...)``, where ``|->`` is not legal SystemVerilog. For a same-cycle
    implication the boolean form is equivalent, so the assertion can be graded
    instead of discarded.

    Only rewrites arguments free of temporal operators; ``|=>`` (next-cycle) is
    left alone, since collapsing it to the same cycle would change what the
    assertion claims.

    Args:
        rtl_text: Design source.

    Returns:
        ``(rewritten_source, count)``.
    """
    lines = rtl_text.splitlines(keepends=True)
    out: List[str] = []
    count = 0

    # Join escaped-newline continuations so one macro call is one unit.
    joined = re.sub(r"\\\n\s*", " ", "".join(lines))

    def fix(match: re.Match) -> str:
        nonlocal count
        head, arg = match.group(1), match.group(2)
        if TEMPORAL.search(arg) or "|=>" in arg:
            return match.group(0)
        parts = _split_top_level(arg, "|->")
        if not parts:
            return match.group(0)
        ante, cons = (p.strip() for p in parts)
        if not ante or not cons:
            return match.group(0)
        count += 1
        return f"{head}(!({ante}) || ({cons}))"

    # `ASSERT(Name, <property>[, clk, rst])  -- rewrite only the property.
    def per_macro(m: re.Match) -> str:
        macro, inner = m.group(1), m.group(2)
        if "|->" not in inner:
            return m.group(0)
        name, _, rest = inner.partition(",")
        if not rest:
            return m.group(0)
        fixed = fix(re.match(r"()(.*)", rest.strip(), re.S))
        return f"`{macro}({name},{fixed})"

    # Macro invocations may span lines without escaped newlines, so match on
    # balanced parentheses rather than to end-of-line.
    out_parts: List[str] = []
    i = 0
    while True:
        m = re.search(r"`(ASSERT\w*)\(", joined[i:])
        if not m:
            out_parts.append(joined[i:])
            break
        start = i + m.start()
        open_paren = i + m.end() - 1
        depth, j = 0, open_paren
        while j < len(joined):
            if joined[j] == "(":
                depth += 1
            elif joined[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(joined):
            out_parts.append(joined[i:])
            break
        call = joined[start:j + 1]
        inner = joined[open_paren + 1:j]
        out_parts.append(joined[i:start])
        if "|->" in inner:
            name, _, rest = inner.partition(",")
            if rest.strip():
                fixed = fix(re.match(r"()(.*)", rest.strip(), re.S))
                out_parts.append(f"`{m.group(1)}({name},{fixed})")
            else:
                out_parts.append(call)
        else:
            out_parts.append(call)
        i = j + 1
    return "".join(out_parts), count


def _balanced_arg(text: str, open_idx: int) -> Optional[Tuple[str, int]]:
    """Return the argument of a call whose '(' is at ``open_idx``, and its end."""
    depth, j = 0, open_idx
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:j], j
        j += 1
    return None


def rewrite_system_tasks(rtl_text: str) -> Tuple[str, Dict[str, int]]:
    """Replace system tasks the Yosys front end rejects with equivalents.

    ``$onehot`` and ``$onehot0`` have exact boolean forms. ``$isunknown`` is
    identically false in a 2-state model, which is the same semantics lowRISC
    already assumes -- their own Yosys macro for X-checks has an empty body.
    Substituting the constant keeps the surrounding assertion gradeable
    instead of discarding it wholesale, while preserving its meaning under
    2-state.

    Args:
        rtl_text: Design source.

    Returns:
        ``(rewritten_source, counts_by_task)``.
    """
    counts = {"$onehot": 0, "$onehot0": 0, "$isunknown": 0}
    text = rtl_text
    for task in ("$onehot0", "$onehot", "$isunknown"):
        out, i = [], 0
        while True:
            k = text.find(task + "(", i)
            if k < 0:
                out.append(text[i:])
                break
            got = _balanced_arg(text, k + len(task))
            if not got:
                out.append(text[i:])
                break
            arg, end = got
            out.append(text[i:k])
            if task == "$onehot":
                out.append(f"(((({arg})) != 0) && (((({arg})) & ((({arg})) - 1)) == 0))")
            elif task == "$onehot0":
                out.append(f"((((({arg})) & ((({arg})) - 1)) == 0))")
            else:
                out.append("1'b0")
            counts[task] += 1
            i = end + 1
        text = "".join(out)
    return text, counts


# `ASSERT_IF and friends are built on `ASSERT with an implication baked into
# the *definition*, so rewriting call sites cannot reach them.
MACRO_PATCHES: Sequence[Tuple[str, str]] = (
    ("`ASSERT(__name, (__enable) |-> (__prop), __clk, __rst)",
     "`ASSERT(__name, (!(__enable) || (__prop)), __clk, __rst)"),
    ("`ASSERT_IF(__name, !$isunknown(__sig), __enable, __clk, __rst)",
     "`ASSERT_IF(__name, 1'b1, __enable, __clk, __rst)"),
)


def prepare_native_tree(
    rtl_dirs: Sequence[Path],
    dest: Path,
    patterns: Sequence[str] = ("*.sv", "*.svh", "*.v"),
) -> Dict[str, int]:
    """Copy a design into `dest`, rewritten so its own assertions can be graded.

    Two transformations, both recorded so the result is auditable:

    * Same-cycle ``|->`` in assertion call sites becomes boolean implication.
    * ``prim_assert.sv``'s complex macros are patched, because they embed
      ``|->`` in the macro *definition* -- no call-site rewrite can reach it.

    Writing a transformed tree rather than patching in memory means a reviewer
    can diff it against upstream and see exactly what was changed to make the
    comparison possible. Nothing about what an assertion claims is altered:
    every implication in this design is same-cycle, so the boolean form is
    equivalent.

    Args:
        rtl_dirs: Source directories to copy, in order.
        dest: Directory to write the prepared tree into.
        patterns: Filename globs to copy.

    Returns:
        Counts of ``files``, ``implications_rewritten`` and ``macros_patched``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    stats = {"files": 0, "implications_rewritten": 0, "macros_patched": 0}

    for d in rtl_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for pat in patterns:
            for src in sorted(d.glob(pat)):
                text = src.read_text(errors="ignore")
                if src.name == "prim_assert.sv":
                    for old, new in MACRO_PATCHES:
                        if old in text:
                            text = text.replace(old, new)
                            stats["macros_patched"] += 1
                else:
                    text, n = rewrite_implications(text)
                    stats["implications_rewritten"] += n
                text, tasks = rewrite_system_tasks(text)
                for k, v in tasks.items():
                    stats[k] = stats.get(k, 0) + v
                (dest / src.name).write_text(text)
                stats["files"] += 1
    log.info("prepared native tree at %s: %s", dest, stats)
    return stats
