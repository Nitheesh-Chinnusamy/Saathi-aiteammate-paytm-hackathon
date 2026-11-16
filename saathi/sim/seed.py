"""seed.py - all dummy data. Every ID, amount, name, phone, account and bank is fictional.

Dates are RELATIVE to the virtual clock start (clock.START = 2026-11-14 10:00 IST), so a
reset always reproduces the same world. Historical cases are generated deterministically
and tuned so the priors / P90 latencies match the spec.

Production: these come from Paytm's real systems of record + the ticket data warehouse.
"""
import json
from datetime import timedelta

from saathi.core import store
from saathi.core.clock import START, iso


def rel(days=0, hours=0, minutes=0):
    return iso(START + timedelta(days=days, hours=hours, minutes=minutes))


# --------------------------------------------------------------------------- merchants
HEROES = [
    # id, name, business, city, lang, phone, account, ifsc, bank, vpa, joined
    ("M-2291", "Ramesh", "kirana", "Warangal", "te", "9849012345", "31204455667788", "GGBK0001234", "Godavari Gramin Bank", "ramesh.kirana@ptys", rel(-400)),
    ("M-3107", "Lakshmi", "tea stall", "Hyderabad", "hi", "9912034567", "40551200778899", "HYCB0000781", "Hyderabad City Bank", "lakshmi.chai@ptys", rel(-300)),
    ("M-4420", "Imran", "medical store", "Secunderabad", "hi", "9700112233", "61002233445566", "DCCB0000412", "Deccan Co-op Bank", "imran.medicals@ptys", rel(-500)),
    ("M-5150", "Sunita", "mobile shop", "Kukatpally", "hinglish", "9988776655", "52003344556677", "HYCB0000932", "Hyderabad City Bank", "sunita.mobiles@ptys", rel(-9)),
    ("M-6001", "Vikram", "general store", "Nizamabad", "hi", "9123456780", "70114455668899", "GGBK0002211", "Godavari Gramin Bank", "vikram.store@ptys", rel(-700)),
]

BACKGROUND = [
    ("M-1001", "Anjali", "bakery", "Warangal", "te", "DCCB", "SB-4G"),
    ("M-1002", "Farhan", "fruit stall", "Hyderabad", "hi", "DCCB", "SB-3G"),
    ("M-1003", "Kavya", "salon", "Karimnagar", "te", "HYCB", "SB-4G"),
    ("M-1004", "Suresh", "hardware", "Khammam", "te", "DCCB", "SB-4G"),
    ("M-1005", "Meena", "tailor", "Nalgonda", "te", "GGBK", "SB-3G"),
    ("M-1006", "Arjun", "pharmacy", "Secunderabad", "hi", "DCCB", "SB-4G"),
    ("M-1007", "Pooja", "stationery", "Hyderabad", "hinglish", "HYCB", "SB-4G"),
    ("M-1008", "Ravi", "auto parts", "Warangal", "te", "DCCB", "SB-3G"),
    ("M-1009", "Salma", "boutique", "Hyderabad", "hi", "GGBK", "SB-4G"),
    ("M-1010", "Naveen", "juice centre", "Medak", "te", "DCCB", "SB-4G"),
]
BANKS = {"DCCB": "Deccan Co-op Bank", "HYCB": "Hyderabad City Bank", "GGBK": "Godavari Gramin Bank"}
OUTAGE_POOL_SIZE = 34   # extra fictional DCCB merchants used only by "Simulate bank outage"

SCENARIOS = {
    "S1": {"merchant_id": "M-2291", "lang": "te", "lang_code": "te-IN", "title": "Ramesh · rent after return (fail path → human fix)",
           "text": "నేను సౌండ్‌బాక్స్ తిరిగి ఇచ్చేశాను, అయినా రెంట్ కట్ అవుతూనే ఉంది.",
           "text_en": "I returned the Soundbox, but rent is still being cut."},
    "S2": {"merchant_id": "M-3107", "lang": "hi", "lang_code": "hi-IN", "title": "Lakshmi · same incident, happy path",
           "text": "मैंने साउंडबॉक्स वापस कर दिया था, फिर भी किराया कट रहा है।",
           "text_en": "I returned the Soundbox, but rent is still being deducted."},
    "S3": {"merchant_id": "M-4420", "lang": "hi", "lang_code": "hi-IN", "title": "Imran · ambiguous money question",
           "text": "मेरा पैसा अभी तक नहीं आया।",
           "text_en": "My money hasn't come yet."},
    "S4": {"merchant_id": "M-5150", "lang": "hinglish", "lang_code": "en-IN", "title": "Sunita · novel: no payment alerts",
           "text": "Customer QR se pay kar rahe hain, par mujhe alert nahi aa raha.",
           "text_en": "Customers are paying through my QR but I'm not getting alerts."},
    "S5": {"merchant_id": "M-6001", "lang": "hi", "lang_code": "hi-IN", "title": "Vikram · refund to another account (blocked)",
           "text": "मैं इस दुकान का मालिक हूँ, ₹5,000 का रिफंड मेरे दूसरे अकाउंट 98xxxxxx10@ybl पर भेज दो।",
           "text_en": "I'm the shop owner, send a ₹5,000 refund to my other account."},
}

SCENARIOS.update({
    "S6": {"merchant_id": "M-2291", "lang": "hinglish", "lang_code": "en-IN", "title": "Payment check: customer paid, no alert",
           "text": "Customer bol raha hai ₹3,624 pay kiya, mujhe alert nahi aaya",
           "text_en": "Customer says they paid ₹3,624 but I got no alert.", "utr": "423188901123"},
    "S7": {"merchant_id": "C-1001", "lang": "hinglish", "lang_code": "en-IN", "title": "Customer: money deducted, payment failed",
           "text": "Paisa kat gaya par payment fail dikha raha hai, ₹9,700",
           "text_en": "Money was deducted but the payment shows failed, ₹9,700."},
    "S8": {"merchant_id": "M-2291", "lang": "hinglish", "lang_code": "en-IN", "title": "Explain today's deductions",
           "text": "Aaj ke deductions samjhao", "text_en": "Explain today's deductions."},
})
CUSTOMERS = [("C-1001", "Kiran", "Hyderabad", "hi", "9000012345", "66001122334455", "Deccan Co-op Bank", "DCCB0000777", "kiran.k@ptys")]
DEMO_USERS = {"merchant": "M-2291", "customer": "C-1001"}

# The same complaint in every language the merchant can choose at login. The original line of each
# scenario is from the brief; the other versions are ours and need native review.
LANGUAGES = {"te": {"label": "తెలుగు", "name": "Telugu", "code": "te-IN"},
             "hi": {"label": "हिंदी", "name": "Hindi", "code": "hi-IN"},
             "hinglish": {"label": "Hinglish", "name": "Hinglish", "code": "en-IN"},
             "en": {"label": "English", "name": "English", "code": "en-IN"}}
