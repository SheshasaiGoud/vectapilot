/**
 * Telugu chrome. Drafted by Claude (sofia-frontend, M8 PR 4); **not reviewed by a native
 * speaker** (D9 (a)): the widget shows `translationUnreviewed` until a named reviewer signs off
 * (the plan's open question 2). Safety text is never translated here (`safety.ts`).
 */

import type { Dictionary } from "./en.ts";

export const te: Dictionary = {
  loading: "చాట్ లోడ్ అవుతోంది…",
  unavailable: "చాట్ ఇప్పుడు అందుబాటులో లేదు.",
  unavailableHelp: "దయచేసి వ్యాపారాన్ని వేరే మార్గంలో సంప్రదించండి.",
  retryLoad: "మళ్లీ ప్రయత్నించండి",
  safetyInEnglish: "భద్రతా సందేశాలు ఆంగ్లంలో చూపబడతాయి.",
  translationUnreviewed: "ఈ అనువాదాన్ని ఇంకా మాతృభాష మాట్లాడేవారు సమీక్షించలేదు.",
  conversation: "సంభాషణ",
  suggestions: "సూచనలు",
  messageLabel: "మీ సందేశం",
  send: "పంపండి",
  counter: "{max} లో {count} అక్షరాలు",
  overLimit: "పంపడానికి చాలా పొడవుగా ఉంది: {over} అక్షరాలు తీసివేయండి.",
  working: "సహాయకుడి సమాధానం కోసం వేచి ఉంది…",
  you: "మీరు",
  sources: "మూలాలు",
  page: "పేజీ {page}",
  staffReply: "{name} సిబ్బంది నుండి సమాధానం",
  noAnswer: "{name} సమాచారంలో దీనికి సమాధానం దొరకలేదు.",
  notAnswered: "సహాయకుడు ఈ సందేశానికి సమాధానం ఇవ్వలేదు.",
  notCoveredLanguage: "సహాయకుడు ఈ భాషలో రాసిన సందేశాలను ఇంకా చదవలేడు.",
  contactHeading: "{name}ను సంప్రదించడానికి ఇతర మార్గాలు",
  networkError: "చాట్‌తో కనెక్షన్ తెగిపోయింది.",
  timeout: "సమయానికి సమాధానం రాలేదు.",
  mayStillBeProcessed: "మీ చివరి సందేశం ఇంకా ప్రాసెస్ అవుతూ ఉండవచ్చు.",
  sessionExpired: "ఈ చాట్ సెషన్ ముగిసింది. మీ తదుపరి సందేశం కొత్త సెషన్‌ను ప్రారంభిస్తుంది.",
  invalidMessage: "ఈ సందేశాన్ని పంపలేకపోయాం. దాన్ని తనిఖీ చేసి, మళ్లీ పంపండి.",
  rateLimited: "తక్కువ సమయంలో చాలా ఎక్కువ సందేశాలు వచ్చాయి.",
  retryIn: "మీరు {seconds} సెకన్లలో మళ్లీ పంపవచ్చు.",
  canSendAgain: "ఇప్పుడు మీరు మళ్లీ పంపవచ్చు.",
  serverError: "మా వైపు ఏదో పొరపాటు జరిగింది, ఈ సందేశానికి సమాధానం ఇవ్వలేదు.",
  sessionUnavailable: "చాట్ ఇప్పుడు ప్రారంభం కాదు.",
  messageKept: "మీ సందేశం ఇంకా కింది బాక్స్‌లో ఉంది.",
  notSent: "పంపబడలేదు",
  mayBeProcessed: "ఇంకా ప్రాసెస్ అవుతూ ఉండవచ్చు",
  endChat: "చాట్ ముగించండి",
  ended: "చాట్ ముగిసింది. సంభాషణ ఈ పేజీ నుండి తొలగించబడింది.",
  startNew: "కొత్త చాట్ ప్రారంభించండి",
  inEnglish: "ఆంగ్లంలో",
};
