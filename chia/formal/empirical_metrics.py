"""Measured mutation coverage, vacuity and minimal-basis metrics.

The three headline numbers this project reports -- fault-kill rate, vacuity
percentage, and redundancy pruned -- are properties of a specific design and a
specific synthesized assertion suite. They have to be *measured*, per module,
by running the solver; there is no closed form for them.

This module measures them:

* :func:`measure_mutation_coverage` builds a single-fault mutant bank with
  :class:`~chia.formal.mutation_node.MutationEngineNode`, runs the assertion
  suite against each mutant, and reports which mutants the suite actually
  catches. A mutant that fails to elaborate is *stillborn* and is excluded; a
  survivor that is provably equivalent to the golden design is excluded too,
  because no assertion can distinguish it. What remains is the honest
  denominator.
* :func:`solve_minimal_basis` runs greedy set cover over the resulting
  property-by-mutant incidence matrix to find the smallest assertion subset
  that preserves the measured kill set.
* :func:`measure_vacuity` discharges a ``cover`` check per assertion antecedent
  and reports which preconditions are actually reachable post-reset.

Every entry point takes ``proof_fn`` -- the caller's model-checking callable --
rather than importing the pipeline, so this module stays free of a circular
import and can be driven from a test, a notebook, or a Ray task.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from .mutation_node import MutationEngineNode
from .state_def import Equivalence, Mutant, Verdict
from .vacuity_node import VacuityGateNode

log = logging.getLogger("EmpiricalMetrics")


class ProofFn(Protocol):
    """Model-checking callable supplied by the caller.

    Matches ``multifile_proof.check_formal_proof``. Returns
    ``(verdict, detail, trace_path, source_files)``.
    """

    def __call__(
        self,
        dut_path: Path,
        module_name: str,
        sva_code: str,
        clk: str = ...,
        rst_n: str = ...,
        bmc_depth: int = ...,
        mode: str = ...,
        rtl_override: Optional[str] = ...,
        capture: Optional[Dict] = ...,
        trace_dir: Optional[Path] = ...,
    ) -> Tuple[Verdict, str, Optional[str], List[Path]]: ...


# --------------------------------------------------------------------------
# Mutation coverage
# --------------------------------------------------------------------------

@dataclass
class PropertyKills:
    """Which mutants one individual assertion caught."""
    property_id: str
    property_code: str
    killed: Set[str] = field(default_factory=set)
    is_essential: bool = False


@dataclass
class MutationCoverageReport:
    """Measured fault-detection strength of an assertion suite.

    Attributes:
        generated: Size of the full single-fault mutant bank.
        sampled: How many mutants were actually checked (``budget`` cap).
        stillborn: Sampled mutants that failed to elaborate. Excluded -- a
            mutant the tool rejects tells you nothing about the assertions.
        equivalent: Survivors proved behaviorally equivalent to golden within
            the miter depth. Excluded -- undetectable by construction.
        active: ``sampled - stillborn - equivalent``; the scoring denominator.
        killed: Active mutants for which some assertion produced a
            counterexample.
        kill_rate: ``killed / active`` as a percentage, or ``None`` when no
            active mutant survived triage (reported as "n/a", never as 100%).
        sampling: Human-readable description of how the bank was sampled, so
            the number in a paper can be reproduced.
    """
    generated: int
    sampled: int
    stillborn: int
    equivalent: int
    active: int
    killed: int
    survived: int
    kill_rate: Optional[float]
    killed_equivalent: int = 0
    equivalence_unknown: int = 0
    survivors: List[str] = field(default_factory=list)
    per_property: List[PropertyKills] = field(default_factory=list)
    seconds: float = 0.0
    sampling: str = "exhaustive"
    equivalence_checked: bool = False
    unsound: List[Tuple[str, str]] = field(default_factory=list)
    scored_property_ids: List[str] = field(default_factory=list)
    sampled_mutants: List[Mutant] = field(default_factory=list)

    @property
    def kill_rate_str(self) -> str:
        """Kill rate formatted for a report, honest about an empty denominator."""
        if self.kill_rate is None:
            return "n/a (no active mutants)"
        return f"{self.kill_rate:.1f}% ({self.killed}/{self.active})"


def stratified_sample(mutants: Sequence[Mutant], budget: int,
                     round_index: int = 0) -> List[Mutant]:
    """Take up to ``budget`` mutants, spread evenly across fault operators.

    Round-robins over operator classes rather than sampling uniformly, so a
    bank dominated by one prolific operator (constant bumping, typically)
    cannot crowd the others out of the sample.

    ``round_index`` rotates which member of each operator class is taken. A
    refinement loop that grades against one fixed sample every iteration is
    being scored on its training set: it writes properties that kill those
    particular faults rather than properties that capture what the design does.
    Measured on cv32e40p_alu, a suite refined against a fixed 24-mutant sample
    scored 81% on that sample and 11% on the full 365-mutant bank. Advancing
    ``round_index`` each iteration shows the loop fresh faults, so a property
    tuned to one sample stops paying off.

    Selection stays deterministic given ``(bank, budget, round_index)``, so a
    reported figure is still reproducible.

    Args:
        mutants: Full mutant bank, in generation order.
        budget: Maximum number to return. Non-positive means no cap.
        round_index: Rotation offset; pass the iteration number to resample.

    Returns:
        The selected mutants, in bank order.
    """
    if budget <= 0 or len(mutants) <= budget:
        return list(mutants)

    by_operator: Dict[str, List[Mutant]] = defaultdict(list)
    for m in mutants:
        by_operator[m.operator].append(m)

    order = sorted(by_operator)
    # Advance each class by however many members a round consumes from it, so
    # consecutive rounds draw disjoint members until the class wraps. Offsetting
    # by 1 would re-draw most of the previous sample.
    per_class = max(1, -(-budget // max(1, len(order))))
    offset = round_index * per_class

    picked: List[Mutant] = []
    depth = 0
    while len(picked) < budget:
        progressed = False
        for op in order:
            bucket = by_operator[op]
            if depth >= len(bucket):
                continue
            picked.append(bucket[(depth + offset) % len(bucket)])
            progressed = True
            if len(picked) == budget:
                break
        if not progressed:
            break
        depth += 1

    index = {id(m): i for i, m in enumerate(mutants)}
    seen, unique = set(), []
    for m in picked:
        if id(m) not in seen:
            seen.add(id(m))
            unique.append(m)
    return sorted(unique, key=lambda m: index[id(m)])


def ensure_formal_cluster(slots: Optional[int] = None) -> int:
    """Attach to a CHIA cluster, starting a local one if none is running.

    Solver tasks request the custom ``formal`` resource so a cluster can keep
    them off nodes without an oss-cad-suite install. A local Ray instance
    advertises no such resource by default, which would make every task queue
    forever, so declare it here.

    Args:
        slots: Concurrent solver slots to advertise locally. Defaults to the
            host's CPU count.

    Returns:
        The number of ``formal`` slots visible in the cluster.
    """
    import ray

    if not ray.is_initialized():
        slots = slots or (os.cpu_count() or 4)
        ray.init(resources={"formal": slots}, ignore_reinit_error=True,
                 log_to_driver=False)
        log.info("started a local CHIA cluster with %d formal slots", slots)

    available = ray.cluster_resources().get("formal", 0)
    if not available:
        raise RuntimeError(
            "This cluster advertises no 'formal' resource, so solver tasks "
            "would queue forever. Either start workers that declare it "
            "(resources: {\"formal\": N} in cluster.yaml), or call "
            "ensure_formal_cluster() before the first ChiaFunction call -- "
            "a local call auto-initialises Ray without it, and the resource "
            "cannot be added afterwards.")
    return int(available)


def _grade_on_cluster(
    tasks: Sequence[Tuple[str, str, Mutant]],
    dut_path: Path,
    module_name: str,
    golden_rtl: str,
    clk: str,
    rst_n: str,
    bmc_depth: int,
) -> List[Tuple[str, Mutant, Verdict]]:
    """Dispatch the (property, mutant) grid across CHIA workers.

    Mutated source is materialised on the driver and shipped with the task
    rather than regenerated remotely, so a worker needs no copy of the mutation
    engine's state -- only the design tree the proof resolves includes against.
    """
    from chia.base.ChiaFunction import get

    from .multifile_proof import prove_remote

    ensure_formal_cluster()
    refs, meta = [], []
    for pid, code, mutant in tasks:
        mutated = MutationEngineNode.materialize(mutant, golden_rtl)
        refs.append(prove_remote.chia_remote(
            str(dut_path), module_name, code, clk, rst_n, bmc_depth,
            "bmc", mutated))
        meta.append((pid, mutant))

    results: List[Tuple[str, Mutant, Verdict]] = []
    for (pid, mutant), outcome in zip(meta, get(refs)):
        try:
            verdict = Verdict(outcome[0])
        except Exception:
            verdict = Verdict.ERROR
        results.append((pid, mutant, verdict))
    return results


# --------------------------------------------------------------------------
# Batched grading
# --------------------------------------------------------------------------

@dataclass
class BatchOutcome:
    """Result of checking a whole property suite against one design in one call.

    Attributes:
        killed: Whether any property was refuted. Exact -- this is the only
            question the kill rate asks.
        refuted_by: Property ids the counterexample violated. A *lower bound*:
            bounded model checking returns one trace, so a property violated
            only by some other input sequence is not reported here.
        error: True when the design failed to elaborate at all.
    """
    killed: bool
    refuted_by: Set[str] = field(default_factory=set)
    error: bool = False


def build_batch(properties: Sequence[Tuple[str, str]]) -> Tuple[str, List[Tuple[str, int, int]]]:
    """Concatenate properties into one block, recording each one's line span.

    Grading a suite of P properties against M mutants costs P*M solver calls
    when each property is checked alone. Checking the whole suite at once costs
    M, because the solver reports every assertion the counterexample violates
    along with its source line -- so the line spans returned here are what maps
    a reported failure back to the property responsible.

    Shared scaffolding is emitted once: each property carries its own copy so
    it can be proved in isolation, and repeating it would redefine it.

    Args:
        properties: ``(property_id, code)`` pairs from
            :func:`partition_assertions`.

    Returns:
        ``(block, spans)`` where each span is ``(property_id, first_line,
        last_line)``, 1-based and relative to the start of ``block``.
    """
    preamble = ""
    bodies: List[Tuple[str, str]] = []
    for pid, code in properties:
        lines = code.splitlines()
        # partition_assertions prepends shared declarations; the assertion
        # itself is the trailing statement.
        if len(lines) > 1:
            head, body = "\n".join(lines[:-1]), lines[-1]
            if not preamble:
                preamble = head
        else:
            body = lines[-1] if lines else ""
        bodies.append((pid, body))

    out: List[str] = []
    if preamble:
        out.extend(preamble.splitlines())
    spans: List[Tuple[str, int, int]] = []
    for pid, body in bodies:
        start = len(out) + 1
        body_lines = body.splitlines() or [""]
        out.extend(body_lines)
        spans.append((pid, start, len(out)))
    return "\n".join(out), spans


def grade_batch(
    dut_path: Path,
    module_name: str,
    rtl: str,
    properties: Sequence[Tuple[str, str]],
    proof_fn: ProofFn,
    clk: str,
    rst_n: str,
    bmc_depth: int,
) -> BatchOutcome:
    """Check a whole property suite against one design in a single solver call.

    Args:
        rtl: Source to check -- the mutant, when grading a mutant.

    Returns:
        A :class:`BatchOutcome`.
    """
    block, spans = build_batch(properties)
    if not block.strip():
        return BatchOutcome(killed=False, error=True)

    capture: Dict = {}
    verdict, _, _, _ = proof_fn(
        dut_path=dut_path, module_name=module_name, sva_code=block,
        clk=clk, rst_n=rst_n, bmc_depth=bmc_depth, rtl_override=rtl,
        capture=capture,
    )
    if verdict is Verdict.ERROR:
        return BatchOutcome(killed=False, error=True)
    if verdict is not Verdict.FAIL:
        return BatchOutcome(killed=False)

    out = capture.get("sby_output", "")
    first = capture.get("sva_first_line")
    reported = [int(m) for m in re.findall(r"Assert failed in \w+: [^\s:]+:(\d+)\.", out)]
    refuted: Set[str] = set()
    if first:
        for line in reported:
            rel = line - first + 1
            for pid, lo, hi in spans:
                if lo <= rel <= hi:
                    refuted.add(pid)
                    break
    return BatchOutcome(killed=True, refuted_by=refuted)


def measure_mutation_coverage(
    dut_path: Path,
    module_name: str,
    golden_rtl: str,
    properties: Sequence[Tuple[str, str]],
    proof_fn: ProofFn,
    clk: str = "clk",
    rst_n: str = "rst_n",
    bmc_depth: int = 10,
    budget: int = 24,
    workers: int = 8,
    oss_cad_bin: str = "",
    classify_equivalents: bool = True,
    equiv_depth: int = 8,
    pre_verified: Optional[Set[str]] = None,
    equivalence_cache: Optional[Dict[str, str]] = None,
    dispatch: str = "local",
    batch: bool = True,
    round_index: int = 0,
) -> MutationCoverageReport:
    """Measure what an assertion suite actually catches.

    For each (property, mutant) pair the property block is checked against the
    mutated design. On a mutant, ``Verdict.FAIL`` is a *kill*: the assertion
    produced a counterexample, which is exactly the fault being detected.
    ``Verdict.PASS`` means the assertion did not notice the fault, and
    ``Verdict.ERROR`` means the mutant never elaborated.

    Args:
        dut_path: Path to the golden design. Used only to resolve packages,
            headers and submodules -- mutated text is passed in memory, so the
            source tree is never written to.
        module_name: Top module under check.
        golden_rtl: Unmodified design source.
        properties: ``(property_id, sva_code)`` pairs to evaluate individually.
        proof_fn: Model checker, typically ``check_formal_proof``.
        budget: Cap on mutants checked. The full bank for a 3000-line core runs
            to tens of thousands of mutants; a capped, operator-stratified
            sample keeps a sweep tractable and is reported as such.
        workers: Concurrent solver invocations.
        oss_cad_bin: ``bin`` of an oss-cad-suite install, for equivalence
            classification.
        classify_equivalents: Whether to prove survivors equivalent before
            counting them against the kill rate. Costs one miter per survivor.
        equiv_depth: Sequential depth for the equivalence miter.
        pre_verified: Property ids already proven sound on this design by an
            earlier call. Skipped by the soundness gate, so an iterative caller
            does not re-discharge the same proof every round.
        equivalence_cache: Mutant id -> equivalence verdict, reused and extended
            across calls. Equivalence depends only on the design, so an
            iterative caller should pass one dict and pay for each miter once.
        round_index: Rotates the mutant sample. Pass the refinement iteration
            so each round grades against different faults; see
            :func:`stratified_sample`.
        batch: Check the whole suite against each mutant in one solver call,
            then fill in the per-property matrix only for mutants that were
            killed. The kill rate is identical either way; this only changes
            how many solver calls it costs. Disabled automatically under Ray
            dispatch, where the grid is already fanned out.
        dispatch: ``"local"`` runs the (property, mutant) grid in a thread pool
            on this host; ``"ray"`` fans it out across CHIA workers advertising
            the ``formal`` resource. The grid is one independent solver call per
            pair and is the dominant cost, so on designs whose elaboration is
            expensive a single host is the binding constraint.

    Returns:
        A :class:`MutationCoverageReport`.
    """
    started = time.time()
    if dispatch == "ray":
        # Must happen before anything else touches a ChiaFunction: the first
        # local call auto-initialises Ray, and a cluster that is already up
        # cannot retroactively be told about the `formal` resource.
        ensure_formal_cluster()
    engine = MutationEngineNode(
        oss_cad_bin=oss_cad_bin, top=module_name, equiv_depth=equiv_depth
    )
    bank = engine.generate(golden_rtl)
    selected = stratified_sample(bank, budget, round_index)
    sampling = (
        "exhaustive"
        if len(selected) == len(bank)
        else f"operator-stratified sample of {len(selected)}/{len(bank)}"
    )
    log.info(
        "mutation coverage on %s: %d mutants generated, %d selected (%s)",
        module_name, len(bank), len(selected), sampling,
    )

    # Soundness gate. A property that does not hold on the golden design
    # "kills" every mutant for the wrong reason -- it is refuted by correct RTL
    # too -- and would inflate the kill rate to 100%. Such properties are
    # excluded from scoring and reported, never silently counted.
    candidates = list(properties) or [("prop_all", "")]
    props: List[Tuple[str, str]] = []
    unsound: List[Tuple[str, str]] = []
    already = pre_verified or set()
    resolved_deps: List[Path] = []
    for pid, code in candidates:
        if not code.strip():
            continue
        if pid in already:
            props.append((pid, code))
            continue
        verdict, detail, _, srcs = proof_fn(
            dut_path=dut_path, module_name=module_name, sva_code=code,
            clk=clk, rst_n=rst_n, bmc_depth=bmc_depth,
        )
        if verdict is Verdict.PASS:
            props.append((pid, code))
            if srcs and not resolved_deps:
                resolved_deps = list(srcs)
        else:
            reason = ("refuted by the golden design"
                      if verdict is Verdict.FAIL else f"not checkable: {detail}")
            unsound.append((pid, reason))
            log.warning("excluding %s from scoring -- %s", pid, reason)

    coverage = {pid: PropertyKills(property_id=pid, property_code=code)
                for pid, code in props}

    if not selected or not props:
        return MutationCoverageReport(
            generated=len(bank), sampled=0, stillborn=0, equivalent=0,
            active=0, killed=0, survived=0, kill_rate=None,
            per_property=list(coverage.values()),
            seconds=time.time() - started, sampling=sampling,
            unsound=unsound, scored_property_ids=[pid for pid, _ in props],
            sampled_mutants=list(selected),
        )

    # One solver call per (property, mutant) pair. Locally these are threads,
    # not processes: each call blocks in a subprocess, so the GIL is not the
    # bottleneck and nothing has to be pickled. On a cluster the same grid is
    # dispatched as CHIA tasks.
    def run_pair(task):
        pid, code, mutant = task
        mutated = MutationEngineNode.materialize(mutant, golden_rtl)
        try:
            verdict, detail, _, _ = proof_fn(
                dut_path=dut_path,
                module_name=module_name,
                sva_code=code,
                clk=clk,
                rst_n=rst_n,
                bmc_depth=bmc_depth,
                rtl_override=mutated,
            )
        except Exception as exc:  # a crashed solver is not evidence either way
            log.warning("proof failed on %s/%s: %s", pid, mutant.mid, exc)
            return pid, mutant, Verdict.ERROR
        return pid, mutant, verdict

    verdicts: Dict[str, List[Verdict]] = defaultdict(list)

    if batch and dispatch != "ray" and len(props) > 1:
        # Two phases. First check the whole suite against each mutant in one
        # call: that answers "was this mutant killed", exactly, at a cost of M
        # calls rather than P*M. Then rebuild the exact per-property matrix
        # only for mutants that were killed, since a batched counterexample
        # reports a lower bound on which properties catch a fault, and set
        # cover needs the full row.
        def run_batch(mutant):
            mutated = MutationEngineNode.materialize(mutant, golden_rtl)
            try:
                return mutant, grade_batch(
                    dut_path, module_name, mutated, props, proof_fn,
                    clk, rst_n, bmc_depth)
            except Exception as exc:
                log.warning("batched grading failed on %s: %s", mutant.mid, exc)
                return mutant, BatchOutcome(killed=False, error=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            batched = list(pool.map(run_batch, selected))

        killed_mutants = []
        for mutant, outcome in batched:
            if outcome.error:
                verdicts[mutant.mid].append(Verdict.ERROR)
                continue
            verdicts[mutant.mid].append(
                Verdict.FAIL if outcome.killed else Verdict.PASS)
            if outcome.killed:
                killed_mutants.append(mutant)
                for pid in outcome.refuted_by:
                    coverage[pid].killed.add(mutant.mid)

        follow_up = [(pid, code, m) for pid, code in props
                     for m in killed_mutants
                     if m.mid not in coverage[pid].killed]
        if follow_up:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for pid, mutant, verdict in pool.map(run_pair, follow_up):
                    if verdict is Verdict.FAIL:
                        coverage[pid].killed.add(mutant.mid)
        log.info("graded %d mutants in %d batched + %d per-property calls "
                 "(unbatched would be %d)",
                 len(selected), len(selected), len(follow_up),
                 len(props) * len(selected))
    else:
        tasks = [(pid, code, m) for pid, code in props for m in selected]
        if dispatch == "ray":
            results = _grade_on_cluster(
                tasks, dut_path, module_name, golden_rtl, clk, rst_n, bmc_depth)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                results = list(pool.map(run_pair, tasks))

        for pid, mutant, verdict in results:
            verdicts[mutant.mid].append(verdict)
            if verdict is Verdict.FAIL:
                coverage[pid].killed.add(mutant.mid)

    # A mutant is stillborn only if no property could be checked against it at
    # all -- if one property errored and another proved, the mutant elaborated.
    stillborn_ids = {
        mid for mid, vs in verdicts.items()
        if vs and all(v is Verdict.ERROR for v in vs)
    }
    killed_ids: Set[str] = set()
    for cov in coverage.values():
        killed_ids |= cov.killed
    killed_ids -= stillborn_ids

    by_mid = {m.mid: m for m in selected}
    survivor_ids = [
        m.mid for m in selected
        if m.mid not in killed_ids and m.mid not in stillborn_ids
    ]

    # A mutant that is behaviourally equivalent to golden is not a fault, so it
    # belongs in neither the numerator nor the denominator. Crucially this is
    # classified for EVERY sampled mutant, not just the survivors: if it were
    # survivor-only, a stronger suite would leave fewer mutants to classify,
    # find fewer equivalents, and end up scored against a different
    # denominator than a weaker one. The scoring population has to be a
    # property of the design alone.
    equivalent_ids: Set[str] = set()
    unknown_equivalence = 0
    if classify_equivalents and oss_cad_bin:
        cache = equivalence_cache if equivalence_cache is not None else {}
        to_classify = [m.mid for m in selected
                       if m.mid not in stillborn_ids and m.mid not in cache]

        from .multifile_proof import check_equivalence
        suffix = dut_path.suffix or ".v"

        def classify(mid: str):
            mutated = MutationEngineNode.materialize(by_mid[mid], golden_rtl)
            try:
                equivalence, _ = check_equivalence(
                    module_name=module_name, golden_rtl=golden_rtl,
                    mutant_rtl=mutated, dependencies=resolved_deps,
                    equiv_depth=equiv_depth, suffix=suffix)
            except Exception as exc:
                log.debug("equivalence check failed on %s: %s", mid, exc)
                return mid, Equivalence.UNKNOWN.value
            return mid, equivalence

        if to_classify:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for mid, equivalence in pool.map(classify, to_classify):
                    cache[mid] = equivalence

        # UNKNOWN stays in the denominator: an unproven mutant counts against
        # the suite. Only a proof of equivalence excuses it.
        equivalent_ids = {
            m.mid for m in selected
            if cache.get(m.mid) == Equivalence.EQUIVALENT.value
        }
        unknown_equivalence = sum(
            1 for m in selected
            if m.mid not in stillborn_ids
            and cache.get(m.mid) == Equivalence.UNKNOWN.value)

    # An equivalent mutant killed by a white-box property is an over-specified
    # assertion reaching into internals, not a fault caught. Drop it from both
    # sides so `killed` stays a subset of `active`.
    killed_equivalent = len(killed_ids & equivalent_ids)
    killed_ids -= equivalent_ids
    survivor_ids = [mid for mid in survivor_ids if mid not in equivalent_ids]
    active = len(selected) - len(stillborn_ids) - len(equivalent_ids)
    killed = len(killed_ids)
    kill_rate = (killed / active * 100.0) if active > 0 else None

    return MutationCoverageReport(
        generated=len(bank),
        sampled=len(selected),
        stillborn=len(stillborn_ids),
        equivalent=len(equivalent_ids),
        active=active,
        killed=killed,
        survived=len(survivor_ids),
        kill_rate=kill_rate,
        killed_equivalent=killed_equivalent,
        equivalence_unknown=unknown_equivalence,
        survivors=sorted(survivor_ids),
        per_property=list(coverage.values()),
        seconds=time.time() - started,
        sampling=sampling,
        equivalence_checked=bool(classify_equivalents and oss_cad_bin),
        unsound=unsound,
        scored_property_ids=[pid for pid, _ in props],
        sampled_mutants=list(selected),
    )


# --------------------------------------------------------------------------
# Minimal invariant basis
# --------------------------------------------------------------------------

@dataclass
class MinimalBasisReport:
    """Smallest assertion subset preserving the measured kill set."""
    candidates: int
    selected_ids: List[str]
    selected_code: str
    kills_all: int
    kills_minimal: int
    redundancy_pct: float
    essential_count: int

    @property
    def coverage_preserved(self) -> bool:
        """Whether the reduced suite kills everything the full suite killed."""
        return self.kills_minimal == self.kills_all


def solve_minimal_basis(report: MutationCoverageReport) -> MinimalBasisReport:
    """Greedy set cover over the measured property-by-mutant incidence matrix.

    Properties with a *unique* kill -- some mutant no other property catches --
    are essential and taken first; the rest are added greedily by marginal
    coverage. Redundancy is then the fraction of candidates the cover did not
    need, measured rather than assumed.

    Args:
        report: Output of :func:`measure_mutation_coverage`.

    Returns:
        A :class:`MinimalBasisReport`. With no measured kills, every candidate
        is retained and redundancy is 0.0 -- there is no evidence on which to
        drop anything.
    """
    props = report.per_property
    universe: Set[str] = set()
    for p in props:
        universe |= p.killed

    if not props or not universe:
        return MinimalBasisReport(
            candidates=len(props),
            selected_ids=[p.property_id for p in props],
            selected_code="\n".join(p.property_code for p in props),
            kills_all=0,
            kills_minimal=0,
            redundancy_pct=0.0,
            essential_count=0,
        )

    killers: Dict[str, List[str]] = defaultdict(list)
    for p in props:
        for mid in p.killed:
            killers[mid].append(p.property_id)

    by_id = {p.property_id: p for p in props}
    essential = sorted({ks[0] for ks in killers.values() if len(ks) == 1})
    for pid in essential:
        by_id[pid].is_essential = True

    selected = list(essential)
    covered: Set[str] = set()
    for pid in selected:
        covered |= by_id[pid].killed

    position = {p.property_id: i for i, p in enumerate(props)}
    while covered != universe:
        remaining = [p for p in props if p.property_id not in selected]
        # Tie-break on declaration order so the basis is stable across runs.
        best = max(
            remaining,
            key=lambda p: (len(p.killed - covered), -position[p.property_id]),
            default=None,
        )
        if best is None or not (best.killed - covered):
            break
        selected.append(best.property_id)
        covered |= best.killed

    selected.sort(key=lambda pid: position[pid])
    redundancy = (1.0 - len(selected) / len(props)) * 100.0 if props else 0.0

    return MinimalBasisReport(
        candidates=len(props),
        selected_ids=selected,
        selected_code="\n".join(by_id[pid].property_code for pid in selected),
        kills_all=len(universe),
        kills_minimal=len(covered),
        redundancy_pct=redundancy,
        essential_count=len(essential),
    )


# --------------------------------------------------------------------------
# Vacuity
# --------------------------------------------------------------------------

@dataclass
class VacuityMeasurement:
    """Measured reachability of assertion antecedents.

    Two denominators, because they answer different questions and are easy to
    confuse. ``vacuity_pct`` is over *distinct antecedents* -- how many trigger
    expressions are unreachable. ``properties_vacuous`` is over *properties* --
    how many assertions therefore pass for free. A suite where twenty
    assertions share one unreachable guard is 1-of-N by the first measure and
    20-of-M by the second; quoting the smaller number understates how much of
    the suite is inert.
    """
    antecedents: int
    reachable: int
    vacuous: int
    vacuity_pct: float
    status: str
    unreached: List[str] = field(default_factory=list)
    error: Optional[str] = None
    properties_total: int = 0
    properties_vacuous: int = 0

    @property
    def properties_vacuous_pct(self) -> Optional[float]:
        """Share of properties whose guard is unreachable."""
        if not self.properties_total:
            return None
        return self.properties_vacuous / self.properties_total * 100.0


def measure_vacuity(
    dut_path: Path,
    module_name: str,
    golden_rtl: str,
    sva_code: str,
    proof_fn: ProofFn,
    clk: str = "clk",
    rst_n: str = "rst_n",
    depth: int = 20,
) -> VacuityMeasurement:
    """Prove each assertion antecedent reachable via SymbiYosys ``cover``.

    An assertion whose trigger is unreachable passes for free. This discharges
    one cover point per antecedent and reports which ones the solver could
    actually reach within ``depth`` cycles.

    Args:
        dut_path: Golden design, for dependency resolution.
        module_name: Top module under check.
        golden_rtl: Unmodified design source (unused directly; cover points are
            injected by ``proof_fn``, which reads the design itself).
        sva_code: The assertion block whose antecedents are extracted.
        proof_fn: Model checker, run here in ``cover`` mode.
        depth: Cover search depth.

    Returns:
        A :class:`VacuityMeasurement`. When the cover run itself errors the
        result is reported as unknown rather than as certified non-vacuous.
    """
    # Antecedents routinely reference scaffolding declared alongside the
    # assertions (an f_cycles warm-up counter, helper registers). Injecting the
    # cover points without those declarations fails elaboration, which would be
    # misreported as "cover run did not complete".
    scaffolding, _, _ = partition_assertions(sva_code)
    antecedents = VacuityGateNode.extract_antecedents(sva_code)
    if not antecedents:
        return VacuityMeasurement(
            antecedents=0, reachable=0, vacuous=0, vacuity_pct=0.0,
            status="n/a (no guarded assertions to check)",
        )

    cover_block = "\n".join(
        f"always @(posedge {clk}) if (f_past_valid) cover_{i:02d}: cover({cond});"
        for i, cond in enumerate(antecedents)
    )
    if scaffolding:
        cover_block = scaffolding + "\n" + cover_block

    capture: Dict = {}
    verdict, detail, _, _ = proof_fn(
        dut_path=dut_path,
        module_name=module_name,
        sva_code=cover_block,
        clk=clk,
        rst_n=rst_n,
        bmc_depth=depth,
        mode="cover",
        capture=capture,
    )
    out = capture.get("sby_output", "")

    if verdict is Verdict.ERROR:
        return VacuityMeasurement(
            antecedents=len(antecedents), reachable=0,
            vacuous=0, vacuity_pct=float("nan"),
            status="unknown (cover run did not complete)",
            error=detail,
        )

    # sby names each cover point as it resolves it, in two places and two
    # different word orders:
    #   "Reached cover statement in step 3 at arbiter: cover_00"
    #   "Unreached cover statement at arbiter: cover_01"
    # and again in the run summary:
    #   "summary:   reached cover statement arbiter.cover_00 at file:3.4-3.9"
    #   "summary: unreached cover statements:"
    #   "summary:     arbiter.cover_01 at file:4.4-4.9"
    # Matching only one form made a partially-reached run look wholly
    # unreachable, because the per-point result then fell back to the run's
    # overall verdict -- which is FAIL whenever *any* point is missed.
    reached = set(re.findall(
        r"[Rr]eached cover statement in step \d+ at [\w.]+: (cover_\d+)", out))
    reached |= set(re.findall(
        r"[Rr]eached cover statement [\w.]*?\b(cover_\d+)\b", out))
    missed = set(re.findall(
        r"[Uu]nreached cover statement at [\w.]+: (cover_\d+)", out))
    missed |= set(re.findall(
        r"summary:\s+[\w.]*\.(cover_\d+) at ", out))
    reached -= missed

    unreached: List[str] = []
    reachable = 0
    for i, cond in enumerate(antecedents):
        name = f"cover_{i:02d}"
        if name in missed:
            hit = False
        elif name in reached:
            hit = True
        else:
            hit = verdict is Verdict.PASS
        if hit:
            reachable += 1
        else:
            unreached.append(cond)

    vacuous = len(antecedents) - reachable
    pct = vacuous / len(antecedents) * 100.0

    # Count how many individual assertions are rendered inert, not just how
    # many distinct guards are unreachable.
    _, parsed, _ = partition_assertions(sva_code)
    props_total = len(parsed)
    props_vacuous = 0
    if unreached and props_total:
        for _, code in parsed:
            body = " ".join(code.splitlines()[-1].split())
            if any(" ".join(c.split()) in body for c in unreached):
                props_vacuous += 1

    prop_note = ""
    if props_total:
        prop_note = (f"; {props_vacuous}/{props_total} properties inert"
                     if props_vacuous else f"; 0/{props_total} properties inert")
    status = (
        f"{pct:.1f}% vacuous ({reachable}/{len(antecedents)} antecedents reachable, "
        f"cover depth {depth}){prop_note}"
    )
    return VacuityMeasurement(
        antecedents=len(antecedents),
        reachable=reachable,
        vacuous=vacuous,
        vacuity_pct=pct,
        status=status,
        unreached=unreached,
        properties_total=props_total,
        properties_vacuous=props_vacuous,
    )


# --------------------------------------------------------------------------
# Assertion block partitioning
# --------------------------------------------------------------------------

# Constructs Yosys' native SystemVerilog front end cannot read without Verific.
# A block containing any of these is rejected before it reaches the solver,
# rather than being scored as an ERROR that looks like a design problem.
UNSUPPORTED_SVA = (
    "assert property", "cover property", "assume property",
    "endproperty", "endsequence", "disable iff",
    "|->", "|=>", "$rose", "$fell", "$stable", "$onehot",
)


def _statement_spans(block: str) -> List[Tuple[int, int]]:
    """Find the span of every top-level ``always``/``initial`` statement.

    Tracks ``begin``/``end`` and ``case``/``endcase`` nesting so a multi-line
    block is returned whole. A naive split on ``assert`` shreds those into
    fragments that neither compile nor mean anything.
    """
    spans: List[Tuple[int, int]] = []
    token = re.compile(r"\b(always(?:_ff|_comb|_latch)?|initial|begin|case[xz]?|end|endcase)\b|;")
    start: Optional[int] = None
    depth = 0
    seen_body = False

    for m in token.finditer(block):
        word = m.group(0)
        if start is None:
            if word in ("always", "always_ff", "always_comb", "always_latch", "initial"):
                start, depth, seen_body = m.start(), 0, False
            continue
        if word in ("begin", "case", "casex", "casez"):
            depth += 1
            seen_body = True
        elif word in ("end", "endcase"):
            depth -= 1
            if depth <= 0:
                spans.append((start, m.end()))
                start = None
        elif word == ";" and depth == 0 and not seen_body:
            spans.append((start, m.end()))
            start = None
    return spans


def partition_assertions(block: str) -> Tuple[str, List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Split a synthesized SVA block into independently checkable properties.

    Shared scaffolding -- a ``f_cycles`` warm-up counter, helper registers --
    is separated out and prepended to every property, because a property that
    references it will not compile once isolated from the block.

    Args:
        block: Raw synthesized SystemVerilog.

    Returns:
        ``(preamble, properties, rejected)`` where ``properties`` and
        ``rejected`` are both ``(property_id, code)`` lists. A statement is
        rejected when it uses a construct the front end cannot parse, or when
        it contains no assertion at all.
    """
    spans = _statement_spans(block)
    covered = set()
    for a, b in spans:
        covered.update(range(a, b))

    # Anything outside a statement span that declares state is scaffolding.
    leftovers = []
    cursor = 0
    for a, b in spans:
        leftovers.append(block[cursor:a])
        cursor = b
    leftovers.append(block[cursor:])
    # The harness already declares and drives f_past_valid; a synthesized
    # re-declaration is a duplicate definition and fails elaboration.
    harness_owned = re.compile(r"\bf_past_valid\b")
    preamble_lines = [
        ln for chunk in leftovers for ln in chunk.splitlines()
        if re.match(r"\s*(reg|wire|logic|integer|localparam)\b", ln)
        and not harness_owned.search(ln)
    ]

    properties: List[Tuple[str, str]] = []
    rejected: List[Tuple[str, str]] = []
    for a, b in spans:
        stmt = block[a:b].strip()
        low = stmt.lower()
        if any(bad in low for bad in UNSUPPORTED_SVA):
            rejected.append((f"stmt_{len(rejected):02d}", stmt))
        elif "assert" not in low:
            # A counter or helper always-block, not a property: scaffolding.
            # Skip anything driving f_past_valid -- the harness owns it.
            if not harness_owned.search(stmt):
                preamble_lines.append(stmt)
        else:
            properties.append((f"prop_{len(properties):02d}", stmt))

    # Properties arrive already carrying their own copy of shared scaffolding,
    # so a concatenated suite yields N copies of the same declaration. Emitting
    # those verbatim is a duplicate-definition error that fails elaboration --
    # which the vacuity gate would then misreport as "cover run did not
    # complete" rather than as an unreachable antecedent.
    seen: Set[str] = set()
    deduped = []
    for line in preamble_lines:
        key = " ".join(line.split())
        if key and key not in seen:
            seen.add(key)
            deduped.append(line)

    preamble = "\n".join(deduped).strip()
    if preamble:
        properties = [(pid, preamble + "\n" + code) for pid, code in properties]
    return preamble, properties, rejected