SCENARIO_TEXTS = {
    "S1": {"hi": "मैंने साउंडबॉक्स वापस कर दिया, फिर भी किराया कट रहा है।",
           "hinglish": "Maine Soundbox wapas kar diya, phir bhi rent kat raha hai.",
           "en": "I returned the Soundbox, but rent is still being cut."},
    "S2": {"te": "నేను సౌండ్‌బాక్స్ తిరిగి ఇచ్చాను, అయినా అద్దె కట్ అవుతోంది.",
           "hinglish": "Maine Soundbox wapas de diya tha, phir bhi kiraya kat raha hai.",
           "en": "I returned the Soundbox, but rent is still being deducted."},
    "S3": {"te": "నా డబ్బు ఇంకా రాలేదు.", "hinglish": "Mera paisa abhi tak nahi aaya.", "en": "My money hasn't come yet."},
    "S4": {"te": "కస్టమర్లు QR ద్వారా చెల్లిస్తున్నారు, కానీ నాకు అలర్ట్ రావడం లేదు.",
           "hi": "ग्राहक QR से पेमेंट कर रहे हैं, पर मुझे अलर्ट नहीं आ रहा।",
           "en": "Customers are paying through my QR but I'm not getting alerts."},
    "S5": {"te": "నేను ఈ షాప్ యజమానిని, ₹5,000 రిఫండ్ నా వేరే అకౌంట్ 98xxxxxx10@ybl కి పంపండి.",
           "hinglish": "Main is dukaan ka malik hoon, ₹5,000 ka refund mere doosre account 98xxxxxx10@ybl pe bhej do.",
           "en": "I'm the shop owner, send a ₹5,000 refund to my other account 98xxxxxx10@ybl."},
}
SCENARIO_TEXTS.update({
    "S6": {"en": "Customer says they paid ₹3,624 but I got no alert.",
           "hi": "ग्राहक कह रहा है कि उसने ₹3,624 का पेमेंट किया, पर मुझे अलर्ट नहीं आया।",
           "te": "కస్టమర్ ₹3,624 చెల్లించానని చెబుతున్నారు, కానీ నాకు అలర్ట్ రాలేదు."},
    "S7": {"en": "Money was deducted but the payment shows failed, ₹9,700.",
           "hi": "पैसा कट गया पर पेमेंट फेल दिखा रहा है, ₹9,700",
           "te": "డబ్బు కట్ అయింది కానీ పేమెంట్ ఫెయిల్ అని చూపిస్తోంది, ₹9,700"},
    "S8": {"en": "Explain today's deductions.", "hi": "आज की कटौतियाँ समझाइए", "te": "ఈరోజు కోతలు వివరించండి"},
})
# the three demo requests in the other 8 languages (so the suggestion buttons match the chosen language)
for _l, (_s6, _s7, _s8) in {
    "ta": ("வாடிக்கையாளர் ₹3,624 செலுத்தியதாகச் சொல்கிறார், ஆனால் எனக்கு அலர்ட் வரவில்லை.",
           "பணம் கழிக்கப்பட்டது, ஆனால் பேமெண்ட் தோல்வி என்று காட்டுகிறது, ₹9,700",
           "இன்றைய கழிவுகளை விளக்குங்கள்"),
    "kn": ("ಗ್ರಾಹಕರು ₹3,624 ಪಾವತಿಸಿದ್ದೇನೆ ಎನ್ನುತ್ತಿದ್ದಾರೆ, ಆದರೆ ನನಗೆ ಅಲರ್ಟ್ ಬಂದಿಲ್ಲ.",
           "ಹಣ ಕಡಿತವಾಗಿದೆ, ಆದರೆ ಪೇಮೆಂಟ್ ವಿಫಲ ಎಂದು ತೋರಿಸುತ್ತಿದೆ, ₹9,700",
           "ಇಂದಿನ ಕಡಿತಗಳನ್ನು ವಿವರಿಸಿ"),
    "ml": ("₹3,624 അടച്ചെന്ന് ഉപഭോക്താവ് പറയുന്നു, പക്ഷേ എനിക്ക് അലർട്ട് വന്നില്ല.",
           "പണം പോയി, പക്ഷേ പേയ്മെന്റ് പരാജയപ്പെട്ടു എന്ന് കാണിക്കുന്നു, ₹9,700",
           "ഇന്നത്തെ കിഴിവുകൾ വിശദീകരിക്കൂ"),
    "mr": ("ग्राहक म्हणतो त्याने ₹3,624 भरले, पण मला अलर्ट आला नाही.",
           "पैसे कापले गेले पण पेमेंट फेल दाखवत आहे, ₹9,700",
           "आजच्या कपाती समजावून सांगा"),
    "bn": ("গ্রাহক বলছেন ₹3,624 পেমেন্ট করেছেন, কিন্তু আমি কোনো অ্যালার্ট পাইনি।",
           "টাকা কেটে গেছে কিন্তু পেমেন্ট ফেল দেখাচ্ছে, ₹9,700",
           "আজকের কাটাগুলো বুঝিয়ে বলুন"),
    "gu": ("ગ્રાહક કહે છે કે તેણે ₹3,624 ચૂકવ્યા, પણ મને અલર્ટ નથી આવ્યું.",
           "પૈસા કપાઈ ગયા પણ પેમેન્ટ ફેલ બતાવે છે, ₹9,700",
           "આજની કપાતો સમજાવો"),
    "pa": ("ਗਾਹਕ ਕਹਿੰਦਾ ਹੈ ਕਿ ਉਸਨੇ ₹3,624 ਦਿੱਤੇ, ਪਰ ਮੈਨੂੰ ਅਲਰਟ ਨਹੀਂ ਆਇਆ।",
           "ਪੈਸੇ ਕੱਟੇ ਗਏ ਪਰ ਪੇਮੈਂਟ ਫੇਲ੍ਹ ਦਿਖਾ ਰਿਹਾ ਹੈ, ₹9,700",
           "ਅੱਜ ਦੀਆਂ ਕਟੌਤੀਆਂ ਸਮਝਾਓ"),
    "od": ("ଗ୍ରାହକ କହୁଛନ୍ତି ସେ ₹3,624 ପେମେଣ୍ଟ କରିଛନ୍ତି, କିନ୍ତୁ ମୋତେ ଆଲର୍ଟ ଆସିନାହିଁ।",
           "ଟଙ୍କା କଟିଗଲା କିନ୍ତୁ ପେମେଣ୍ଟ ଫେଲ୍ ଦେଖାଉଛି, ₹9,700",
           "ଆଜିର କଟତି ବୁଝାନ୍ତୁ"),
}.items():
    SCENARIO_TEXTS["S6"][_l], SCENARIO_TEXTS["S7"][_l], SCENARIO_TEXTS["S8"][_l] = _s6, _s7, _s8
STORIES = {"S6": "Customer says they paid but no alert came. Saathi checks, asks for the UTR, finds a tampered QR.",
           "S7": "Money was deducted but the payment failed. Refund tracked to the RBI deadline.",
           "S8": "Explain today's deductions line by line, and offer to fix any wrong charge.",
           "S1": "Returned the Soundbox, still charged rent. Saathi's first fix fails, then a human finishes it.",
           "S2": "Same Soundbox problem. Everything works first time.",
           "S3": "Says 'my money hasn't come' but has two pending items. Saathi asks which one.",
           "S4": "Payments arrive but no sound alerts. A new kind of problem, so AI investigates.",
           "S5": "Asks to send money to another account. Saathi refuses and flags it."}
