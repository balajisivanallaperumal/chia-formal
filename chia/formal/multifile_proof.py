"""Multi-file, package-aware bounded model checking as a CHIA node.

Real RTL does not arrive as one self-contained file. A CV32E40P module pulls in
packages, headers and submodules scattered across a source tree, and a proof
harness that cannot resolve those is useless on anything but a toy. This module
resolves them, splices properties into the design, and runs SymbiYosys.

Two entry points, same work:

* :func:`check_formal_proof` runs in-process. Use it from a script.
* :func:`prove_remote` is the same call wrapped as a CHIA function, so it
  dispatches to cluster workers advertising the ``formal`` resource. Grading a
  property suite against a mutant bank is one independent solver call per
  (property, mutant) pair -- embarrassingly parallel, and the reason a large
  module is intractable on a single host.
"""
from __future__ import annotations

import os
import re
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from chia.base.ChiaFunction import ChiaFunction

from .state_def import Verdict


def _default_oss_cad_bin() -> str:
    """Locate the oss-cad-suite ``bin`` directory.

    Prefers whatever is already on PATH so a cluster worker with its own
    install is not overridden by a head-node path that does not exist there.
    """
    sby = shutil.which("sby")
    if sby:
        return str(Path(sby).parent)
    root = Path(os.environ.get("CHIA_ROOT", Path(__file__).resolve().parents[2]))
    return str(root / "tools" / "oss-cad-suite" / "bin")


