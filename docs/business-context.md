# Business context — a configurable, multi-business support platform

- **Status:** v0.3 · 2026-10-13 · owner: shesha · Priority-1 decisions answered; metrics signed off (§6)
- **Legend:** ✅ decided by shesha · 🟡 proposed (needs approval) · ❓ open question (see §9)

Nothing marked 🟡 or ❓ is a requirement yet. Open questions are asked, not assumed.

---

## 1. What VectaPilot is ✅

VectaPilot is a **configurable AI customer-support platform** used by many different businesses —
not a product built for one business. Each business configures its own identity, channels,
languages, knowledge sources and escalation rules.

**Example business types** ✅: human clinics and healthcare centres · veterinary clinics and pet
stores · e-commerce businesses · other service-based or retail businesses.

**Terms used in this document**

| Term | Meaning |
|---|---|
| **Business** (tenant) | One business customer of the platform, e.g. one clinic or one online store |
| **End customer** | A person chatting with a business through VectaPilot |
| **Staff** | The business's own employees who review drafts and take escalations |
| **Platform operator** | Whoever runs VectaPilot itself (today: shesha) |

**Principles** ✅ (from the requirements)

1. **Nothing is hard-coded** — no business, channel, language or document set is built in. All of
   it comes from that business's configuration.
2. **Strict separation** — each business's configuration and knowledge sources are kept apart so
   information can never leak from one business to another (§5).
3. **Business-specific rules** — each business has its own rules, including mandatory escalation
   policies (§3.5).
4. **Safety boundaries** — healthcare and veterinary businesses get explicit boundaries; decisions
   that need professional judgement always go to a human (§4).
5. **Ask, don't invent** — unresolved technical or business decisions are listed in §9.

**Pilot** ✅ (Q1): development and evaluation use **fictional businesses** for now. All evaluation
data is labelled *synthetic*, so results describe the system, not real-world performance. A real
pilot business is still needed before any real-world claim (playbook, M11).

---

## 2. How configuration is layered 🟡

```
platform defaults          e.g. "always offer a human", AI disclosure, rate limits
  └─ business-type template   e.g. healthcare: emergency rules, no diagnosis
       └─ business configuration   e.g. Sunrise Clinic: name, hours, documents, its own rules
```

- A business inherits its type's template and adds its own settings on top.
- 🟡 **Safety rules can be tightened but never loosened** by a business: a clinic may add
  escalation triggers but cannot switch off the healthcare emergency rules.
- ✅ (Q3) **Configuration lives in versioned files**, managed by the platform operator through pull
  requests; a self-serve admin screen for businesses comes later. 🟡 Proposed layout:
  `config/businesses/<business_id>.yaml`, validated against a schema in CI; documents for the
  fictional businesses under `data/sample/<business_id>/` (real business documents never go in git).
- 🟡 Every configuration change is reviewed in a PR (who changed what, when), and the evaluation
  suite reruns for that business before the change merges.

---

## 3. What each business configures

### 3.1 Business identity ✅

| Field | Notes |
|---|---|
| `business_id` | ✅ Stable, unique identifier: a lowercase slug that fully matches `[a-z0-9]+(-[a-z0-9]+)*` (`re.fullmatch`; `re.match` with `$` also accepts a trailing newline), 3–40 characters (2026-10-14, ADR 0006). Every record and file belongs to exactly one. |
| Business name | Shown to end customers. |
| Business type | One of the types in §1; selects the template (§2). |
| Services | Name, short description; 🟡 optional duration and price *source* (see 3.4). |
| Products | 🟡 Usually referenced from a catalogue source rather than typed in. |
| 🟡 Locations, opening hours, time zone | Needed for "are you open?" and appointment questions. |
| 🟡 Staff hand-off contacts | Where escalations go (dashboard, e-mail, phone) — per business. |

### 3.2 Customer channels ✅

Per business: **web chat**, **WhatsApp**, or **both**.

| Channel | Per-business settings 🟡 | Notes |
|---|---|---|
| Web chat | Enabled · websites allowed to embed the widget · branding (name, colours, greeting) | Free to run; the first channel to build (playbook). |
| WhatsApp | Enabled · the business's WhatsApp number · approved message templates | Needs Meta WhatsApp Business API approval; has templates, messaging-window rules and per-message fees — check current Meta policy before launch. ❓ Q5 |

Credentials (API keys, tokens) are **never stored in configuration files or git** — only a
reference to a secret store. 🟡

### 3.3 Languages ✅