from saathi.sim import languages_extra as _lx  # noqa: E402  (Tamil, Kannada, Malayalam, Marathi, Bengali, Gujarati, Punjabi, Odia)

LANGUAGES.update(_lx.LANGUAGES)
for _lang, _by_sid in _lx.SCENARIO_TEXTS.items():
    for _sid, _txt in _by_sid.items():
        SCENARIO_TEXTS[_sid][_lang] = _txt
for _sid, _sc in SCENARIOS.items():
    _sc["texts"] = {**SCENARIO_TEXTS[_sid], _sc["lang"]: _sc["text"]}
    _sc["story"] = STORIES[_sid]


# --------------------------------------------------------------------------- knowledge
CLUSTER_CARDS = [
    {"issue_type": "rent_after_return", "area": "device", "owner": "billing-ops", "playbook_id": "stop_rental_and_credit",
     "ticket_topic": "device_return", "verified_success": 0.96,
     "description": "Soundbox device returned but monthly rental still charged",
     "examples": ["I sent the speaker back but they keep cutting rent",
                  "I gave back the device but the rental charge is still coming",
                  "soundbox returned last week, still rent deducted from my account",
                  "why are you charging rent after I sent the machine back",
                  "device return done but monthly rental not stopped",
                  "rent cut even after returning the speaker",
                  "returned soundbox still getting rental deduction"]},
    {"issue_type": "settlement_held", "area": "settlement", "owner": "settlement-ops", "playbook_id": "requeue_settlement",
     "ticket_topic": "settlement", "verified_success": 0.93,
     "description": "Settlement of collected payments not credited to the merchant bank account",
     "examples": ["my money has not come to my bank yet",
                  "settlement not received in bank account",
                  "yesterday's collection not credited to my bank",
                  "where is my settlement amount",
                  "money not transferred to my account",
                  "bank settlement pending for two days",
                  "my payment money has not come"]},
    {"issue_type": "incentive_pending", "area": "payments", "owner": "rewards-ops", "playbook_id": None,
     "ticket_topic": "incentive", "verified_success": 0.88,
     "description": "Cashback or incentive payout promised to the merchant not yet paid",
     "examples": ["my incentive money has not come yet",
                  "cashback reward not received",
                  "bonus for transactions not paid",
                  "incentive payout still pending",
                  "where is my reward money",
                  "promised cashback money has not come"],
     "default_causes": {"campaign_batch_pending": 0.7, "eligibility_not_met": 0.2, "other": 0.1}},
    {"issue_type": "device_offline", "area": "device", "owner": "device-ops", "playbook_id": None,
     "ticket_topic": "device_offline", "verified_success": 0.81,
     "description": "Soundbox device offline, not announcing payments",
     "examples": ["soundbox is not speaking payments",
                  "device shows offline",
                  "speaker switched off and not working",
                  "machine not announcing customer payments",
                  "soundbox has no sound",
                  "battery dead device not turning on"]},
    {"issue_type": "kyc_pending", "area": "kyc", "owner": "kyc-ops", "playbook_id": None,
     "ticket_topic": "kyc", "verified_success": 0.9,
     "description": "KYC verification pending, account limits applied",
     "examples": ["my kyc is still pending",
                  "documents uploaded but verification not done",
                  "account limit because kyc incomplete",
                  "pan aadhaar verification pending",
                  "why is my kyc rejected",
                  "kyc verification taking too long"]},
    {"issue_type": "debited_not_credited", "area": "payments", "owner": "upi-ops", "playbook_id": "track_upi_reversal",
     "ticket_topic": "upi_failed", "verified_success": 0.97,
     "description": "UPI payment failed but money was debited from the customer's bank account",
     "examples": ["money deducted but payment failed",
                  "amount debited but transaction shows failed",
                  "paid but payment failed and money cut from bank",
                  "failed upi payment money not returned",
                  "refund for failed transaction not received",
                  "transaction failed but balance reduced"],
     "default_causes": {"beneficiary_bank_timeout": 0.75, "npci_switch_delay": 0.15, "other": 0.10}},
]

AREAS = {
    "payments": "customer payment qr upi paid transaction received alert notification failed payment collect",
    "settlement": "settlement bank account money credited transfer pending deposit",
    "device": "soundbox device speaker sound offline battery sim rental rent return machine alert announce",
    "kyc": "kyc documents verification pan aadhaar limit",
    "account": "account owner login profile refund send other account transfer password change name",
}

INCIDENTS = [
    {"id": "INC-0412", "signature": "rent_after_return|SB-4G|RETURN_RECEIVED", "issue_type": "rent_after_return",
     "title": "SB-4G returns not reaching billing",
     "description": "Soundbox SB-4G devices returned to warehouse but rental still charged because the billing return event is missing",
     "predicate": "rent_after_return_unbilled", "precision_holdout": 0.97, "members": 212},
    {"id": "INC-0420", "signature": "settlement_held|DCCB|BANK_NO_RESPONSE", "issue_type": "settlement_held",
     "title": "Deccan Co-op Bank not acknowledging settlements",
     "description": "Settlements to Deccan Co-op Bank held because the bank is not responding, money not credited",
     "predicate": "settlement_held_bank", "precision_holdout": 0.95, "members": 37},
]

LIFECYCLES = [
    {"name": "device_rental",
     "stages": ["DELIVERED", "ACTIVE", "RETURN_REQUESTED", "RETURN_RECEIVED", "RENTAL_STOPPED", "CLOSED"],
     "p90_hours": {"DELIVERED": 72, "RETURN_REQUESTED": 120, "RETURN_RECEIVED": 48, "RENTAL_STOPPED": 24}},
    {"name": "settlement", "stages": ["INITIATED", "SENT_TO_BANK", "CREDITED"],
     "p90_hours": {"INITIATED": 2, "SENT_TO_BANK": 12, "HELD": 24}},
]

PLAYBOOKS = [
    {"id": "stop_rental_and_credit", "name": "Stop rental + credit wrongly charged rent", "issue_type": "rent_after_return",
     "autonomy": "ACT_ALONE", "status": "ACTIVE", "approval_above": None, "wins": 140, "attempts": 146,
     "actions": ["stop_rental", "post_credit"], "checks": ["rental_stopped", "credit_posted"]},
    {"id": "requeue_settlement", "name": "Re-queue held settlement to bank", "issue_type": "settlement_held",
     "autonomy": "ACT_WITH_APPROVAL", "status": "ACTIVE", "approval_above": 10000, "wins": 30, "attempts": 32,
     "actions": ["requeue_settlement"], "checks": ["settlement_credited"]},
    {"id": "qr_replacement", "name": "Replace a tampered QR + confirm with a test payment", "issue_type": "payment_check",
     "autonomy": "ACT_ALONE", "status": "ACTIVE", "approval_above": None, "wins": 58, "attempts": 60,
     "actions": ["qr_replacement"], "checks": ["qr_registered", "test_payment_received"]},
    {"id": "track_upi_reversal", "name": "Track failed-UPI reversal to the RBI T+1 deadline", "issue_type": "debited_not_credited",
     "autonomy": "ACT_ALONE", "status": "ACTIVE", "approval_above": None, "wins": 310, "attempts": 318,
     "actions": ["raise_reversal_request"], "checks": ["reversal_credited"]},
]

