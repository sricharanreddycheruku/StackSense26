"""Keyword rules over project.description. Pure, deterministic, no network."""
from __future__ import annotations

import re

from .model import Signal
from .schema import FIELD_INDEX

# Storage verb followed, within a few words, by a place ("data must stay in", "hosted only within").
_STAY = r"\b(?:stay|stays|stored|store|kept|keep|hosted|host|reside|resides|remain|remains|held|located)\b[\w ]{0,20}?"

# (pattern, [(field, value), ...], why). Rules run in list order; output is sorted.
_RULES = [
    (r"\bchat(?:s|ting)?\b|\blive\b|\breal-?time\b|\bpresence\b|\bmultiplayer\b|\bcollaborat\w*|\bcursors?\b|\bwhiteboard\w*",
     [("requirements.realtime", True)], "suggests live updates"),
    (r"\bwhiteboard\w*|\bco-?edit\w*|\bcollaborative edit\w*|\bcursors\b|\bmultiplayer\b",
     [("derived.collab", True)], "suggests several people editing the same state"),
    (r"\boffline\b|\bintermittent\b|\bno (?:signal|network|internet)\b|\bsyncs? when\b|\b[23]g\b|\brural\b|\bvillages?\b",
     [("requirements.offline_support", True), ("requirements.connectivity", "intermittent")],
     "suggests unreliable connectivity"),
    (r"\bsearch\w*", [("requirements.full_text_search", True)], "suggests search over content"),
    (r"\bupload\w*|\bphotos?\b|\bimages?\b|\bvideos?\b|\bdocuments?\b|\bkyc\b|\battachments?\b",
     [("requirements.file_media_storage", True)], "suggests user files"),
    (r"\bget paid\b|\bpay(?:s|ing|ment|ments)?\b|\bpaid\b|\bcheckout\b|\bsubscriptions?\b|\bper seat\b|\bupi\b|\bemi\b"
     r"|\bbilling\b|\binvoices?\b|\bpayouts?\b|\bdisburs\w*",
     [("requirements.payments", True)], "suggests taking or sending money"),
    (r"\botp\b|\bphone numbers?\b|\bmobile numbers?\b", [("requirements.auth", "phone_otp")],
     "suggests sign-in by phone number"),
    (r"\bsso\b|\bsaml\b|\bentra\b|\bazure ad\b|\bokta\b|\bemployees?\b|\bstaff\b",
     [("requirements.auth", "enterprise_sso")], "suggests company sign-in"),
    (r"\btenants?\b|\borgani[sz]ations\b|\bteams pay\b|\bper seat\b|\bworkspaces?\b",
     [("requirements.multi_tenant", True)], "suggests several customer organisations"),
    (r"\brecommend\w*|\bscoring model\b|\bcredit scor\w*|\bpredict\w*|\bclassif\w*|\bml\b|\bmachine learning\b",
     [("requirements.ml_features", "inference_api")], "suggests a prediction model"),
    (r"\bllm\b|\brag\b|\bchatbot\b|\bgpt\b|\bassistant\b|\bsemantic search\b",
     [("requirements.ml_features", "llm_rag")], "suggests a language-model feature"),
    (r"\bblog\b|\bportfolio\b|\bmarkdown\b|\bdocs site\b|\blanding page\b",
     [("derived.static_hint", True)], "suggests mostly static content"),
    (r"\bcms\b|\bnewsroom\b|\beditorial\b|\bwordpress\b|\bjournalists?\b|\bcontent (?:team|editors?|managers?)\b"
     r"|\beditors? (?:publish|write|update|edit)\w*",
     [("derived.cms", True)], "suggests editors publishing content"),
    (r"\bledger\b|\bdouble-entry\b|\btransactions?\b|\bloans?\b|\bbooking\b|\binventory\b|\borders?\b",
     [("requirements.consistency", "strong")], "suggests records that must stay exact"),
    (r"\bloans?\b|\blending\b|\bkyc\b|\bbank\w*|\bupi\b|\bemi\b",
     [("requirements.data_sensitivity", "financial")], "suggests financial data"),
    (r"\bpatients?\b|\bclinic\w*|\bmedical\b|\bhealth records?\b",
     [("requirements.data_sensitivity", "health")], "suggests health data"),
    (r"\bindia\b|\bindian\b|\bbengaluru\b|\bbangalore\b|\bmumbai\b|\bdelhi\b|\bhyderabad\b|\bchennai\b"
     r"|\bpune\b|\bkolkata\b|\brupees?\b|\bupi\b",
     [("scale.user_geography", "india")], "suggests users in India"),
    (r"\beurope\w*|\beu\b|\bgdpr\b|\bgermany\b|\bfrance\b",
     [("scale.user_geography", "eu")], "suggests users in Europe"),
    (_STAY + r"(?:in|within|inside) india\b|\bindian (?:data ?cent(?:re|er)s?|regions?|servers?)\b",
     [("infrastructure.data_residency", "india")], "says data must be stored in India"),
    (_STAY + r"(?:in|within|inside) (?:the )?(?:eu|european union|europe)\b|\beu (?:data ?cent(?:re|er)s?|regions?)\b",
     [("infrastructure.data_residency", "eu")], "says data must be stored in the EU"),
    (_STAY + r"(?:in|within|inside) (?:the )?(?:us|usa|united states)\b|\bus (?:data ?cent(?:re|er)s?|regions?)\b",
     [("infrastructure.data_residency", "us")], "says data must be stored in the US"),
    (r"\bgps\b|\blocation\b|\bmaps?\b|\bgeo-?tag\w*", [("derived.geo", True)], "suggests location data"),
    (r"\bsms\b", [("requirements.notifications", "sms")], "suggests text messages"),
    (r"\be-?mail\w*", [("requirements.notifications", "email")], "suggests email"),
    (r"\bnotify\b|\bnotifications?\b|\balerts?\b|\breminders?\b|\breports?\b|\bschedul\w*|\be-?mail\w*|\bsms\b",
     [("requirements.background_jobs", True)], "suggests asynchronous work"),
]
_COMPILED = [(re.compile(p), targets, why) for p, targets, why in _RULES]
_RAM = re.compile(r"(\d+(?:\.\d+)?) ?gb ram\b")
_LOW_END = re.compile(r"\blow-?end\b")


