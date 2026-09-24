"""Mutation-guided assertion refinement: the closed loop.

One-shot property synthesis produces assertions that look plausible and catch
little. The mutant bank is what turns that into a measurable target: every
mutant the suite fails to kill is a concrete, localized statement about what
the assertions do not constrain, expressed as a one-line RTL diff.

This module closes the loop:

1. Synthesize candidate properties from the RTL.
2. Reject any refuted by the *golden* design. A property that fails on correct
   RTL kills every mutant for the wrong reason, and would otherwise let the
   loop "converge" on garbage.
3. Score the survivors: which mutants does the suite actually catch?
4. Hand the surviving mutants back to the model as diffs -- *this fault was
   injected here and nothing noticed* -- and ask for assertions targeting them.
5. Repeat until every active mutant is killed, the model stops making progress,
   or the iteration budget runs out.

The model only ever *proposes*. Every property it returns is admitted by the
solver or discarded by it, so no assertion enters the suite on the model's
say-so, and the kill rate the loop reports is measured at every step.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .empirical_metrics import (
    MinimalBasisReport,
    MutationCoverageReport,
    ProofFn,
    measure_mutation_coverage,
    measure_vacuity,
    partition_assertions,
    detect_clock_reset,
    rtl_around_lines,
    solve_minimal_basis,
)
from .state_def import Mutant
from .vertex_ai_synthesizer import VertexAISynthesizer

log = logging.getLogger("RefinementLoop")


REFINE_SYSTEM = """You are a formal verification engineer strengthening an \
assertion suite for a Verilog module.

A mutation engine injected single-line faults into the design. The assertions \
below FAILED TO DETECT the faults listed as SURVIVORS: with each fault present, \
every assertion still passed. Your job is to write additional assertions that \
are violated by those faults but still hold on the correct design.

Think about what each surviving fault actually changes in the design's \
behaviour, then constrain that behaviour.

Two failure modes account for almost every missed fault:

1. Well-formedness properties ("the output is one-hot", "the output is a subset \
of the input") are all satisfied by a design that produces NO output at all. If \
a survivor disables, stalls or permanently resets the design, you need a \
PROGRESS property: a pending request must produce a response.

2. Aggregate progress properties ("some request was granted") are satisfied by a \
design that always serves the SAME client. If a survivor changes *which* client \
is served -- a corrupted priority pointer, a rotation constant, an arbitration \
order -- then aggregate progress will not catch it. You need a PER-CHANNEL \
bounded-response property, written separately for EVERY channel index: if \
channel i's request is held continuously for W cycles, channel i must be granted \
within that window. Emit one such assertion per index, not one for index 0."""


def _format_survivors(survivors: Sequence[Mutant], limit: int = 20) -> str:
    """Render surviving mutants as localized RTL diffs for the model."""
    lines = []
    for m in survivors[:limit]:
        lines.append(
            f"- line {m.line_no} [{m.operator}] survived\n"
            f"    original: {m.original}\n"
            f"    mutated : {m.mutated}"
        )
    if len(survivors) > limit:
        lines.append(f"- ... and {len(survivors) - limit} more")
    return "\n".join(lines)


