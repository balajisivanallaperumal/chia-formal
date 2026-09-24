"""Vacuity and Reachability Gate checking as a CHIA formal node.

In formal verification, an assertion can pass trivially (vacuously) if:
1. An 'assume' statement is contradictory or over-constrained (assume(False)).
2. The trigger condition / antecedent 'if (cond)' is unreachable.

This node performs automated Vacuity and Precondition Reachability checks
using SymbiYosys 'mode cover' to guarantee that every synthesized property
is mathematically reachable and non-vacuous post-reset.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from chia.base.ChiaFunction import ChiaFunction, get
from .state_def import ProofResult, Verdict
from .symbiyosys_node import SymbiYosysNode

log = logging.getLogger("VacuityGateNode")


@dataclass
class VacuityCheckResult:
    """Outcome of reachability check for a single assertion antecedent."""
    property_index: int
    antecedent_expression: str
    is_reachable: bool
    step_reached: int = 0
    trace_info: str = ""


@dataclass
class VacuityReport:
    """Complete report on the non-vacuity and reachability of an assertion suite."""
    is_fully_sound: bool
    total_antecedents_checked: int
    reachable_count: int
    vacuous_count: int
    details: List[VacuityCheckResult] = field(default_factory=list)


class VacuityGateNode:
    """Verifies that assertion triggers and preconditions are non-vacuous using formal reachability."""

    logging_name = "VacuityGateNode"

    def __init__(self, logging_level: int = logging.DEBUG):
        self.logging_level = logging_level

    @staticmethod
    def extract_antecedents(properties_block: str) -> List[str]:
        """Extract condition expressions from 'if (condition) assert(...)' statements with balanced parens."""
        antecedents: List[str] = []
        for line in properties_block.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if "assert" not in line and "cover" not in line and "assume" not in line:
                continue

            idx = 0
            while True:
                pos = line.find("if", idx)
                if pos == -1:
                    break
                # Find '(' after if
                open_pos = line.find("(", pos)
                if open_pos == -1 or open_pos > pos + 5:
                    idx = pos + 2
                    continue
                # Match balanced parentheses
                depth = 0
                close_pos = -1
                for i in range(open_pos, len(line)):
                    if line[i] == "(":
                        depth += 1
                    elif line[i] == ")":
                        depth -= 1
                        if depth == 0:
                            close_pos = i
                            break
                if close_pos != -1:
                    cond = line[open_pos + 1 : close_pos].strip()
                    if cond != "f_past_valid" and cond != "!rst_n":
                        cond = re.sub(r"^f_past_valid\s*&&\s*", "", cond).strip()
                        if cond and cond not in antecedents:
                            antecedents.append(cond)
                    idx = close_pos + 1
                else:
                    break
        return antecedents

    @ChiaFunction()
    def check_vacuity(
        self,
        sby: SymbiYosysNode,
        rtl: str,
        properties_block: str,
    ) -> VacuityReport:
        """Run formal cover checks on all preconditions to guarantee non-vacuity."""
        antecedents = self.extract_antecedents(properties_block)
        if not antecedents:
            return VacuityReport(
                is_fully_sound=True,
                total_antecedents_checked=0,
                reachable_count=0,
                vacuous_count=0,
                details=[]
            )

        log.info(f"Running formal reachability/vacuity checks on {len(antecedents)} antecedents...")

        cover_lines = []
        for idx, cond in enumerate(antecedents):
            cover_lines.append(f"always @(posedge clk) if (f_past_valid) cover_{idx:02d}: cover({cond});")

        cover_block = "\n".join(cover_lines)

        cover_sby = SymbiYosysNode(
            oss_cad_bin=sby.oss_cad_bin,
            depth=sby.depth,
            engine=sby.engine,
            mode="cover",
            top=sby.top,
            timeout_seconds=sby.timeout_seconds,
        )

        proof_result = get(cover_sby.prove.chia_remote(cover_sby, rtl, cover_block))

        results: List[VacuityCheckResult] = []
        vacuous_count = 0
        reachable_count = 0

        for idx, cond in enumerate(antecedents):
            # In SBY cover mode, PASS means all cover points were reached!
            is_reached = (proof_result.verdict is Verdict.PASS)
            if f"cover_{idx:02d}" in proof_result.detail and "UNREACHABLE" in proof_result.detail:
                is_reached = False

            if is_reached:
                reachable_count += 1
            else:
                vacuous_count += 1

            results.append(VacuityCheckResult(
                property_index=idx,
                antecedent_expression=cond,
                is_reachable=is_reached,
                trace_info=proof_result.detail,
            ))

        is_sound = (vacuous_count == 0)

        return VacuityReport(
            is_fully_sound=is_sound,
            total_antecedents_checked=len(antecedents),
            reachable_count=reachable_count,
            vacuous_count=vacuous_count,
            details=results,
        )
