# Paper claims → sources

Every number in the paper, and where it comes from.
`T` = output of `python scripts/make_paper_tables.py --out results/refinement`.

## Loop results (`results/refinement/*/reverified.json`, `refinement_eval.json`)

| Claim | Value | Source |
|---|---|---|
| Modules evaluated | 22 | `T`: `\nmodules` |
| Runs (seeds) | 41; 1–2 per module; 3 of 44 requested did not complete | `T`: `\nruns`; `seeds_requested` vs `len(runs)` (DecodeUnit, cv32e40p_alu, cv32e40p_alu_div) |
| Properties proposed | 1849 | `T`: `\nproposed` |
| Refuted by the correct design | 673 (36%) | `T`: disposition table, `\punsound` |
| Duplicates | 416 (22%) | `T`: disposition table |
| Admitted | 760 (41%) | `T`: disposition table |
| Syntax rejections | 0 | `T`: disposition table |
| Per-module min/mean/max, Act. column (Table II) | — | `T`: per-module rows (`full_bank.active`, `full_bank_min/mean/max`) |
| Overall full-bank mean / range | 54.5%, 0.3–100% (over 32 seeds of the 18 scored modules) | `T`: prose figures |
| Runs with no scoreable suite | 9 / 41 | `T`: "seeds yielding no scoreable suite" |
| Combinational (depth-1) modules | 9 (8 scoreable) | `T`: `\ncomb`; `depth == 1` in reverified.json |
| Sampled bank size | 24 | `sampled.budget` in every seed |
| DecodeUnit / FPUDecoder banks | 2485 / 1150 generated, 2452 / 1140 active | `full_bank.generated/active` |
| ALUExeUnit bank | 3 active | `full_bank.active` |
| Mean / max sampled-vs-full gap | 19.4 / 70.0 points (32 scored seeds) | `T`: gap lines (`overfit_gap_points`) |
| cv32e40p_alu fixed: 81.0% sampled, 11.0% full (365 active) | — | `cv32e40p_alu/reverified.json` |
| cv32e40p_mult fixed: 82.4% sampled, 17.5% full | — | `cv32e40p_mult/reverified.json` (seed 0) |

| Sampled kill rate, first iteration → final | mean 39.1% → 73.9%, median 33.3% → 87.3% (32 runs) | `T`: "sampled kill rate, first iteration -> final" (`initial_kill_rate`, `final_kill_rate`) |
| Iterations per run | mean 2.6, max 4 | `T` |
| Wall time per run | median 1.2 min, max 22.1 min, total 2.5 h (41 runs) | `T` (`wall_seconds`) |
| Seeds below the diagonal in Fig. 2 | 25 / 32 | `T`: "seeds with full-bank rate below sampled rate" |
| Fig. 2 scatter points | 32 (sampled, full) pairs | `T`: "scatter" line |
| IBuf / serv_ctrl / serv_bufreg first suite ≤ 30%, refined 72.7–100% | — | `iterations[0].kill_rate`, `final_kill_rate` in each `refinement_eval.json` |

## Rotating sample (`results/resample_ablation/*/reverified.json`)

| Claim | Value |
|---|---|
| cv32e40p_alu rotating | 76.2% sampled, 14.8% full (gap 61.4 → narrows 8.6 ≈ 9) |
| cv32e40p_mult rotating | 58.8% sampled, 15.1% full (gap 43.7 → narrows 21.2) |
| Full-bank change | +3.8 (alu), −2.4 (mult) points |

## Human baseline (`results/human_baseline/human_vs_loop.json`)

| Claim | Value |
|---|---|
| Gradeable assertions | 14 over 5 modules (sum of `inventory.gradeable`) |
| Active mutants | 116 (24+24+20+24+24) |
| Human kill rate | 9/116 = 7.8% |
| Loop kill rate | 36/116 = 31.0%, on the same 24-mutant sample the loop was tuned on (a tuning-sample score) |
| branch_predict: loop ⊇ human | `human_only == []`, `both == 8` |
| fetch_fifo: loop has no property; human kills one | `loop_properties == 0`, `human_only == ["m084_EQ_TO_NEQ_L223_0"]` |
| ibex inventory: 185 total, 72 X-checks (39%), 52 `\|->` (28%), 61 plain (33%) | whole-tree inventory from `examples/formal_verification/human_vs_loop.py`; **not archived as JSON** — re-run to regenerate |

## Mode B (`results/bug_hunt/cwe_1240/fuse_mem/bug_hunt.json`)

| Claim | Value |
|---|---|
| Hardened suite | 8 properties, 45% kill rate on the fixed version |
| Applied / clean / findings / inconclusive | 8 / 8 / 0 / 0 |

## Qualitative or development-run claims (no archived record)

These come from development runs and have no committed JSON behind them.

- An unsound liveness property lifts an arbiter suite to 100%. This follows by
  construction: a property that is false on the golden design is refuted by
  every mutant.
- 8-cycle vs 6-cycle fairness windows (vacuity at a 16-cycle bound).
- BoomCore elaboration > 1 h vs Rocket top < 2 min.