# --------------------------------------------------------------------------
# RTL excerpting for prompts
# --------------------------------------------------------------------------

# Characters of RTL to put in a prompt before eliding. Gemini 2.5 Flash has a
# 1M-token window (~4 MB), so this is ~5% of capacity, not a capacity limit:
# every module this project can actually model-check is smaller than this and
# is sent whole. The cap exists because the multi-MB generated files it would
# otherwise send (BOOM's LSU is 4 MB) are far too slow to verify anyway, so
# spending latency and attention on them buys nothing. Override with
# CHIA_RTL_PROMPT_BUDGET.
RTL_PROMPT_BUDGET = int(os.environ.get("CHIA_RTL_PROMPT_BUDGET", 200_000))


def rtl_for_prompt(rtl_text: str, budget: Optional[int] = None) -> str:
    """Return the design for a prompt, eliding the middle only if it must.

    Blind head-truncation is worse than it looks. Slicing a 35 KB ALU to its
    first 8 KB shows the model the port list and the operand-negation prelude
    and nothing else, so it writes properties about those and none about the
    shifter, comparator or min/max logic further down -- and the resulting kill
    rate measures the prompt window rather than the method.

    When the design does not fit, the port list is kept intact (properties must
    reference real port names) and the remaining budget is spread across evenly
    spaced windows of the body, with explicit elision markers so the model is
    not misled into thinking it has seen everything.

    Args:
        rtl_text: Full design source.
        budget: Approximate character budget; defaults to
            :data:`RTL_PROMPT_BUDGET`.

    Returns:
        Source text to embed in a prompt.
    """
    budget = budget or RTL_PROMPT_BUDGET
    if len(rtl_text) <= budget:
        return rtl_text

    lines = rtl_text.splitlines()
    # The header through the end of the port list: everything a property is
    # allowed to name.
    head_end = 0
    for i, line in enumerate(lines[:400]):
        if ");" in line or re.match(r"\s*\);", line):
            head_end = i + 1
            break
    head_end = head_end or min(80, len(lines))
    head = "\n".join(lines[:head_end])
    if len(head) > budget // 2:
        # Keep the declaration lines most likely to name ports.
        kept, used = [], 0
        for line in lines[:head_end]:
            if used + len(line) + 1 > budget // 2:
                break
            kept.append(line)
            used += len(line) + 1
        head = "\n".join(kept) + "\n// ... remainder of port list omitted ..."

    body = lines[head_end:]
    remaining = max(budget - len(head), budget // 4)
    windows = 6
    # Measure real characters rather than assuming a line length: generated
    # RTL averages ~150 chars/line where hand-written is nearer 40, so a
    # line-count budget overshoots by 3-4x on exactly the largest files.
    per_window_chars = max(500, remaining // windows)

    chunks, step = [], max(1, len(body) // windows)
    for w in range(windows):
        start = w * step
        seg, used = [], 0
        for line in body[start:]:
            if used + len(line) + 1 > per_window_chars:
                break
            seg.append(line)
            used += len(line) + 1
        if not seg:
            continue
        first = head_end + start + 1
        chunks.append(f"// ... lines {first}-{first + len(seg) - 1} ...\n"
                      + "\n".join(seg))

    return (head + "\n\n// NOTE: this design is "
            f"{len(lines)} lines and has been excerpted. Regions between the\n"
            "// markers below are omitted; do not assume signals you cannot see\n"
            "// here are absent, and prefer properties over what is shown.\n\n"
            + "\n\n".join(chunks))


def rtl_around_lines(
    rtl_text: str,
    focus_lines: Sequence[int],
    context: int = 50,
    budget: Optional[int] = None,
) -> str:
    """Excerpt the design around specific lines, keeping the port list.

    The refinement loop knows exactly which lines its surviving mutants sit on.
    Showing the model those neighbourhoods is far more useful than showing it
    the top of the file, which is what it gets from plain truncation.

    Args:
        rtl_text: Full design source.
        focus_lines: 1-based line numbers to centre windows on.
        context: Lines of context either side of each focus line.
        budget: Approximate character budget.

    Returns:
        Source text covering the port list plus the requested neighbourhoods.
    """
    budget = budget or RTL_PROMPT_BUDGET
    if len(rtl_text) <= budget or not focus_lines:
        return rtl_for_prompt(rtl_text, budget)

    lines = rtl_text.splitlines()
    head_end = 0
    for i, line in enumerate(lines[:400]):
        if ");" in line:
            head_end = i + 1
            break
    head_end = head_end or min(80, len(lines))

    wanted: Set[int] = set(range(0, head_end))
    for ln in focus_lines:
        lo = max(0, ln - 1 - context)
        hi = min(len(lines), ln + context)
        wanted.update(range(lo, hi))

    out, prev = [], None
    for i in sorted(wanted):
        if prev is not None and i != prev + 1:
            out.append(f"// ... lines {prev + 2}-{i} omitted ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


# --------------------------------------------------------------------------
# Clock / reset discovery
# --------------------------------------------------------------------------

@dataclass
class ClockReset:
    """Clock and reset discovered from a module's port list.

    Attributes:
        clk: Clock port name, or ``None`` for a purely combinational module.
        rst: Reset port name, or ``None`` if the module has no reset.
        active_low: Whether reset asserts low. Drives both the harness's reset
            assumption and the guard properties must use.
        guard: Expression that is true exactly when the design is out of reset
            and enough history exists to evaluate ``$past``. Properties must be
            guarded with this -- using the raw reset name is wrong for an
            active-high reset and yields assertions that never fire.
        sequential: Whether the module has any clocked process.
    """
    clk: Optional[str]
    rst: Optional[str]
    active_low: bool
    guard: str
    sequential: bool

    @property
    def combinational(self) -> bool:
        """Whether the module has no clocked process to attach to.

        Arithmetic units -- ALUs, multipliers, FP fused-multiply-add, decoders
        -- are frequently pure functions of their inputs. They carry no clock
        port, so clocked assertions cannot be written against them, but they
        are the classic formal target and must not be skipped.
        """
        return not self.sequential or self.clk is None

    @property
    def checkable(self) -> bool:
        """Whether assertions of some form can be attached.

        True for both clocked and combinational modules; the caller picks the
        assertion shape from :attr:`combinational`.
        """
        return True

    @property
    def assertion_template(self) -> str:
        """The shape properties for this module must take.

        Combinational logic has no state, so there is no reset to sequence
        past, no history for ``$past`` to read, and no clock to sample on.
        """
        if self.combinational:
            return "always @(*) assert (<expression>);"
        return f"always @(posedge {self.clk}) if ({self.guard}) assert (<expression>);"

    @property
    def exhaustive(self) -> bool:
        """Whether a passing check is a complete proof rather than a bounded one.

        With no flip-flops there is no reachability question: every input
        combination is reachable in one step, so a depth-1 bounded check covers
        the whole state space. For combinational modules a PASS is therefore a
        proof for all inputs, not merely to a depth.
        """
        return self.combinational


# Ordered most-specific first: `rst_ni` must win over `rst`, or a substring
# match renames the port and every generated assertion references a signal
# that does not exist.
_CLOCK_NAMES = ("clk_i", "i_clk", "clk", "clock_i", "clock", "CLK", "aclk")
_RESET_NAMES = ("rst_ni", "rst_n", "resetn", "reset_n", "arstn", "RST_N",
                "i_rst_n", "i_rst", "rst_i", "reset_i", "rst", "reset", "RST")


def detect_clock_reset(rtl_text: str) -> ClockReset:
    """Infer clock, reset and the correct assertion guard from the port list.

    Reads declared input ports rather than scanning the whole file, so a
    mention of ``reset`` in a comment or a submodule instantiation does not
    invent a port the module does not have.

    Args:
        rtl_text: Module source.

    Returns:
        A :class:`ClockReset`. ``clk``/``rst`` are ``None`` when absent --
        combinational blocks and reset-free datapaths are both common, and
        silently defaulting to ``clk``/``rst_n`` produces "undeclared
        identifier" errors that look like tool failures.
    """
    ports = set(re.findall(
        r"\binput\s+(?:wire\s+|logic\s+|reg\s+)?(?:\[[^\]]*\]\s*)?(\w+)", rtl_text))

    clk = next((c for c in _CLOCK_NAMES if c in ports), None)
    rst = next((r for r in _RESET_NAMES if r in ports), None)
    sequential = bool(re.search(r"always(?:_ff)?\s*@\s*\(\s*(?:posedge|negedge)",
                                rtl_text, re.I))

    active_low = bool(rst) and (
        rst.lower().endswith(("_n", "_ni", "n", "_b"))
        and not rst.lower().endswith("_in")
    )
    # `i_rst`, `rst`, `rst_i` assert high; `rst_n`, `rst_ni`, `resetn` assert low.
    if rst and rst.lower() in ("rst", "reset", "i_rst", "rst_i", "reset_i"):
        active_low = False

    if not sequential or clk is None:
        # No clocked process: assertions are combinational and unguarded.
        guard = ""
    elif rst is None:
        guard = "f_past_valid"
    elif active_low:
        guard = f"f_past_valid && {rst}"
    else:
        guard = f"f_past_valid && !{rst}"

    return ClockReset(clk=clk, rst=rst, active_low=active_low,
                      guard=guard, sequential=sequential)


def combine_properties(preamble: str, properties: Sequence[Tuple[str, str]]) -> str:
    """Join independently-checkable properties back into one block.

    :func:`partition_assertions` prepends the shared scaffolding to *every*
    property so each can be proved on its own. Concatenating those directly
    emits the scaffolding once per property, which is a redefinition error.
    This strips the repeated copy and emits it once.

    Args:
        preamble: Shared scaffolding returned by :func:`partition_assertions`.
        properties: ``(property_id, code)`` pairs from the same call.

    Returns:
        A single block that elaborates.
    """
    bodies = []
    for _, code in properties:
        if preamble and code.startswith(preamble):
            code = code[len(preamble):]
        bodies.append(code.strip())
    joined = "\n".join(b for b in bodies if b)
    return f"{preamble}\n{joined}" if preamble else joined