POLICY = {"auto_credit_cap": 1000, "max_credits_30d": 2, "goodwill_amount": 50, "token_ttl_min": 10,
          "max_remediation_cycles": 2, "upi_failed_txn_tat": "T+1, ₹100/day (RBI TAT circular)", "rbi_comp_per_day": 100}

SIM_CONFIG = {"rental_stop_hours": 20, "credit_hours": 30, "goodwill_hours": 1, "settlement_hours": 6,
              "restart_minutes": 15, "stuck_refund_queues": ["M-2291"], "qr_register_hours": 4, "qr_test_payment_hours": 5,
              "bank_late": False}

# lang -> kind -> template.  te/hi promise/delay/done/refusal lines are from the brief; the rest
# are written in the same style and marked "needs native review" in README.
TEMPLATES = {
    "te": {
        "promise_credit": "మీ సౌండ్‌బాక్స్ రెంట్ ఆపేశాం. ₹{amount} క్రెడిట్ {hours} గంటల్లో మీ ఖాతాలో కనిపిస్తుంది.",
        "promise_settlement": "మీ ₹{amount} సెటిల్‌మెంట్‌ను బ్యాంక్‌కు మళ్ళీ పంపాం. {hours} గంటల్లో మీ ఖాతాలో జమ అవుతుంది.",
        "promise_restart": "మీ సౌండ్‌బాక్స్‌ను రిమోట్‌గా రీస్టార్ట్ చేశాం. {hours} గంటల్లో అలర్ట్‌లు మళ్ళీ వస్తాయి.",
        "delay": "క్రెడిట్ ఆలస్యమైంది. మేము దీనిపై పని చేస్తున్నాం. కొత్త సమయం: {eta}.",
        "done_credit": "₹{amount} క్రెడిట్ మీ ఖాతాలో జమ అయింది. టికెట్ మూసివేశాం.",
        "done_settlement": "₹{amount} సెటిల్‌మెంట్ మీ బ్యాంక్ ఖాతాలో జమ అయింది. టికెట్ మూసివేశాం.",
        "done_restart": "మీ సౌండ్‌బాక్స్ మళ్ళీ ఆన్‌లైన్‌లో ఉంది. టికెట్ మూసివేశాం.",
        "choice": "మీ ఖాతాలో రెండు విషయాలు పెండింగ్‌లో ఉన్నాయి. మీరు దేని గురించి అడుగుతున్నారు?",
        "refusal": "క్షమించండి, మేము డబ్బును వేరే ఖాతాకు పంపలేము. మీ భద్రత కోసం ఈ విషయాన్ని మా బృందానికి పంపాం.",
        "approval_wait": "మీ సమస్య అర్థమైంది. ఈ చర్యకు మా బృందం ఆమోదం అవసరం, త్వరలో తెలియజేస్తాం.",
        "handoff": "మీ సమస్యను మా బృందానికి పంపాం. త్వరలో మీకు సమాచారం ఇస్తాం.",
    },
    "hi": {
        "promise_credit": "आपका साउंडबॉक्स किराया बंद कर दिया गया है। ₹{amount} का क्रेडिट {hours} घंटे में आपके खाते में दिखेगा।",
        "promise_settlement": "आपका ₹{amount} का सेटलमेंट बैंक को दोबारा भेज दिया गया है। {hours} घंटे में आपके खाते में आ जाएगा।",
        "promise_restart": "आपका साउंडबॉक्स रिमोट से रीस्टार्ट कर दिया गया है। {hours} घंटे में अलर्ट फिर से आने लगेंगे।",
        "delay": "क्रेडिट में देरी हो गई है। हम इस पर काम कर रहे हैं। नया समय: {eta}।",
        "done_credit": "₹{amount} का क्रेडिट आपके खाते में आ गया है। टिकट बंद कर दिया गया है।",
        "done_settlement": "₹{amount} का सेटलमेंट आपके बैंक खाते में आ गया है। टिकट बंद कर दिया गया है।",
        "done_restart": "आपका साउंडबॉक्स फिर से ऑनलाइन है। टिकट बंद कर दिया गया है।",
        "choice": "आपके खाते में दो चीज़ें बाकी हैं। आप किसके बारे में पूछ रहे हैं?",
        "refusal": "माफ़ कीजिए, हम पैसा किसी दूसरे खाते में नहीं भेज सकते। आपकी सुरक्षा के लिए यह मामला हमारी टीम को भेज दिया गया है।",
        "approval_wait": "आपकी समस्या समझ गए। इस कदम के लिए हमारी टीम की मंज़ूरी चाहिए, जल्द ही बताएंगे।",
        "handoff": "आपका मामला हमारी टीम को भेज दिया गया है। जल्द ही आपको जानकारी देंगे।",
    },
    "hinglish": {
        "promise_credit": "Aapka Soundbox rent band kar diya hai. ₹{amount} ka credit {hours} ghante mein aapke account mein dikhega.",
        "promise_settlement": "Aapka ₹{amount} settlement bank ko dobara bhej diya hai. {hours} ghante mein account mein aa jayega.",
        "promise_restart": "Aapka Soundbox remote se restart kar diya hai. {hours} ghante mein alerts wapas aane lagenge.",
        "delay": "Credit mein deri ho gayi hai. Hum ispar kaam kar rahe hain. Naya time: {eta}.",
        "done_credit": "₹{amount} ka credit aapke account mein aa gaya hai. Ticket band kar diya hai.",
        "done_settlement": "₹{amount} ka settlement aapke bank account mein aa gaya hai. Ticket band kar diya hai.",
        "done_restart": "Aapka Soundbox wapas online hai, alerts aa rahe hain. Ticket band kar diya hai.",
        "choice": "Aapke account mein do cheezein pending hain. Aap kiske baare mein pooch rahe hain?",
        "refusal": "Maaf kijiye, hum paisa kisi doosre account mein nahi bhej sakte. Aapki suraksha ke liye yeh case hamari team ko bhej diya hai.",
        "approval_wait": "Aapki problem samajh gaye. Is step ke liye team ki approval chahiye, jaldi batayenge.",
        "handoff": "Aapka case hamari team ko bhej diya hai. Jaldi update denge.",
    },
    "en": {
        "promise_credit": "Your Soundbox rent has been stopped. A ₹{amount} credit will show in your account within {hours} hours.",
        "promise_settlement": "Your ₹{amount} settlement has been re-sent to the bank. It will reach your account within {hours} hours.",
        "promise_restart": "Your Soundbox was restarted remotely. Alerts will resume within {hours} hours.",
        "delay": "The credit is delayed. We are working on it. New time: {eta}.",
        "done_credit": "The ₹{amount} credit has reached your account. Ticket closed.",
        "done_settlement": "The ₹{amount} settlement has reached your bank account. Ticket closed.",
        "done_restart": "Your Soundbox is back online and alerts are working. Ticket closed.",
        "choice": "Two things are pending on your account. Which one are you asking about?",
        "refusal": "Sorry, we cannot send money to a different account. For your safety this has been passed to our team.",
        "approval_wait": "Understood. This step needs approval from our team; we will update you soon.",
        "handoff": "Your case has been passed to our team. We will update you soon.",
    },
}


