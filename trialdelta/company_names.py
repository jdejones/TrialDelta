"""Healthcare_Data's company-name cleaning, shared by discovery and matching.

Suffixes originate in utilities.clean_company_names and clinical_trials.normalize.
Retain names without suffixes, strip endings only, and handle stacked suffixes
deterministically instead of globally replacing an arbitrarily chosen suffix.
"""

import re
import unicodedata

SUFFIXES = (
    "incorporated", "corporation", "laboratories", "holdings", "holding",
    "services", "limited", "company", "group", "gmbh", "corp", "inc", "ltd",
    "llc", "plc", "spa", "co", "ag", "nv", "sa", "se", "a", "s",
)
_ENDING = re.compile(r"\s+(?:" + "|".join(SUFFIXES) + r")$", re.IGNORECASE)


def clean_company_name(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).replace("&", " and ")
    # Registry lead sponsors use both "N.V." and "NV", for example.
    value = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda match: match.group().replace(".", ""), value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = " ".join(value.split())
    while value and (cleaned := _ENDING.sub("", value)) != value:
        value = cleaned.strip()
    if value.casefold() in SUFFIXES:
        return ""
    return value


def sponsor_key(value):
    return clean_company_name(value).casefold()
