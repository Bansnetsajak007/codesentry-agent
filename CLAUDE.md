# CodeSentry — Instructions for Claude Code

## Current phase
We are in Phase 1. The full spec is in `docs/PHASE_1_SPEC.md`. Read it before doing any work.

## Working agreement
- Follow the build order in the spec strictly. Do not skip ahead.
- Ask before making design decisions with real trade-offs (see the Guardrails section).
- Do not add dependencies not listed in the spec without asking.
- Do not import `openai` outside `agent/llm.py`.
- After each build-order step: run tests, verify manually, commit with a clear message.
- Prefer boring, obvious code. Type hints everywhere. `mypy --strict` must pass on `codesentry/`.

## Commit style
- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- One build-order step = one focused commit (or a small series).

## When stuck
Stop and ask. Don't invent features or dependencies to unblock yourself.