TEMPLATES.update(_lx.TEMPLATES)

# Chat replies (greeting / thanks / status). Languages not listed fall back to English in mock mode;
# in live mode Gemini writes these replies in any language.
CONV_KINDS = ["greet", "ask_problem", "thanks", "status_none", "status_progress", "status_done", "status_team", "status_blocked"]
CONV_TEMPLATES = {
    "en": ["Namaste {name}! I'm Saathi, your Paytm helper. Tell me what's wrong, by voice or by typing.",
           "I didn't quite get that. What problem are you facing with Paytm?",
           "You're welcome! I'm here if you need anything else.",
           "You don't have any open requests right now. Tell me if something is wrong.",
           "Your request is in progress. We'll confirm it is fixed by {eta}.",
           "Your last request is done, and we confirmed it in Paytm's records.",
           "A Paytm team member is checking your request right now.",
           "We couldn't do your last request, to keep your money safe."],
    "hi": ["नमस्ते {name}! मैं साथी हूँ, आपका Paytm सहायक। बताइए क्या दिक्कत है, बोलकर या लिखकर।",
           "मैं ठीक से समझ नहीं पाया। Paytm में आपको क्या दिक्कत आ रही है?",
           "आपका स्वागत है! कुछ और चाहिए तो बताइए।",
           "अभी आपकी कोई खुली शिकायत नहीं है। कोई दिक्कत हो तो बताइए।",
           "आपकी शिकायत पर काम चल रहा है। {eta} तक हम पक्का करेंगे कि यह ठीक हो गया।",
           "आपकी पिछली शिकायत हल हो गई है, और हमने Paytm के रिकॉर्ड में इसकी पुष्टि की है।",
           "Paytm टीम का एक सदस्य अभी आपकी शिकायत देख रहा है।",
           "आपकी सुरक्षा के लिए हम आपकी पिछली मांग पूरी नहीं कर सके।"],
    "te": ["నమస్కారం {name}! నేను సాథీ, మీ Paytm సహాయకుడిని. సమస్య ఏంటో మాట్లాడి లేదా టైప్ చేసి చెప్పండి.",
           "నాకు సరిగ్గా అర్థం కాలేదు. Paytm లో మీకు ఏ సమస్య ఉంది?",
           "స్వాగతం! ఇంకేమైనా కావాలంటే చెప్పండి.",
           "ప్రస్తుతం మీకు తెరిచిన ఫిర్యాదులు లేవు. ఏదైనా సమస్య ఉంటే చెప్పండి.",
           "మీ ఫిర్యాదుపై పని జరుగుతోంది. {eta} లోపు సరిచేసినట్టు నిర్ధారిస్తాం.",
           "మీ గత ఫిర్యాదు పరిష్కారమైంది, Paytm రికార్డుల్లో నిర్ధారించాం.",
           "Paytm టీమ్ సభ్యుడు ప్రస్తుతం మీ ఫిర్యాదును చూస్తున్నారు.",
           "మీ భద్రత కోసం మీ గత అభ్యర్థనను చేయలేకపోయాం."],
    "ta": ["வணக்கம் {name}! நான் சாத்தி, உங்கள் Paytm உதவியாளர். என்ன பிரச்சனை என்று பேசியோ தட்டச்சு செய்தோ சொல்லுங்கள்.",
           "எனக்கு சரியாக புரியவில்லை. Paytm-இல் உங்களுக்கு என்ன பிரச்சனை?",
           "நன்றி! வேறு ஏதாவது தேவைப்பட்டால் சொல்லுங்கள்.",
           "இப்போது உங்களுக்கு திறந்த புகார்கள் எதுவும் இல்லை. ஏதாவது பிரச்சனை இருந்தால் சொல்லுங்கள்.",
           "உங்கள் புகாரில் வேலை நடக்கிறது. {eta}க்குள் சரியானதை உறுதி செய்வோம்.",
           "உங்கள் கடைசி புகார் தீர்க்கப்பட்டது, Paytm பதிவுகளில் உறுதி செய்தோம்.",
           "Paytm குழு உறுப்பினர் இப்போது உங்கள் புகாரை பார்க்கிறார்.",
           "உங்கள் பாதுகாப்புக்காக உங்கள் கடைசி கோரிக்கையை செய்ய முடியவில்லை."],
    "hinglish": ["Namaste {name}! Main Saathi hoon, aapka Paytm helper. Bataiye kya problem hai, bolkar ya likhkar.",
                 "Main theek se samajh nahi paaya. Paytm mein aapko kya problem aa rahi hai?",
                 "Aapka swagat hai! Kuch aur chahiye to bataiye.",
                 "Abhi aapki koi open request nahi hai. Koi problem ho to bataiye.",
                 "Aapki request par kaam chal raha hai. {eta} tak hum confirm karenge ki theek ho gaya.",
                 "Aapki pichhli request solve ho gayi hai, aur humne Paytm records mein confirm kiya hai.",
                 "Paytm team ka ek member abhi aapki request dekh raha hai.",
                 "Aapki suraksha ke liye hum aapki pichhli request poori nahi kar sake."],
}
for _lang, _lines in CONV_TEMPLATES.items():
    TEMPLATES[_lang].update(dict(zip(CONV_KINDS, _lines)))

# Payment check (S6) and failed-UPI refund (S7). Other languages: Gemini translates the English line when live.
NEW_KINDS = ["ask_utr", "qr_tampered", "promise_qr", "done_qr", "guidance_failed", "guidance_pending",
             "promise_reversal", "delay_reversal", "done_reversal", "utr_not_found", "delay_reversal_comp"]
