<div align="center">

# CandidateOS

### AI Resume Shortlisting & Interview Assistant
</div>

---

## Project Overview

CandidateOS is a production-grade AI system that automates the entire candidate evaluation workflow — from raw file uploads to a ranked shortlist and personalised interview plan.

A recruiter uploads a resume (PDF, DOCX, or TXT) and a job description. The system:

1. **Extracts** text and hyperlinks from any file format
2. **Parses** both documents into structured JSON using GPT-4o-mini
3. **Scores** the candidate across 4 independent dimensions using GPT-4o in parallel
4. **Verifies** every public profile found in the resume — GitHub, LinkedIn, LeetCode, Kaggle
5. **Generates** a tailored interview plan with candidate-specific questions, follow-ups, and an interviewer rubric

Everything streams live to the UI as it completes — no waiting for the full pipeline to finish.

---

## Live Demo

**URL:** https://ai-resume-assistant.onrender.com

> First load may take 30–60 seconds on the free tier (cold start wake-up)

**To test immediately:**
1. Open the URL
2. Click **"Load Demo Data"** — fills in a sample resume and JD automatically
3. Click **"Run Evaluation"**
4. Watch the live progress and results appear step by step

---

## Assignment Coverage

| Requirement | Status | Implementation |
|-------------|--------|---------------|
| Part 1 — System Design | ✅ | See `SYSTEM_DESIGN.md` |
| Option A — Scoring Engine | ✅ | `backend/modules/scoring_engine.py` |
| Option B — Verification Engine | ✅ | `backend/modules/verification_engine.py` |
| Option C — Question Generator | ✅ | `backend/modules/question_generator.py` |
| Explainability (why is score low?) | ✅ | Every score has `explanation` + `evidence[]` |
| Semantic Similarity (Kafka ≈ Kinesis) | ✅ | LLM with technology equivalence graph |
| Tier Classification (A / B / C) | ✅ | Composite score thresholds |
| UI | ✅ | Single-page app served by FastAPI |
| Batch evaluation | ✅ | `POST /api/batch` — rank N candidates vs 1 JD |
| SSE streaming | ✅ | `POST /api/evaluate/stream` — live progress |

---

## Project Structure

```
AI_resume_shortlisting_and_interview_assistant_system/
│
├── README.md
├── SYSTEM_DESIGN.md              ← Full architecture + diagrams
├── docker-compose.yml
├── .env.example
│
├── docs/
│   └── diagrams/                 ← 5 architecture diagrams (PNG)
│
├── backend/
│   ├── main.py                   ← FastAPI app + all routes + frontend serving
│   ├── models.py                 ← All Pydantic data models (single source of truth)
│   ├── config.py                 ← Settings loaded from .env
│   ├── cli.py                    ← Run pipeline from terminal (no server needed)
│   ├── Dockerfile
│   ├── requirements.txt
│   │
│   └── modules/
│       ├── file_extractor.py     ← PDF / DOCX / TXT → plain text + hyperlinks
│       ├── llm_client.py         ← OpenAI wrapper with retry + sanitization
│       ├── parser.py             ← Text → structured ParsedResume / JobDescription
│       ├── scoring_engine.py     ← 4-dimensional parallel scoring
│       ├── verification_engine.py← GitHub API, LeetCode GraphQL, LinkedIn, Kaggle
│       ├── question_generator.py ← Tier-aware personalised interview plan
│       ├── batch_evaluator.py    ← Rank N candidates vs 1 JD
│       └── streamer.py           ← SSE real-time progress events
│
├── frontend/
│   └── index.html                ← Full UI (no build step — served by FastAPI)
│
└── samples/
    ├── sample_resume.txt         ← Test resume
    └── sample_jd.txt             ← Test job description
```

---

## How to Run

### Prerequisites

- Python 3.11 or higher
- An OpenAI API key — get one at https://platform.openai.com/api-keys

---

### Option 1 — Run Locally (recommended for development)

**Step 1 — Clone the repository**

```bash
git clone https://github.com/rithwik-2005/AI_resume_shortlisting_and_interview_assistant_system.git
cd AI_resume_shortlisting_and_interview_assistant_system
```

**Step 2 — Create a virtual environment**

```bash
# Windows
cd backend
python -m venv venv
venv\Scripts\activate

# Mac / Linux
cd backend
python3.11 -m venv venv
source venv/bin/activate
```

