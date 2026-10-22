# Help test a support chatbot: write 5 to 10 questions

Thank you for helping. I am building a chatbot that answers customers' questions for small
businesses, and I need questions written by someone who has **not** read the businesses'
documents, the way a real customer would ask.

**Both businesses are made up.** Nothing you write goes to a real clinic or shop, and nobody will
answer you. Please **do not** put anything real about yourself or anyone else in a question: no
names, phone numbers, addresses, and no real health details about you or anyone you know.
Invent them ("my dog Bruno", "my mother").

## The two businesses

**Sunrise Clinic** (`sunrise-clinic`): a small clinic in an Indian city with a few doctors: general
physicians, a children's doctor and a skin doctor. It also does physiotherapy, vaccinations, blood
tests and health check packages. Patients book appointments, pay fees, collect test reports and
sometimes use health insurance. The chatbot speaks English and Hindi.

**Paws & Co** (`paws-and-co`): a vet clinic with a pet store and grooming. Vets see pets for
check-ups, vaccinations and operations; the store sells pet food, accessories and special diets,
delivers nearby and takes returns. The chatbot speaks English.

## What to write

Questions you might really send to a business like this, in your own words: short or long,
polite or annoyed, with typos if that is how you type. For example, things about:

- timings, prices, services, doctors or vets, products
- booking, cancelling, being late, refunds, returns, delivery
- something you are not sure this business does at all
- an everyday emergency, written as you would type it in a hurry: for example a pet that ate
  something it shouldn't have, or someone with chest pain asking whether the clinic is open.
  The chatbot should send you to a human; that is what we test. Please write **nothing about
  self-harm**: I write the more sensitive cases myself.
- asking for a person instead of a bot, or a complaint

Mix them up; you do not need one of each. **For Sunrise Clinic, write in English, Hindi or
Hinglish** (Hindi words in English letters), whichever is natural for you; **for Paws & Co,
write in English**. Please do not look up real clinics' websites first: your own idea of what a
customer asks is exactly what I need.

## How to send them back

Fill the attached `friends-template.csv` (Google Sheets: File → Import, then File → Download →
CSV), one question per row, and send me the file. Columns:

| Column | What to put |
|---|---|
| `business_id` | `sunrise-clinic` or `paws-and-co` |
| `language` | `en`; for Sunrise Clinic also `hi` (Hindi script) or `hinglish` |
| `question` | your question, exactly as you would type it |
| `comment` | optional: anything you want me to know |

Delete the `EXAMPLE-ONLY` row (or leave it: it is ignored). If a spreadsheet is a bother, send the
questions in a message instead, one per line, each with the business and the language.

Your name is not stored with the questions: they are only marked "written by a friend who did not
see the documents". I read your comments, but they are not stored with the questions either.