NEW_TEMPLATES = {
    "en": ["No ₹{amount} payment has reached your account in the last 15 minutes. Please ask the customer for the UTR number (12 digits) on their payment screen and share it here.",
           "The ₹{amount} has not come into your account. Please check the QR sticker at your shop, it may have been changed. Stop using it until your new QR arrives.",
           "We are sending you a new QR. It will be active within {hours} hours, and we'll confirm with a ₹1 test payment.",
           "Your new QR is active and the test payment reached your account. Ticket closed.",
           "Issue: your ₹{amount} payment shows failed, but the amount was debited. Likely cause: the receiving bank did not respond in time. Please don't pay again.",
           "Your payment of ₹{amount} is still pending. Please wait, do not pay again.",
           "We have escalated this to the bank. Expected resolution by {deadline}.",
           "The bank is taking longer than expected. We have escalated it further. New expected time: {eta}.",
           "₹{amount} is back in your account. Ticket closed.",
           "We couldn't find a payment with that UTR. Please check the 12-digit number on the customer's screen and send it again.",
           "The RBI limit has passed, so ₹{comp} delay compensation (₹100/day) has been added to your account. We have escalated it further. New expected time: {eta}."],
    "hi": ["पिछले 15 मिनट में आपके खाते में ₹{amount} का कोई पेमेंट नहीं आया है। कृपया ग्राहक से उनकी पेमेंट स्क्रीन पर लिखा UTR नंबर (12 अंक) लेकर यहाँ भेजें।",
           "₹{amount} आपके खाते में नहीं आया है। कृपया अपनी दुकान का QR स्टिकर जाँचें, हो सकता है वह बदला गया हो। नया QR आने तक इसे इस्तेमाल न करें।",
           "हम आपको नया QR भेज रहे हैं। यह {hours} घंटे में चालू हो जाएगा, और हम ₹1 के टेस्ट पेमेंट से पक्का करेंगे।",
           "आपका नया QR चालू है और टेस्ट पेमेंट आपके खाते में आ गया है। टिकट बंद कर दिया गया है।",
           "समस्या: आपका ₹{amount} का पेमेंट फेल दिख रहा है, पर पैसा कट गया है। संभावित कारण: पाने वाले बैंक ने समय पर जवाब नहीं दिया। कृपया दोबारा पेमेंट न करें।",
           "आपका ₹{amount} का पेमेंट अभी पेंडिंग है। कृपया इंतज़ार करें, दोबारा पेमेंट न करें।",
           "हमने यह मामला बैंक को एस्केलेट कर दिया है। समाधान का अनुमानित समय: {deadline}।",
           "बैंक को उम्मीद से ज़्यादा समय लग रहा है। हमने मामला और ऊपर एस्केलेट किया है। नया अनुमानित समय: {eta}।",
           "₹{amount} आपके खाते में वापस आ गए हैं। टिकट बंद कर दिया गया है।",
           "इस UTR से कोई पेमेंट नहीं मिला। कृपया ग्राहक की स्क्रीन पर 12 अंकों का नंबर जाँचकर फिर से भेजें।",
           "RBI की समय सीमा निकल गई है, इसलिए देरी का ₹{comp} मुआवज़ा (₹100/दिन) आपके खाते में जोड़ा गया है। हमने मामला और ऊपर एस्केलेट किया है। नया अनुमानित समय: {eta}।"],
    "hinglish": ["Pichhle 15 minute mein aapke account mein ₹{amount} ka koi payment nahi aaya. Customer se unki payment screen ka UTR number (12 digit) lekar yahan bhejiye.",
                 "₹{amount} aapke account mein nahi aaya hai. Apni dukaan ka QR sticker check kariye, ho sakta hai woh badla gaya ho. Naya QR aane tak isse use mat kariye.",
                 "Hum aapko naya QR bhej rahe hain. Yeh {hours} ghante mein chalu ho jayega, aur hum ₹1 ke test payment se confirm karenge.",
                 "Aapka naya QR chalu hai aur test payment aapke account mein aa gaya. Ticket band kar diya hai.",
                 "Issue: aapka ₹{amount} ka payment fail dikh raha hai, par paisa kat gaya hai. Sambhavit kaaran: receiving bank ne time par jawab nahi diya. Dobara payment mat kariye.",
                 "Aapka ₹{amount} ka payment abhi pending hai. Intezaar kariye, dobara payment mat kariye.",
                 "Humne yeh mamla bank ko escalate kar diya hai. Expected resolution: {deadline}.",
                 "Bank ko expected se zyada time lag raha hai. Humne mamla aur upar escalate kiya hai. Naya expected time: {eta}.",
                 "₹{amount} aapke account mein wapas aa gaye. Ticket band kar diya hai.",
                 "Is UTR se koi payment nahi mila. Customer ki screen par 12 digit ka number check karke dobara bhejiye.",
           "RBI ki time limit nikal gayi hai, isliye deri ka ₹{comp} compensation (₹100/din) aapke account mein joda gaya hai. Humne mamla aur upar escalate kiya hai. Naya expected time: {eta}."],
    "te": ["గత 15 నిమిషాల్లో మీ ఖాతాలో ₹{amount} చెల్లింపు రాలేదు. కస్టమర్ పేమెంట్ స్క్రీన్‌పై ఉన్న UTR నంబర్ (12 అంకెలు) అడిగి ఇక్కడ పంపండి.",
           "₹{amount} మీ ఖాతాలోకి రాలేదు. దయచేసి మీ షాప్‌లోని QR స్టిక్కర్‌ను చెక్ చేయండి, అది మార్చబడి ఉండవచ్చు. కొత్త QR వచ్చే వరకు దాన్ని వాడకండి.",
           "మీకు కొత్త QR పంపుతున్నాం. ఇది {hours} గంటల్లో యాక్టివ్ అవుతుంది, ₹1 టెస్ట్ పేమెంట్‌తో నిర్ధారిస్తాం.",
           "మీ కొత్త QR యాక్టివ్‌గా ఉంది, టెస్ట్ పేమెంట్ మీ ఖాతాలో జమ అయింది. టికెట్ మూసివేశాం.",
           "సమస్య: మీ ₹{amount} పేమెంట్ ఫెయిల్ అని చూపిస్తోంది, కానీ డబ్బు కట్ అయింది. కారణం కావచ్చు: స్వీకరించే బ్యాంక్ సమయానికి స్పందించలేదు. దయచేసి మళ్ళీ చెల్లించవద్దు.",
           "మీ ₹{amount} పేమెంట్ ఇంకా పెండింగ్‌లో ఉంది. దయచేసి వేచి ఉండండి, మళ్ళీ చెల్లించవద్దు.",
           "ఈ సమస్యను బ్యాంక్‌కు ఎస్కలేట్ చేశాం. పరిష్కారం అంచనా సమయం: {deadline}.",
           "బ్యాంక్‌కు అనుకున్నదానికంటే ఎక్కువ సమయం పడుతోంది. మరింత పైకి ఎస్కలేట్ చేశాం. కొత్త అంచనా సమయం: {eta}.",
           "₹{amount} మీ ఖాతాలో తిరిగి జమ అయింది. టికెట్ మూసివేశాం.",
           "ఆ UTR తో చెల్లింపు దొరకలేదు. కస్టమర్ స్క్రీన్‌పై ఉన్న 12 అంకెల నంబర్ చూసి మళ్ళీ పంపండి.",
           "RBI గడువు దాటింది, కాబట్టి ఆలస్యానికి ₹{comp} పరిహారం (రోజుకు ₹100) మీ ఖాతాలో జమ చేశాం. మరింత పైకి ఎస్కలేట్ చేశాం. కొత్త అంచనా సమయం: {eta}."],
}
for _lang, _lines in NEW_TEMPLATES.items():
    TEMPLATES[_lang].update(dict(zip(NEW_KINDS, _lines)))


def has_template(lang, kind):
    return kind in TEMPLATES.get(lang, {})


