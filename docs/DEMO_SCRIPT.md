# 2-Minute Demo Video Walkthrough Script

> **Goal**: Demonstrate working Phase 1 AI lead qualification, zero-guessing extraction, configurable YAML scoring, CRM deduplication, and human-in-the-loop email review in under 2 minutes.

---

## Pre-Recording Setup (15 seconds before hitting Record)
```bash
# 1. Populate the database with the 20 evaluated historical enquiries
.venv/bin/python scripts/evaluate_20_enquiries.py --provider deterministic_local --export-docs

# 2. Start the FastAPI server
.venv/bin/uvicorn app.main:app --reload --port 8000
```
Open two browser tabs:
1. **Operator Dashboard**: `http://127.0.0.1:8000/dashboard`
2. **Evaluation Report**: `docs/EVALUATION_REPORT.md` (or terminal output)

---

## Scene-by-Scene Script (2:00 Total)

### 0:00 – 0:25 | Architecture & Zero-Guessing Contract
- Show [`specs/001-ai-lead-qualification.md`](../specs/001-ai-lead-qualification.md) and [`config/scoring_rules.yaml`](../config/scoring_rules.yaml).
- **Talking point**: *"We built this using Spec-Driven Development. Every incoming web form or email is persisted to the database first so zero leads can ever be lost. The AI only extracts structured facts with verbatim evidence quotes—it is never allowed to guess missing budgets or timelines, and lead scoring (`Hot`/`Warm`/`Medium`/`Cold`) is 100% deterministic in `scoring_rules.yaml`."*

### 0:25 – 1:00 | 20 Real Past Enquiries Benchmark
- Show the terminal table or [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md).
- Highlight three contrasting cases:
  1. **`ENQ-01` (`Hot`, Score 100)**: Full contact info, Central London commercial lease, £110k budget, immediate start $\rightarrow$ synced to CRM.
  2. **`ENQ-08` (`Warm`, Score 70)**: High-intent buyer in Bristol, **no budget stated** $\rightarrow$ `budget` is strictly `null` (not guessed!), and the AI draft email politely asks for their budget range.
  3. **`ENQ-17` & `ENQ-20` (`Needs Review`)**: Vague 5-word enquiry and a prompt-injection attack are automatically flagged `NEEDS REVIEW` with explicit governance reasons.

### 1:00 – 1:40 | Interactive Operator Dashboard & Live Submission
- Switch to `http://127.0.0.1:8000/dashboard`.
- Click the **Hot**, **Warm**, and **Needs Review** filter pills at the top.
- Expand **"+ Simulate Live Inbound Enquiry"**, submit a test enquiry, and show it appear immediately at the top of the table with its CRM Record ID.
- Click **"Review & Approve Draft Email"** on a lead, tweak a word in the subject line, and click **"Approve & Send Email"** to show human-in-the-loop sign-off (`APPROVED_AND_QUEUED`).

### 1:40 – 2:00 | Reliability, Retries & Automated Test Suite
- Run `.venv/bin/pytest` in the terminal showing all unit, retry-backoff, deduplication, and security tests passing in 2 seconds.