def synthesize_targeted_properties(
    rtl_text: str,
    top_module: str,
    survivors: Sequence[Mutant],
    existing: Sequence[str],
    tautologies: Sequence[str] = (),
    clk: str = "clk",
    rst_n: str = "rst_n",
    model_name: Optional[str] = None,
    combinational: bool = False,
) -> str:
    """Ask the model for assertions that kill specific surviving mutants.

    Args:
        rtl_text: Golden design source.
        top_module: Module under verification.
        survivors: Mutants the current suite failed to kill.
        existing: Assertions already in the suite, so the model extends rather
            than restates them.
        tautologies: Assertions measured to kill nothing, named explicitly so
            the model stops producing that shape.
        clk: Clock signal name.
        rst_n: Reset signal name.
        model_name: Vertex model override.

    Returns:
        Raw SystemVerilog text from the model, or ``""`` on failure.
    """
    model_name = (
        model_name
        or os.environ.get("CHIA_VERTEX_MODEL")
        or os.environ.get("VERTEX_MODEL")
        or "gemini-2.5-flash"
    )
    existing_block = "\n".join(existing) or "(none yet)"
    dead_weight = (
        "\nThese assertions were measured to kill ZERO mutants. Replace them "
        "with something stronger:\n" + "\n".join(tautologies)
        if tautologies else ""
    )

    if combinational:
        shape_line = "always @(*) assert (<expression>);"
        shape_rules = (
            "- This module is PURELY COMBINATIONAL: no clock, no reset, no state.\n"
            "- FORBIDDEN additionally: `$past`, `posedge`, `f_past_valid`,\n"
            "  `f_cycles`, and any clock or reset signal. None exist here.\n"
            "- Write algebraic input-to-output relations. Guard\n"
            "  operation-specific claims with the opcode, e.g.\n"
            "  `always @(*) assert (op != OP_ADD || result == a + b);`")
    else:
        shape_line = f"always @(posedge {clk}) if (f_past_valid && {rst_n}) assert (<expression>);"
        shape_rules = (
            "- Do NOT declare `f_past_valid`; the harness provides it.\n"
            "- Allowed: `$past(sig)`, `$past(sig, n)`, `$signed`, bit- and\n"
            "  part-selects.")

    prompt = f"""{REFINE_SYSTEM}

MODULE: {top_module}

RTL (excerpted around the surviving faults):
```verilog
{rtl_around_lines(rtl_text, [m.line_no for m in survivors])}
```

ASSERTIONS ALREADY IN THE SUITE (all proven to hold on the correct design):
```systemverilog
{existing_block}
```
{dead_weight}

SURVIVORS -- faults your new assertions must catch:
{_format_survivors(survivors)}

Emit ONLY immediate assertions inside clocked always blocks, exactly this shape:

    {shape_line}

HARD CONSTRAINTS -- output violating these is discarded:
- One `always` statement per assertion. No `begin`/`end` grouping.
- FORBIDDEN: `property`, `endproperty`, `assert property`, `sequence`, `|->`,
  `|=>`, `##`, `$rose`, `$fell`, `$stable`, `$onehot`, `$onehot0`,
  `disable iff`, named assertion labels.
{shape_rules}
- `$past(sig, n)` with n > 1 needs n cycles of history. Guard such an assertion
  with `f_cycles > 8'd<n>` and emit this counter exactly once, before them:
      reg [7:0] f_cycles = 8'd0;
      always @(posedge {clk}) if (f_cycles != 8'hff) f_cycles <= f_cycles + 8'd1;
- Reference only signals declared in the RTL above.
- Every assertion MUST hold on the correct design shown above. An assertion
  that is false on the correct design is worthless and will be discarded.

Output a single ```systemverilog code block and nothing else."""

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
        cfg: Dict = {"temperature": 0.3, "max_output_tokens": 4096}
        if "flash" in model_name:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        elif "2.5-pro" in model_name:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=2048)

        text = ""
        for attempt in range(4):
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt,
                    config=types.GenerateContentConfig(**cfg),
                )
                text = response.text or ""
                break
            except Exception as api_err:
                transient = any(
                    marker in str(api_err)
                    for marker in ("429", "RESOURCE_EXHAUSTED", "Resource exhausted",
                                   "503", "UNAVAILABLE", "DEADLINE_EXCEEDED")
                )
                if not transient or attempt == 3:
                    raise
                delay = 2 ** attempt + random.uniform(0.5, 1.5)
                log.warning("Vertex throttled on %s, retrying in %.1fs (%d/4)",
                            top_module, delay, attempt + 1)
                time.sleep(delay)
    except Exception as exc:
        log.warning("targeted synthesis failed on %s: %s", top_module, exc)
        return ""

    blocks = re.findall(r"```(?:systemverilog|verilog)?\s*(.*?)\s*```",
                        text, re.DOTALL | re.IGNORECASE)
    if not blocks:
        opened = re.search(r"```(?:systemverilog|verilog)?\s*(.*)", text,
                           re.DOTALL | re.IGNORECASE)
        blocks = [opened.group(1)] if opened else []
    return "\n".join(blocks) if blocks else text