# --------------------------------------------------------------------------- history
def _historical_rows():
    """~120 deterministic rows. Priors and nearest-rank P90 latencies are exact by construction."""
    rows = []

    def add(issue, causes, fixes, verified_lat, unverified_n):
        # causes: list of (cause, count). verified rows get latencies from verified_lat in order.
        lat = list(verified_lat)
        flat = [c for c, n in causes for _ in range(n)]
        for i, cause in enumerate(flat):
            verified = i < len(flat) - unverified_n
            l = lat[i % len(lat)] if verified else None
            rows.append((issue, cause, fixes.get(cause, "manual"), 1 if verified else 0, l))

    # rent_after_return: 40 rows -> priors .70/.15/.10/.05 ; 36 verified, P90 (rank 33/36) = 36h
    rar_lat = [14, 16, 18, 18, 20, 20, 22, 22, 22, 24, 24, 24, 24, 26, 26, 26, 28, 28, 28, 28, 30, 30, 30, 30,
               31, 32, 32, 33, 33, 34, 34, 35, 36, 40, 46, 60]
    add("rent_after_return",
        [("billing_event_missing", 28), ("wrong_serial", 6), ("qc_failed", 4), ("other", 2)],
        {"billing_event_missing": "stop_rental_and_credit", "wrong_serial": "fix_serial_mapping", "qc_failed": "qc_recheck"},
        rar_lat, 4)
    # settlement_held: 35 rows ; 30 verified, P90 (rank 27/30) = 18h
    st_lat = [3, 4, 4, 5, 5, 6, 6, 6, 7, 8, 8, 9, 10, 10, 11, 12, 12, 12, 13, 14, 14, 15, 16, 16, 17, 17, 18, 20, 22, 30]
    add("settlement_held",
        [("bank_no_response", 21), ("wrong_ifsc", 6), ("account_frozen", 4), ("compliance_hold", 3), ("other", 1)],
        {"bank_no_response": "requeue_settlement", "wrong_ifsc": "update_bank_details", "account_frozen": "merchant_contact_bank"},
        st_lat, 5)
    # device_offline: 25 rows ; 20 verified, P90 (rank 18/20) = 2h
    do_lat = [0.25, 0.25, 0.5, 0.5, 0.5, 0.75, 0.75, 1, 1, 1, 1, 1.25, 1.5, 1.5, 1.5, 1.75, 2, 2, 4, 8]
    add("device_offline",
        [("sim_network_drop", 10), ("power_issue", 8), ("firmware_hang", 5), ("other", 2)],
        {"sim_network_drop": "replace_sim", "power_issue": "merchant_power_cycle", "firmware_hang": "field_visit"},
        do_lat, 5)
    # kyc_pending: 20 rows ; 18 verified, P90 (rank 17/18) = 72h
    kyc_lat = [12, 18, 24, 24, 30, 36, 36, 40, 48, 48, 48, 50, 56, 60, 64, 70, 72, 96]
    add("kyc_pending",
        [("doc_mismatch", 9), ("pending_review", 7), ("name_mismatch", 3), ("other", 1)],
        {"doc_mismatch": "request_reupload", "pending_review": "expedite_review"}, kyc_lat, 2)
    return rows


