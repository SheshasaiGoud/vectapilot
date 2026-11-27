/**
 * Hindi chrome. Drafted by Claude (sofia-frontend, M8 PR 4); **not reviewed by a native speaker**
 * (D9 (a)): the widget shows `translationUnreviewed` until a named reviewer signs off (the plan's
 * open question 2). Safety text is never translated here (`safety.ts`).
 */

import type { Dictionary } from "./en.ts";

export const hi: Dictionary = {
  loading: "चैट लोड हो रही है…",
  unavailable: "चैट अभी उपलब्ध नहीं है।",
  unavailableHelp: "कृपया व्यवसाय से किसी दूसरे तरीके से संपर्क करें।",
  retryLoad: "फिर से कोशिश करें",
  safetyInEnglish: "सुरक्षा संदेश अंग्रेज़ी में दिखाए जाते हैं।",
  translationUnreviewed: "इस अनुवाद की अभी किसी मूल भाषी ने समीक्षा नहीं की है।",
  conversation: "बातचीत",
  suggestions: "सुझाव",
  messageLabel: "आपका संदेश",
  send: "भेजें",
  counter: "{max} में से {count} अक्षर",
  overLimit: "भेजने के लिए बहुत लंबा है: {over} अक्षर हटाएँ।",
  working: "सहायक के जवाब का इंतज़ार है…",
  you: "आप",
  sources: "स्रोत",
  page: "पृष्ठ {page}",
  staffReply: "{name} के स्टाफ़ का जवाब",
  noAnswer: "{name} की जानकारी में इसका जवाब नहीं मिला।",
  notAnswered: "सहायक ने इस संदेश का जवाब नहीं दिया।",
  notCoveredLanguage: "सहायक अभी इस भाषा में लिखे संदेश नहीं पढ़ सकता।",
  contactHeading: "{name} से संपर्क के दूसरे तरीके",
  networkError: "चैट से कनेक्शन टूट गया।",
  timeout: "समय पर कोई जवाब नहीं आया।",
  mayStillBeProcessed: "हो सकता है आपके पिछले संदेश पर अभी भी काम हो रहा हो।",
  sessionExpired: "यह चैट सत्र खत्म हो गया है। आपका अगला संदेश नया सत्र शुरू करेगा।",
  invalidMessage: "यह संदेश नहीं भेजा जा सका। इसे जाँचें, फिर दोबारा भेजें।",
  rateLimited: "कम समय में बहुत ज़्यादा संदेश भेजे गए।",
  retryIn: "आप {seconds} सेकंड में फिर से भेज सकते हैं।",
  canSendAgain: "अब आप फिर से भेज सकते हैं।",
  serverError: "हमारी तरफ़ कुछ गड़बड़ हो गई, और इस संदेश का जवाब नहीं दिया गया।",
  sessionUnavailable: "चैट अभी शुरू नहीं हो सकती।",
  messageKept: "आपका संदेश नीचे वाले बॉक्स में अभी भी है।",
  notSent: "नहीं भेजा गया",
  mayBeProcessed: "शायद अभी भी प्रोसेस हो रहा है",
  endChat: "चैट खत्म करें",
  ended: "चैट खत्म हो गई। बातचीत इस पेज से हटा दी गई।",
  startNew: "नई चैट शुरू करें",
  inEnglish: "अंग्रेज़ी में",
};