OSS_CAD_BIN = _default_oss_cad_bin()
REPO_DIR = Path(os.environ.get("CHIA_ROOT", Path(__file__).resolve().parents[2]))


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments so declarations can be found reliably.

    Chisel emits an ``extern_modules.sv`` containing nothing but lines like
    ``// external module plusarg_reader``. A module index that scans for
    ``module <name>`` without stripping comments records that file as the
    definition site of every blackbox it mentions, then adds a file that
    declares nothing -- and the missing-module error never clears.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def check_formal_proof(
    dut_path: Path,
    module_name: str,
    sva_code: str,
    clk: str = "clk",
    rst_n: str = "rst_n",
    bmc_depth: int = 10,
    mode: str = "bmc",
    rtl_override: Optional[str] = None,
    capture: Optional[Dict] = None,
    trace_dir: Optional[Path] = None,
    defines: Optional[Sequence[str]] = None,
    extra_search_dirs: Optional[Sequence[Path]] = None,
) -> Tuple[Verdict, str, Optional[str], List[Path]]:
    """
    Run multi-file, package-aware, include-aware SBY model checking with strict 3-way verdict triaging:
      - Verdict.PASS: No counterexample within the bound (in `cover` mode: all cover points reached).
      - Verdict.FAIL: Real assertion violation (counterexample trace generated).
      - Verdict.ERROR: Tool / elaboration / syntax failure (DO NOT mask as PASS).

    Args:
        mode: SymbiYosys mode. `bmc` for bounded assertion checking, `prove` for
            k-induction, `cover` for reachability (used by the vacuity gate).
        rtl_override: Source text to verify in place of `dut_path`'s contents.
            `dut_path` is still used to resolve packages, headers and submodules,
            so a mutant can be checked without writing it into the source tree.
        capture: If supplied, populated with the raw `sby` output under
            `sby_output` and the mode under `mode`, for callers that need to
            parse per-cover-point or per-assert detail.
        trace_dir: If supplied, a counterexample waveform is copied here before
            the scratch directory is destroyed, and the returned path points at
            the copy. A bug report without its trace is an assertion, not
            evidence.
        defines: Extra preprocessor macros, passed as ``-D<name>`` to whichever
            front end is used. Needed to activate a design's own in-tree
            assertions: lowRISC's `prim_assert` dispatches on ``YOSYS``, and
            without it every `ASSERT in ibex compiles to nothing.
        extra_search_dirs: Additional directories to search for packages,
            headers and submodules. Vendored assertion infrastructure often
            sits outside the design's own rtl/ tree.
    """
    dut_path = Path(dut_path).resolve()
    dut_dir = dut_path.parent
    base_dir = dut_dir.parent if dut_dir.name in ["rtl", "bhv", "sva", "include", "src"] else dut_dir
    
    # Establish scoped search directories for packages, headers, and submodules
    search_dirs = [dut_dir, base_dir]
    for arch_cand in [
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "cv32e40p",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "ibex",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "cva6",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "rocket",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "boom",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "serv",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "picorv32",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "hazard3",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "flute",
        REPO_DIR / "examples" / "formal_verification" / "soc_cores" / "toooba",
    ]:
        if arch_cand.exists() and (arch_cand.name.lower() in str(dut_path).lower() or arch_cand.name.lower() in module_name.lower()):
            search_dirs.extend([
                arch_cand, 
                arch_cand / "rtl", 
                arch_cand / "bhv", 
                arch_cand / "core",
                arch_cand / "core" / "include",
                arch_cand / "core" / "frontend",
                arch_cand / "core" / "cache_subsystem",
                arch_cand / "core" / "cva6_mmu",
                arch_cand / "core" / "pmp" / "src",
                arch_cand / "core" / "pmp" / "include",
                arch_cand / "common" / "local" / "util",
                arch_cand / "rtl" / "include", 
                arch_cand / "vendor",
            ])

    for extra in extra_search_dirs or []:
        search_dirs.append(Path(extra))
    search_dirs = list(dict.fromkeys(d.resolve() for d in search_dirs if d.exists()))
    # A define prefixed with "!" is undefined instead. slang predefines
    # SYNTHESIS, and lowRISC's prim_assert dispatches VERILATOR -> SYNTHESIS ->
    # YOSYS in that order, so SYNTHESIS silently wins and every `ASSERT expands
    # to nothing. Undefining it is what makes a design's own assertions
    # reachable at all.
    define_flags = " ".join(
        f"-U{d[1:]}" if d.startswith("!") else f"-D{d}"
        for d in (defines or []))

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        
        # Prepare target file with targeted SVA injection
        content = rtl_override if rtl_override is not None else dut_path.read_text(errors="ignore")
        # A combinational module has no clock to sample on, no reset to
        # sequence past and no history for `$past`, so it gets no scaffolding
        # at all -- the properties are plain `always @(*) assert(...)`.
        # Note this is not a weaker check but a stronger one: with no
        # flip-flops every input combination is reachable in a single step, so
        # a depth-1 pass is exhaustive rather than bounded.
        rst_active_low = (
            rst_n.lower().endswith(("_n", "ni", "_b", "resetn"))
            or rst_n.upper() in ("RST_N", "RESETN", "RST_NI")
        )
        rst_first_cycle = "f_past_valid" if rst_active_low else "!f_past_valid"
        if not clk or clk.lower() in ("none", ""):
            preamble = "\n`ifdef FORMAL\n"
        else:
            preamble = (
                f"\n`ifdef FORMAL\n"
                f"reg f_past_valid = 1'b0;\n"
                f"always @(posedge {clk}) f_past_valid <= 1'b1;\n"
            )
            # Reset-free datapaths are common (SERV's ALU, most pipeline
            # stages). Constraining a port the module does not declare fails
            # elaboration.
            if rst_n and rst_n.lower() not in ("none", ""):
                preamble += (
                    f"always @(posedge {clk}) assume ({rst_n} == {rst_first_cycle});\n"
                )
        postamble = "\n`endif\n"
        sva_wrapped = preamble + sva_code + postamble
        
        if "// PROPERTIES_INJECTION_POINT" in content:
            injected = content.replace("// PROPERTIES_INJECTION_POINT", sva_wrapped)
        else:
            pattern = rf"(\bmodule\s+{re.escape(module_name)}\b[\s\S]*?)(\bendmodule\b)"
            m = re.search(pattern, content)
            if m:
                injected = content[:m.end(1)] + "\n" + sva_wrapped + "\n" + content[m.start(2):]
            else:
                idx = content.rfind("endmodule")
                if idx != -1:
                    injected = content[:idx] + "\n" + sva_wrapped + "\n" + content[idx:]
                else:
                    injected = content + "\n" + sva_wrapped
                
        target_src = td_path / dut_path.name
        target_src.write_text(injected)
        
        included_files: Dict[str, Path] = {dut_path.name: target_src}
        # Where each included file actually lives on disk. `included_files`
        # points into the scratch directory, which is destroyed on return, so
        # returning it hands callers paths that no longer exist.
        origin_files: Dict[str, Path] = {dut_path.name: dut_path}
        
        # Include only the packages the design actually references, closing
        # over their own imports. Adding every *_pkg.sv in the search tree
        # drags in whatever else happens to live there -- ibex ships a UVM
        # cosim agent package and a string-utility package, neither reachable
        # from the RTL, and both fail to elaborate. A package the design never
        # named cannot be needed to elaborate it.
        pkg_index: Dict[str, Path] = {}
        for sdir in search_dirs:
            for pkg in sorted(sdir.rglob("*_pkg.sv")):
                if not pkg.is_file():
                    continue
                for name in re.findall(r"\bpackage\s+(\w+)\s*;",
                                       _strip_comments(pkg.read_text(errors="ignore"))):
                    pkg_index.setdefault(name, pkg)

        def referenced_packages(text: str) -> set:
            body = _strip_comments(text)
            return (set(re.findall(r"\bimport\s+(\w+)\s*::", body))
                    | set(re.findall(r"\b(\w+)\s*::\s*\w", body)))

        defined_packages = set()
        pending = referenced_packages(content)
        while pending:
            name = pending.pop()
            if name in defined_packages or name not in pkg_index:
                continue
            pkg = pkg_index[name]
            defined_packages.add(name)
            if pkg.name in included_files:
                continue
            txt = pkg.read_text(errors="ignore")
            dest = td_path / pkg.name
            dest.write_text(txt)
            included_files[pkg.name] = dest
            origin_files[pkg.name] = pkg
            pending |= referenced_packages(txt) - defined_packages
                
        # Discover headers (.svh, .vh)
        for sdir in search_dirs:
            for h in sorted(list(sdir.rglob("*.svh")) + list(sdir.rglob("*.vh"))):
                if h.is_file():
                    dest = td_path / h.name
                    dest.write_text(h.read_text(errors="ignore"))
                
        # Index all modules in scoped directories
        module_to_file: Dict[str, Path] = {}
        for sdir in search_dirs:
            for f in list(sdir.rglob("*.sv")) + list(sdir.rglob("*.v")):
                if any(skip in f.name for skip in ["_tb", "tracer", "rvfi", "insn_trace"]):
                    continue
                txt = _strip_comments(f.read_text(errors="ignore"))
                for m in re.findall(r"\bmodule\s+(\w+)", txt):
                    if m not in module_to_file:
                        module_to_file[m] = f
                    
        env = os.environ.copy()
        env["PATH"] = f"{OSS_CAD_BIN}:{env.get('PATH', '')}"
        
        # Iteratively resolve missing submodule dependencies
        use_slang = True
        max_resolve_iters = 25
        yosys_res = None
        
        inc_args = " ".join([f"-I{d}" for d in search_dirs])
        for iter_i in range(max_resolve_iters):
            pkgs = [f.name for n, f in included_files.items() if "_pkg" in n]
            non_pkgs = [f.name for n, f in included_files.items() if "_pkg" not in n and f.name != target_src.name]
            ordered = pkgs + non_pkgs + [target_src.name]
            src_names = " ".join(ordered)
            
            if use_slang:
                yosys_cmd = f"read_slang --top {module_name} -DFORMAL {define_flags} -I. -I{td_path} {inc_args} -Wno-index-oob {src_names}; hierarchy -check -top {module_name}; proc; opt"
            else:
                yosys_cmd = f"read_verilog -formal -sv -DFORMAL {define_flags} -I{td_path} {inc_args} {src_names}; hierarchy -check -top {module_name}; proc; opt"
                
            yosys_res = subprocess.run(
                [f"{OSS_CAD_BIN}/yosys", "-p", yosys_cmd],
                cwd=td,
                capture_output=True,
                text=True,
                env=env
            )
            
            if yosys_res.returncode == 0:
                break
                
            out = yosys_res.stdout + yosys_res.stderr
            if use_slang and ("unrecognized option" in out or ("syntax error" in out and "unknown module" not in out)):
                if "unknown module" not in out:
                    use_slang = False
                    continue
                    
            missing = set(
                re.findall(r"unknown module ['\"]?(\w+)['\"]?", out) +
                re.findall(r"Module `?\\?(\w+)' referenced", out)
            )
            if not missing:
                break
                
            added = False
            for m_name in missing:
                cand = module_to_file.get(m_name)
                if not cand:
                    for sdir in search_dirs:
                        for search_cand in [sdir / f"{m_name}.sv", sdir / f"{m_name}.v", sdir / f"{m_name.lower()}.sv", sdir / f"{m_name.lower()}.v"]:
                            if search_cand.exists():
                                cand = search_cand
                                break
                        if cand:
                            break
                if cand and cand.name not in included_files and cand.is_file():
                    dest = td_path / cand.name
                    dest.write_text(cand.read_text(errors="ignore"))
                    included_files[cand.name] = dest
                    origin_files[cand.name] = cand
                    added = True
                    
            if not added:
                break
                
        if yosys_res.returncode != 0:
            err_lines = [l for l in yosys_res.stdout.splitlines() + yosys_res.stderr.splitlines() if "ERROR" in l or "syntax error" in l or "error:" in l]
            return (Verdict.ERROR, "; ".join(err_lines[:2]) or f"Yosys elaboration failed (rc={yosys_res.returncode})", None, list(origin_files.values()))
            
        # Run SBY model check
        pkgs = [f.name for n, f in included_files.items() if "_pkg" in n]
        non_pkgs = [f.name for n, f in included_files.items() if "_pkg" not in n and f.name != target_src.name]
        ordered = pkgs + non_pkgs + [target_src.name]
        src_names = " ".join(ordered)
        files_section = "\n".join(str(f) for f in included_files.values())
        
        frontend_cmd = f"read_slang --top {module_name} -DFORMAL {define_flags} -I. {inc_args} -Wno-index-oob {src_names}" if use_slang else f"read_verilog -formal -sv -DFORMAL {define_flags} -I. {src_names}"
        sby_cfg = f"""[options]
mode {mode}
depth {bmc_depth}

[engines]
smtbmc z3

[script]
{frontend_cmd}
prep -top {module_name}
clk2fflogic

[files]
{files_section}
"""
        sby_file = td_path / f"{module_name}.sby"
        sby_file.write_text(sby_cfg)
        
        sby_res = subprocess.run(
            [f"{OSS_CAD_BIN}/sby", "-f", str(sby_file.name)],
            cwd=td,
            capture_output=True,
            text=True,
            env=env
        )
        out = sby_res.stdout + sby_res.stderr
        if capture is not None:
            capture["sby_output"] = out
            capture["mode"] = mode
            # Where the caller's property block begins in the file the solver
            # saw. Failure messages carry absolute line numbers, so a caller
            # batching several properties into one check needs this offset to
            # map a reported line back to the property that occupies it.
            idx = injected.find(sva_code) if sva_code else -1
            capture["sva_first_line"] = (
                injected[:idx].count("\n") + 1 if idx >= 0 else None)
        
        if "DONE (PASS" in out:
            detail = ("No counterexample within the bound (BMC)" if mode == "bmc"
                      else f"No counterexample ({mode})")
            return (Verdict.PASS, detail, None, list(origin_files.values()))
        elif "DONE (FAIL" in out or "Assert failed" in out:
            trace_path = None
            for p in td_path.glob(f"{module_name}*/engine_0/trace.vcd"):
                if trace_dir is not None:
                    # The scratch directory is about to be removed, so the
                    # trace has to be copied out to outlive this call.
                    dest_dir = Path(trace_dir)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    stem = re.sub(r"\W+", "_", module_name)[:40]
                    dest = dest_dir / f"{stem}_{abs(hash(sva_code)) % 10**8}.vcd"
                    shutil.copyfile(p, dest)
                    trace_path = str(dest)
                else:
                    trace_path = str(p)
                break
            return (Verdict.FAIL, "Assertion violation / counterexample trace detected", trace_path, list(origin_files.values()))
        else:
            err_lines = [l for l in out.splitlines() if "ERROR" in l]
            return (Verdict.ERROR, "; ".join(err_lines[:2]) or f"SBY tool execution error (rc={sby_res.returncode})", None, list(origin_files.values()))