@dataclass
class RefinementIteration:
    """One pass of propose -> gate -> score."""
    index: int
    proposed: int
    admitted: int
    rejected_syntax: int
    rejected_duplicate: int
    rejected_unsound: int
    suite_size: int
    active: int
    killed: int
    kill_rate: Optional[float]
    newly_killed: List[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class RefinementResult:
    """Outcome of the full mutation-guided refinement loop."""
    module: str
    iterations: List[RefinementIteration] = field(default_factory=list)
    final_properties: List[Tuple[str, str]] = field(default_factory=list)
    final_coverage: Optional[MutationCoverageReport] = None
    final_basis: Optional[MinimalBasisReport] = None
    converged: bool = False
    stop_reason: str = ""
    pruned_ineffective: int = 0
    seconds: float = 0.0

    @property
    def initial_kill_rate(self) -> Optional[float]:
        return self.iterations[0].kill_rate if self.iterations else None

    @property
    def final_kill_rate(self) -> Optional[float]:
        return self.iterations[-1].kill_rate if self.iterations else None


def refine_until_killed(
    dut_path: Path,
    module_name: str,
    golden_rtl: str,
    proof_fn: ProofFn,
    clk: str = "clk",
    rst_n: str = "rst_n",
    bmc_depth: int = 12,
    budget: int = 0,
    workers: int = 12,
    oss_cad_bin: str = "",
    max_iterations: int = 4,
    seed_properties: Optional[Sequence[Tuple[str, str]]] = None,
    model_name: Optional[str] = None,
    dispatch: str = "local",
    patience: int = 2,
    prune_ineffective: bool = True,
    resample: bool = True,
) -> RefinementResult:
    """Strengthen an assertion suite until it kills every active mutant.

    Args:
        dut_path: Golden design, used to resolve packages and submodules.
        module_name: Module under verification.
        golden_rtl: Golden design source.
        proof_fn: Model checker, typically ``check_formal_proof``.
        bmc_depth: Bounded-model-check depth for scoring.
        budget: Mutant sampling cap; 0 means the whole bank.
        max_iterations: Refinement rounds before giving up.
        seed_properties: Start from these instead of a fresh synthesis pass.
        model_name: Vertex model override.
        dispatch: ``"local"`` grades on this host; ``"ray"`` fans the
            (property, mutant) grid out across CHIA workers.
        patience: Stop after this many consecutive rounds that kill nothing
            new. Grading costs one solver call per (property, mutant) pair, so
            a loop that has stopped making progress is spending real time to
            re-confirm the same score.
        resample: Draw a different mutant sample each round. Grading against a
            fixed sample scores the loop on its training set -- it learns to
            kill those particular faults rather than to characterise the
            design, and the suite then generalises poorly to the full bank.
        prune_ineffective: Drop properties measured to kill nothing from later
            rounds. A property's kill set is fixed for a given mutant bank, so
            one that kills nothing now never will; carrying it only inflates
            the grid. Pruned properties stay in the duplicate filter, so the
            model is not handed them again.

    Returns:
        A :class:`RefinementResult` recording every round, so the kill-rate
        trajectory can be reported rather than just the endpoint.
    """
    started = time.time()
    result = RefinementResult(module=module_name)

    if seed_properties is None:
        raw = "\n".join(
            c.sva_code
            for c in VertexAISynthesizer.synthesize_all_contracts(golden_rtl, module_name)
        )
        _, seeded, rejected = partition_assertions(raw)
    else:
        seeded, rejected = list(seed_properties), []

    # Ids must never be reused: measure_mutation_coverage keys its coverage
    # map by id, so a collision silently drops a property from scoring.
    next_id = 0
    seen_bodies: set = set()

    def fresh_ids(codes):
        """Assign ids to genuinely new properties, dropping repeats.

        The model re-proposes assertions it has already been shown -- on this
        design roughly 40% of its later output. Admitting the duplicates costs
        a soundness proof and a full mutant sweep each, and inflates the
        reported suite size without changing what it catches.
        """
        nonlocal next_id
        out = []
        for code in codes:
            body = " ".join(code.splitlines()[-1].split())
            if body in seen_bodies:
                continue
            seen_bodies.add(body)
            out.append((f"prop_{next_id:02d}", code))
            next_id += 1
        return out

    suite: List[Tuple[str, str]] = fresh_ids([code for _, code in seeded])
    verified: set = set()
    equivalence_cache: Dict[str, str] = {}
    rejected_duplicate = len(seeded) - len(suite)
    stalled = 0
    pruned_total = 0
    # Cumulative kills per property, across every round it has faced. With a
    # rotating sample a property that kills nothing in one round may kill in
    # the next, so pruning must judge a property on its whole record.
    lifetime_kills: Dict[str, int] = defaultdict(int)
    rounds_seen: Dict[str, int] = defaultdict(int)
    rejected_syntax = len(rejected)
    proposed = len(seeded) + rejected_syntax

    for index in range(max_iterations):
        round_started = time.time()
        if not suite:
            result.stop_reason = "no admissible properties were synthesized"
            break

        coverage = measure_mutation_coverage(
            dut_path=dut_path, module_name=module_name, golden_rtl=golden_rtl,
            properties=suite, proof_fn=proof_fn, clk=clk, rst_n=rst_n,
            bmc_depth=bmc_depth, budget=budget, workers=workers,
            oss_cad_bin=oss_cad_bin, classify_equivalents=True,
            pre_verified=verified, equivalence_cache=equivalence_cache,
            dispatch=dispatch, round_index=index if resample else 0,
        )
        # The gate already dropped unsound properties; keep the suite in sync
        # so the next prompt shows the model only what actually holds.
        scored = set(coverage.scored_property_ids)
        unsound_now = len(suite) - len(scored)
        suite = [(pid, code) for pid, code in suite if pid in scored]
        verified |= scored

        previously_killed = set()
        for prior in result.iterations:
            previously_killed |= set(prior.newly_killed)
        killed_now = set()
        for p in coverage.per_property:
            killed_now |= p.killed

        result.iterations.append(RefinementIteration(
            index=index,
            proposed=proposed,
            admitted=len(suite),
            rejected_syntax=rejected_syntax,
            rejected_duplicate=rejected_duplicate,
            rejected_unsound=unsound_now,
            suite_size=len(suite),
            active=coverage.active,
            killed=coverage.killed,
            kill_rate=coverage.kill_rate,
            newly_killed=sorted(killed_now - previously_killed),
            seconds=time.time() - round_started,
        ))
        result.final_coverage = coverage
        log.info("iteration %d: %s over %d properties",
                 index, coverage.kill_rate_str, len(suite))

        newly = result.iterations[-1].newly_killed
        stalled = stalled + 1 if not newly else 0

        for p in coverage.per_property:
            lifetime_kills[p.property_id] += len(p.killed)
            rounds_seen[p.property_id] += 1

        if prune_ineffective:
            # Drop only properties that have killed nothing across every round
            # they have faced. Judging on a single round would discard general
            # properties that simply missed one sample; requiring a minimum
            # number of rounds before pruning keeps a newly added property from
            # being cut before it has been tried.
            min_rounds = 2 if resample else 1
            dead = [pid for pid, _ in suite
                    if rounds_seen[pid] >= min_rounds and lifetime_kills[pid] == 0]
            survivors_exist = any(lifetime_kills[pid] for pid, _ in suite)
            if dead and survivors_exist:
                pruned_total += len(dead)
                suite = [(pid, code) for pid, code in suite if pid not in dead]
                log.info("  pruned %d properties with no kills in %d+ rounds "
                         "(suite now %d)", len(dead), min_rounds, len(suite))

        # "No survivors" is only success if something was actually graded. When
        # the soundness gate rejects every candidate, or every mutant is
        # stillborn or equivalent, the survivor list is empty because nothing
        # was ever tested -- an empty suite kills nothing, it merely leaves
        # nothing to survive. Treating that as convergence is the same vacuous
        # success this loop exists to detect.
        if coverage.kill_rate is None:
            result.stop_reason = (
                "no scoreable properties: every candidate was refuted by the "
                "golden design" if not suite else
                "no active mutants: all were stillborn or provably equivalent")
            break
        if not coverage.survivors:
            result.converged = True
            result.stop_reason = "every active mutant is killed"
            break
        if stalled >= patience:
            result.stop_reason = (
                f"no new kills in {stalled} consecutive rounds")
            break
        if index == max_iterations - 1:
            result.stop_reason = f"iteration budget ({max_iterations}) exhausted"
            break

        by_mid = {m.mid: m for m in coverage.sampled_mutants}
        survivors = [by_mid[mid] for mid in coverage.survivors if mid in by_mid]
        tautologies = []
        for p in coverage.per_property:
            if p.killed:
                continue
            body = p.property_code.splitlines()[-1]
            vac = measure_vacuity(
                dut_path=dut_path, module_name=module_name, golden_rtl=golden_rtl,
                sva_code=p.property_code, proof_fn=proof_fn, clk=clk, rst_n=rst_n,
                depth=bmc_depth,
            )
            if vac.vacuous:
                tautologies.append(
                    f"{body}\n    -> VACUOUS: its trigger is unreachable within "
                    f"{bmc_depth} cycles. Use a shorter window or an easier-to-reach "
                    f"antecedent.")
            else:
                tautologies.append(
                    f"{body}\n    -> TOO WEAK: the trigger is reachable but no fault "
                    f"violates this. It likely restates combinational logic.")

        raw = synthesize_targeted_properties(
            rtl_text=golden_rtl, top_module=module_name, survivors=survivors,
            existing=[code.splitlines()[-1] for _, code in suite],
            tautologies=tautologies, clk=clk, rst_n=rst_n, model_name=model_name,
            combinational=detect_clock_reset(golden_rtl).combinational,
        )
        if not raw.strip():
            result.stop_reason = "model returned no further properties"
            break

        _, fresh, rejected = partition_assertions(raw)
        rejected_syntax = len(rejected)
        proposed = len(fresh) + rejected_syntax
        if not fresh:
            result.stop_reason = "all proposed properties were syntactically unusable"
            break

        before = len(suite)
        suite += fresh_ids([code for _, code in fresh])
        rejected_duplicate = len(fresh) - (len(suite) - before)

    result.final_properties = suite
    result.pruned_ineffective = pruned_total
    if result.final_coverage is not None:
        result.final_basis = solve_minimal_basis(result.final_coverage)
    if not result.stop_reason:
        result.stop_reason = "loop completed"
    result.seconds = time.time() - started
    return result
