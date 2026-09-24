"""Typed state passed along the edges of the formal-collateral loop."""
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    """Outcome of a bounded model check.

    Attributes:
        PASS: No counterexample exists within the configured depth. On golden
            RTL this means the property is consistent with the design; on a
            mutant it means the property FAILED to detect the injected fault.
        FAIL: A counterexample was found. On golden RTL this is an
            over-constraint or a genuine bug (escalated, never dropped); on a
            mutant it is a KILL -- the property caught the fault.
        ERROR: Elaboration or syntax failure. Not a result: a malformed
            property is sent back for repair rather than scored.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


class Equivalence(str, Enum):
    """Whether a mutant is a real fault or a semantic no-op.

    Attributes:
        DISTINGUISHABLE: A separating input sequence exists; a real fault that
            belongs in the scoring denominator.
        EQUIVALENT: Proved indistinguishable from golden within the bound. No
            property or test can ever kill it, so counting it would deflate
            every kill rate reported against this mutant bank.
        UNKNOWN: The check did not converge (timeout or tool error). Excluded
            from scoring and reported separately -- never silently treated as
            either of the above.
    """
    DISTINGUISHABLE = "DISTINGUISHABLE"
    EQUIVALENT = "EQUIVALENT"
    UNKNOWN = "UNKNOWN"


@dataclass
class Mutant:
    """One single-fault mutation of the design under study.

    Attributes:
        mid: Stable identifier, ``m<NNN>_<OPERATOR>_L<line>_<occurrence>``.
        operator: Mutation operator that produced it (e.g. ``DROP_GUARD``).
        line_no: 1-indexed source line the mutation was applied to.
        original: The unmutated source line, stripped.
        mutated: The mutated source line, stripped.
    """
    mid: str
    operator: str
    line_no: int
    original: str
    mutated: str


@dataclass
class ProofResult:
    """Result of one SymbiYosys invocation.

    Attributes:
        verdict: See :class:`Verdict`.
        detail: Human-readable outcome (counterexample step, error text).
        failed_assert: Source location of the assertion that fired, if any.
        depth: BMC depth the check was run to.
        engine: Solver backend used.
    """
    verdict: Verdict
    detail: str = ""
    failed_assert: str = ""
    depth: int = 0
    engine: str = ""


@dataclass
class MutantOutcome:
    """How one mutant fared against the test suite and against the properties.

    Attributes:
        mutant: The mutation itself.
        killed_by_tests: The existing simulation suite detected it.
        equivalence: Whether it is a real fault at all.
        killed_by_properties: The generated assertions detected it.
        proof: The proof result that produced ``killed_by_properties``.
    """
    mutant: Mutant
    killed_by_tests: bool = False
    equivalence: Equivalence = Equivalence.UNKNOWN
    killed_by_properties: bool = False
    proof: ProofResult | None = None

    @property
    def is_scoring_target(self) -> bool:
        """True iff this mutant belongs in the kill-rate denominator.

        A mutant scores only when the existing tests MISS it and it is a real
        fault. Killing what the suite already catches earns no credit -- that
        is the whole point of grading collateral rather than counting it.
        """
        return (not self.killed_by_tests
                and self.equivalence is Equivalence.DISTINGUISHABLE)


@dataclass
class GradeReport:
    """Scored outcome of one property set against a mutant bank.

    Attributes:
        golden: Verdict of the property set on unmodified RTL. Anything but
            PASS means the set is not scoreable.
        score: Fraction of scoring targets killed, or None if not scoreable.
        outcomes: Per-mutant detail.
        surviving: Mutants that are real faults, missed by tests AND by the
            properties -- the feedback handed to the next refine round.
    """
    golden: ProofResult
    score: float | None = None
    outcomes: list[MutantOutcome] = field(default_factory=list)
    surviving: list[Mutant] = field(default_factory=list)
