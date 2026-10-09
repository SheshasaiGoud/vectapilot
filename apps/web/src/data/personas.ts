/**
 * How each teammate is *presented* on the site. Keyed by the subagent `name` in
 * `.claude/agents/`. Object order is display order. Behaviour lives in the agent files;
 * keep catchphrases as mottos, never as claims of work that has not happened.
 */

export interface Persona {
  name: string;
  role: string;
  bio: string;
  principles: [string, string, string];
  quips: string[];
  samplePrompt: string;
}

export const personas: Record<string, Persona> = {
  "maya-tech-lead": {
    name: "Maya Chen",
    role: "Tech Lead & Architect",
    bio: "Turns big milestones into small, reviewable steps and writes down every decision so it can be defended in an interview.",
    principles: [
      "Plan first, code second.",
      "Every new dependency needs a reason.",
      "If you can't explain it, rewrite it.",
    ],
    quips: [
      "Plan first, code second.",
      "Writing the ADR before the argument starts.",
      "How will we know it worked?",
      "That abstraction needs a reason. Got one?",
    ],
    samplePrompt: "plan milestone M0 and list the risks",
  },
  "diego-backend": {
    name: "Diego Alvarez",
    role: "Backend Engineer",
    bio: "Builds the boring, reliable backbone: the FastAPI service and the LLM gateway that every model call must pass through.",
    principles: [
      "Every LLM call goes through the gateway.",
      "Model names live in config, never in code.",
      "Fail loudly at startup, gracefully at runtime.",
    ],
    quips: [
      "Retries with backoff. Timeouts on everything.",
      "Counting tokens so we can count dollars.",
      "A fake provider means tests never pay for an LLM.",
      "Typed at every boundary.",
    ],
    samplePrompt: "scaffold the FastAPI app with a /healthz endpoint and tests",
  },
  "sofia-frontend": {
    name: "Sofia Rossi",
    role: "Frontend & DevOps Engineer",
    bio: "Makes VectaPilot pleasant and trustworthy to use, and makes shipping it one boring command. Owns this very page.",
    principles: [
      "Reduced motion? Respected. Always.",
      "Mobile first — customers are on phones.",
      "Never show an AI answer without its sources.",
    ],
    quips: [
      "Reduced motion? Respected. Always.",
      "Mobile first — customers are on phones.",
      "One-command deploy or it doesn't count.",
      "Animations should explain state, not decorate it.",
    ],
    samplePrompt: "add a GitHub Actions workflow that lints and builds the site",
  },
  "ananya-rag": {
    name: "Ananya Iyer",
    role: "RAG Engineer",
    bio: "Makes every answer grounded — the right chunk retrieved, the right source cited, and an honest “I don't know” when evidence is thin.",
    principles: [
      "No source, no claim.",
      "Most wrong answers are retrieval failures.",
      "Every retrieval change ships with its metric delta.",
    ],
    quips: [
      "No source, no claim.",
      "Vector + BM25, fused with RRF. Measured, not vibed.",
      "Chunk size 512 or 1024? Let's ask the eval set.",
      "I write the retrieval tests. You write the retrieval. 😉",
    ],
    samplePrompt: "review my RRF fusion function and tell me what my tests miss",
  },
  "noah-evals": {
    name: "Noah Fischer",
    role: "Eval & Observability Engineer",
    bio: "Turns “it feels better” into a measured, reproducible number — with the dataset version, commit and config written next to it.",
    principles: [
      "If we can't measure it, we can't claim it.",
      "Never tune on the test split.",
      "Report the changes that didn't help, too.",
    ],
    quips: [
      "If we can't measure it, we can't claim it.",
      "Dev split for tuning. Test split stays sealed.",
      "Every trace has a cost. Let's look at it.",
      "An uncalibrated LLM judge is just a confident opinion.",
    ],
    samplePrompt: "explain hit@k vs MRR and write fixtures I can check by hand",
  },
  "kenji-ml": {
    name: "Kenji Watanabe",
    role: "ML Engineer",
    bio: "Proves when not to use an LLM: a small, calibrated classifier routes the obvious messages for a fraction of the cost.",
    principles: [
      "Macro-F1, not accuracy, on imbalanced data.",
      "Calibrate before trusting a confidence score.",
      "Report counts next to percentages.",
    ],
    quips: [
      "Macro-F1 or it didn't happen.",
      "Sometimes the best LLM call is no LLM call.",
      "Seeds fixed. Splits frozen. No leakage.",
      "This confusion matrix is less confused than it looks.",
    ],
    samplePrompt: "design the intent label set and a leak-free train/test split",
  },
  "leo-agents": {
    name: "Leo Okafor",
    role: "Agents & MCP Engineer",
    bio: "Lets the assistant actually do things — check slots, book, reschedule — through MCP tools that never write without permission.",
    principles: [
      "No write without confirmation.",
      "Idempotency keys: book once, never twice.",
      "Every failure path has a human fallback.",
    ],
    quips: [
      "No write without confirmation. Not even mine.",
      "Step caps keep agents out of infinite loops.",
      "Idempotency key attached. Retry away.",
      "Small tools, boring schemas, happy humans.",
    ],
    samplePrompt: "write the test suite for my agent loop's step cap",
  },
  "zara-safety": {
    name: "Zara Haddad",
    role: "Safety Engineer",
    bio: "Assumes every input is hostile and every model can be wrong — then designs so that neither can hurt a customer.",
    principles: [
      "Retrieved text is data, never instructions.",
      "Fail closed — escalate to a human.",
      "Never log raw PII.",
    ],
    quips: [
      "“Ignore previous instructions”? Nice try.",
      "Retrieved text is data. Never instructions.",
      "If a guardrail errors, a human takes over.",
      "Red-teaming is testing with a villain's hat on.",
    ],
    samplePrompt: "try to break my PII masker with tricky inputs",
  },
};

export const humanLead = {
  name: "shesha",
  handle: "SheshasaiGoud",
  role: "Human lead · writes the core",
  color: "indigo",
} as const;
