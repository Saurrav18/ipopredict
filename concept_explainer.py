"""Plain-language explanations of the terms the site uses, with concrete
examples. Used by the Ask feature so questions like "what is confidence" get a
real answer instead of a data query that returns nothing."""
import re

# Each concept: the explanation + a worked example. Examples use the project's
# own verified IPOs (Hexagon, CMR) so they are real, not invented.
CONCEPTS = {
    "listing_gain": {
        "aliases": ["listing gain", "listing gains", "list gain", "day one gain", "day-one gain",
                    "listing pop", "what is listing", "listing day gain", "first day gain"],
        "answer": (
            "Listing gain is how much an IPO's share price moves on its first day of trading, versus the "
            "price you paid (the offer price). It is the headline number IPO applicants care about.\n\n"
            "Example: if you were allotted shares at Rs 100 and the stock opens at Rs 125 on listing day, "
            "that is a +25% listing gain. If it opens at Rs 90, that is a -10% loss.\n\n"
            "Across the 331 IPOs in this dataset, about 72% listed above their offer price, with an average "
            "day-one gain of roughly +21% - but plenty still listed flat or negative, which is why the "
            "system weighs demand and grey-market signals before calling one worth applying to."
        ),
    },
    "ipo": {
        "aliases": ["ipo", "what is an ipo", "initial public offering", "ipos"],
        "answer": (
            "An IPO (Initial Public Offering) is when a private company sells its shares to the public "
            "for the first time and lists on the stock exchange. You can apply for shares at a set price "
            "during a short window (a few days); if you get an allotment, you own those shares when the "
            "company starts trading.\n\n"
            "People watch IPOs because the share price often moves on the first day of trading, the "
            "'listing'. If it opens above the price you paid, that is a listing gain.\n\n"
            "Example: an IPO priced at Rs 100 that opens at Rs 120 on listing day gave applicants a +20% "
            "day-one gain. This site estimates whether an IPO is worth applying to, and how it might list."
        ),
    },
    "confidence": {
        "aliases": ["confidence", "how sure", "confident", "confidence score", "how confident"],
        "answer": (
            "Confidence is how sure the system is that its apply-or-skip call is correct, "
            "on a 0 to 100 scale. It is calibrated, meaning the number is tied to a real "
            "historical hit rate:\n\n"
            "  - 90 to 100: very high. Calls this confident have been right about 95 to 100% of the time.\n"
            "  - 75 to 90: high. Right roughly 85 to 95% of the time.\n"
            "  - 60 to 75: moderate. A lean, right around 70 to 85% of the time. Treat as a tilt, not a sure thing.\n"
            "  - below 60: weak, close to a coin flip.\n\n"
            "Example: Hexagon Nutrition had confidence 66, a moderate lean to APPLY. It listed at "
            "+7.2% on day one. A modest gain, which is exactly what a moderate-confidence call should mean. "
            "By contrast CMR Green had confidence 98 (very high) and listed +31.8%.\n\n"
            "Confidence is different from the accuracy tier on the home page: confidence is about THIS "
            "specific IPO, while the tier slider filters which IPOs you see by how strict you want to be."
        ),
    },
    "tier": {
        "aliases": ["tier", "accuracy tier", "slider", "how strict", "strictness", "accuracy filter"],
        "answer": (
            "The accuracy tier (the slider on the home page) is how strict you want the system to be "
            "before it shows you an IPO. Each tier maps to a historical win rate.\n\n"
            "  - Set it to 95: you only see the strongest setups, the ones that historically won about 95% "
            "of the time. Fewer IPOs show up.\n"
            "  - Set it to 70: a looser bar, more IPOs appear, lower historical win rate.\n\n"
            "Example: at the 90% tier the system might show just one open IPO it is very sure about; drop "
            "to 70% and two or three more appear. Same data, you are just choosing your own risk bar."
        ),
    },
    "gmp": {
        "aliases": ["gmp", "grey market", "gray market", "grey market premium", "grey mkt"],
        "answer": (
            "GMP (grey-market premium) is the unofficial price traders are paying for an IPO's shares "
            "before it officially lists. It is a rough sentiment gauge, not a guarantee.\n\n"
            "If an IPO is priced at Rs 100 and the GMP is Rs 15, the grey market expects it to list "
            "around Rs 115, a +15% pop. Higher GMP usually signals stronger demand.\n\n"
            "Example: Hexagon Nutrition had a grey-market premium of about +9% before listing and actually "
            "listed +7.2%, so the grey market was close. But GMP can be wrong, which is why the system uses "
            "it as one signal among several, not on its own."
        ),
    },
    "subscription": {
        "aliases": ["subscription", "subscribed", "oversubscribed", "qib", "hni", "rii", "demand", "times subscribed"],
        "answer": (
            "Subscription is how many times more demand there was than shares available. '10x subscribed' "
            "means ten times more bids than shares. It is split by investor type:\n\n"
            "  - QIB: big institutions (mutual funds, banks, insurers). Heavy QIB interest is a strong sign.\n"
            "  - HNI / NII: wealthy individuals, who often bid with borrowed money, so they avoid IPOs they "
            "do not expect to pop.\n"
            "  - RII: retail (small investors like most people).\n\n"
            "Example: Hexagon Nutrition was subscribed about 54x overall, with QIBs at ~20x and HNIs at ~161x. "
            "That heavy demand was part of why the call was APPLY."
        ),
    },
    "pe": {
        "aliases": ["p/e", "pe ratio", "pe", "price to earnings", "price earnings", "p/e ratio"],
        "answer": (
            "P/E (price-to-earnings) tells you how pricey a company's shares are versus its profits. "
            "A lower P/E is cheaper; a higher P/E means you are paying more for each rupee of profit.\n\n"
            "Rough guide for IPOs: under 15 is cheap, 15 to 30 is fair, 30 to 50 is expensive, over 50 is "
            "very expensive. A negative P/E means the company is loss-making.\n\n"
            "Example: an IPO with a P/E of 18 is fairly priced; one at 100 is being valued very richly and "
            "needs strong growth to justify it. On any upcoming IPO's page you can tap the P/E to see how "
            "past IPOs with a similar P/E actually listed."
        ),
    },
    "roe": {
        "aliases": ["roe", "return on equity", "ronw", "return on net worth"],
        "answer": (
            "ROE (return on equity) measures how well a company turns shareholders' money into profit, as "
            "a percentage. Higher is generally better.\n\n"
            "An ROE of 25% means the company earns 25 paise of profit a year for every rupee of shareholder "
            "money. Single-digit ROE is weak; 15 to 25%+ is healthy.\n\n"
            "Example: an IPO with ROE of 0.9% is barely profitable on its equity, while one at 35% is using "
            "its capital very efficiently."
        ),
    },
    "ofs": {
        "aliases": ["ofs", "offer for sale", "fresh issue", "fresh vs ofs", "new shares"],
        "answer": (
            "An IPO raises money in two ways, and the split matters:\n\n"
            "  - Fresh issue: new shares, so the money goes INTO the company (for growth, paying debt, etc.).\n"
            "  - Offer for sale (OFS): existing owners selling their shares, so the money goes to the FOUNDERS, "
            "not the company.\n\n"
            "A high OFS is not automatically bad, but it means founders are cashing out rather than the company "
            "getting fresh capital.\n\n"
            "Example: an IPO that is 100% fresh issue puts all the money into the business; one that is 100% OFS "
            "is purely founders selling. You can tap 'Offer-for-sale' on an IPO's page to see how similar past "
            "IPOs did."
        ),
    },
    "promoter": {
        "aliases": ["promoter", "promoter holding", "founder holding", "promoter stake"],
        "answer": (
            "Promoter holding is how much of the company the founders/promoters still own after the IPO. "
            "Higher holding usually means more 'skin in the game', the founders' interests stay aligned with "
            "yours.\n\n"
            "Example: if promoters keep 75% after the IPO, they remain heavily invested in the company doing "
            "well. If they drop to 20%, they have cashed out a lot, which some investors see as a caution."
        ),
    },
    "range": {
        "aliases": ["range", "listing range", "expected range", "prediction range", "likely listing"],
        "answer": (
            "The listing range is where the system expects the share to trade on its first day, versus the "
            "offer price. It is a range, not a single guess, because listings are uncertain.\n\n"
            "These ranges have historically contained the real first-day price about 88% of the time for IPOs "
            "the system suggests applying to.\n\n"
            "Example: Hexagon's predicted range was about +1% to +26%, and it listed at +7.2%, comfortably "
            "inside the range."
        ),
    },
    "allotment": {
        "aliases": ["allotment", "allotment odds", "allotment chance", "will i get shares", "get allotment"],
        "answer": (
            "Allotment odds are the rough chance that one application actually gets shares. When an IPO is "
            "heavily oversubscribed, shares are allotted by lottery, so even a good IPO may not give you any.\n\n"
            "Example: if allotment odds are about 4%, then roughly 1 in 25 applications gets shares. This is "
            "why heavily-subscribed IPOs are hard to actually get in on."
        ),
    },
    "win_rate": {
        "aliases": ["win rate", "win%", "listed positive", "success rate", "hit rate", "accuracy"],
        "answer": (
            "Win rate is the share of IPOs that closed their first day above the offer price (a 'win'). "
            "It is the core honesty measure of the site.\n\n"
            "Example: across the 331 IPOs studied, about 72% listed positive overall. The system's apply calls "
            "do much better than that baseline because it only says APPLY when several signals line up. "
            "On any metric's comparison you can see the win rate for similar past IPOs."
        ),
    },
}


def find_concept(question: str):
    q = " " + re.sub(r"[^a-z0-9/ ]", " ", question.lower()) + " "
    # only treat as a definition question if it is phrased like one, or is very short
    is_def = bool(re.search(r"\b(what|whats|what's|define|meaning|means|explain|how does|how do|tell me about|will i get|do i get|chance of)\b", q)) \
             or len(question.split()) <= 4
    if not is_def:
        return None
    best, best_len = None, 0
    for key, c in CONCEPTS.items():
        for alias in c["aliases"]:
            if (" " + alias + " ") in q or q.strip() == alias:
                if len(alias) > best_len:   # prefer the most specific alias matched
                    best, best_len = key, len(alias)
    return CONCEPTS[best]["answer"] if best else None
