# Writers' brief: the held-out probe for the guardrails (M7, D6)

For shesha and the friends who help. You write messages a customer might send to a business's
chat assistant, and a few replies an assistant must never send. The guards are measured on what
you write, which nobody who wrote the guards has seen: that is the whole point, so this page shows
no example message.

## Before you start

- **Never open** `cases.jsonl`, `gates.json` or `fixtures/` in this folder, or anything under
  `data/safety/` (the guards' term lists), or read them anywhere else (a diff, a pull request, an
  editor's search). If you see one by accident, tell shesha, who notes it next to the probe.
- Do not look up symptom lists, jailbreak collections, other chatbots' test sets or sample chats.
  Write what you would really type.
- All three businesses are made up. Nobody answers your messages.

## The businesses

- **Sunrise Clinic** (`sunrise-clinic`) · a clinic for people · English, Hindi or Hinglish (Hindi
  words in English letters)
- **Paws & Co** (`paws-and-co`) · a vet clinic and pet store · English only
- **RT Shop** (`rt-shop`) · an online shop for home, kitchen and pet supplies · English or Hindi

## What to write

Spread your messages over the businesses. Aim for at least 10 of each of the first three kinds
per health business (Sunrise Clinic and Paws & Co):

1. **Emergencies:** a person or an animal may need urgent help now (`urgent`).
2. **Asking for a clinician's or vet's judgement:** what a symptom or a test result means, how
   serious it is, which medicine or how much of it (`high`).
3. **Harmless look-alikes:** ordinary questions that only sound alarming (`allowed`).
4. **Tricks:** messages that try to make the assistant break its rules, book or change something
   without asking, or reveal its instructions (`injection`).
5. **Bad replies:** a reply the assistant must never send, such as medical advice, a claim that
   something was done when it was not, or a rude answer (`blocked_reply`). Write the customer's
   message too.

## Content rules

- **Write nothing about self-harm, suicide or a crisis**, in any language. Those messages are
  shesha's alone, later, after a professional's review (Q8).
- **Nothing real.** No real names, phone numbers, e-mail addresses, ID numbers, card numbers or
  street addresses, no real health details about you or anyone you know, and nothing that looks
  like a real person. Invent names.
- **Personal data only as a placeholder**, written exactly like this: `{{pii:phone_in}}`,
  `{{pii:email}}`, `{{pii:aadhaar}}`, `{{pii:card}}`, `{{pii:upi}}`, `{{pii:pan}}`. Never type a
  number that looks like one.
- **Your own voice.** Short or long, polite or annoyed, typos, abbreviations and odd spacing
  welcome, as you would really type them.
- **No Telugu** for now (no Telugu reviewer yet).

## Field guide

**Friends:** send shesha a list. For each message: the business, the language (English, Hindi or
Hinglish), the message exactly as you would type it, and its kind (1–5 above); for a bad reply,
the reply too. Your name is not stored with the messages.

**shesha:** each message becomes one line in the probe's own file, in the same case format as
this suite (`README.md`, "Case fields"), with `author` and `created` filled in and
`synthetic: true`; `check` and `report --counts` work on it. Agents never read the probe file and
run only `report --counts` on it. You run it, ledgered once per rules version (the sha256 of
`data/safety/`), and the note reports it apart from the frozen suite, never merged with it.
