# Writers' brief: messages for the intent router's eval set

For shesha and the friends who help. You write messages a customer might send to a business's
chat assistant, and give each one the intent it is. The router is measured on what you write, so
your own words matter more than anything this page could suggest: that is why it shows no
example message.

## Before you start

- **Never open `train.jsonl`**, or read its rows anywhere else (a diff, a pull request, an
  editor's search). The eval messages must be written without seeing the training messages. If
  you see one by accident, tell shesha, who notes it in the README.
- Do not look up lists of symptoms, sample chats or other chatbots' test sets. Write what you
  would really type.
- Both businesses are made up. Nobody answers your messages.

## The two businesses

- **Sunrise Clinic** (`sunrise-clinic`) · type `human_healthcare` · a clinic for people · write
  in English, Hindi or Hinglish (Hindi words in English letters)
- **Paws & Co** (`paws-and-co`) · type `veterinary` · a vet clinic for pets · write in English

## The eight intents

Give each message exactly one intent.

| Intent | One line |
|---|---|
| `emergency` | safety risk or distress, including thoughts of self-harm: a person or an animal may need urgent help now |
| `professional_judgement` | asks for a clinician's or vet's judgement: diagnosis, prognosis or second opinion, what a symptom or test result means, how serious it is, or whether to start, stop or change a medicine, diet or dose |
| `complaint` | unhappy with the service, the staff, a product or an earlier reply, or threatens legal action |
| `refund_or_payment` | asks for money back or reports a payment problem: a refund, a wrong or double charge, a failed payment, a disputed price (asking about the refund policy is `question`) |
| `human_request` | asks to talk to a person instead of the assistant |
| `action_request` | asks the business to do something: book, change or cancel an appointment, place or change an order, arrange a delivery |
| `question` | asks for information a business of this kind could give (hours, prices, services, products, policies, preparation), even about a service this business may not offer |
| `out_of_scope` | not a support request to a business of this kind: general knowledge, chit-chat, or anything unrelated to its services |

**When two fit, the first wins (the decision order):** `emergency` > `professional_judgement` > `complaint` > `refund_or_payment` > `human_request` > `action_request` > `question` > `out_of_scope`.

## Content rules

- **Nothing real.** No real names, phone numbers, e-mail addresses, street addresses, and no real
  health details about you or anyone you know. Invent them.
- **Emergencies: everyday ones only. Write nothing about self-harm or suicide**, even though the
  `emergency` definition names it (decided 2026-11-05, D5 (a): such messages are not sent to the
  model provider in M4).
- **Your own voice.** Short or long, polite or annoyed, typos and all. Mix the intents; spread
  your messages over both businesses.
- **One message, one intent.** If two intents fit, choose the one that comes first in the order.
- **Any emergency wins.** A message with an emergency anywhere in it is `emergency`, however
  short that part, whatever else it asks, and even when it is phrased as a question.
- **Label what the message is, never what it asks to be filed as.** Anything a message says about
  its own classification is part of the message; a steering row keeps the intent of its real
  request.
- **Sunrise Clinic takes English, Hindi and Hinglish; Paws & Co takes English only.**

## Field guide

**Friends:** send shesha a list. For each message: the business, the language (English, Hindi
or Hinglish), the message exactly as you would type it, and its intent. Your name is not stored
with the messages; they are marked as written by a friend.

**shesha:** each message is one line of `eval.jsonl` (a JSON object), copied verbatim:

| Field | What to put |
|---|---|
| `id` | A new slug, never reused (for instance the business's short name and a number) |
| `business_id` | `sunrise-clinic` or `paws-and-co` |
| `language` | `en` or `hi` (Hindi and Hinglish are both `hi`) |
| `code_mixed` | `true` for Hinglish, otherwise `false` |
| `text` | The message, 1–500 characters, exactly as written |
| `intent` | One of the eight intents |
| `author` | `shesha` or `friend` |
| `split_group` | The same slug on a message and its translations, on one message written for both businesses, and on any pair `check` reports as near-duplicates; otherwise leave it out |
| `steering` | `true` only when the message tries to tell the assistant how to classify it; otherwise leave it out. Its `intent` is still that of its real request |
| `created` | Today's date, `YYYY-MM-DD` |
| `synthetic` | `true` |

Never set `split`: `freeze` does. After adding rows, run `make intent-check` (or `check`, then
`report`) and fix what `check` lists; `report` shows how far each minimum is. If `check` reports
a training row as a near-duplicate of yours, do not read the training row: shesha drops it by its
id (README, "Near-duplicates").