**Step 3 — Install dependencies**

```bash
pip install -r requirements.txt
```

**Step 4 — Create the `.env` file**

Create a file called `.env` inside the `backend/` folder:

```
OPENAI_API_KEY=sk-your-key-here
OPENAI_MODEL=gpt-4o
OPENAI_EXTRACT_MODEL=gpt-4o-mini
```

> On Windows with Notepad: File → Save As → filename: `.env` → Save as type: **All Files**

**Step 5 — Start the server**

```bash
uvicorn main:app --reload --port 8000
```

**Step 6 — Open the app**

Go to: **http://localhost:8000**

---

### Option 2 — Run with Docker

**Step 1 — Make sure Docker Desktop is running**

**Step 2 — Create `backend/.env`** (same as Step 4 above)

**Step 3 — Build and start**

```bash
docker-compose up --build -d
```

**Step 4 — Open the app**

Go to: **http://localhost:8000**

```bash
# View live logs
docker-compose logs -f

# Stop
docker-compose down
```

---

### Option 3 — CLI (no server, terminal only)

```bash
cd backend
source venv/bin/activate   # or venv\Scripts\activate on Windows

# Evaluate a single candidate
python cli.py evaluate --resume path/to/resume.pdf --jd path/to/jd.docx

# Batch rank multiple candidates
python cli.py batch --jd path/to/jd.txt resume1.pdf resume2.pdf resume3.docx

# Parse files only (see extracted JSON)
python cli.py parse --resume path/to/resume.pdf --jd path/to/jd.txt

# Skip verification or questions
python cli.py evaluate --resume resume.pdf --jd jd.txt --no-verify --no-questions
```

---

## API Keys

| Key | Required | Where to get it |
|-----|----------|----------------|
| `OPENAI_API_KEY` | **Yes** | https://platform.openai.com/api-keys |
| `GITHUB_TOKEN` | No | https://github.com/settings/tokens (raises API rate limit from 60 → 5000/hr) |

The system works fully without a GitHub token. The only difference is the GitHub verification is limited to 60 API requests per hour instead of 5,000.

---

## API Reference

All endpoints accept `multipart/form-data` for file uploads.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Serves the frontend UI |
| `GET` | `/api/health` | Health check → `{"status":"ok"}` |
| `POST` | `/api/evaluate` | Full pipeline — returns complete JSON result |
| `POST` | `/api/evaluate/stream` | Full pipeline — Server-Sent Events streaming |
| `POST` | `/api/batch` | Rank N resumes vs 1 JD — returns leaderboard |
| `POST` | `/api/parse` | Extract + parse files only |
| `POST` | `/api/score` | Score pre-parsed data (JSON body) |
| `POST` | `/api/verify` | Verify social profiles (JSON body) |
| `POST` | `/api/questions` | Generate interview plan (JSON body) |

**Interactive API docs:** http://localhost:8000/docs

**Example curl:**
```bash
curl -X POST http://localhost:8000/api/evaluate \
  -F "resume_file=@samples/sample_resume.txt" \
  -F "jd_file=@samples/sample_jd.txt"
```

---

## Design Approach

### Why FastAPI?

FastAPI gives automatic OpenAPI documentation, native async support for SSE streaming, and tight Pydantic integration — all of which this system relies on heavily. The alternative was Flask, but Flask requires significantly more boilerplate for streaming responses and has no native async file upload handling.

### Why one container instead of nginx + backend?

The initial design had two containers (FastAPI backend + nginx frontend). Docker Hub was unreachable on the development network, so nginx could not be pulled. The solution was to serve the frontend HTML directly from FastAPI using `FileResponse` — eliminating the nginx dependency entirely and simplifying the deployment to a single container with a single port.

### How scoring works

Each of the four scoring dimensions is a separate GPT-4o call with a specialised system prompt. All four fire **simultaneously** using Python's `ThreadPoolExecutor`:

```
Exact Match (25%)       — literal keyword overlap between resume skills and JD requirements
Semantic Similarity (35%)— conceptual fit using a technology equivalence graph
Achievement Impact (25%) — presence of quantified metrics ("reduced latency by 40%")
Ownership (15%)          — signals of owning systems ("Led", "Architected", "Built from scratch")
```

The **semantic similarity dimension** gets the highest weight (35%) because it is the hardest signal to fake and the most predictive of actual job performance. A candidate who solved the same class of problems with different tools is genuinely qualified, even without exact keyword matches.