@ChiaFunction(resources={"formal": 1})
def prove_remote(
    dut_path: str,
    module_name: str,
    sva_code: str,
    clk: str = "clk",
    rst_n: str = "rst_n",
    bmc_depth: int = 10,
    mode: str = "bmc",
    rtl_override: Optional[str] = None,
) -> Tuple[str, str]:
    """Cluster-dispatchable wrapper around :func:`check_formal_proof`.

    Returns ``(verdict_value, detail)`` rather than the full tuple: the source
    file list and trace path are local paths on the worker and mean nothing to
    the caller, so shipping them back across the object store is waste.

    Args:
        dut_path: Path to the design, as a string so it pickles cheaply.

    Returns:
        ``(verdict, detail)`` where verdict is a :class:`Verdict` value.
    """
    verdict, detail, _, _ = check_formal_proof(
        dut_path=Path(dut_path), module_name=module_name, sva_code=sva_code,
        clk=clk, rst_n=rst_n, bmc_depth=bmc_depth, mode=mode,
        rtl_override=rtl_override,
    )
    return verdict.value, detail


EQUIV_SCRIPT = """
{reads}
proc; async2sync; opt; memory_map; opt -full
miter -equiv -flatten -make_assert {gold_top} {mut_top} miter
hierarchy -top miter
sat -seq {depth} -set-init-zero -verify -prove-asserts miter
"""