def parse(text) -> list[Signal]:
    """Keyword hits for a description, sorted by (field, keyword, value); applied is always False."""
    if not isinstance(text, str) or not text.strip():
        return []
    low = text.lower()
    found: dict[tuple, Signal] = {}

    def add(keyword, field, value, why):
        key = (field, keyword, repr(value))
        if key not in found:
            found[key] = Signal(keyword, field, value, False,
                                f"description mentions '{keyword}', which {why}")

    for pattern, targets, why in _COMPILED:
        for m in pattern.finditer(low):
            for field, value in targets:
                add(m.group(0), field, value, why)
    for m in _RAM.finditer(low):
        num = float(m.group(1))
        add(m.group(0), "requirements.min_device_ram_gb", int(num) if num == int(num) else num,
            "states the RAM of the weakest device")
    for m in _LOW_END.finditer(low):
        add(m.group(0), "requirements.min_device_ram_gb", 2, "suggests low-memory devices")
    return sorted(found.values(), key=lambda s: (s.field, s.keyword, repr(s.value)))


def suggest(text) -> list[dict]:
    """Suggestions for the UI: one row per (field, value) on real input fields, keywords merged."""
    merged: dict[tuple, dict] = {}
    for s in parse(text):
        if s.field not in FIELD_INDEX:
            continue
        row = merged.setdefault((s.field, repr(s.value)), {
            "field": s.field, "value": s.value, "keywords": [], "reason": s.reason})
        if s.keyword not in row["keywords"]:
            row["keywords"].append(s.keyword)
    return sorted(merged.values(), key=lambda r: (r["field"], repr(r["value"])))
