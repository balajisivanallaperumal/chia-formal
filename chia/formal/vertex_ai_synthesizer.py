#!/usr/bin/env python3
"""
CHIA Agentic Formal Verification Synthesizer & Auto-Repair Engine.
Powered exclusively by Google Cloud Vertex AI (Gemini 2.5 Flash / Pro).
"""

import os
import re
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

log = logging.getLogger("CHIA-VertexAI")


@dataclass
class SynthesizedSVAContract:
    contract_name: str
    target_domain: str
    sva_code: str
    description: str
    assumptions: List[str] = None
    guarantees: List[str] = None

    def __post_init__(self):
        if self.assumptions is None:
            self.assumptions = []
        if self.guarantees is None:
            self.guarantees = []


class VertexAISynthesizer:
    """
    Pure Agentic SystemVerilog Assertion (SVA) Synthesizer and Silicon Auto-Repair
    powered exclusively by Google Cloud Vertex AI (Gemini 2.5 Flash / Pro).
    """

    @classmethod
    def inspect_signals(cls, rtl_text: str) -> Dict[str, List[str]]:
        """
        Classify hardware ports and internal registers into microarchitectural categories.
        """
        classified = {
            "clock": [],
            "reset": [],
            "ready": [],
            "valid": [],
            "req": [],
            "grant": [],
            "enable": [],
            "data": [],
            "state": [],
            "other": []
        }

        port_pattern = re.compile(r"^\s*(?:input|output|inout|wire|reg)\s+(?:\[[^\]]+\]\s+)?(\w+)", re.M)
        for match in port_pattern.finditer(rtl_text):
            raw_sig = match.group(1)
            sig = raw_sig.lower()
            if "clk" in sig or "clock" in sig:
                classified["clock"].append(raw_sig)
            elif "rst" in sig or "reset" in sig:
                classified["reset"].append(raw_sig)
            elif "ready" in sig or "rdy" in sig:
                classified["ready"].append(raw_sig)
            elif "valid" in sig or "vld" in sig:
                classified["valid"].append(raw_sig)
            elif "req" in sig:
                classified["req"].append(raw_sig)
            elif "grant" in sig or "gnt" in sig:
                classified["grant"].append(raw_sig)
            elif "en" in sig or "enable" in sig:
                classified["enable"].append(raw_sig)
            elif "data" in sig or "payload" in sig or "addr" in sig or "result" in sig:
                classified["data"].append(raw_sig)
            elif "state" in sig or "fsm" in sig or "status" in sig or "mode" in sig:
                classified["state"].append(raw_sig)
            else:
                classified["other"].append(raw_sig)

        return classified

    @classmethod
    def synthesize_vertex_ai_invariants(
        cls,
        rtl_text: str,
        top_module: str,
        clk: str = "clk",
        rst_n: str = "rst_n"
    ) -> List[SynthesizedSVAContract]:
        """
        Synthesizes mathematically sound SystemVerilog Assertions via Google Cloud Vertex AI.
        """
        # Credentials come from the environment only. Earlier revisions probed
        # a list of absolute paths to a service-account key checked into the
        # working tree; that made the key easy to leak and tied the code to one
        # machine. Use `gcloud auth application-default login`, or point
        # GOOGLE_APPLICATION_CREDENTIALS at a key kept outside the repo.
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds and not Path(creds).exists():
            log.warning("GOOGLE_APPLICATION_CREDENTIALS points at a missing file: %s", creds)
        if not creds:
            adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
            if adc.exists():
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc)

        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            log.warning("GOOGLE_CLOUD_PROJECT is unset; Vertex synthesis will fail")
        
        try:
            import time
            import random
            time.sleep(random.uniform(0.1, 0.5))
            
            from google import genai
            from google.genai import types
            client = genai.Client(
                vertexai=True, 
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
            )
            
            # Auto-detect clock, reset and -- critically -- reset POLARITY.
            # The guard has to be `!rst` for an active-high reset; emitting
            # `rst` there produces assertions whose trigger is never true, so
            # they pass vacuously on golden RTL and on every mutant alike.
            from .empirical_metrics import detect_clock_reset, rtl_for_prompt
            detected = detect_clock_reset(rtl_text)
            clk_name = detected.clk or clk
            rst_name = detected.rst or rst_n
            guard = detected.guard
            combinational = detected.combinational

            # Yosys' native SystemVerilog front end accepts *immediate*
            # assertions inside clocked always blocks. It does not accept
            # `property`/`endproperty`, `assert property`, implication
            # operators or sequence delays without Verific, so the prompt
            # constrains the model to the subset the solver can actually read.
            if combinational:
                # No clock, no reset, no state: the module is a function of
                # its inputs. `$past` has nothing to read and would not
                # elaborate, and a depth-1 check is exhaustive, so the useful
                # properties are algebraic relations between inputs and
                # outputs rather than temporal ones.
                shape = f"""Emit ONLY combinational assertions, in exactly this shape:

    always @(*) assert (<expression>);

This module is PURELY COMBINATIONAL -- it has no clock, no reset and no state.
- FORBIDDEN additionally: `$past`, `posedge`, `negedge`, `f_past_valid`,
  `f_cycles`, and any reference to a clock or reset signal. None exist here.
- Write algebraic relations between inputs and outputs: case-coverage of an
  opcode, result bounds, one-hot or mutual-exclusion of decoded control
  signals, sign/zero-extension correctness, flag consistency (a zero flag
  agreeing with a zero result), and identities that must hold for every input.
- Guard operation-specific claims with the relevant opcode or select signal,
  e.g. `always @(*) assert (op != OP_ADD || result == a + b);` written as an
  implication via `||`."""
            else:
                shape = f"""Emit ONLY immediate assertions inside clocked always blocks, in exactly this shape:

    always @(posedge {clk_name}) if ({guard}) assert (<expression>);

- Use exactly the guard `{guard}` shown above -- it already accounts for this
  design's reset polarity.
- Allowed: `$past(sig)` and `$past(sig, n)`, `$signed`, bit- and part-selects.
- `$past(sig, n)` with n > 1 needs n cycles of history. If you use it, replace
  `f_past_valid` in the guard with `f_cycles > 8'd<n>`, and emit this counter
  exactly once, before the assertions:
      reg [7:0] f_cycles = 8'd0;
      always @(posedge {clk_name}) if (f_cycles != 8'hff) f_cycles <= f_cycles + 8'd1;
- Prefer invariants that constrain STATE and PROGRESS, not just output shape:
  mutual exclusion, one-hot encodings written longhand
  (`x == 0 || (x & (x - 1)) == 0`), pointer and counter bounds, handshake
  stability, FSM reachable-state sets, and "a pending request eventually
  produces a response" within a fixed bound."""

            prompt = f"""Read the Verilog RTL for module `{top_module}` and synthesize formal invariants.

{shape}

HARD CONSTRAINTS -- output that violates these is discarded:
- One `always` statement per assertion. No `begin`/`end` grouping.
- FORBIDDEN: `property`, `endproperty`, `assert property`, `cover property`,
  `sequence`, `|->`, `|=>`, `##`, `$rose`, `$fell`, `$stable`, `$onehot`,
  `$onehot0`, `disable iff`, and named assertion labels (`name: assert`).
- Reference only signals declared in the RTL below. Do not invent names.
- Do not restate a combinational assignment verbatim -- an assertion that
  simply repeats an `assign` is a tautology and scores zero.

Output a single ```systemverilog code block and nothing else.

RTL Netlist:
```verilog
{rtl_for_prompt(rtl_text)}
```"""
            model_name = os.environ.get("CHIA_VERTEX_MODEL") or os.environ.get("VERTEX_MODEL") or "gemini-2.5-flash"
            
            gen_kwargs = {
                "temperature": 0.2,
                "max_output_tokens": 4096,
            }
            if "flash" in model_name:
                gen_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            elif "2.5-pro" in model_name:
                gen_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=1024)
                
            for attempt in range(4):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(**gen_kwargs)
                    )
                    text = response.text or ""
                    code_blocks = re.findall(r"```(?:systemverilog|verilog)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
                    if not code_blocks:
                        # Unterminated fence (response hit the token cap):
                        # take the body after the opening fence rather than
                        # letting markdown reach the solver as if it were RTL.
                        opened = re.search(r"```(?:systemverilog|verilog)?\s*(.*)", text,
                                           re.DOTALL | re.IGNORECASE)
                        code_blocks = [opened.group(1)] if opened else []
                    sva_code = "\n".join(code_blocks) if code_blocks else text
                    
                    if sva_code.strip():
                        return [SynthesizedSVAContract(
                            contract_name=f"{top_module.upper()}_VERTEX_AI_GEMINI_INVARIANTS",
                            target_domain=f"Vertex AI ({model_name}) Reasoning",
                            sva_code=sva_code.strip(),
                            description=f"AI-synthesized formal invariant contracts via Google Vertex AI ({model_name})",
                        )]
                    break
                except Exception as api_err:
                    if "429" in str(api_err) or "RESOURCE_EXHAUSTED" in str(api_err) or "Resource exhausted" in str(api_err):
                        sleep_time = (2 ** attempt) + random.uniform(0.5, 1.5)
                        log.warning(f"Vertex AI rate limited (429/ResourceExhausted) on {top_module}, retrying in {sleep_time:.2f}s (attempt {attempt+1}/4)...")
                        time.sleep(sleep_time)
                    else:
                        raise api_err
        except Exception as e:
            log.warning(f"Vertex AI SVA synthesis error on {top_module}: {e}")
            
        return []

    @classmethod
    def synthesize_all_contracts(cls, rtl_text: str, top_module: str, use_vertex_ai: bool = True) -> List[SynthesizedSVAContract]:
        """
        Agentic SVA Contract Synthesis exclusively powered by Google Cloud Vertex AI (Gemini).
        """
        contracts = cls.synthesize_vertex_ai_invariants(rtl_text, top_module)
        return contracts

    @staticmethod
    def infer_optimal_bmc_depth(rtl_text: str, top_module: str) -> int:
        """
        Infer optimal Bounded Model Checking (BMC) depth horizon based on RTL microarchitecture.
        """
        name_lower = top_module.lower()
        
        # 1. Macro core and hierarchical top-level integrations (covers interface horizon)
        if any(k in name_lower for k in ["mkcore", "mkp1_core", "mkcpu", "cva6", "cv32e40p_top"]):
            return 2
        elif "core" in name_lower or "top" in name_lower or "soc" in name_lower:
            return 3

        # 2. Multi-cycle iterative arithmetic / divider FSMs
        if "div" in name_lower or "multicycle" in name_lower:
            return 10

        # 3. FIFOs, queues, and prefetch buffers
        if "fifo" in name_lower or "prefetch" in name_lower or "aligner" in name_lower:
            depth_m = re.search(r"parameter\s+(?:int\s+)?(?:DEPTH|BUFFER_SIZE|FIFO_DEPTH)\s*=\s*(\d+)", rtl_text, re.I)
            if depth_m:
                return max(int(depth_m.group(1)) + 2, 8)
            return 10

        # 4. Instruction decode subsystem
        if "decoder" in name_lower:
            return 3

        # 5. Pure combinational execution units / bit manipulation
        has_seq = bool(re.search(r"always(?:_ff)?\s*@\s*\(\s*posedge", rtl_text, re.I) or re.search(r"always_latch", rtl_text, re.I))
        if not has_seq or any(k in name_lower for k in ["popcnt", "ff_one", "alu_comb", "mux"]):
            return 2

        if name_lower.endswith("_alu") or name_lower == "alu":
            if not re.search(r"always(?:_ff)?\s*@\s*\(\s*posedge", rtl_text, re.I):
                return 2
            return 3

        # 6. Pipeline stages, FSMs, CSRs, LSU, Bus interfaces, interrupt controller
        if any(k in name_lower for k in ["controller", "load_store", "cs_registers", "int_controller", "sleep", "obi", "pmp", "register_file", "stage", "hwloop"]):
            return 5

        # 7. Fallback default
        return 5

    @staticmethod
    def infer_optimal_job_count(requested_jobs: Optional[int], total_tasks: int) -> int:
        """
        Dynamically calculate optimal Ray/Process worker pool size.
        """
        if requested_jobs is not None and requested_jobs > 0:
            return requested_jobs

        cpu_count = os.cpu_count() or 4
        if total_tasks <= 1:
            return 1
        elif total_tasks <= 4:
            return min(total_tasks, cpu_count)
        else:
            return min(cpu_count, max(2, min(total_tasks, 16)))


# Backward Compatibility Alias
UniversalPropertySynthesizer = VertexAISynthesizer
