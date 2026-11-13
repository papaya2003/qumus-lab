# Repository instructions

This is `qumus-lab`, the software for the Qumus lab PC (real instruments). Read [AGENTS.md](AGENTS.md)
first: its safety rules (a person at the bench for anything that moves, switches or heats; no weakening of
the safety layer without explicit OK; `source: measured` only for bench-measured values) always apply.
Current documentation starts at [README.md](README.md) and [docs/architecture.md](docs/architecture.md).

- Status 2026-11-12: real mode refuses motion until the L3 bench measurements are filled in
  (`docs/l3-measurements.csv`); the tape gantry does not move yet (missing `M17` on connect suspected).
- The L6 scripted Hall bar (`qumus_lab/hallbar_script.py`) passed against `--demo`; against qumus-simlab on
  the Mac (11-11) it exfoliated and searched, then `stack_device` failed with an `RGBRecognitionError`.
- This repository is public for now (owner's decision, 2026-11-10) even where older text says private.

## Qumus project map (shared by all Qumus repositories, 2026-11-12)

Owner: Jialei Chen (GitHub `papaya2003`), Sanfeng Wu lab, Princeton. Talk to Jialei in Chinese;
code, comments, commits and docs in English. Jialei has no software background: explain simply.

| Repository | Role | main (2026-11-12) |
| --- | --- | --- |
| `qumus-agent` | The brain: AI agents (OpenAI Agents SDK), web UI on 8765, History, budget, run store, contract client. Never contains laboratory code. | 1792a5e |
| `qumus-simlab` | Simulated Hall-bar lab as its own program: exfoliation 8781, stacking 8782, vdW reactor 8783. Never imports qumus-agent. | 80dfe3f |
| `qumus-lab` | Windows lab-PC software: drivers, SafetyGate, module services 8781/8782, debug page 8780 (never tunnel it). | 5f76aa1 |
| `lab-contract` | Contract 2 spec, schemas, `conformance`, `labkit` (stations) and `labclient` (the brain's only client). Private. | 917e41c |
| `qumus-v2` | Frozen phase-1 demo; main 0cc7826 must not change. | 0cc7826 |

- The brain talks to every station only over HTTP (contract 2; older services still speak v1 and are
  auto-detected). Same computer or another computer differs only by URL; no shared files or databases.
  `stations.yaml` in qumus-agent picks sim or real per module.
- Standing permission: code, commit, push branches, open PRs, merge to main, install deps, run tests in these
  repositories (not qumus-v2 main). Ask Jialei first before: driving real hardware; pushing anywhere else or
  changing repository visibility; deleting files outside the repositories and project notes; changing an
  already decided design decision; any paid model call (state the cap).
- Never commit keys, `.env`, lab data or photos, GDS files, target bundles, run databases or model weights.
- GitHub Actions has no minutes: run `ruff` and `pytest` locally and say so in the PR.
- Contract changes need Jialei's approval (2.x is additive only). Simulated operations must match Qumus v1
  exactly; when the sim cannot support a v1 operation, change the sim, not the flow.
- Full context, history, decisions and next steps live in the project notes
  (`notes/handoff-20261112/HANDOFF.md` and `CLAUDE.md` there), not in this repository.
