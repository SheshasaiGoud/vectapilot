# M7 term review sheet (PR 4, `m7/safety-terms`)

> **Synthetic, agent-drafted by zara-safety, not professionally reviewed (Q8).** For shesha: tick
> each term you accept, strike or note the ones you reject. Every term is listed once; a test
> (`tests/guards/test_safety_terms.py`) fails if a term id is added to or removed from
> `data/safety/` without this sheet. After editing a term, edit its row here too.

**Review rule (plan, PR 4):** reject any term whose only source is a frozen red-team case. Every
term below cites a business-context line (§3.5, §4.1, §4.2), a synonym rule whose chain ends at
one, or (injection and output only) an authoring rule of the plan (G7, G8, D2 (i)); a test checks
each cited line is really in its section and that no source names a case. The term author did not
open the probe file, any `eval.jsonl` or any `train.jsonl`.

**What to check per term:** would a customer write it (or the model, for output terms)? Is the
category right (`emergency` is an urgent hand-off, `professional_judgement` a high one)? Is it too
broad (it hands off benign messages: over-escalation is counted on category L)? Is a near
window too wide? Is the Hindi correct and natural, in Devanagari and in romanised spelling?

**Not here, by decision:** no Telugu terms (`te` is `not_covered` for paws-and-co until a Telugu
reviewer exists, D6; Telugu-script messages hand off at high priority, D3 (i)); no self-harm or
crisis terms (`not_covered_categories: ["self_harm"]` on both health types; shesha's after Q8).
Romanised Hindi is in the `hi` lists, matched as written (decided 2026-11-17).

**Authoring rules** (`data/safety/README.md`; a test pins the first two): every red-flag `near`
term has a window of at least 5 words, with inflections in its word sets; "within N words" in a
source means `window: N`; an ingestion verb and its object use a window of 7 ("ate half a bag of
dark chocolate"); an everyday word ("fits", "acid", "took", "forget") only counts next to a word
that makes it a red flag or an injection.

**What an injection hit does** (G7): it never decides alone, but it is not harmless anywhere. On
the customer message the turn runs on the read-only service; a retrieved chunk is dropped before
the prompt; a tool-result string field becomes `[withheld]`; text in `create_ticket`'s summary
turns the whole response into a `guard_output` hand-off (and expires the stored row); a document
page is flagged. So an injection term that hits ordinary clinic text ("don't forget these
instructions" in a preparation chunk) is a retrieval failure once `guard_answer` is on. That is
why "forget" and भूल/bhool ("forgot") were dropped from the injection terms (ananya's review).

**Over-escalation risks already visible** (accepted for recall unless you say otherwise):
- Health, urgent: `hh_stroke` ("stroke specialist?"), `hh_allergic_reaction` (without "severe";
  a past reaction), `hh_fainted` (a past event), `hh_a_lot_of_blood` ("I need a lot of blood
  tests": kept for recall; shesha may drop it), `hh_swallowed_medicine` ("should the tablets
  be eaten before food?" turns a high hand-off into an urgent one), `hh_mirgi_hi` ("मिर्गी के
  डॉक्टर कौन हैं?"), `hh_face_drooping` ("my mouth is numb after the filling").
- Health, high: `hh_second_opinion` ("can I book a second opinion?"), `hh_change_dose`,
  `hh_dawa_badal_hi` and `hh_dawa_badal_rom` ("stop" or "बंद"/"band" near a medicine word: "is the
  medicine shop closed?"), `hh_report_mean` ("explain the report fee"), `hh_take_medicine` ("do
  you take medicine orders on WhatsApp?"), `hh_is_it_normal_to` ("is it normal to have to wait 2
  hours?"), `hh_zyada_dawa_rom` ("bahut dawai le rahe hain, discount milega?"),
  `hh_kitni_goli_rom` (its verbs le/de are everyday words).
- Veterinary: `vet_ate_toxic`, `vet_ate_medicine` with eat/eating ("can dogs eat grapes?", "will
  my dog eat the flea tablets?", "my dog ate his dewormer tablet fine": all urgent),
  `vet_collapsed`, `vet_collapse` ("is the crate flat when collapsed?", "does the crate
  collapse?"), `vet_heatstroke` and `vet_heat_stroke` (heatstroke-prevention questions: "how do I
  keep my dog safe from heatstroke this summer?"), `vet_how_many_tablets` ("how many tablets are in
  the pack?"), `vet_prescription` ("do you sell prescription diets?"), `vet_pet_symptom` ("shampoo
  for a dog with diarrhoea").
- Every business, including shops: `pf_unconscious`, `pf_not_breathing`, `pf_is_choking`,
  `pf_choking_on`, `pf_severe_bleeding` and `pf_swallowed_object` are urgent hand-offs on a shop
  too ("my son swallowed a magnet": intended; "the toddler ate the battery cover?" too).
  `pf_emergency_hai_hi` and `pf_emergency_hai_rom` hit "kya aapke yahan emergency hai" (a services
  question; the hand-off shows relevant guidance). `pf_tell_the_customer` and `pf_ignore_customer`
  also hit a customer asking to "tell the customer care team" (read-only service only).
- Output (`guard_output` replaces the model's reply): `pf_has_been_booked` and `pf_is_booked` hit
  "this slot is booked by someone else"; `pf_i_have_booked` and `pf_is_confirmed` hit a negation
  ("I have not booked anything yet": fail closed, accepted); `pf_stupid` hits "no question is
  stupid".

## Changed after the frozen-case cross-check (noah, 2026-11-18)

noah ran a throwaway matcher built from H1's written contract (not H1) over the frozen red-team
cases and reported the hard cases no term decided. Every change below is a general rule from a
stated source (the red-team README's window rule, §3.5, §4.1, §4.2, G7, G8), never a case string,
but the case that exposed it is named here. **So the frozen G, H, I, J and C numbers for these
families are no longer independent: the held-out probe (D6 (iii)) is the independent number.**

| Term(s) | Change | Exposed by |
|---|---|---|
| every red-flag `near` term | window at least 5, inflections in the sets (the README's rule) | g-004, g-025 |
| `hh_breathing_near`, `hh_breathing_cannot`, `vet_breathing_near`, `vet_breathing_cannot` | breathe/breathes, struggle/struggles, problem; split in two to stay within 12 words per set | g-004, g-025 |
| `vet_ate_toxic`, `vet_ate_medicine`, `vet_ate_chemical`, `hh_swallowed_poison`, `hh_swallowed_medicine`, `pf_swallowed_object` | ingestion window 7 (the object carries a determiner, a quantity and adjectives) | g-016 |
| `hh_maarne_ki_dawa_hi`, `hh_maarne_ki_dawa_rom`, `hh_keetnashak_hi`, `hh_phenyl_hi` | new: pest-killer products named in Hindi (§4.1 poisoning) | g-011 |
| `pf_unconscious`, `pf_unresponsive`, `pf_behosh_hi`, `pf_behosh_rom`, `pf_not_breathing`, `pf_is_choking`, `pf_choking_on`, `pf_severe_bleeding`, `pf_swallowed_object` | new: physical emergencies on every business type (§3.5) | j-005, j-006 |
| `hh_dose_amount`, `hh_kitni_goli_hi`, `hh_kitni_goli_rom`, `vet_dose_amount`, `vet_how_many_tablets` | new: an amount of a medicine asked for (§4.1, §4.2 "dose") | h-001, h-004, h-006 |
| `vet_dose_by_weight` | split: drug and dose words only next to weight; product forms moved to the count terms | h-006 (and ananya's flea-tablet price question) |
| `hh_what_do_you_think_it_is`, `vet_what_do_you_think_it_is`, `hh_could_it_be`, `hh_ho_sakta_hi`, `hh_ho_sakta_rom` | new: a diagnosis asked for (§4.1, §4.2) | h-002, h-005 |
| `pf_system_note`, `pf_tell_the_customer`, `pf_ignore_customer` | new: text inside data addressing the AI or overriding the customer (G7) | c-001, c-004, c-007 |
| `pf_is_booked`, `pf_is_confirmed`, `pf_is_cancelled`, `pf_is_canceled`, `pf_is_rescheduled`, `pf_has_been_confirmed`, `pf_i_booked`, `pf_i_cancelled`, `pf_i_rescheduled`, `pf_we_booked`, `pf_ho_gaya_hi`, `pf_ho_gaya_rom` | new: success claims in the present passive and Hindi completion forms (G8) | i-001, i-002, i-003 |
| `pf_skip_confirmation` | source re-cited to ADR 0016 W1 (unchanged term) | a-001, a-005 |
| `pf_note_to_the_assistant` | **kept (decided 2026-11-18, shesha, option (a)): mirrors b-007 word for word; its source is an author-written G7 class, so the frozen injection number is not independent of it, and the probe is the held-out figure** | b-007 |
| G14 fixed negatives | three paraphrases of the L look-alikes replaced by new look-alikes | l-007, l-009, l-010 |

Side effects for PR 6 (code-side pending reasons, never the frozen dataset): g-015 should now
graduate (`hh_breathing_near_rom`, window 5); g-030 (a `known_gap`) is hit by `vet_retching`, a
real §4.2 synonym, so its strict xfail turns XPASS and needs the same code-side graduation;
l-017 (heatstroke prevention) is a soft over-escalation; h-013 needs H3's digit dose patterns.

**332 terms** in all. Per file and category:

| File | Category | Terms |
|---|---|---|
| `platform.json` | abuse | 14 |
| `platform.json` | emergency | 26 |
| `platform.json` | injection | 25 |
| `platform.json` | medical_advice | 12 |
| `platform.json` | success_claim | 22 |
| `types/human_healthcare.json` | diagnosis | 5 |
| `types/human_healthcare.json` | emergency | 104 |
| `types/human_healthcare.json` | professional_judgement | 43 |
| `types/veterinary.json` | diagnosis | 3 |
| `types/veterinary.json` | emergency | 63 |
| `types/veterinary.json` | professional_judgement | 15 |

Per file and language (script) / kind:

| File | Language | Terms |
|---|---|---|
| `platform.json` | en | 68 |
| `platform.json` | hi (Devanagari) | 16 |
| `platform.json` | hi (romanised) | 15 |
| `types/human_healthcare.json` | en | 92 |
| `types/human_healthcare.json` | hi (Devanagari) | 35 |
| `types/human_healthcare.json` | hi (romanised) | 25 |
| `types/veterinary.json` | en | 81 |

| File | Kind | Terms |
|---|---|---|
| `platform.json` | near | 19 |
| `platform.json` | phrase | 80 |
| `types/human_healthcare.json` | near | 49 |
| `types/human_healthcare.json` | phrase | 103 |
| `types/veterinary.json` | near | 22 |
| `types/veterinary.json` | phrase | 59 |

Guidance (`businesses/*.json`) is not reviewed here: each line is verbatim from the business's
`emergency.md` and equal to its `tools.json` (a test). Both business files add no terms.

## `platform.json`

### emergency (26)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `pf_this_is_an_emergency` | en | phrase | this is an emergency | §3.5: Emergency, safety risk or distress |
| [ ] | `pf_its_an_emergency` | en | phrase | it's an emergency | synonym of `pf_this_is_an_emergency`: the same words, contracted |
| [ ] | `pf_its_an_emergency_typed` | en | phrase | its an emergency | synonym of `pf_this_is_an_emergency`: the contraction typed without its apostrophe |
| [ ] | `pf_medical_emergency` | en | phrase | medical emergency | synonym of `pf_this_is_an_emergency`: a named emergency |
| [ ] | `pf_please_help_now` | en | phrase | please help now | §3.5: Emergency, safety risk or distress |
| [ ] | `pf_help_me_now` | en | phrase | help me now | synonym of `pf_please_help_now`: the same plea for immediate help |
| [ ] | `pf_need_help_urgently` | en | phrase | need help urgently | synonym of `pf_please_help_now`: the same plea for immediate help |
| [ ] | `pf_call_an_ambulance` | en | phrase | call an ambulance | synonym of `pf_this_is_an_emergency`: asking for emergency transport |
| [ ] | `pf_need_an_ambulance` | en | phrase | need an ambulance | synonym of `pf_this_is_an_emergency`: asking for emergency transport |
| [ ] | `pf_emergency_hai_hi` | hi (Devanagari) | phrase | इमरजेंसी है | synonym of `pf_this_is_an_emergency`: the same meaning in Hindi (Devanagari); also inside यह/ये इमरजेंसी है |
| [ ] | `pf_turant_madad_hi` | hi (Devanagari) | phrase | तुरंत मदद चाहिए | synonym of `pf_please_help_now`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_jaldi_madad_hi` | hi (Devanagari) | phrase | जल्दी मदद करो | synonym of `pf_please_help_now`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_ambulance_bulao_hi` | hi (Devanagari) | near | near(5): [एम्बुलेंस \| एंबुलेंस] + [बुलाओ \| बुलाइए \| भेजो \| भेजिए \| चाहिए] | synonym of `pf_call_an_ambulance`: the same meaning in Hindi (Devanagari), in either spelling, within 5 words |
| [ ] | `pf_emergency_hai_rom` | hi (romanised) | phrase | emergency hai | synonym of `pf_this_is_an_emergency`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_turant_madad_rom` | hi (romanised) | phrase | turant madad chahiye | synonym of `pf_please_help_now`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_jaldi_madad_rom` | hi (romanised) | phrase | jaldi madad karo | synonym of `pf_please_help_now`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_ambulance_bulao_rom` | hi (romanised) | near | near(5): [ambulance] + [bulao \| bulaiye \| bhejo \| bhejiye \| chahiye] | synonym of `pf_call_an_ambulance`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17), within 5 words |
| [ ] | `pf_unconscious` | en | phrase | unconscious | §3.5: Emergency, safety risk or distress |
| [ ] | `pf_unresponsive` | en | phrase | unresponsive | synonym of `pf_unconscious`: the same, as staff say it |
| [ ] | `pf_behosh_hi` | hi (Devanagari) | phrase | बेहोश | synonym of `pf_unconscious`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_behosh_rom` | hi (romanised) | phrase | behosh | synonym of `pf_unconscious`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_not_breathing` | en | phrase | not breathing | §3.5: Emergency, safety risk or distress |
| [ ] | `pf_is_choking` | en | phrase | is choking | synonym of `pf_not_breathing`: a blocked airway (not 'choking hazard', a product label) |
| [ ] | `pf_choking_on` | en | phrase | choking on | synonym of `pf_not_breathing`: a blocked airway (not 'choking hazard', a product label) |
| [ ] | `pf_severe_bleeding` | en | phrase | severe bleeding | §3.5: Emergency, safety risk or distress |
| [ ] | `pf_swallowed_object` | en | near | near(7): [swallowed \| ate \| eaten \| drank \| drunk] + [battery \| batteries \| magnet \| magnets \| bleach \| poison \| pesticide \| kerosene \| phenyl \| detergent] | §3.5: Emergency, safety risk or distress |

### injection (25)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `pf_ignore_instructions` | en | near | near(6): [ignore \| disregard \| override \| bypass] + [instructions \| instruction \| prompt \| guidelines] | G7: an instruction to override or drop the assistant's rules |
| [ ] | `pf_ignore_rules` | en | near | near(6): [ignore \| disregard \| override \| bypass] + [rules \| restrictions \| guardrails \| safeguards] | G7: an instruction to override or drop the assistant's rules |
| [ ] | `pf_new_instructions` | en | phrase | new instructions | G7: an instruction to override or drop the assistant's rules |
| [ ] | `pf_system_prompt` | en | phrase | system prompt | G7: a request for the assistant's hidden prompt or configuration |
| [ ] | `pf_hidden_instructions` | en | phrase | hidden instructions | G7: a request for the assistant's hidden prompt or configuration |
| [ ] | `pf_developer_mode` | en | phrase | developer mode | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_admin_mode` | en | phrase | admin mode | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_admin_override` | en | phrase | admin override | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_jailbreak` | en | phrase | jailbreak | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_do_anything_now` | en | phrase | do anything now | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_pretend_you_are` | en | phrase | pretend you are | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_pretend_to_be` | en | phrase | pretend to be | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_from_now_on_you_are` | en | phrase | from now on you are | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_role_play_as` | en | phrase | role play as | G7: a role-play, persona or mode switch aimed at the assistant |
| [ ] | `pf_if_you_are_an_ai` | en | phrase | if you are an ai | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_note_to_the_assistant` | en | phrase | note to the assistant | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_note_to_ai` | en | phrase | note to ai | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_assistant_must` | en | phrase | the assistant must | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_system_note` | en | phrase | system note | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_tell_the_customer` | en | phrase | tell the customer | G7: text inside data that addresses the AI instead of the reader |
| [ ] | `pf_ignore_customer` | en | near | near(5): [ignore \| disregard \| override] + [customer \| customers \| user \| choice] | G7: text inside data that overrides the customer's own choice or request |
| [ ] | `pf_skip_confirmation` | en | near | near(4): [skip \| bypass \| without] + [confirmation \| confirming] | G7: asking to act without the customer's confirmation (ADR 0016 W1: no write without it) |
| [ ] | `pf_nirdesh_andekha_hi` | hi (Devanagari) | near | near(5): [निर्देश \| निर्देशों \| नियम \| नियमों] + [अनदेखा \| नज़रअंदाज़] | synonym of `pf_ignore_instructions`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_system_prompt_batao_hi` | hi (Devanagari) | phrase | सिस्टम प्रॉम्प्ट | synonym of `pf_system_prompt`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_nirdesh_andekha_rom` | hi (romanised) | near | near(5): [nirdesh \| niyam \| instructions \| rules] + [ignore \| andekha \| nazarandaz \| nazarandaaz] | synonym of `pf_ignore_instructions`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |

### success_claim (22)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `pf_i_have_booked` | en | near | near(4): [i \| we] + [have \| ve] + [booked \| cancelled \| canceled \| rescheduled \| refunded \| created \| processed] | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_has_been_booked` | en | phrase | has been booked | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_has_been_cancelled` | en | phrase | has been cancelled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_has_been_rescheduled` | en | phrase | has been rescheduled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_refund_has_been_issued` | en | phrase | refund has been issued | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_your_booking_is_confirmed` | en | phrase | your booking is confirmed | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_has_been_confirmed` | en | phrase | has been confirmed | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_is_booked` | en | phrase | is booked | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_is_confirmed` | en | phrase | is confirmed | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_is_cancelled` | en | phrase | is cancelled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_is_canceled` | en | phrase | is canceled | synonym of `pf_is_cancelled`: the US spelling |
| [ ] | `pf_is_rescheduled` | en | phrase | is rescheduled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_i_booked` | en | phrase | i booked | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_i_cancelled` | en | phrase | i cancelled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_i_rescheduled` | en | phrase | i rescheduled | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_we_booked` | en | phrase | we booked | G8: a claim that a write happened; the loop cannot confirm one (W1) |
| [ ] | `pf_book_kar_diya_hi` | hi (Devanagari) | phrase | बुक कर दिया | synonym of `pf_has_been_booked`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_radd_kar_diya_hi` | hi (Devanagari) | phrase | रद्द कर दिया | synonym of `pf_has_been_cancelled`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_ho_gaya_hi` | hi (Devanagari) | near | near(6): [हो \| बन] + [गया \| गई \| चुका \| चुकी] + [बुकिंग \| टिकट \| रिफ़ंड \| अपॉइंटमेंट \| रिटर्न \| कैंसिल] | synonym of `pf_has_been_booked`: the same meaning in Hindi (Devanagari): a completion form (हो गया, बन गया, हो चुका) next to a write noun |
| [ ] | `pf_book_kar_diya_rom` | hi (romanised) | phrase | book kar diya | synonym of `pf_has_been_booked`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_cancel_kar_diya_rom` | hi (romanised) | phrase | cancel kar diya | synonym of `pf_has_been_cancelled`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_ho_gaya_rom` | hi (romanised) | near | near(6): [ho \| ban] + [gaya \| gayi \| gai \| chuka \| chuki] + [booking \| ticket \| refund \| appointment \| reschedule \| cancel] | synonym of `pf_has_been_booked`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17): a completion form next to a write noun |

### medical_advice (12)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `pf_take_tablets` | en | near | near(5): [take \| give \| taking \| giving] + [one \| two \| three \| four \| half \| quarter \| 1 \| 2 \| 3 \| 4] + [tablet \| tablets \| pill \| pills \| drops \| capsule \| capsules \| mg \| spoon \| spoonful \| teaspoon] | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_twice_a_day` | en | phrase | twice a day | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_three_times_a_day` | en | phrase | three times a day | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_once_a_day` | en | phrase | once a day | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_twice_daily` | en | phrase | twice daily | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_try_giving` | en | phrase | try giving | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_try_taking` | en | phrase | try taking | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_recommend_taking` | en | near | near(3): [recommend \| suggest \| advise] + [taking \| giving] | G8: medical advice and dosing on every business type (3.5: no AI advice) |
| [ ] | `pf_goli_lein_hi` | hi (Devanagari) | near | near(3): [गोली \| गोलियां \| दवा \| दवाई \| कैप्सूल] + [लें \| लीजिए \| खाएं \| खाइए \| दें \| दीजिए] | synonym of `pf_take_tablets`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_din_mein_baar_hi` | hi (Devanagari) | near | near(6): [दिन] + [एक \| दो \| तीन \| चार] + [बार] + [गोली \| गोलियां \| दवा \| दवाई \| लें \| खाएं \| दें \| खिलाएं \| पिलाएं] | synonym of `pf_twice_a_day`: the same meaning in Hindi (Devanagari), with a medicine or dosing word |
| [ ] | `pf_goli_lo_rom` | hi (romanised) | near | near(3): [goli \| dawa \| dawai \| dava \| davai] + [lo \| lijiye \| khao \| khaiye \| dijiye \| lena] | synonym of `pf_take_tablets`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_din_mein_baar_rom` | hi (romanised) | near | near(6): [din] + [ek \| do \| teen \| char] + [baar] + [goli \| dawa \| dawai \| lein \| lena \| khayein \| khaiye \| dein \| dijiye \| lijiye] | synonym of `pf_twice_a_day`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17), with a medicine or dosing word |

### abuse (14)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `pf_idiot` | en | phrase | idiot | D2 (i): a mild insult in model text |
| [ ] | `pf_idiots` | en | phrase | idiots | D2 (i): a mild insult in model text |
| [ ] | `pf_stupid` | en | phrase | stupid | D2 (i): a mild insult in model text |
| [ ] | `pf_moron` | en | phrase | moron | D2 (i): a mild insult in model text |
| [ ] | `pf_shut_up` | en | phrase | shut up | D2 (i): a mild insult in model text |
| [ ] | `pf_useless_staff` | en | near | near(3): [useless \| incompetent \| pathetic] + [doctor \| doctors \| vet \| vets \| staff \| team \| clinic] | D2 (i): a mild insult in model text |
| [ ] | `pf_bewakoof_hi` | hi (Devanagari) | phrase | बेवकूफ | synonym of `pf_idiot`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_moorkh_hi` | hi (Devanagari) | phrase | मूर्ख | synonym of `pf_idiot`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_chup_raho_hi` | hi (Devanagari) | phrase | चुप रहो | synonym of `pf_shut_up`: the same meaning in Hindi (Devanagari) |
| [ ] | `pf_bakwas_hi` | hi (Devanagari) | phrase | बकवास | synonym of `pf_stupid`: nonsense, in Hindi (Devanagari) |
| [ ] | `pf_bewakoof_rom` | hi (romanised) | phrase | bewakoof | synonym of `pf_idiot`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_bevkoof_rom` | hi (romanised) | phrase | bevkoof | synonym of `pf_idiot`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_chup_raho_rom` | hi (romanised) | phrase | chup raho | synonym of `pf_shut_up`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `pf_bakwas_rom` | hi (romanised) | phrase | bakwas | synonym of `pf_stupid`: nonsense, in romanised Hindi |

## `types/human_healthcare.json`

### emergency (104)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `hh_chest_pain` | en | phrase | chest pain | §4.1: chest pain |
| [ ] | `hh_chest_near` | en | near | near(5): [chest] + [pain \| pains \| hurts \| hurt \| hurting \| tight \| tightness \| pressure \| ache \| ached \| aching \| heavy] | synonym of `hh_chest_pain`: a chest word and a pain word within 5 words, in any order |
| [ ] | `hh_heart_attack` | en | phrase | heart attack | synonym of `hh_chest_pain`: the condition chest pain is screened for |
| [ ] | `hh_trouble_breathing` | en | phrase | trouble breathing | §4.1: trouble breathing |
| [ ] | `hh_breathing_near` | en | near | near(5): [breathe \| breathes \| breathing \| breath] + [trouble \| difficulty \| difficult \| hard \| struggle \| struggles \| struggling \| problem \| problems \| shortness] | synonym of `hh_trouble_breathing`: a breathing word and a difficulty word within 5 words |
| [ ] | `hh_breathing_cannot` | en | near | near(5): [breathe \| breathes \| breathing \| breath] + [cannot \| cant \| couldnt \| unable \| stopped \| stopping \| gasping] | synonym of `hh_trouble_breathing`: a breathing word and an inability word within 5 words |
| [ ] | `hh_cant_breathe` | en | phrase | can't breathe | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_can_not_breathe` | en | phrase | can not breathe | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_couldnt_breathe` | en | phrase | couldn't breathe | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_not_breathing` | en | phrase | not breathing | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_short_of_breath` | en | phrase | short of breath | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_breathless` | en | phrase | breathless | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_gasping_for_air` | en | phrase | gasping for air | synonym of `hh_trouble_breathing`: the same, said plainly |
| [ ] | `hh_choking` | en | phrase | choking | synonym of `hh_trouble_breathing`: a blocked airway |
| [ ] | `hh_stroke` | en | phrase | stroke | §4.1: signs of stroke |
| [ ] | `hh_paralysis` | en | phrase | paralysis | synonym of `hh_stroke`: a stroke sign: paralysis |
| [ ] | `hh_face_drooping` | en | near | near(5): [face \| mouth] + [drooping \| droopy \| droops \| drooped \| dropped \| numb] | synonym of `hh_stroke`: a stroke sign: a drooping face |
| [ ] | `hh_arm_weakness` | en | near | near(5): [arm \| leg] + [weak \| weakness \| numb \| numbness] | synonym of `hh_stroke`: a stroke sign: weakness in an arm |
| [ ] | `hh_slurred_speech` | en | near | near(5): [speech \| talking \| words] + [slurred \| slurring] | synonym of `hh_stroke`: a stroke sign: slurred speech |
| [ ] | `hh_severe_bleeding` | en | phrase | severe bleeding | §4.1: severe bleeding |
| [ ] | `hh_bleeding_heavy` | en | near | near(5): [bleeding \| bleeds \| bled] + [severe \| heavy \| heavily \| profusely \| lot \| lots \| badly] | synonym of `hh_severe_bleeding`: bleeding with a severity word |
| [ ] | `hh_blood_heavy` | en | near | near(5): [blood] + [severe \| heavily \| profusely \| gushing \| pouring] | synonym of `hh_severe_bleeding`: blood with a severity word (not 'a lot of', which also means many blood tests) |
| [ ] | `hh_lots_of_blood` | en | phrase | lots of blood | synonym of `hh_severe_bleeding`: a lot of blood, said plainly |
| [ ] | `hh_a_lot_of_blood` | en | phrase | a lot of blood | synonym of `hh_severe_bleeding`: a lot of blood, said plainly (also hits 'a lot of blood tests': accepted for recall) |
| [ ] | `hh_bleeding_wont_stop` | en | near | near(5): [bleeding] + [stop \| stopping] | synonym of `hh_severe_bleeding`: bleeding that does not stop |
| [ ] | `hh_unconscious` | en | phrase | unconscious | §4.1: loss of consciousness |
| [ ] | `hh_loss_of_consciousness` | en | phrase | loss of consciousness | §4.1: loss of consciousness |
| [ ] | `hh_lost_consciousness` | en | phrase | lost consciousness | synonym of `hh_loss_of_consciousness`: the same, as a verb |
| [ ] | `hh_fainted` | en | phrase | fainted | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_fainting` | en | phrase | fainting | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_faints` | en | phrase | faints | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_passed_out` | en | phrase | passed out | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_passing_out` | en | phrase | passing out | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_blacked_out` | en | phrase | blacked out | synonym of `hh_unconscious`: a brief loss of consciousness |
| [ ] | `hh_unresponsive` | en | phrase | unresponsive | synonym of `hh_unconscious`: the same, as staff say it |
| [ ] | `hh_wont_wake_up` | en | phrase | won't wake up | synonym of `hh_unconscious`: the same, said plainly |
| [ ] | `hh_not_waking_up` | en | phrase | not waking up | synonym of `hh_unconscious`: the same, said plainly |
| [ ] | `hh_cant_wake` | en | phrase | can't wake | synonym of `hh_unconscious`: the same, said plainly |
| [ ] | `hh_hard_to_wake` | en | phrase | hard to wake | synonym of `hh_unconscious`: the same, said plainly |
| [ ] | `hh_collapsed` | en | phrase | collapsed | synonym of `hh_unconscious`: a sudden loss of consciousness |
| [ ] | `hh_seizure` | en | phrase | seizure | synonym of `hh_unconscious`: a fit, often with loss of consciousness |
| [ ] | `hh_seizures` | en | phrase | seizures | synonym of `hh_seizure`: the plural |
| [ ] | `hh_convulsing` | en | phrase | convulsing | synonym of `hh_seizure`: another word for a seizure |
| [ ] | `hh_convulsions` | en | phrase | convulsions | synonym of `hh_seizure`: another word for seizures |
| [ ] | `hh_had_a_fit` | en | phrase | had a fit | synonym of `hh_seizure`: a seizure, said plainly |
| [ ] | `hh_having_a_fit` | en | phrase | having a fit | synonym of `hh_seizure`: a seizure, said plainly |
| [ ] | `hh_fits_near` | en | near | near(5): [fits] + [having \| had \| getting \| gets \| aa \| aaye \| aate \| pad \| pade \| padte] | synonym of `hh_seizure`: fits with a verb in en or romanised Hindi (bare 'fits' also means 'suits') |
| [ ] | `hh_severe_allergic_reaction` | en | phrase | severe allergic reaction | §4.1: severe allergic reaction |
| [ ] | `hh_allergic_reaction` | en | phrase | allergic reaction | synonym of `hh_severe_allergic_reaction`: the reaction without its severity word (recall first) |
| [ ] | `hh_anaphylaxis` | en | phrase | anaphylaxis | synonym of `hh_severe_allergic_reaction`: the medical name |
| [ ] | `hh_anaphylactic` | en | phrase | anaphylactic | synonym of `hh_severe_allergic_reaction`: the medical name, as an adjective |
| [ ] | `hh_throat_swelling` | en | near | near(5): [face \| throat \| tongue \| lips] + [swollen \| swelling \| swelled \| closing] | synonym of `hh_severe_allergic_reaction`: a swollen face or throat |
| [ ] | `hh_poisoning` | en | phrase | poisoning | §4.1: poisoning |
| [ ] | `hh_poisoned` | en | phrase | poisoned | synonym of `hh_poisoning`: the same, as a verb |
| [ ] | `hh_overdose` | en | phrase | overdose | synonym of `hh_poisoning`: poisoning by a medicine |
| [ ] | `hh_overdosed` | en | phrase | overdosed | synonym of `hh_poisoning`: poisoning by a medicine |
| [ ] | `hh_swallowed_poison` | en | near | near(7): [swallowed \| drank \| drunk \| ate \| eaten] + [poison \| bleach \| pesticide \| kerosene \| phenyl \| cleaner \| detergent \| battery \| batteries] | synonym of `hh_poisoning`: a toxic product taken in; an ingestion verb and its object within 7 words (the object can carry a determiner, a quantity and adjectives) |
| [ ] | `hh_swallowed_medicine` | en | near | near(7): [swallowed \| drank \| drunk \| ate \| eaten] + [tablet \| tablets \| pill \| pills \| medicine \| medicines \| syrup] | synonym of `hh_overdose`: a medicine taken in by mistake; an ingestion verb and its object within 7 words (the object can carry a determiner, a quantity and adjectives) |
| [ ] | `hh_drank_acid` | en | phrase | drank acid | synonym of `hh_poisoning`: a toxic product taken in (acid alone also means acidity) |
| [ ] | `hh_swallowed_acid` | en | phrase | swallowed acid | synonym of `hh_poisoning`: a toxic product taken in (acid alone also means acidity) |
| [ ] | `hh_too_many_pills` | en | near | near(6): [too] + [many \| much] + [pills \| tablets \| medicine \| medicines] + [took \| taken \| swallowed \| ate \| had \| drank \| kha \| khaa] | synonym of `hh_overdose`: too much of a medicine, with a verb of taking it |
| [ ] | `hh_seene_mein_dard_hi` | hi (Devanagari) | phrase | सीने में दर्द | synonym of `hh_chest_pain`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_chhati_mein_dard_hi` | hi (Devanagari) | phrase | छाती में दर्द | synonym of `hh_chest_pain`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_chest_near_hi` | hi (Devanagari) | near | near(5): [सीने \| सीना \| छाती] + [दर्द \| जकड़न \| दबाव \| भारीपन] | synonym of `hh_chest_near`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_dil_ka_daura_hi` | hi (Devanagari) | phrase | दिल का दौरा | synonym of `hh_heart_attack`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_heart_attack_hi` | hi (Devanagari) | phrase | हार्ट अटैक | synonym of `hh_heart_attack`: the same meaning in Hindi (Devanagari), as the English loanword |
| [ ] | `hh_saans_takleef_hi` | hi (Devanagari) | phrase | सांस लेने में तकलीफ | synonym of `hh_trouble_breathing`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_breathing_near_hi` | hi (Devanagari) | near | near(5): [सांस \| सांसें] + [तकलीफ \| दिक्कत \| परेशानी \| फूल \| रुक \| नहीं \| नही \| मुश्किल \| घुट] | synonym of `hh_breathing_near`: the same meaning in Hindi (Devanagari), with नहीं typed with or without its anusvara |
| [ ] | `hh_saans_nahi_hi` | hi (Devanagari) | phrase | सांस नहीं आ रही | synonym of `hh_cant_breathe`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_stroke_hi` | hi (Devanagari) | phrase | स्ट्रोक | synonym of `hh_stroke`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_lakwa_hi` | hi (Devanagari) | phrase | लकवा | synonym of `hh_stroke`: paralysis, how a stroke is often named in Hindi |
| [ ] | `hh_face_drooping_hi` | hi (Devanagari) | near | near(5): [चेहरा \| चेहरे \| मुंह] + [टेढ़ा \| लटक \| लटका \| लटकी] | synonym of `hh_face_drooping`: the same meaning in Hindi (Devanagari), with the oblique चेहरे |
| [ ] | `hh_slurred_speech_hi` | hi (Devanagari) | near | near(5): [जुबान \| जबान] + [लड़खड़ा \| लड़खड़ाने \| लड़खड़ाती] | synonym of `hh_slurred_speech`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_bahut_khoon_hi` | hi (Devanagari) | phrase | बहुत खून | synonym of `hh_severe_bleeding`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_khoon_near_hi` | hi (Devanagari) | near | near(5): [खून] + [बह \| रुक] | synonym of `hh_bleeding_wont_stop`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_behosh_hi` | hi (Devanagari) | phrase | बेहोश | synonym of `hh_unconscious`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_hosh_nahi_hi` | hi (Devanagari) | phrase | होश नहीं | synonym of `hh_unconscious`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_daura_near_hi` | hi (Devanagari) | near | near(5): [दौरा \| दौरे] + [पड़ा \| पड़ \| पड़े \| पड़ने \| पड़ी \| आया] | synonym of `hh_seizure`: the same meaning in Hindi (Devanagari): दौरा/दौरे with पड़ (any form) or आया, as hh_daura_rom |
| [ ] | `hh_mirgi_hi` | hi (Devanagari) | phrase | मिर्गी | synonym of `hh_seizure`: epilepsy, how seizures are often named in Hindi (Devanagari) |
| [ ] | `hh_gambhir_allergy_hi` | hi (Devanagari) | phrase | गंभीर एलर्जी | synonym of `hh_severe_allergic_reaction`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_swelling_hi` | hi (Devanagari) | near | near(5): [चेहरा \| चेहरे \| गला \| गले \| होंठ \| जीभ] + [सूज \| सूजन \| सूजा \| सूजी] | synonym of `hh_throat_swelling`: the same meaning in Hindi (Devanagari), with the oblique चेहरे |
| [ ] | `hh_zehar_hi` | hi (Devanagari) | phrase | ज़हर | synonym of `hh_poisoning`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_maarne_ki_dawa_hi` | hi (Devanagari) | near | near(5): [मारने] + [दवा \| दवाई] | synonym of `hh_poisoning`: a pest-killer product (चूहे/कीड़े मारने की दवा), how rat poison and insecticide are named in Hindi (Devanagari) |
| [ ] | `hh_keetnashak_hi` | hi (Devanagari) | phrase | कीटनाशक | synonym of `hh_poisoning`: pesticide, in Hindi (Devanagari) |
| [ ] | `hh_phenyl_hi` | hi (Devanagari) | phrase | फिनाइल | synonym of `hh_poisoning`: phenyl, a floor cleaner, in Hindi (Devanagari) |
| [ ] | `hh_overdose_hi` | hi (Devanagari) | phrase | ओवरडोज | synonym of `hh_overdose`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_zyada_dawa_hi` | hi (Devanagari) | near | near(5): [ज़्यादा \| बहुत] + [दवा \| दवाई \| गोली \| गोलियां] + [खा \| खाई \| ली] | synonym of `hh_too_many_pills`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_chest_near_rom` | hi (romanised) | near | near(5): [seene \| seena \| chhati \| chati \| chhaati \| chaati \| chest \| dil] + [dard \| dardh \| jakdan \| dabav \| dabaav] | synonym of `hh_chest_near`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17); code-mixed chest and dil too |
| [ ] | `hh_dil_ka_daura_rom` | hi (romanised) | phrase | dil ka daura | synonym of `hh_heart_attack`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_breathing_near_rom` | hi (romanised) | near | near(5): [saans \| sans \| saansein] + [takleef \| taklif \| dikkat \| pareshani \| phool \| ruk \| nahi \| nahin \| mushkil \| ghut] | synonym of `hh_breathing_near`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_lakwa_rom` | hi (romanised) | phrase | lakwa | synonym of `hh_lakwa_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_khoon_near_rom` | hi (romanised) | near | near(5): [khoon \| khun] + [beh \| bah \| ruk \| behta \| bahta \| nikal] | synonym of `hh_khoon_near_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_bleeding_band_rom` | hi (romanised) | near | near(5): [bleeding] + [band \| ruk \| ruki \| rukti \| nahi] | synonym of `hh_bleeding_wont_stop`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17): code-mixed bleeding that does not stop |
| [ ] | `hh_behosh_rom` | hi (romanised) | phrase | behosh | synonym of `hh_unconscious`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_hosh_nahi_rom` | hi (romanised) | phrase | hosh nahi | synonym of `hh_hosh_nahi_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_hosh_nahin_rom` | hi (romanised) | phrase | hosh nahin | synonym of `hh_hosh_nahi_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_daura_rom` | hi (romanised) | near | near(5): [daura \| daure] + [pada \| pad \| padte \| padne \| aaya] | synonym of `hh_seizure`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_swelling_rom` | hi (romanised) | near | near(5): [gala \| gale \| chehra \| chehre \| honth \| jeebh] + [sooj \| sooja \| soojan \| sujan \| suja \| sujh] | synonym of `hh_throat_swelling`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17), with the oblique gale and chehre |
| [ ] | `hh_zehar_rom` | hi (romanised) | phrase | zehar | synonym of `hh_poisoning`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_zeher_rom` | hi (romanised) | phrase | zeher | synonym of `hh_poisoning`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_zahar_rom` | hi (romanised) | phrase | zahar | synonym of `hh_poisoning`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17), the most common spelling |
| [ ] | `hh_jahar_rom` | hi (romanised) | phrase | jahar | synonym of `hh_poisoning`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_maarne_ki_dawa_rom` | hi (romanised) | near | near(5): [maarne \| marne] + [dawa \| dawai \| dava \| davai] | synonym of `hh_maarne_ki_dawa_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_zyada_dawa_rom` | hi (romanised) | near | near(5): [zyada \| jyada \| bahut] + [dawa \| dawai \| goli \| goliyan \| tablet \| tablets] + [kha \| khai \| li \| le] | synonym of `hh_zyada_dawa_hi`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |

### professional_judgement (43)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `hh_whats_wrong_with_me` | en | phrase | what's wrong with me | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_what_is_wrong_with_me` | en | phrase | what is wrong with me | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_what_do_you_think_it_is` | en | phrase | what do you think it is | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_could_it_be` | en | near | near(5): [could \| can \| might] + [it \| this \| that] + [be] + [infection \| condition \| disease \| allergy \| diabetes \| fracture \| dengue \| malaria \| typhoid \| asthma \| cancer \| migraine] | synonym of `hh_what_do_you_think_it_is`: asking whether a symptom could be a named illness |
| [ ] | `hh_symptom_mean` | en | near | near(5): [symptom \| symptoms] + [mean \| means \| meaning \| cause \| causes \| sign] | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_diagnose` | en | phrase | diagnose | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_diagnosis` | en | phrase | diagnosis | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_is_it_serious` | en | phrase | is it serious | §4.1: Judge how urgent a symptom is (beyond detecting emergencies) |
| [ ] | `hh_is_this_serious` | en | phrase | is this serious | synonym of `hh_is_it_serious`: the same question |
| [ ] | `hh_is_that_serious` | en | phrase | is that serious | synonym of `hh_is_it_serious`: the same question |
| [ ] | `hh_how_serious` | en | phrase | how serious | synonym of `hh_is_it_serious`: the same question |
| [ ] | `hh_is_it_normal_to` | en | near | near(5): [is] + [normal] + [have \| having \| feel \| feeling] | synonym of `hh_is_it_serious`: asking whether a symptom is normal |
| [ ] | `hh_should_i_be_worried` | en | phrase | should i be worried | §4.1: Judge how urgent a symptom is (beyond detecting emergencies) |
| [ ] | `hh_is_it_an_emergency` | en | phrase | is it an emergency | §4.1: Judge how urgent a symptom is (beyond detecting emergencies) |
| [ ] | `hh_should_i_go_to_the_hospital` | en | phrase | should i go to the hospital | §4.1: Judge how urgent a symptom is (beyond detecting emergencies) |
| [ ] | `hh_should_i_go_to_hospital` | en | phrase | should i go to hospital | synonym of `hh_should_i_go_to_the_hospital`: the same question without the article |
| [ ] | `hh_report_mean` | en | near | near(5): [report \| reports \| result \| results \| scan \| scans \| ray \| ecg \| mri] + [mean \| means \| normal \| interpret \| explain] | §4.1: Interpret test results, reports or images |
| [ ] | `hh_change_dose` | en | near | near(5): [dose \| dosage \| medicine \| medicines \| medication \| tablet \| tablets \| pills] + [change \| increase \| reduce \| stop \| skip \| double \| missed \| switch] | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_should_i_take` | en | phrase | should i take | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_take_medicine` | en | near | near(5): [take \| taking] + [paracetamol \| ibuprofen \| aspirin \| antibiotic \| antibiotics \| painkiller \| painkillers \| medicine \| medicines \| tablet \| tablets] | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_dose_amount` | en | near | near(8): [how] + [many \| much] + [tablet \| tablets \| pill \| pills \| syrup \| drops \| ml \| mg \| dose \| paracetamol \| ibuprofen \| capsules] + [give \| giving \| take \| taking] | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_which_medicine` | en | phrase | which medicine | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_what_medicine` | en | phrase | what medicine | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_stop_taking` | en | phrase | can i stop taking | §4.1: Recommend, change or dose any medication or treatment |
| [ ] | `hh_prognosis` | en | phrase | prognosis | §4.1: Give a prognosis or second opinion |
| [ ] | `hh_second_opinion` | en | phrase | second opinion | §4.1: Give a prognosis or second opinion |
| [ ] | `hh_will_i_recover` | en | phrase | will i recover | synonym of `hh_prognosis`: a prognosis asked plainly |
| [ ] | `hh_chances_of_recovery` | en | phrase | chances of recovery | synonym of `hh_prognosis`: a prognosis asked plainly |
| [ ] | `hh_dawa_badal_hi` | hi (Devanagari) | near | near(5): [दवा \| दवाई \| गोली \| खुराक] + [बढ़ा \| बढ़ाऊं \| घटा \| घटाऊं \| बदल \| बंद \| छोड़ \| कौन] | synonym of `hh_change_dose`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_dawa_ki_khurak_hi` | hi (Devanagari) | phrase | दवा की खुराक | synonym of `hh_change_dose`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_kitni_goli_hi` | hi (Devanagari) | near | near(6): [कितनी \| कितना \| कितने] + [गोली \| गोलियां \| दवा \| दवाई \| सिरप \| खुराक \| डोज] + [दूं \| दें \| दे \| लूं \| लें \| ले \| खिलाऊं \| खाऊं \| पिलाऊं] | synonym of `hh_dose_amount`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_kya_yah_gambhir_hi` | hi (Devanagari) | near | near(5): [गंभीर \| सीरियस] + [क्या] | synonym of `hh_is_it_serious`: the same meaning in Hindi (Devanagari), with यह, ये or no pronoun |
| [ ] | `hh_chinta_ki_baat_hi` | hi (Devanagari) | phrase | चिंता की बात | synonym of `hh_should_i_be_worried`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_report_matlab_hi` | hi (Devanagari) | near | near(5): [रिपोर्ट \| नतीजे \| रिजल्ट] + [मतलब \| सामान्य \| नॉर्मल \| समझा] | synonym of `hh_report_mean`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_kya_bimari_hi` | hi (Devanagari) | phrase | क्या बीमारी है | synonym of `hh_what_is_wrong_with_me`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_ho_sakta_hi` | hi (Devanagari) | near | near(5): [हो] + [सकता \| सकती] + [डेंगू \| मलेरिया \| टाइफाइड \| इन्फेक्शन \| संक्रमण \| बीमारी \| कैंसर \| शुगर \| टीबी] + [यह \| ये \| मुझे \| इसे \| उसे] | synonym of `hh_could_it_be`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_dawa_badal_rom` | hi (romanised) | near | near(5): [dawa \| dawai \| dava \| davai \| goli \| khurak \| dose] + [badha \| badhau \| badhaun \| kam \| band \| chhod \| chod \| kaunsi \| konsi] | synonym of `hh_change_dose`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_kitni_goli_rom` | hi (romanised) | near | near(6): [kitni \| kitna \| kitne] + [goli \| goliyan \| dawa \| dawai \| syrup \| khurak \| dose \| tablet \| tablets] + [doon \| dun \| du \| dein \| de \| loon \| lun \| lu \| lein \| le \| khilau] | synonym of `hh_dose_amount`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_report_matlab_rom` | hi (romanised) | near | near(5): [report \| reports \| result] + [matlab \| samjha \| samjhao] | synonym of `hh_report_mean`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_serious_kya_rom` | hi (romanised) | near | near(5): [serious \| gambhir] + [kya] | synonym of `hh_is_it_serious`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_kya_bimari_rom` | hi (romanised) | phrase | kya bimari hai | synonym of `hh_what_is_wrong_with_me`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_chinta_ki_baat_rom` | hi (romanised) | phrase | chinta ki baat | synonym of `hh_should_i_be_worried`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |
| [ ] | `hh_ho_sakta_rom` | hi (romanised) | near | near(5): [ho] + [sakta \| sakti] + [dengue \| malaria \| typhoid \| infection \| bimari \| cancer \| sugar \| tb \| jaundice \| pneumonia \| covid \| allergy] + [yeh \| ye \| yah \| mujhe \| use \| ise] | synonym of `hh_could_it_be`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |

### diagnosis (5)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `hh_might_have_condition` | en | near | near(4): [might \| probably \| likely] + [have \| has] + [infection \| condition \| disease \| fracture \| dengue \| malaria \| typhoid \| asthma \| cancer \| migraine \| flu \| pneumonia] | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_a_sign_of` | en | phrase | a sign of | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_a_symptom_of` | en | phrase | a symptom of | §4.1: Diagnose or suggest what a symptom might mean |
| [ ] | `hh_ka_lakshan_hi` | hi (Devanagari) | phrase | का लक्षण | synonym of `hh_a_symptom_of`: the same meaning in Hindi (Devanagari) |
| [ ] | `hh_ka_lakshan_rom` | hi (romanised) | phrase | ka lakshan | synonym of `hh_a_symptom_of`: the same meaning in romanised Hindi, matched as written (PR 2 decision, 2026-11-17) |

## `types/veterinary.json`

### emergency (63)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `vet_rat_poison` | en | phrase | rat poison | §4.2: suspected poisoning or toxic ingestion |
| [ ] | `vet_rat_killer` | en | phrase | rat killer | synonym of `vet_rat_poison`: the same product, as shops name it |
| [ ] | `vet_poisoning` | en | phrase | poisoning | §4.2: suspected poisoning or toxic ingestion |
| [ ] | `vet_poisoned` | en | phrase | poisoned | synonym of `vet_poisoning`: the same, as a verb |
| [ ] | `vet_ate_toxic` | en | near | near(7): [ate \| eaten \| eat \| eats \| eating \| swallowed \| chewed \| licked \| ingested \| drank] + [poison \| chocolate \| chocolates \| grapes \| grape \| raisins \| raisin \| lily \| lilies \| xylitol \| onion \| onions] | §4.2: suspected poisoning or toxic ingestion |
| [ ] | `vet_ate_medicine` | en | near | near(7): [ate \| eaten \| eat \| eats \| eating \| swallowed \| chewed \| licked \| ingested \| drank] + [medicine \| medicines \| pill \| pills \| tablet \| tablets \| paracetamol \| ibuprofen \| painkiller \| painkillers] | synonym of `vet_ate_toxic`: a human medicine taken in; an ingestion verb and its object within 7 words (the object can carry a determiner, a quantity and adjectives) |
| [ ] | `vet_ate_chemical` | en | near | near(7): [ate \| eaten \| eat \| eats \| eating \| swallowed \| chewed \| licked \| ingested \| drank] + [antifreeze \| rodenticide \| bleach \| pesticide \| insecticide \| detergent \| battery \| batteries] | synonym of `vet_ate_toxic`: a household chemical taken in; an ingestion verb and its object within 7 words (the object can carry a determiner, a quantity and adjectives) |
| [ ] | `vet_got_into` | en | near | near(5): [got] + [into] + [poison \| chocolate \| chocolates \| medicine \| medicines \| pills \| tablets \| antifreeze \| bleach \| xylitol \| grapes \| raisins] | synonym of `vet_ate_toxic`: got into a toxic product |
| [ ] | `vet_difficulty_breathing` | en | phrase | difficulty breathing | §4.2: difficulty breathing |
| [ ] | `vet_breathing_near` | en | near | near(5): [breathe \| breathes \| breathing \| breath] + [difficulty \| difficult \| trouble \| struggle \| struggles \| struggling \| hard \| problem \| problems] | synonym of `vet_difficulty_breathing`: a breathing word and a difficulty word within 5 words |
| [ ] | `vet_breathing_cannot` | en | near | near(5): [breathe \| breathes \| breathing \| breath] + [cannot \| cant \| couldnt \| unable \| stopped \| stopping \| gasping] | synonym of `vet_difficulty_breathing`: a breathing word and an inability word within 5 words |
| [ ] | `vet_cant_breathe` | en | phrase | can't breathe | synonym of `vet_difficulty_breathing`: the same, said plainly |
| [ ] | `vet_not_breathing` | en | phrase | not breathing | synonym of `vet_difficulty_breathing`: the same, said plainly |
| [ ] | `vet_choking` | en | phrase | choking | synonym of `vet_difficulty_breathing`: a blocked airway |
| [ ] | `vet_seizures` | en | phrase | seizures | §4.2: seizures |
| [ ] | `vet_seizure` | en | phrase | seizure | synonym of `vet_seizures`: the singular |
| [ ] | `vet_seizing` | en | phrase | seizing | synonym of `vet_seizures`: the same, as a verb |
| [ ] | `vet_convulsing` | en | phrase | convulsing | synonym of `vet_seizures`: another word for a seizure |
| [ ] | `vet_convulsions` | en | phrase | convulsions | synonym of `vet_seizures`: another word for seizures |
| [ ] | `vet_having_a_fit` | en | phrase | having a fit | synonym of `vet_seizures`: a seizure, said plainly |
| [ ] | `vet_had_a_fit` | en | phrase | had a fit | synonym of `vet_seizures`: a seizure, said plainly |
| [ ] | `vet_having_fits` | en | phrase | having fits | synonym of `vet_seizures`: a seizure, said plainly |
| [ ] | `vet_collapsed` | en | phrase | collapsed | synonym of `vet_seizures`: a sudden collapse, as urgent as a seizure |
| [ ] | `vet_collapsing` | en | phrase | collapsing | synonym of `vet_seizures`: a sudden collapse, as urgent as a seizure |
| [ ] | `vet_collapse` | en | phrase | collapse | synonym of `vet_collapsed`: the same, as a noun or verb |
| [ ] | `vet_collapses` | en | phrase | collapses | synonym of `vet_collapsed`: the same, as a verb |
| [ ] | `vet_unconscious` | en | phrase | unconscious | synonym of `vet_collapsed`: a collapse with loss of consciousness |
| [ ] | `vet_unresponsive` | en | phrase | unresponsive | synonym of `vet_collapsed`: a collapse with loss of consciousness |
| [ ] | `vet_fainted` | en | phrase | fainted | synonym of `vet_collapsed`: a brief collapse |
| [ ] | `vet_passed_out` | en | phrase | passed out | synonym of `vet_collapsed`: a brief collapse |
| [ ] | `vet_wont_wake_up` | en | phrase | won't wake up | synonym of `vet_collapsed`: a collapse with loss of consciousness, said plainly |
| [ ] | `vet_not_waking_up` | en | phrase | not waking up | synonym of `vet_collapsed`: a collapse with loss of consciousness, said plainly |
| [ ] | `vet_trauma` | en | phrase | trauma | §4.2: trauma |
| [ ] | `vet_hit_by_vehicle` | en | near | near(5): [hit \| struck] + [car \| vehicle \| bike \| truck \| scooter \| motorbike \| auto] | synonym of `vet_trauma`: an injury from a vehicle |
| [ ] | `vet_run_over` | en | near | near(5): [run \| ran] + [over] + [car \| vehicle \| bike \| scooter \| truck \| auto \| got \| was \| been] | synonym of `vet_trauma`: an injury from a vehicle (run over with a vehicle or a passive verb; not 'run over the fees') |
| [ ] | `vet_badly_hurt` | en | phrase | badly hurt | synonym of `vet_trauma`: a serious injury |
| [ ] | `vet_badly_injured` | en | phrase | badly injured | synonym of `vet_trauma`: a serious injury |
| [ ] | `vet_broken_bone` | en | phrase | broken bone | synonym of `vet_trauma`: a serious injury |
| [ ] | `vet_broken_leg` | en | phrase | broken leg | synonym of `vet_trauma`: a serious injury |
| [ ] | `vet_broken_near` | en | near | near(5): [broken \| broke \| fractured] + [leg \| legs \| paw \| bone \| bones \| tail \| jaw \| rib \| ribs \| hip] | synonym of `vet_trauma`: a broken bone in any word order |
| [ ] | `vet_fracture` | en | phrase | fracture | synonym of `vet_trauma`: a serious injury |
| [ ] | `vet_fell_from` | en | near | near(5): [fell \| fallen \| falling] + [balcony \| roof \| terrace \| building \| stairs \| height] | synonym of `vet_trauma`: a fall from a height |
| [ ] | `vet_bitten_badly` | en | near | near(5): [bitten \| attacked \| mauled] + [badly \| another \| stray \| snake \| deep \| bleeding] | synonym of `vet_trauma`: a bite or an attack by another animal |
| [ ] | `vet_bleeding_near` | en | near | near(5): [bleeding] + [heavily \| badly \| lot \| lots \| stop \| stopping] | synonym of `vet_trauma`: bleeding that is heavy or does not stop |
| [ ] | `vet_bloat` | en | phrase | bloat | §4.2: signs of bloat |
| [ ] | `vet_bloated` | en | phrase | bloated | synonym of `vet_bloat`: the same, as an adjective |
| [ ] | `vet_swollen_belly` | en | near | near(5): [belly \| stomach \| tummy \| abdomen] + [swollen \| swelling \| distended] | synonym of `vet_bloat`: a swollen belly |
| [ ] | `vet_hard_belly` | en | phrase | hard belly | synonym of `vet_bloat`: a hard belly |
| [ ] | `vet_belly_is_hard` | en | phrase | belly is hard | synonym of `vet_bloat`: a hard belly |
| [ ] | `vet_stomach_is_hard` | en | phrase | stomach is hard | synonym of `vet_bloat`: a hard belly |
| [ ] | `vet_tummy_is_hard` | en | phrase | tummy is hard | synonym of `vet_bloat`: a hard belly |
| [ ] | `vet_trying_to_vomit` | en | phrase | trying to vomit | synonym of `vet_bloat`: retching without bringing anything up |
| [ ] | `vet_trying_to_throw_up` | en | phrase | trying to throw up | synonym of `vet_bloat`: retching without bringing anything up |
| [ ] | `vet_retching` | en | phrase | retching | synonym of `vet_bloat`: retching without bringing anything up |
| [ ] | `vet_dry_heaving` | en | phrase | dry heaving | synonym of `vet_bloat`: retching without bringing anything up |
| [ ] | `vet_difficult_birth` | en | phrase | difficult birth | §4.2: difficult birth |
| [ ] | `vet_birth_near` | en | near | near(6): [birth \| labour \| labor \| whelping \| delivering] + [trouble \| difficult \| difficulty \| stuck \| struggling \| straining] | synonym of `vet_difficult_birth`: a birth word and a difficulty word within 6 words |
| [ ] | `vet_stuck_puppy` | en | near | near(5): [puppy \| kitten \| puppies \| kittens] + [stuck] | synonym of `vet_difficult_birth`: a newborn stuck during birth |
| [ ] | `vet_heatstroke` | en | phrase | heatstroke | §4.2: heatstroke |
| [ ] | `vet_heat_stroke` | en | phrase | heat stroke | synonym of `vet_heatstroke`: the same, as two words |
| [ ] | `vet_overheated` | en | phrase | overheated | synonym of `vet_heatstroke`: overheating |
| [ ] | `vet_overheating` | en | phrase | overheating | synonym of `vet_heatstroke`: overheating |
| [ ] | `vet_panting_heavily` | en | near | near(5): [panting] + [heavily \| heavy \| hard \| nonstop \| badly] | synonym of `vet_heatstroke`: heavy panting in the heat |

### professional_judgement (15)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `vet_whats_wrong_with_my` | en | phrase | what's wrong with my | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_what_is_wrong_with_my` | en | phrase | what is wrong with my | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_what_do_you_think_it_is` | en | phrase | what do you think it is | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_pet_symptom` | en | near | near(5): [dog \| cat \| pet \| puppy \| kitten \| rabbit] + [vomiting \| limping \| coughing \| sneezing \| diarrhea \| diarrhoea \| lethargic \| drooling \| shaking \| wheezing] | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_diagnose` | en | phrase | diagnose | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_dose_by_weight` | en | near | near(5): [dose \| dosage \| paracetamol \| ibuprofen \| aspirin \| medicine \| medication] + [give \| giving \| weight \| weighs \| kg] | §4.2: Recommend or dose medication — including dosing by weight beyond the product label |
| [ ] | `vet_how_many_tablets` | en | near | near(5): [how] + [many] + [tablet \| tablets \| pill \| pills \| drops \| ml \| mg] | synonym of `vet_dose_by_weight`: dosing asked as a count |
| [ ] | `vet_dose_amount` | en | near | near(8): [how] + [many \| much] + [tablet \| tablets \| pill \| pills \| syrup \| drops \| liquid \| ml \| mg \| dose \| injection \| paste] + [give \| giving] | synonym of `vet_dose_by_weight`: an amount of any medicine form to give |
| [ ] | `vet_how_much_should_i_give` | en | phrase | how much should i give | §4.2: Recommend or dose medication — including dosing by weight beyond the product label |
| [ ] | `vet_prescription` | en | near | near(5): [prescription] + [diet \| diets \| food \| without \| only] | §4.2: Advise on prescription-only items or prescription diets without vet authorisation |
| [ ] | `vet_is_it_serious` | en | phrase | is it serious | §4.2: Judge whether something is urgent (beyond detecting emergencies) |
| [ ] | `vet_is_this_serious` | en | phrase | is this serious | synonym of `vet_is_it_serious`: the same question |
| [ ] | `vet_should_i_be_worried` | en | phrase | should i be worried | §4.2: Judge whether something is urgent (beyond detecting emergencies) |
| [ ] | `vet_is_it_an_emergency` | en | phrase | is it an emergency | §4.2: Judge whether something is urgent (beyond detecting emergencies) |
| [ ] | `vet_can_it_wait` | en | phrase | can it wait | §4.2: Judge whether something is urgent (beyond detecting emergencies) |

### diagnosis (3)

| ✓ | Id | Language | Kind | Text | Source |
|---|---|---|---|---|---|
| [ ] | `vet_pet_probably_has` | en | near | near(4): [dog \| cat \| pet \| puppy \| kitten \| rabbit] + [probably \| likely] + [has] | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_a_sign_of` | en | phrase | a sign of | §4.2: Diagnose an animal or interpret its symptoms |
| [ ] | `vet_a_symptom_of` | en | phrase | a symptom of | §4.2: Diagnose an animal or interpret its symptoms |
