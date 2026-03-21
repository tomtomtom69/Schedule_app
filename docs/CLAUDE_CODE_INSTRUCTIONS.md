# Claude Code Instructions

## How To Use This Documentation

This `/docs` folder contains the complete specification and implementation guides for the Geiranger Sjokolade Scheduling Application. Read them in order.

### Document Index

| File | Purpose | Read When |
|------|---------|-----------|
| `SPEC.md` | Full requirements specification | First — understand the domain |
| `STRUCTURE.md` | Project file/folder structure | Before writing any code |
| `PHASE1_FOUNDATION.md` | Docker, DB, models, CSV upload | Implementing Phase 1 |
| `PHASE2_DEMAND.md` | Demand forecasting engine | Implementing Phase 2 |
| `PHASE3_SOLVER.md` | Schedule constraint solver | Implementing Phase 3 |
| `PHASE4_UI.md` | Streamlit UI pages | Implementing Phase 4 |
| `PHASE5_LLM.md` | LLM advisory integration | Implementing Phase 5 |
| `PHASE6_EXPORT.md` | Excel/PDF export, polish | Implementing Phase 6 |

## Coding Instructions

### Workflow
1. Read `SPEC.md` and `STRUCTURE.md` first
2. Implement one phase at a time, in order (Phase 1 → 2 → 3 → 4 → 5 → 6)
3. Read the relevant `PHASEn_*.md` before starting each phase
4. **Write all code files without requesting approval for each step**
5. **Only ask for approval before running system commands** (docker build, docker-compose up, pip install, npm install, etc.)
6. After completing each phase, verify the acceptance criteria listed at the bottom of that phase's doc

### Critical Rules

1. **LLM calls:** ALL OpenAI API calls go through `src/llm_client.py`. No other file imports `openai`. The model is read from `LLM_MODEL` in `.env` via `src/config.py`.

2. **Docker isolation:**
   - Streamlit port: **8510** (host) → 8501 (container)
   - Postgres volume: **scheduler_postgres_data**
   - Container names: prefix with **geiranger-scheduler-**
   - Do NOT use port 8501 on host
   - Do NOT use volume name `postgres_data`

3. **No hardcoded model names.** The string `gpt-4o-mini` or `gpt-4o` should appear ONLY in `.env` and `.env.example`. Nowhere else in the codebase.

4. **Season range:** May 1 – October 15 only. No winter. No November/December.

5. **Pydantic + SQLAlchemy dual models:** Each entity has both. They live in the same file under `src/models/`.

6. **Solver independence:** `src/solver/` has zero LLM dependency. It takes typed objects in, returns typed objects out.

7. **Test with sample data:** Create small test fixtures (3-5 employees, 5-10 ship days) to verify each phase before moving on.

### Environment Setup

Before coding, ensure the project root has:
```
.env          (copied from .env.example, with real values)
.env.example  (committed to repo)
```

### Running the App

```bash
docker-compose up --build
```

Access at: `http://localhost:8510`