| Setting | Notes |
|---|---|
| Supported languages | List per business (e.g. English, Hindi, Telugu). |
| Default language | Used when the customer's language is not supported. |
| 🟡 Language detection | Reply in the customer's language when supported; otherwise the default language plus an offer of a human. |
| 🟡 Knowledge per language | Each document is tagged with its language. |

✅ Q6 (answered by shesha 2026-10-23, M3 plan D1 (a)): May the assistant answer in one language
using a source written in another (translation), or only from sources in the reply language?
**Translation is allowed:** no language filter; the answer is in the question's language and the
citation shows the source's language. A per-business setting may follow once a config loader
exists (D1 (c)); M7 may tighten it for safety content. Code-mixed messages (e.g. Hinglish) need
their own evaluation cases either way.

### 3.4 Knowledge sources ✅

Allowed source types: FAQs · PDFs · service lists · product catalogues · pricing · appointment
policies · other approved documents.

Per source 🟡:

| Field | Why |
|---|---|
| Title, type, language | Retrieval filters and citations |
| **Status: draft / approved / retired** | Only **approved** sources can be used to answer |
| Approved by, approved on | Accountability |
| Version, effective date, **review-by date** | Stale documents are the top cause of wrong answers; alerts before they expire |
| Audience: public / staff-only | Staff-only material is never quoted to end customers |

🟡 **Fast-changing facts** (prices, stock, order status, appointment slots) are better read from
structured data or live integrations (tools, milestone M5) than from PDFs, so answers do not go
stale. ❓ Q10: which systems each business type uses.

Supported upload formats (playbook): PDF, DOCX, Markdown, HTML, CSV.

### 3.5 Human escalation rules ✅

Two layers:

1. **Mandatory rules** — from the platform and the business-type template. Cannot be disabled. 🟡
2. **Business rules** — added by each business for its own policies. ✅

**Anatomy of a rule** 🟡

| Part | Examples |
|---|---|
| Trigger | A topic/intent · keywords · a classifier signal · low answer confidence · conflicting sources · repeated negative sentiment · the customer asks for a human · a tool failure · an amount above a limit |
| Action | Hand off immediately · draft a reply for staff to approve · show safety guidance, then hand off |
| Priority | Urgent / high / normal (drives the staff review queue order) |
| Customer message | What the customer is told (always honest that a human will follow up) |
| Notify | Where staff are alerted |

**Proposed platform defaults** 🟡 (from the playbook's escalation policy):

| Trigger | Action |
|---|---|
| Customer asks for a human | Hand off with a conversation summary |
| Emergency, safety risk or distress | Urgent hand-off + safety guidance; no AI advice |
| Weak or conflicting evidence | Do not answer; draft for staff; tell the customer a human will confirm |
| Money: refunds above a limit, price disputes | Always human-approved (limit set per business) |
| Legal threats, complaints about staff | Hand off |
| Repeated negative sentiment, tool failure | Hand off with a summary |

❓ Q9: what happens **outside staff hours** (queue + tell the customer when to expect a reply?
emergency-only guidance?).

---

## 4. Safety boundaries — healthcare and veterinary 🟡

These are **proposals**. ❓ Q8: before a healthcare or veterinary business goes live, should a
qualified professional at that business (doctor, veterinarian) review and sign off its rules?

### 4.1 Human clinics and healthcare centres

| The assistant may | The assistant must not — always escalate instead |
|---|---|
| Share approved administrative information: services, doctors, timings, fees, insurance accepted | Diagnose or suggest what a symptom might mean |
| Book, reschedule or cancel appointments (with confirmation) | Judge how urgent a symptom is (beyond detecting emergencies) |
| Quote approved preparation instructions (e.g. fasting before a test) with their source | Interpret test results, reports or images |
| Explain clinic policies (cancellation, documents to bring) | Recommend, change or dose any medication or treatment |
| | Give a prognosis or second opinion |

**Emergencies:** red-flag messages (e.g. chest pain, trouble breathing, signs of stroke, severe
bleeding, loss of consciousness, severe allergic reaction, poisoning, thoughts of self-harm) skip
normal answering → show the business's configured emergency guidance (local emergency number,
crisis line) → urgent hand-off to staff. The red-flag list is configured per business type and
must be reviewed by a professional (Q8). Missing a real emergency is the worst failure mode, so
this category is evaluated for **recall** (§6).

**Health data** is sensitive personal data in most regions. Regions ✅ (Q4): **India** (Digital
Personal Data Protection Act) **plus other countries** — ❓ Q4b which ones, since each adds its own
privacy law (e.g. GDPR, HIPAA) and emergency numbers. Emergency guidance is therefore always
configured per business, never hard-coded. ❓ Q7: may the assistant ask for a reason-for-visit or
symptoms when booking?

