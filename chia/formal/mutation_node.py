"""Mutant-bank construction and equivalence classification as CHIA nodes.

Together these build the scoring denominator the loop optimizes against:
:meth:`MutationEngineNode.generate` injects single faults, and
:meth:`MutationEngineNode.classify` proves which of them are real faults at
all. Both are deterministic; neither consults a model.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from chia.base.ChiaFunction import ChiaFunction

from .state_def import Equivalence, Mutant

# Operators model faults a designer plausibly writes -- swapped comparisons,
# inverted polarity, dropped backpressure, off-by-one increments, pointer
# confusion -- rather than random character noise, which mostly produces
# mutants that fail to elaborate and tell you nothing.
OPERATORS: list[tuple[str, str, object]] = [
    # 1. Relational & Equality
    ("EQ_TO_NEQ",          r"==",                             "!="),
    ("NEQ_TO_EQ",          r"!=",                             "=="),
    ("LT_TO_LE",           r"(?<![<>=!])<(?![<=])",           "<="),
    ("GT_TO_GE",           r"(?<![<>=!])>(?![>=])",           ">="),
    ("LE_TO_LT",           r"<=",                             "<"),
    ("GE_TO_GT",           r">=",                             ">"),
    
    # 2. Logical & Bitwise Inversions (Crucial for ALUs & Decoders)
    ("AND_TO_OR",          r"&&",                             "||"),
    ("OR_TO_AND",          r"\|\|",                           "&&"),
    ("BITWISE_AND_TO_OR",  r"(?<=\s)&(?=\s)",                 "|"),
    ("BITWISE_OR_TO_AND",  r"(?<=\s)\|(?=\s)",                "&"),
    ("BITWISE_XOR_TO_AND", r"(?<=\s)\^(?=\s)",                "&"),
    
    # 3. Arithmetic & Shifts
    ("ADD_TO_SUB",         r"(?<![+\-])\+(?![+=])",           "-"),
    ("SUB_TO_ADD",         r"(?<![+\-])-(?![-=>])",           "+"),
    ("SHL_TO_SHR",         r"<<",                             ">>"),
    ("SHR_TO_SHL",         r">>",                             "<<"),
    ("INC_OFF_BY_ONE",     r"\+\s*1'b1",                      "+ 2'd2"),
    ("DEC_OFF_BY_ONE",     r"-\s*1'b1",                       "- 2'd2"),
    
    # 4. Decimal & Hexadecimal Constant Bumping (Opcodes & CSRs)
    ("DEC_CONST_BUMP",     r"(\d+)'d(\d+)",
     lambda m: f"{m.group(1)}'d{(int(m.group(2)) + 1) % (2 ** int(m.group(1)))}"),
    ("HEX_CONST_BUMP",     r"(\d+)'h([0-9a-fA-F]+)",
     lambda m: f"{m.group(1)}'h{(int(m.group(2), 16) + 1) % (2 ** int(m.group(1))):X}"),
    
    # 5. Control Guards & Inversions
    ("DROP_NOT",           r"!(?=[A-Za-z_])",                 ""),
    ("DROP_GUARD",         r"&&\s*!\s*\w+\b",                 ""),
    
    # 6. Decoupled Handshake & Ready/Valid Protocol Faults
    ("DROP_READY_IN_FIRE", r"&&\s*\w*ready\w*",               ""),
    ("DROP_VALID_IN_FIRE", r"&&\s*\w*valid\w*",               ""),
    
    # 7. Zero Register Bypass Protection ($x0)
    ("DROP_X0_ZERO_GUARD", r"&&\s*\w*(?:rd|dst)\w*\s*!=\s*(?:5'd0|5'h0|\d+'h0)", ""),
    
    # 8. Byte Masking Polarity Inversion
    ("INVERT_WMASK_BIT",   r"~\s*(?:wmask|byte_en)",          "wmask"),
    
    # 9. Bitfield Index & Slicing Off-by-Ones
    ("BIT_INDEX_BUMP",     r"\[(\d+)\]",
     lambda m: f"[{int(m.group(1)) + 1}]"),
    
    # 10. Signed vs Unsigned Comparison Stripping
    ("STRIP_SIGNED_CAST",  r"\$signed\(([^)]+)\)",            r"\1"),
    
    # 11. Bitwise Polarity & Gate Drops
    ("DROP_BITWISE_NOT",   r"~(?=[A-Za-z_])",                 ""),
    
    # 12. Asynchronous Reset Active Polarity Flip
    ("RESET_POLARITY_FLIP",r"!\s*(?:rst_n|reset_n)",          "rst_n"),
    
    # 13. FIFO / Ring Buffer Wrap Phase Flip
    ("FIFO_WRAP_PHASE_FLIP", r"(\w*ptr\w*\[[^\]]+\])\s*\^\s*(\w*ptr\w*\[[^\]]+\])", r"\1 == \2"),
    
    # 14. Pipeline Forwarding: Drop RegWrite Guard (Forwarding from non-writing instruction)
    ("DROP_REGWRITE_GUARD",  r"&&\s*\w*(?:reg_?write|rf_?wen|wen|write_en)\w*",     ""),
    
    # 15. Pipeline Hazard: Drop Load-Use Interlock Stall Guard
    ("DROP_LOAD_USE_STALL",  r"&&\s*\w*(?:is_load|mem_read|load_hazard|stall)\w*",  ""),
    
    # 16. Branch Resolution Polarity Inversion
    ("FLIP_BRANCH_TAKEN_COND", r"(\w*(?:branch|taken|br_eq|br_lt)\w*)\s*\^\s*(\w*)", r"\1 & \2"),
    
    # 17. RISC-V Privilege Access Control Check Drop (CWE-1194)
    ("DROP_PRIVILEGE_CHECK", r"&&\s*\w*(?:prv|privilege|priv)\w*\s*(?:>=|==|>)\s*\w*", ""),
    
    # 18. RISC-V Read-Only CSR Overwrite Bypass (CWE-1299)
    ("ALLOW_RO_CSR_OVERWRITE", r"&&\s*!\s*\w*(?:csr_addr|addr)\w*\[11:10\]\s*==\s*2'b11", ""),
    
    # 19. RISC-V Global Interrupt Enable Mask Drop (CWE-1272)
    ("DROP_MIE_INTERRUPT_MASK", r"&&\s*\w*(?:mstatus_mie|sstatus_sie|mie_term)\w*", ""),
    
    # 20. RISC-V Trap Return (mret/sret) Privilege Drop Inversion
    ("FLIP_MRET_PRV_RESTORE", r"(\w*(?:prv|priv)\w*)\s*<=\s*\w*(?:mstatus_mpp|sstatus_spp)\w*", r"\1 <= 2'b11"),
    
    # 21. Supervisor User Memory / PMP Access Protection Bypass
    ("BYPASS_SUM_PMP_PROTECTION", r"&&\s*\w*(?:mstatus_sum|pmp_ok|access_ok)\w*", ""),
]


def derived_swap_operators(rtl_text: str,
                           max_pairs_per_width: int = 12) -> list[tuple[str, str, str]]:
    """Build signal-swap operators from the design's own state declarations.

    Confusing one state register for another of the same width is a fault
    designers actually commit, but which registers exist is design-specific --
    so read the declarations rather than hardcoding a pair of pointer names.

    Args:
        rtl_text: Design source to read declarations from.
        max_pairs_per_width: Cap on ordered pairs drawn from each width class.
            Permutations are quadratic in the number of same-width registers:
            a 3000-line core has ~80 registers sharing one width, which alone
            yields 6300 operators and a bank of tens of thousands of mutants.
            The cap keeps the bank proportional to the design instead of to its
            square; pairs are taken in sorted order so the selection is stable
            across runs.
    """
    import itertools
    by_width: dict[str, list[str]] = {}
    for m in re.finditer(r"^\s*reg\s*(\[[^\]]*\])?\s*(\w+)\s*(?:;|,)", rtl_text, re.M):
        by_width.setdefault(m.group(1) or "1", []).append(m.group(2))
    ops = []
    for names in by_width.values():
        pairs = itertools.islice(itertools.permutations(sorted(set(names)), 2),
                                 max_pairs_per_width)
        ops.extend((f"SWAP_{a}_{b}", rf"\b{a}\b", b) for a, b in pairs)
    return ops


# Port lists, parameters and declarations produce stillborn mutants that never
# elaborate; skipping them keeps the bank informative instead of noisy.
#
# Assertion lines are skipped for a different and more important reason. When
# a design carries its own in-tree assertions -- as production RTL does -- a
# mutation landing inside one silently weakens the very property being graded,
# so the suite appears to miss a fault it was never given a fair chance at.
# The mutant bank must perturb the design, never the specification.
SKIP_LINE = re.compile(
    r"^\s*(//|`|module\b|endmodule\b|parameter\b|input\b|output\b"
    r"|reg\s|wire\s+\[?\w*\]?\s*\w+\s*;"
    r"|assert\b|assume\b|cover\b|restrict\b"
    r"|\w+\s*:\s*assert\b|\w+\s*:\s*assume\b)")

# Assertion text can span continuation lines and macro invocations, so a
# line-prefix test is not enough: anything mentioning these is specification,
# not design.
ASSERTION_TOKENS = re.compile(
    r"`ASSERT|`ASSUME|`COVER|`DV_FCOV|\bassert\s*\(|\bassume\s*\("
    r"|\bassert\s+property|\bassume\s+property|\$isunknown")

# Both designs start from a zero initial state. Without this the miter lets the
# solver choose DIFFERENT initial memory contents per design and reports a
# difference no reachable trace exhibits -- an artifact, not a fault.
EQUIV_SCRIPT = """
read_verilog -sv gold.v
read_verilog -sv mut.v
proc; async2sync; opt; memory_map; opt -full
miter -equiv -flatten -make_assert {gold_top} {mut_top} miter
hierarchy -top miter
sat -seq {depth} -set-init-zero -verify -prove-asserts miter
"""


class MutationEngineNode:
    """Builds a single-fault mutant bank and proves which mutants are real.

    One mutation per mutant is the deliberate discipline: a kill then
    attributes to exactly one injected fault, so the resulting kill rate says
    something specific about what the collateral detects.
    """

    logging_name = "MutationEngineNode"

    def __init__(
        self,
        oss_cad_bin: str,
        top: str = "fifo",
        equiv_depth: int = 12,
        timeout_seconds: int = 300,
        logging_level: int = logging.DEBUG,
    ):
        """Configure a mutation engine.

        Args:
            oss_cad_bin: ``bin`` directory of an oss-cad-suite install
                providing ``yosys``. Prepended to ``PATH``.
            top: Module name to mutate and to compare in the miter.
            equiv_depth: Sequential depth for the equivalence proof. A mutant
                proved equivalent only to this bound is reported as such --
                bounded equivalence, not absolute.
            timeout_seconds: Wall-clock cap on one ``yosys`` invocation.
            logging_level: Logger verbosity for this node.
        """
        self.oss_cad_bin = oss_cad_bin
        self.top = top
        self.equiv_depth = equiv_depth
        self.timeout_seconds = timeout_seconds
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @staticmethod
    def _mask_noncode(line: str) -> str:
        """Blank out comment tails and string bodies, preserving offsets.

        Mutation sites are matched against this masked copy so a ``!=`` inside
        a comment is never mutated, while match offsets still index correctly
        into the original line.
        """
        out, in_str = list(line), False
        i = 0
        while i < len(line):
            c = line[i]
            if not in_str and c == "/" and line[i:i + 2] == "//":
                for j in range(i, len(line)):
                    out[j] = " "
                break
            if c == '"':
                in_str = not in_str
                out[i] = " "
            elif in_str:
                out[i] = " "
            i += 1
        return "".join(out)

    @ChiaFunction()
    def generate(self, rtl_text: str) -> list[Mutant]:
        """Enumerate every single-fault mutant of ``rtl_text``.

        Args:
            rtl_text: Design source to mutate.

        Returns:
            One :class:`Mutant` per (site, operator) pair, in source order.
        """
        lines = rtl_text.splitlines()
        operators = OPERATORS + derived_swap_operators(rtl_text)
        mutants: list[Mutant] = []
        # A multi-line assertion continues until its statement ends; track it
        # so continuation lines are skipped too.
        in_assertion = False
        for ln, line in enumerate(lines, start=1):
            if ASSERTION_TOKENS.search(line):
                in_assertion = not line.rstrip().endswith((";", ")")) or \
                    line.rstrip().endswith("\\")
                continue
            if in_assertion:
                if line.rstrip().endswith(";") or not line.strip():
                    in_assertion = False
                continue
            if SKIP_LINE.match(line) or "INJECTION_POINT" in line:
                continue
            code = self._mask_noncode(line)
            if not code.strip():
                continue
            for op_name, pattern, repl in operators:
                for occ, m in enumerate(re.finditer(pattern, code)):
                    text = repl(m) if callable(repl) else repl
                    mutated = line[:m.start()] + text + line[m.end():]
                    if mutated == line:
                        continue
                    mutants.append(Mutant(
                        mid=f"m{len(mutants):03d}_{op_name}_L{ln}_{occ}",
                        operator=op_name, line_no=ln,
                        original=line.strip(), mutated=mutated.strip()))
        self.logger.info("generated %d mutants", len(mutants))
        return mutants

    @staticmethod
    def materialize(mutant: Mutant, rtl_text: str) -> str:
        """Rebuild full design source with one mutation applied.

        Args:
            mutant: The mutation to apply.
            rtl_text: The unmodified design source.

        Returns:
            Complete mutated source text.
        """
        lines = rtl_text.splitlines()
        original = lines[mutant.line_no - 1]
        indent = original[:len(original) - len(original.lstrip())]
        lines[mutant.line_no - 1] = indent + mutant.mutated
        return "\n".join(lines) + "\n"

    @ChiaFunction(resources={"formal": 1})
    def classify(self, golden_text: str, mutant_text: str) -> tuple[str, str]:
        """Prove whether a mutant is behaviorally distinguishable from golden.

        Builds a miter of the two designs and asks the SAT engine for an input
        sequence that separates them within ``equiv_depth`` cycles.

        Args:
            golden_text: Unmodified design source.
            mutant_text: Mutated design source.

        Returns:
            ``(equivalence, detail)`` where equivalence is the string value of
            an :class:`Equivalence` member.
        """
        env = dict(os.environ,
                   PATH=f"{self.oss_cad_bin}:{os.environ.get('PATH', '')}")
        wd = Path(tempfile.mkdtemp(prefix="chia_equiv_"))
        try:
            gold_top, mut_top = f"{self.top}_gold", f"{self.top}_mut"
            rename = lambda t, new: re.sub(
                rf"\bmodule\s+{self.top}\b", f"module {new}", t, count=1)
            (wd / "gold.v").write_text(rename(golden_text, gold_top))
            (wd / "mut.v").write_text(rename(mutant_text, mut_top))
            (wd / "run.ys").write_text(EQUIV_SCRIPT.format(
                gold_top=gold_top, mut_top=mut_top, depth=self.equiv_depth))

            try:
                proc = subprocess.run(
                    ["yosys", "-s", "run.ys"], cwd=wd, env=env,
                    capture_output=True, text=True,
                    timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                return (Equivalence.UNKNOWN.value,
                        f"yosys exceeded {self.timeout_seconds}s")

            out = proc.stdout + proc.stderr
            if "Called with -verify and proof did fail" in out:
                return (Equivalence.DISTINGUISHABLE.value,
                        "separating input sequence exists")
            if "SAT proof finished - no model found: SUCCESS" in out:
                return (Equivalence.EQUIVALENT.value,
                        f"no separating sequence within {self.equiv_depth} cycles")
            errs = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
            return (Equivalence.UNKNOWN.value,
                    "; ".join(errs[:2])[:300] or f"unrecognized (rc={proc.returncode})")
        finally:
            shutil.rmtree(wd, ignore_errors=True)