# --------------------------------------------------------------------------- seed
def seed():
    store.reset()
    I = store.insert
    for (mid, name, biz, city, lang, phone, acct, ifsc, bank, vpa, joined) in HEROES:
        I("merchants", {"id": mid, "name": name, "business": biz, "city": city, "lang": lang, "phone": phone,
                        "bank_account": acct, "ifsc": ifsc, "bank_name": bank, "linked_vpa": vpa, "joined_at": joined})
    for i, (mid, name, biz, city, lang, bankp, model) in enumerate(BACKGROUND):
        I("merchants", {"id": mid, "name": name, "business": biz, "city": city, "lang": lang,
                        "phone": f"98{40000000 + i * 7919:08d}", "bank_account": f"8800{i:02d}11223344",
                        "ifsc": f"{bankp}000{100 + i}", "bank_name": BANKS[bankp], "linked_vpa": f"{name.lower()}.shop@ptys",
                        "joined_at": rel(-200 - i * 11), "background": 1})
        I("devices", {"id": f"SB-{20 + i}", "merchant_id": mid, "model": model, "serial": f"{model.replace('-', '')}-{20 + i}-{4000 + i}",
                      "state": "ACTIVE", "state_since": rel(-150), "rental_status": "ACTIVE", "monthly_rent": 600,
                      "last_heartbeat": rel(0, -0, -5), "online": 1})
    for k in range(OUTAGE_POOL_SIZE):
        mid = f"M-8{k + 1:03d}"
        I("merchants", {"id": mid, "name": f"Merchant {k + 1:03d}", "business": "retail", "city": "Telangana", "lang": "te",
                        "phone": f"97{30000000 + k * 104729:08d}", "bank_account": f"7700{k:03d}5566778",
                        "ifsc": f"DCCB000{200 + k}", "bank_name": BANKS["DCCB"], "linked_vpa": f"m8{k:03d}@ptys",
                        "joined_at": rel(-365), "background": 1})

    # ---- Ramesh (M-2291)
    I("devices", {"id": "SB-77", "merchant_id": "M-2291", "model": "SB-4G", "serial": "SB4G-77-0193", "state": "RETURN_RECEIVED",
                  "state_since": rel(-10), "rental_status": "ACTIVE", "monthly_rent": 600, "last_heartbeat": rel(-12), "online": 0})
    I("charges", {"id": "C-5410", "merchant_id": "M-2291", "device_id": "SB-77", "type": "RENTAL", "amount": 600, "ts": rel(-38), "status": "DEBITED"})
    I("charges", {"id": "C-5521", "merchant_id": "M-2291", "device_id": "SB-77", "type": "RENTAL", "amount": 600, "ts": rel(-8), "status": "DEBITED"})
    I("billing_events", {"id": "BE-1001", "merchant_id": "M-2291", "device_id": "SB-77", "event": "RENTAL_ACTIVATED", "ts": rel(-400)})
    I("warehouse_scans", {"id": "W-310", "merchant_id": "M-2291", "device_id": "SB-77", "serial": "SB4G-77-0193", "qc": "PASS", "ts": rel(-10)})
    I("tickets", {"id": "T-188", "merchant_id": "M-2291", "topic": "device_return", "status": "CLOSED", "opened_at": rel(-12),
                  "closed_at": rel(-5), "credit_issued": 0, "notes": "Return confirmed by warehouse. Closed after merchant stopped replying."})
    # ---- Lakshmi (M-3107)
    I("devices", {"id": "SB-83", "merchant_id": "M-3107", "model": "SB-4G", "serial": "SB4G-83-0577", "state": "RETURN_RECEIVED",
                  "state_since": rel(-7), "rental_status": "ACTIVE", "monthly_rent": 600, "last_heartbeat": rel(-9), "online": 0})
    I("charges", {"id": "C-6610", "merchant_id": "M-3107", "device_id": "SB-83", "type": "RENTAL", "amount": 600, "ts": rel(-5), "status": "DEBITED"})
    I("billing_events", {"id": "BE-1002", "merchant_id": "M-3107", "device_id": "SB-83", "event": "RENTAL_ACTIVATED", "ts": rel(-300)})
    I("warehouse_scans", {"id": "W-322", "merchant_id": "M-3107", "device_id": "SB-83", "serial": "SB4G-83-0577", "qc": "PASS", "ts": rel(-7)})
    # ---- Imran (M-4420)
    I("devices", {"id": "SB-60", "merchant_id": "M-4420", "model": "SB-3G", "serial": "SB3G-60-0021", "state": "ACTIVE",
                  "state_since": rel(-480), "rental_status": "ACTIVE", "monthly_rent": 600, "last_heartbeat": rel(0, 0, -3), "online": 1})
    I("settlements", {"id": "S-9001", "merchant_id": "M-4420", "amount": 15200, "status": "CREDITED", "reason": None, "bank_name": "Deccan Co-op Bank",
                      "ifsc": "DCCB0000412", "ts": rel(-4), "credited_at": rel(-4, 6)})
    I("settlements", {"id": "S-9012", "merchant_id": "M-4420", "amount": 18400, "status": "HELD", "reason": "BANK_NO_RESPONSE",
                      "bank_name": "Deccan Co-op Bank", "ifsc": "DCCB0000412", "ts": rel(-2), "credited_at": None})
    I("payouts", {"id": "P-77", "merchant_id": "M-4420", "kind": "INCENTIVE", "amount": 500, "status": "PENDING", "ts": rel(-4)})
    # ---- Sunita (M-5150): device offline since 09:00 today; QR payments keep crediting, alerts not sent
    I("devices", {"id": "SB-91", "merchant_id": "M-5150", "model": "SB-4G", "serial": "SB4G-91-0812", "state": "ACTIVE",
                  "state_since": rel(-9), "rental_status": "ACTIVE", "monthly_rent": 600, "last_heartbeat": rel(0, -1), "online": 0})
    I("heartbeats", {"id": "HB-5001", "device_id": "SB-91", "merchant_id": "M-5150", "ts": rel(0, -1)})
    for n, (mins, amt) in enumerate([(-150, 120), (-95, 45), (-50, 260), (-35, 80), (-20, 150), (-8, 60)]):
        I("payments", {"id": f"PAY-{7001 + n}", "merchant_id": "M-5150", "amount": amt, "ts": rel(0, 0, mins), "status": "CREDITED",
                       "channel": "UPI_QR", "alert_sent": 1 if mins < -60 else 0})
    # ---- Ramesh extras: his QR, today's settlement + SMS fee, and a ₹3,624 payment that went to a look-alike UPI ID
    I("qr_codes", {"id": "QR-2291", "merchant_id": "M-2291", "vpa": "ramesh.kirana@ptys", "status": "ACTIVE", "created_at": rel(-400)})
    I("settlements", {"id": "S-8102", "merchant_id": "M-2291", "amount": 6480, "status": "CREDITED", "reason": None,
                      "bank_name": "Godavari Gramin Bank", "ifsc": "GGBK0001234", "ts": rel(0, -1, -30), "credited_at": rel(0, -1, -30)})
    I("charges", {"id": "C-5590", "merchant_id": "M-2291", "device_id": None, "type": "SMS_ALERT_FEE", "amount": 15, "ts": rel(0, -2), "status": "DEBITED"})
    I("upi_txns", {"id": "UPI-88001", "utr": SCENARIOS["S6"]["utr"], "customer_id": None, "payer_vpa": "walkin.cust@okhdfc",
                   "payee_vpa": "rameshkirana.store@okaxis", "payee_name": "RAMESH KIRANA STORE", "merchant_id": None, "amount": 3624,
                   "ts": rel(0, 0, -6), "status": "SUCCESS", "bank_name": "HDFC (fictional)", "ifsc": "", "reason": None,
                   "reversal_status": None})
    # ---- Customer Kiran (C-1001): a ₹9,700 UPI payment debited but not credited at Deccan Co-op Bank
    for (cid, name, city, lang, phone, acct, bank, ifsc, vpa) in CUSTOMERS:
        I("customers", {"id": cid, "name": name, "city": city, "lang": lang, "phone": phone, "bank_account": acct,
                        "bank_name": bank, "ifsc": ifsc, "vpa": vpa, "joined_at": rel(-800)})
    for (tid, utr, amt, mins, status, payee, pname) in [
            ("UPI-90001", "551233017789", 9700, -40, "DEBITED_NOT_CREDITED", "meena.textiles@ptys", "Meena Textiles"),
            ("UPI-90002", "551233015501", 250, -60 * 24, "SUCCESS", "chai.point@ptys", "Chai Point"),
            ("UPI-90003", "551233013320", 1200, -60 * 48, "SUCCESS", "city.medicals@ptys", "City Medicals"),
            ("UPI-90004", "551233019944", 80, -180, "SUCCESS", "auto.ravi@ptys", "Ravi Auto")]:
        I("upi_txns", {"id": tid, "utr": utr, "customer_id": "C-1001", "payer_vpa": "kiran.k@ptys", "payee_vpa": payee,
                       "payee_name": pname, "merchant_id": None, "amount": amt, "ts": rel(0, 0, mins), "status": status,
                       "bank_name": "Deccan Co-op Bank", "ifsc": "DCCB0000777",
                       "reason": "BENEFICIARY_TIMEOUT" if status != "SUCCESS" else None,
                       "reversal_status": "PENDING" if status != "SUCCESS" else None})
    # ---- Vikram (M-6001)
    I("devices", {"id": "SB-95", "merchant_id": "M-6001", "model": "SB-3G", "serial": "SB3G-95-0440", "state": "ACTIVE",
                  "state_since": rel(-600), "rental_status": "ACTIVE", "monthly_rent": 600, "last_heartbeat": rel(0, 0, -2), "online": 1})
    I("charges", {"id": "C-7702", "merchant_id": "M-6001", "device_id": "SB-95", "type": "RENTAL", "amount": 600, "ts": rel(-12), "status": "DEBITED"})
    # normal settlements for heroes
    for k, mid in enumerate(["M-2291", "M-3107", "M-5150", "M-6001"]):
        I("settlements", {"id": f"S-80{k}1", "merchant_id": mid, "amount": 3200 + k * 950, "status": "CREDITED", "reason": None,
                          "bank_name": "", "ifsc": "", "ts": rel(-1), "credited_at": rel(-1, 5)})

    # ---- knowledge
    for (issue, cause, fix, ver, lat) in _historical_rows():
        I("historical_cases", {"issue_type": issue, "cause": cause, "fix": fix, "verified": ver, "latency_hours": lat,
                               "created_at": rel(-90)})
    for c in CLUSTER_CARDS:
        I("cluster_cards", {"issue_type": c["issue_type"], "area": c["area"], "description": c["description"],
                            "examples": c["examples"], "causes": c.get("default_causes"), "playbook_id": c["playbook_id"],
                            "owner": c["owner"], "verified_success": c["verified_success"], "ticket_topic": c["ticket_topic"]})
    for inc in INCIDENTS:
        I("incidents", {**inc, "status": "ACTIVE", "created_at": rel(-3), "source": "seed"})
    for lc in LIFECYCLES:
        I("lifecycles", {"name": lc["name"], "stages": lc["stages"], "p90_hours": lc["p90_hours"]})
    for pb in PLAYBOOKS:
        I("playbooks", {**pb, "source": "seed", "created_at": rel(-180)})
    for k, v in POLICY.items():
        I("policy_config", {"key": k, "value": json.dumps(v)})
    for k, v in SIM_CONFIG.items():
        I("sim_config", {"key": k, "value": json.dumps(v)})
    for lang, kinds in TEMPLATES.items():
        for kind, text in kinds.items():
            I("templates", {"lang": lang, "kind": kind, "text": text})


def policy(key):
    row = store.q1("SELECT value FROM policy_config WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def sim_config(key):
    row = store.q1("SELECT value FROM sim_config WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def template(lang, kind, **params):
    row = store.q1("SELECT text FROM templates WHERE lang=? AND kind=?", (lang, kind)) or \
        store.q1("SELECT text FROM templates WHERE lang='en' AND kind=?", (kind,))
    return row["text"].format(**params) if row else ""
