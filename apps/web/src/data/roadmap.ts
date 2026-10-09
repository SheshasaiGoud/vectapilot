/**
 * The playbook's milestones and who owns them. Teammate status on the site is *derived*
 * from `currentMilestone` — move it forward and every card updates itself.
 */

export interface Milestone {
  id: string;
  title: string;
  summary: string;
  owners: string[];
}

export const currentMilestone = "M0";

export const milestones: Milestone[] = [
  {
    id: "M0",
    title: "Foundations",
    summary: "Repo, Docker Compose, CI, health endpoint, success metrics",
    owners: ["maya-tech-lead", "diego-backend", "sofia-frontend"],
  },
  {
    id: "M1",
    title: "LLM gateway",
    summary: "Retries, structured output, cost accounting, fake provider",
    owners: ["diego-backend"],
  },
  {
    id: "M2",
    title: "Ingestion + baseline RAG",
    summary: "Loaders, chunking, pgvector, cited answers, eval v0",
    owners: ["ananya-rag", "noah-evals"],
  },
  {
    id: "M3",
    title: "RAG v2",
    summary: "Hybrid search, RRF fusion, reranking, abstention",
    owners: ["ananya-rag", "noah-evals"],
  },
  {
    id: "M4",
    title: "Intent router",
    summary: "Trained classifier with an LLM fallback",
    owners: ["kenji-ml", "noah-evals"],
  },
  {
    id: "M5",
    title: "MCP tools + agent",
    summary: "Bookings with confirmation and idempotency",
    owners: ["leo-agents"],
  },
  {
    id: "M6",
    title: "Document extraction",
    summary: "OCR fallback, schemas, per-field confidence",
    owners: ["ananya-rag"],
  },
  {
    id: "M7",
    title: "Safety",
    summary: "PII masking, injection defense, red-team suite",
    owners: ["zara-safety"],
  },
  {
    id: "M8",
    title: "Staff dashboard",
    summary: "Review queue, decisions, corrections become evals",
    owners: ["sofia-frontend"],
  },
  {
    id: "M9",
    title: "Observability + cost",
    summary: "Traces, dashboards, caching, model routing",
    owners: ["noah-evals", "diego-backend"],
  },
  {
    id: "M10",
    title: "Deploy + CI evals",
    summary: "One-command deploy and a regression gate",
    owners: ["sofia-frontend", "noah-evals"],
  },
  {
    id: "M11",
    title: "Pilot + write-up",
    summary: "Real users, final metrics, case study",
    owners: ["maya-tech-lead"],
  },
];