The composite score is a weighted sum. Tier thresholds:
- Tier A (≥ 75) → Fast-track
- Tier B (≥ 50) → Technical screen
- Tier C (< 50) → Needs evaluation

### Why LLM for scoring instead of embeddings?

Three approaches were evaluated:

| Approach | Kafka ≈ Kinesis? | Explainable? | Handles new tech? |
|----------|----------------|--------------|------------------|
| Cosine similarity (embeddings) | Sometimes | No | No |
| Fine-tuned classifier | Sometimes | No | No |
| **LLM reasoning (chosen)** | **Always** | **Yes** | **Yes** |

LLM reasoning produces explainable scores with cited evidence from the resume. A recruiter can read *why* a candidate scored 78 instead of just seeing the number. This explainability requirement ruled out embedding-based approaches.

### How semantic similarity detects Kafka ≈ Kinesis

The scoring prompt includes an explicit technology equivalence graph as few-shot examples:

```
Message queues / streaming:
  Apache Kafka ↔ AWS Kinesis ↔ RabbitMQ ↔ AWS SQS ↔ Pulsar

Container orchestration:
  Kubernetes ↔ AWS ECS ↔ AWS Fargate ↔ Nomad

(and 6 more clusters...)
```

The model is asked to score conceptual fit, not keyword presence. Result: a developer with Kinesis + SQS experience scores ~90% on a Kafka role.

### How hyperlinks are extracted from PDFs

PDF hyperlinks are stored as URI annotations — completely invisible to standard text extraction. A word like "GitHub" can be a clickable link to `https://github.com/username`, but the URL is not in the text layer.

The file extractor uses three passes:
1. `page.get_text("text")` — visible text
2. `page.get_links()` — URI annotations (the actual hyperlinks)
3. `page.get_text("rawdict")` — raw content stream scan for embedded URLs

This is why GitHub and LinkedIn links from resume hyperlinks are correctly extracted and verified, even when the resume only shows the text "GitHub Profile" as a clickable word.

---

## Assumptions

1. **Resume quality:** The system assumes resumes are text-based PDFs. Image-only or scanned PDFs (no text layer) will extract empty text. A future improvement would add OCR via Tesseract.

2. **LinkedIn verification:** LinkedIn blocks all server-to-server requests with HTTP 999. This is not a missing profile — it is LinkedIn's anti-scraping policy. The system shows the link as "Link Provided ✓" and links directly to the profile. Manual verification is required.

3. **LeetCode API:** LeetCode's GraphQL API blocks requests without browser cookies and CSRF tokens. The system attempts three fallback strategies. If all fail, the profile link is shown as "Link Provided ✓" for manual verification.

4. **GitHub without a token:** Without `GITHUB_TOKEN`, the GitHub API allows 60 requests per hour. For testing purposes this is sufficient. For production at scale, a token should be added.

5. **OpenAI model availability:** The system uses `gpt-4o` for scoring and question generation. If your API account does not have GPT-4o access, change `OPENAI_MODEL=gpt-4o-mini` in `.env` — this works fine but may produce slightly less nuanced semantic matching.

6. **Scoring is relative, not absolute:** A composite score of 72 does not mean the candidate is objectively 72% qualified. It means they are a strong fit for *this specific JD*. The same candidate evaluated against a different JD may score very differently.

7. **Free tier cold start:** The Render free tier spins down after 15 minutes of inactivity. The first request after a sleep takes 30–60 seconds. This is expected behaviour on the free tier.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | **Required.** Your OpenAI secret key |
| `OPENAI_MODEL` | `gpt-4o` | Model for scoring and question generation |
| `OPENAI_EXTRACT_MODEL` | `gpt-4o-mini` | Model for parsing (cheaper) |
| `GITHUB_TOKEN` | (empty) | Optional. Raises GitHub rate limit from 60 → 5000/hr |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API Framework | FastAPI 0.115 |
| LLM (scoring + questions) | OpenAI GPT-4o |
| LLM (parsing + extraction) | OpenAI GPT-4o-mini |
| PDF extraction | PyMuPDF (fitz) |
| DOCX extraction | python-docx |
| Data validation | Pydantic v2 |
| HTTP client | httpx |
| Parallelism | ThreadPoolExecutor |
| Frontend | Vanilla HTML / CSS / JS |
| Containerisation | Docker |
| Deployment | Render (free tier) |