def check_equivalence(
    module_name: str,
    golden_rtl: str,
    mutant_rtl: str,
    dependencies: Optional[List[Path]] = None,
    equiv_depth: int = 8,
    timeout_seconds: int = 300,
    suffix: str = ".v",
) -> Tuple[str, str]:
    """Decide whether a mutant is behaviourally distinguishable from golden.

    Builds a miter of the two designs and asks the SAT engine for an input
    sequence separating them. Unlike
    :meth:`~chia.formal.mutation_node.MutationEngineNode.classify`, the
    submodules and packages the design depends on are included, so this works
    on RTL that is not a single self-contained file.

    That distinction matters more than it sounds: without the dependencies the
    miter fails to elaborate and returns ``UNKNOWN`` in milliseconds. Since
    ``UNKNOWN`` is deliberately kept in the scoring denominator, equivalence
    exclusion then silently does nothing, and every reported kill rate on a
    multi-file design is an under-estimate.

    Args:
        module_name: Top module, present in both sources.
        golden_rtl: Unmodified design source.
        mutant_rtl: Mutated design source.
        dependencies: Supporting files, as returned in the fourth element of
            :func:`check_formal_proof`. The entry for the design under test is
            skipped -- gold and mutant copies replace it.
        equiv_depth: Sequential depth for the proof. Equivalence established
            only to this bound is bounded equivalence, not absolute.
        suffix: File extension to write sources under; ``.sv`` for designs
            using SystemVerilog constructs.

    Returns:
        ``(equivalence, detail)`` using :class:`~chia.formal.state_def.Equivalence`
        values.
    """
    from .state_def import Equivalence

    env = dict(os.environ, PATH=f"{OSS_CAD_BIN}:{os.environ.get('PATH', '')}")
    gold_top, mut_top = f"{module_name}_gold", f"{module_name}_mut"

    def rename(text: str, new: str) -> str:
        return re.sub(rf"\bmodule\s+{re.escape(module_name)}\b",
                      f"module {new}", text, count=1)

    with tempfile.TemporaryDirectory(prefix="chia_equiv_") as td:
        wd = Path(td)
        names = []
        for dep in dependencies or []:
            dep = Path(dep)
            if not dep.exists():
                continue
            text = dep.read_text(errors="ignore")
            # The design under test appears in the dependency list; the gold
            # and mutant copies stand in for it, so including it too would
            # redefine the module.
            if re.search(rf"\bmodule\s+{re.escape(module_name)}\b", text):
                continue
            (wd / dep.name).write_text(text)
            names.append(dep.name)

        (wd / f"gold{suffix}").write_text(rename(golden_rtl, gold_top))
        (wd / f"mut{suffix}").write_text(rename(mutant_rtl, mut_top))
        names += [f"gold{suffix}", f"mut{suffix}"]

        # Packages need the slang front end; yosys' native reader cannot parse
        # them, which is why every miter on a SystemVerilog design used to
        # return UNKNOWN. Plain Verilog falls back to read_verilog.
        frontends = [
            f"read_slang -DFORMAL {' '.join(names)}",
            "\n".join(f"read_verilog -sv -DFORMAL {n}" for n in names),
        ]
        out = ""
        proc = None
        for reads in frontends:
            (wd / "run.ys").write_text(EQUIV_SCRIPT.format(
                reads=reads, gold_top=gold_top, mut_top=mut_top,
                depth=equiv_depth))
            try:
                proc = subprocess.run(["yosys", "-s", "run.ys"], cwd=wd,
                                      env=env, capture_output=True, text=True,
                                      timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                return (Equivalence.UNKNOWN.value,
                        f"yosys exceeded {timeout_seconds}s")
            out = proc.stdout + proc.stderr
            if ("Called with -verify and proof did fail" in out
                    or "SAT proof finished" in out):
                break
        if "Called with -verify and proof did fail" in out:
            return (Equivalence.DISTINGUISHABLE.value,
                    "separating input sequence exists")
        if "SAT proof finished - no model found: SUCCESS" in out:
            return (Equivalence.EQUIVALENT.value,
                    f"no separating sequence within {equiv_depth} cycles")
        errs = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        return (Equivalence.UNKNOWN.value,
                "; ".join(errs[:2])[:300] or f"unrecognized (rc={proc.returncode})")
