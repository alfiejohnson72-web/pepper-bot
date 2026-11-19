"""
Guardrails shared by the Telegram bot and the admin dashboard.

AB Labs sells cosmetic skincare only. Two checks live here:
  is_blocked(text)          customer messages that never reach the AI
  product_problems(fields)  products the dashboard refuses to save or the bot refuses to show
"""

from __future__ import annotations

import re

# Customer chat: anything on this list gets a fixed answer and never reaches the AI.
BLOCKED = re.compile(
    r"\b("
    r"reta(trutide)?|sema(glutide)?|ozempic|wegovy|rybelsus|mounjaro|zepbound|"
    r"tirzepatide|tirz|cagrilintide|glp[- ]?1s?|"
    r"bpc[- ]?157|bpc|tb[- ]?500|thymosin|melanotan|mt[- ]?(1|2|ii)|pt[- ]?141|"
    r"cjc[- ]?1295|cjc|ipamorelin|tesamorelin|sermorelin|hexarelin|ghrp[- ]?\d*|"
    r"hgh|igf[- ]?1(\s?lr3)?|aod[- ]?9604|selank|semax|epitalon|mots[- ]?c|"
    r"inject\w*|jab|jabs|vials?|syringes?|needles?|reconstitut\w*|"
    r"bac(teriostatic)? water|research (use|chemicals?|peptides?|only)|"
    r"weight[- ]?loss|fat loss|dosing|dosage|doses?|mcg|micrograms?"
    r")\b",
    re.IGNORECASE,
)

BLOCKED_REPLY = (
    "just so u know we only sell skincare that goes on ur skin, "
    "no injectables, research peptides or weight loss stuff and i cant advise on them. "
    "for anything medical ur gp or a pharmacist is the one to ask\n\n"
    "happy to help u pick some skincare tho 👇"
)


def is_blocked(text: str | None) -> bool:
    return bool(BLOCKED.search(text or ""))


# Products: stricter than chat. Also catches the short code names, product
# formats that aren't skincare, and anything dosed in mg.
PRODUCT_BLOCKED = re.compile(
    r"\b("
    r"reta(trutide)?|ret|sema(glutide)?|ozempic|wegovy|rybelsus|mounjaro|zepbound|"
    r"tirzepatide|tirz|cagrilintide|glp[- ]?1s?|"
    r"bpc[- ]?157|bpc|bp[- ]?15\w*|tb[- ]?500|tb4|thymosin|melanotan|mt|mt[- ]?(1|2|ii)|pt[- ]?141|"
    r"cjc[- ]?1295|cjc|ipamorelin|tesamorelin|sermorelin|hexarelin|ghrp[- ]?\d*|"
    r"hgh|igf[- ]?1(\s?lr3)?|aod[- ]?9604|selank|semax|epitalon|mots[- ]?c|kisspeptin|dsip|"
    r"inject\w*|jabs?|vials?|ampoules?\s+for\s+injection|syringes?|needles?|pens?|"
    r"reconstitut\w*|lyophili[sz]ed|bac(teriostatic)? water|"
    r"research (use|chemicals?|peptides?|only|purposes)|not for human (use|consumption)|"
    r"for research|laboratory use only|"
    r"nasal|intranasal|sublingual|subcutaneous|intramuscular|oral|capsules?|tablets?|pills?|"
    r"weight[- ]?loss|lose weight|fat loss|fat burn\w*|appetite|slimming|"
    r"dosing|dosage|doses?|mcg|micrograms?|\d+(\.\d+)?\s?(mg|mcg|iu)"
    r")\b",
    re.IGNORECASE,
)

PRODUCT_REFUSAL = (
    "This can't be saved. AB Labs sells cosmetic skincare only, so products can't mention "
    "injectables, research peptides, weight-loss drugs, nasal sprays, capsules or mg doses. "
    "Found: {found}."
)


def product_problems(*texts: str | None) -> list[str]:
    """Return the blocked words found in any of the given fields (empty list = OK)."""
    found: list[str] = []
    for t in texts:
        for m in PRODUCT_BLOCKED.finditer(t or ""):
            word = m.group(0).strip()
            if word.lower() not in (f.lower() for f in found):
                found.append(word)
    return found
