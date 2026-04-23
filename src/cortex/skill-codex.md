---
name: cortex
description: Repo-first Cortex workflow for token-budgeted context and orientation
trigger: /cortex
---

# /cortex

Use Cortex as the first-pass repo context layer before broad raw-file exploration.

Workflow:
1. If `.cortex/cortex_report.md` is missing or stale, run `cortex refresh .`
2. Read `.cortex/cortex_report.md`
3. If the report is insufficient, run `cortex bundle . --task "<question>" --budget 4000`
4. Answer from the bundle before opening many raw files
