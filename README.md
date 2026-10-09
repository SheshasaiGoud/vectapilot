# VectaPilot

**AI Support & Operations Copilot for small businesses** — a supervised assistant that answers customer
questions from the business's own documents (with citations), takes safe actions through permissioned
tools, extracts data from uploaded documents, and hands risky or uncertain cases to a human with a
drafted reply.

> **Status:** 🚧 Milestone M0 (foundations) — the project has just started. Nothing below is built yet;
> this README will be updated as each milestone lands. Any metric shown here will be a real,
> measured number — never a placeholder.

## The problem

Clinics, coaching institutes, local e-commerce stores and real-estate agencies answer the same questions
all day — fees, timings, policies, order status, appointment changes. Staff are interrupted constantly,
replies are slow after hours, and answers are inconsistent. Generic chatbots hallucinate policies, can't
take real actions, and give staff no way to supervise them.

## The approach

| Principle | What it means in VectaPilot |
|---|---|
| **Grounded** | Every factual answer cites its source; the system refuses when evidence is weak |
| **Supervised** | Staff approve, edit or reject drafts; every correction becomes an eval case |
| **Acts safely** | Write actions need explicit confirmation, idempotency keys and audit logs |
| **Measured** | An eval suite runs in CI; regressions block merges |
| **Observable** | Every request is traced with latency, tokens and cost |

## Planned architecture

```
message → guardrails → router (classifier, LLM fallback) → RAG | agent + MCP tools | document extraction | human review
        → response composer (grounding check, citations) → trace + cost + audit log
```

**Planned stack:** Python 3.12 · FastAPI · Postgres + pgvector · Redis · MCP · Next.js + Tailwind · Langfuse · Docker · GitHub Actions

## Roadmap

| | Milestone | Status |
|---|---|---|
| M0 | Foundations — repo, Docker Compose, CI, health endpoint | 🚧 In progress |
| M1 | LLM gateway — retries, structured output, cost accounting | ⏳ |
| M2 | Ingestion + baseline RAG with citations | ⏳ |
| M3 | RAG v2 — hybrid search, reranking, abstention | ⏳ |
| M4 | Intent router — trained classifier + LLM fallback | ⏳ |
| M5 | MCP tools + agent loop with confirmations | ⏳ |
| M6 | Document extraction pipeline | ⏳ |
| M7 | Safety — PII masking, injection defense, red-team suite | ⏳ |
| M8 | Staff review dashboard | ⏳ |
| M9 | Observability + cost optimization | ⏳ |
| M10 | Deployment + CI eval gates | ⏳ |
| M11 | Pilot with a real business + case study | ⏳ |

## How this project is built

VectaPilot is a hands-on learning project. It is built with a team of **virtual AI teammates**
(Claude Code subagents), each owning part of the roadmap. The core AI logic — retrieval and fusion,
the agent loop, router fallback, eval metrics and guardrail checks — is **hand-written by me**; the
virtual teammates scaffold, test and review.
