"""Run SymbiYosys bounded model checking as a CHIA node.

CHIA's library covers simulation, synthesis, and place-and-route, but has no
formal-verification node -- there is no Yosys, SymbiYosys, or model-checker
integration anywhere in ``chia/``. This node fills that gap, and is the
deterministic judge the property-synthesis loop is built around: the agent
proposes assertions, this node decides whether they hold, and no LLM sits
anywhere in that decision.
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

from .state_def import ProofResult, Verdict

INJECTION_MARKER = "// PROPERTIES_INJECTION_POINT"

# Scaffolding spliced in ahead of every property set: a past-valid guard so
# $past() is never sampled before it has a value, and a reset discipline that
# restricts the solver to legal post-reset traces. Without this, BMC reports
# counterexamples that begin in unreachable states -- true of the model, but
# meaningless about the design.
#
# f_past_valid tracks rst_n rather than latching 1'b1 unconditionally, and the
# difference is not cosmetic. Latching 1'b1 makes the guard true on the FIRST
# post-reset cycle, where $past() still reaches back ACROSS the reset boundary
# into pre-reset state the solver chose freely. A correct property of the form
# `if (idle) assert (state == $past(state))` then fails at step 2 on a design
# that fully satisfies it -- the gate rejects good collateral and blames the
# generator. Gating on rst_n delays the guard one cycle so $past() only ever
# samples post-reset state.
FORMAL_PREAMBLE = """
`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= rst_n;
    initial assume (rst_n == 1'b0);
    always @(posedge clk) if (f_past_valid) assume (rst_n);
"""
FORMAL_POSTAMBLE = """
`endif
"""

SBY_TEMPLATE = """[options]
mode {mode}
depth {depth}
expect pass,fail

[engines]
smtbmc --nopresat {engine}

[script]
read_verilog -formal -sv dut.v
prep -top {top}

[files]
{dut_path}
"""


class SymbiYosysNode:
    """Bounded model checking of a property set against a Verilog design.

    The node splices a property block into the design at its
    ``// PROPERTIES_INJECTION_POINT`` marker, writes a SymbiYosys task, and
    runs it. Properties see the design's internals, so white-box invariants
    over pointers and internal state are expressible -- which is where
    generated collateral earns its keep.

    .. note::
        Yosys' native SystemVerilog front end accepts assertions inside clocked
        ``always`` blocks with ``$past``, but **not** named assertion labels or
        full ``property``/``sequence`` blocks (those need Verific). Property
        generators targeting this node must emit the supported subset; the
        loop's sanity gate rejects the rest rather than letting a syntax error
        masquerade as a verification result.
    """

    logging_name = "SymbiYosysNode"

    def __init__(
        self,
        oss_cad_bin: str,
        depth: int = 20,
        engine: str = "z3",
        mode: str = "bmc",
        top: str = "fifo",
        timeout_seconds: int = 300,
        logging_level: int = logging.DEBUG,
    ):
        """Configure a SymbiYosys runner.

        Args:
            oss_cad_bin: Absolute path to the ``bin`` directory of an
                oss-cad-suite install providing ``sby``, ``yosys``, and the
                solvers. Prepended to ``PATH`` for every invocation.
            depth: BMC unrolling depth. A property that holds here is proved
                only up to this bound -- report it as bounded, not absolute.
            engine: ``smtbmc`` solver backend (``z3``, ``boolector``, ...).
            mode: SymbiYosys mode; ``bmc`` for bounded checking, ``prove`` for
                unbounded k-induction.
            top: Top module name to elaborate.
            timeout_seconds: Wall-clock cap on one ``sby`` invocation.
            logging_level: Logger verbosity for this node.
        """
        self.oss_cad_bin = oss_cad_bin
        self.depth = depth
        self.engine = engine
        self.mode = mode
        self.top = top
        self.timeout_seconds = timeout_seconds
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @staticmethod
    def build_source(rtl_text: str, properties: str) -> str:
        """Splice a property block into RTL at its injection point.

        Args:
            rtl_text: The full design source, containing the marker.
            properties: Property block to inject, without the ``ifdef``
                guard or past-valid scaffolding (both are added here).

        Returns:
            Complete formal-ready source text.

        Raises:
            ValueError: If the design carries no injection marker.
        """
        if INJECTION_MARKER not in rtl_text:
            raise ValueError(
                f"RTL has no {INJECTION_MARKER!r}; cannot attach properties")
        return rtl_text.replace(
            INJECTION_MARKER, FORMAL_PREAMBLE + properties + FORMAL_POSTAMBLE)

    @ChiaFunction(resources={"formal": 1})
    def prove(self, rtl_text: str, properties: str,
              keep_dir: str | None = None) -> ProofResult:
        """Model check ``properties`` against ``rtl_text``.

        Args:
            rtl_text: Design source with an injection marker. Pass golden RTL
                to check consistency, or a mutant to test for detection.
            properties: The property block to check.
            keep_dir: If set, the SymbiYosys working directory is copied here
                for post-mortem inspection of counterexample traces.

        Returns:
            A :class:`ProofResult`. On a mutant, ``Verdict.FAIL`` is a kill.
        """
        env = dict(os.environ,
                   PATH=f"{self.oss_cad_bin}:{os.environ.get('PATH', '')}")
        wd = Path(tempfile.mkdtemp(prefix="chia_sby_"))
        try:
            dut = wd / "dut.v"
            dut.write_text(self.build_source(rtl_text, properties))
            (wd / "proof.sby").write_text(SBY_TEMPLATE.format(
                mode=self.mode, depth=self.depth,
                engine=self.engine, top=self.top, dut_path=dut))

            try:
                proc = subprocess.run(
                    ["sby", "-f", "proof.sby"], cwd=wd, env=env,
                    capture_output=True, text=True,
                    timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                return ProofResult(Verdict.ERROR,
                                   f"sby exceeded {self.timeout_seconds}s",
                                   depth=self.depth, engine=self.engine)

            return self._parse(proc.stdout + proc.stderr, proc.returncode)
        finally:
            if keep_dir:
                dest = Path(keep_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(wd, dest, dirs_exist_ok=True)
            shutil.rmtree(wd, ignore_errors=True)

    def _parse(self, out: str, returncode: int) -> ProofResult:
        """Turn raw ``sby`` output into a typed verdict."""
        if "DONE (PASS" in out:
            return ProofResult(Verdict.PASS,
                               f"no counterexample within depth {self.depth}",
                               depth=self.depth, engine=self.engine)
        if "DONE (FAIL" in out:
            which = re.search(r"Assert failed in \w+: (\S+)", out)
            step = re.search(r"BMC failed!.*?\n.*?step (\d+)", out, re.S)
            return ProofResult(
                Verdict.FAIL,
                f"counterexample at step {step.group(1)}" if step
                else "counterexample found",
                failed_assert=which.group(1) if which else "",
                depth=self.depth, engine=self.engine)
        errs = [ln for ln in out.splitlines() if "ERROR" in ln]
        return ProofResult(
            Verdict.ERROR,
            "; ".join(errs[:3])[:400] or f"unrecognized sby outcome (rc={returncode})",
            depth=self.depth, engine=self.engine)
