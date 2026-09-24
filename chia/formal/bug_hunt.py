"""Apply a hardened assertion suite to suspect RTL and report what breaks.

The refinement loop in :mod:`chia.formal.refinement_loop` assumes its design is
correct -- that is exactly what the soundness gate enforces, discarding any
property the design refutes. Pointed at buggy RTL it would therefore throw away
the one assertion that found the bug.

Bug hunting needs the two halves separated:

1. **Harden** against a clean reference. Mutation coverage says how much of the
   fault space the suite catches, and every property is proved to hold on
   correct RTL.
2. **Hunt**: run that suite against the suspect design. A property that held on
   the reference and fails here is a *finding*, and its counterexample is the
   evidence.

The distinction matters because the two runs ask opposite questions of the same
verdict. On the reference, ``FAIL`` means the property is wrong. On the
suspect, ``FAIL`` means the design is.

What this cannot do is tell a genuine defect from a deliberate behavioural
difference between two versions of a design. It narrows a large design to a
short list of concrete, counterexample-backed discrepancies; deciding which are
bugs is still a human judgement.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .empirical_metrics import ProofFn
from .state_def import Verdict

log = logging.getLogger("BugHunt")


@dataclass
class Finding:
    """One property that holds on the reference and fails on the suspect."""
    property_id: str
    assertion: str
    detail: str
    counterexample: Optional[str] = None

    @property
    def has_evidence(self) -> bool:
        return self.counterexample is not None


@dataclass
class BugHuntReport:
    """Outcome of applying a hardened suite to a suspect design.

    Attributes:
        applied: Properties that held on the reference and were therefore
            eligible to be run against the suspect.
        not_applicable: Properties dropped because they do not hold on the
            *reference*. A suite carried over from another design version can
            contain these; they say nothing about the suspect.
        findings: Properties refuted by the suspect -- the candidate bugs.
        inconclusive: Properties that failed to elaborate against the suspect,
            usually an interface difference. Reported, never counted as clean.
        clean: Properties that hold on both.
    """
    reference: str
    suspect: str
    module: str
    applied: int = 0
    not_applicable: List[Tuple[str, str]] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    inconclusive: List[Tuple[str, str]] = field(default_factory=list)
    clean: int = 0
    seconds: float = 0.0

    @property
    def summary(self) -> str:
        return (f"{len(self.findings)} finding(s) from {self.applied} applied "
                f"properties; {self.clean} clean, "
                f"{len(self.inconclusive)} inconclusive")


def hunt(
    reference_path: Path,
    suspect_path: Path,
    module_name: str,
    properties: Sequence[Tuple[str, str]],
    proof_fn: ProofFn,
    clk: str = "clk",
    rst_n: str = "rst_n",
    bmc_depth: int = 12,
    trace_dir: Optional[Path] = None,
    suspect_module: Optional[str] = None,
) -> BugHuntReport:
    """Run a hardened suite against suspect RTL and collect refutations.

    Args:
        reference_path: Design the suite was hardened against, assumed correct.
        suspect_path: Design under investigation.
        module_name: Top module in the reference.
        properties: ``(property_id, sva_code)`` from a hardening run.
        proof_fn: Model checker, typically ``check_formal_proof``.
        bmc_depth: Search depth. A bug deeper than this bound is not found;
            absence of findings is not evidence of absence.
        trace_dir: Where to persist counterexample waveforms.
        suspect_module: Top module in the suspect, if it differs.

    Returns:
        A :class:`BugHuntReport`.
    """
    started = time.time()
    suspect_top = suspect_module or module_name
    report = BugHuntReport(
        reference=str(reference_path), suspect=str(suspect_path),
        module=module_name,
    )

    for pid, code in properties:
        # Re-confirm on the reference. A suite is only meaningful here if it
        # still holds on known-good RTL; otherwise a failure on the suspect is
        # equally explained by the property being wrong.
        ref_verdict, ref_detail, _, _ = proof_fn(
            dut_path=reference_path, module_name=module_name, sva_code=code,
            clk=clk, rst_n=rst_n, bmc_depth=bmc_depth,
        )
        if ref_verdict is not Verdict.PASS:
            report.not_applicable.append(
                (pid, f"does not hold on the reference: {ref_detail[:120]}"))
            continue

        report.applied += 1
        verdict, detail, trace, _ = proof_fn(
            dut_path=suspect_path, module_name=suspect_top, sva_code=code,
            clk=clk, rst_n=rst_n, bmc_depth=bmc_depth, trace_dir=trace_dir,
        )
        assertion = " ".join(code.splitlines()[-1].split())
        if verdict is Verdict.FAIL:
            report.findings.append(Finding(
                property_id=pid, assertion=assertion,
                detail=detail, counterexample=trace))
            log.info("FINDING %s: %s", pid, assertion[:100])
        elif verdict is Verdict.ERROR:
            report.inconclusive.append((pid, detail[:160]))
        else:
            report.clean += 1

    report.seconds = time.time() - started
    return report