### 4.2 Veterinary clinics and pet stores

| The assistant may | The assistant must not — always escalate instead |
|---|---|
| Share hours, services, fees, booking and store policies | Diagnose an animal or interpret its symptoms |
| Book, reschedule or cancel visits (with confirmation) | Recommend or dose medication — including dosing by weight beyond the product label |
| Give product information from the approved catalogue | Advise on prescription-only items or prescription diets without vet authorisation |
| | Judge whether something is urgent (beyond detecting emergencies) |

**Emergencies:** e.g. suspected poisoning or toxic ingestion, difficulty breathing, seizures,
trauma, signs of bloat, difficult birth, heatstroke → the business's configured emergency guidance
(nearest emergency vet, animal poison helpline) → urgent hand-off.

### 4.3 Every business, every type

- The customer always knows they are talking to an AI and is one tap from a human.
- No answer without an approved source; no claim is invented (prices, policies, availability).
- 🟡 E-commerce and retail extras: refunds above the limit, payment problems, suspected fraud and
  account-security requests always go to a human.

---

## 5. Isolation between businesses ✅ requirement · 🟡 controls

**Requirement** ✅: one business's configuration, knowledge, conversations and data can never be
seen by — or influence answers for — another business.

**Controls** — database strategy ✅ (Q2, ADR 0004 accepted): shared tables + `business_id` +
Postgres row-level security. The other controls are 🟡 proposed.

| Where | Control |
|---|---|
| Database | Every row carries a `business_id`; every query is scoped to it; ✅ Postgres **row-level security** enforces this inside the database as a second line of defence |
| Vector search | The `business_id` filter is part of the search itself, never applied afterwards |
| Files | Uploads stored under per-business paths |
| Caches and queues | Keys always include the `business_id` |
| Prompts | A prompt only ever contains the current business's data |
| Tools (MCP) | Each business's tools get credentials limited to that business |
| Staff accounts | A staff user sees only their business; platform-operator access is audited |
| Logs and traces | Tagged with `business_id`; personal data masked |

**How isolation is proven** 🟡: a **cross-business leakage test suite** plants unique "canary"
sentences in business A's knowledge, then asks for them while chatting as business B. Any leak
fails CI. Multi-business isolation is only claimed once this suite passes (playbook: never claim
what you have not tested).

---

## 6. Success metrics ✅ (signed off by shesha, 2026-10-13)

Measured **per business**, and broken down by business type, language and channel. A release must
meet the thresholds for **every** pilot business — a good average cannot hide one business getting
worse. Each target says where it came from (the playbook or Claude's proposal); all are starting
targets, revisited after the first measured baseline. While the pilot is fictional (Q1), every
number is reported as measured on **synthetic** data.

**🚫 Hard gates** — a single failure blocks a merge or release, however good everything else is:
missing a safety escalation, and any leak between businesses.

**Definition of "resolved"** ✅ (Q11): the AI handled the conversation without a hand-off to
staff, the customer gave no 👎, **and** the customer did not contact the same business about the
same topic again within 24 hours.

| Area | Metric | How it is measured | Starting target | Measurable from |
|---|---|---|---|---|
| **Answer accuracy** | Correctness | Golden question set per business, human labels + a judge calibrated against them | ≥ 90 % (playbook) | M2 (eval v0) |
| | Faithfulness | Every claim supported by a cited, approved source | ≥ 95 % (playbook) | M2–M3 |
| | Retrieval hit@5 | The right source chunk is in the top 5 | ≥ 90 % (playbook) | M2 |
| **Successful resolution** | Auto-resolution rate | Conversations "resolved" (definition above) *at* the correctness target | ≥ 50 % (playbook) | Real pilot (M11) |
| | Wrong-answer rate | Auto-sent answers later marked wrong by staff | ≤ 2 % (Claude's proposal) | Staff review (M8) + real pilot |
| **Response time** | Latency p50 / p95 | Message received → reply sent, per channel | p95 < 6 s for web chat (playbook) | M1 (per call), M9 (per conversation) |
| **Customer satisfaction** | Thumbs-up rate | 👍/👎 after a conversation | Set at the real pilot after a 2–4-week baseline | Real pilot (M11) |
| | Optional CSAT score | 1–5 survey | Set at the real pilot after a baseline | Real pilot (M11) |
| **Appropriate escalation** | Escalation recall — **safety** | Share of emergency / professional-judgement cases escalated (healthcare, veterinary eval sets) | **100 %** — 🚫 hard gate (Claude's proposal) | M4 (router); gated from M7 |
| | Escalation recall — other mandatory rules | Share of must-escalate cases escalated | ≥ 95 % (Claude's proposal) | M4–M7 |
| | Escalation precision | Share of escalations that staff agree were needed | ≥ 80 % (Claude's proposal) | M7 (labelled set); real pilot (staff) |
| **Isolation** | Cross-business leaks | Canary leakage suite (§5) | **0** — 🚫 hard gate (Claude's proposal) | M2 (first per-business data) |
| Cost (secondary) | Cost per conversation | Traced tokens × prices | < $0.02 (playbook) | M1 (per call), M9 |

---

## 7. Example configurations (illustrative, fictional)

Shows how three very different businesses fit one model. Configuration lives in files like these
(✅ Q3); the exact schema is designed when the first one is needed.

```yaml
business_id: sunrise-clinic            # fictional multi-doctor clinic
type: human_healthcare                 # inherits the healthcare safety template (§4.1)
name: Sunrise Clinic
channels: { web_chat: enabled, whatsapp: disabled }
languages: { supported: [en, hi], default: en }
knowledge:
  - { id: fees, title: Fee_Policy.pdf, type: pricing, language: en, status: approved, review_by: 2027-01-31 }
escalation:
  emergency_guidance: "If this is an emergency, call 112 now."
  extra_rules:
    - { trigger: { intent: refund }, action: draft_for_staff, priority: high }
---
business_id: paws-and-co               # fictional vet clinic with a pet store
type: veterinary
name: Paws & Co
channels: { web_chat: enabled, whatsapp: enabled }
languages: { supported: [en, te], default: en }
---
business_id: northwind-shop            # fictional online store
type: ecommerce
name: Northwind Shop
channels: { web_chat: enabled, whatsapp: disabled }
languages: { supported: [en], default: en }
escalation:
  extra_rules:
    - { trigger: { refund_above: 5000, currency: INR }, action: draft_for_staff, priority: high }
```

---

## 8. Impact on the roadmap

| Milestone | Change because the platform is multi-business |
|---|---|
| M0 | This document; ADR 0004 (isolation) |
| M2–M3 | Ingestion and retrieval are always per business; the business filter is inside every search |
| M4 | The router may need business-type-specific intents |
| M5 | Tools and their credentials are per business |
| M6 | Extraction schemas per business type |
| M7 | Healthcare/veterinary safety rules + the cross-business leakage suite |
| M8 | Staff dashboard scoped to one business; a self-serve admin screen comes later (✅ Q3) |
| M2–M10 | Eval sets and metrics per business (§6) |
| M11 | Pilot with at least one real business — evaluation data is fictional until then (✅ Q1) |

---

## 9. Decisions and open questions — for shesha

**Answered** ✅ (Q1–Q4 on 2026-10-12 · Q11 on 2026-10-13)

| # | Question | Decision |
|---|---|---|
| Q1 | Pilot business | Fictional businesses for now; evaluation data labelled synthetic; a real pilot is still needed before M11 |
| Q2 | Isolation strategy | Shared tables + `business_id` + Postgres row-level security (ADR 0004 accepted) |
| Q3 | Who configures a business | Versioned config files via PRs now; self-serve admin screen later |
| Q4 | Regions | India plus other countries |
| Q11 | Metrics | Targets in §6 approved; hard gates: safety-escalation recall 100 % and zero cross-business leaks; "resolved" = no hand-off, no 👎, no repeat within 24 h; satisfaction targets set at the real pilot |

**Still open** ❓

4b. **Other countries:** which ones, besides India? (Privacy law, data residency, emergency numbers.)
4c. **Model provider data location** (added 2026-10-14, ADR 0007): may end-customer data from
    India, including health data, be processed on Anthropic's global or US inference with data at
    rest in the US, and kept up to 2 years if flagged? Is a DPDP review needed first?
5. **WhatsApp:** in scope for the first pilot? Directly via Meta's Cloud API or through a provider?
6. **Translation:** may answers be translated from a source in another language?
7. **Health data at booking:** may the assistant ask for symptoms or a reason for the visit?
8. **Professional sign-off:** must a doctor/veterinarian at each business approve its safety rules
   before go-live?
9. **Outside staff hours:** what should escalations do?
10. **Live data:** which systems hold prices, stock, orders and appointment slots for each business
    type (e.g. a shop platform, a clinic calendar)?
