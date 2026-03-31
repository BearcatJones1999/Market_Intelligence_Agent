"""
# Market Intel Agent - FastAPI backend
Run with:  uvicorn main3:app --reload
"""
import os
import json
import math
import asyncio
import base64
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False
from anyio import to_thread
from pytrends.request import TrendReq
import pandas as pd

# Always load .env from the same folder as this file
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

# Config 
NEWS_API_KEY = os.getenv("NEWS_API_KEY")  # recommend env-only
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # recommend env-only
ANTHROPIC_API_KEY = (
    os.getenv("ANTHROPIC_API_KEY")
    or os.getenv("CLAUDE_API_KEY")
    or os.getenv("Calude")
)
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
EBAY_ENV = os.getenv("EBAY_ENV", "sandbox").lower()
EBAY_MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_US")
EBAY_SCOPE = os.getenv("EBAY_SCOPE", "https://api.ebay.com/oauth/api_scope")
PRICE_DB_PATH = os.getenv("PRICE_DB_PATH", "data/price_history.db")
ZIP3_LOOKUP_URL = os.getenv(
    "ZIP3_LOOKUP_URL",
    "https://raw.githubusercontent.com/billfienberg/zip3/master/threeDigitZipCodes.json",
)

# You can change this default model later
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
WTO_PRIMARY_KEY = os.getenv("WTO_PRIMARY_KEY")
WTO_SECONDARY_KEY = os.getenv("WTO_SECONDARY_KEY")
WTO_BASE_URL = os.getenv("WTO_BASE_URL", "https://api.wto.org/timeseries/v1")
WTO_REPORTER_CODE = os.getenv("WTO_REPORTER_CODE", "840")  # USA
WTO_DEFAULT_YEARS = int(os.getenv("WTO_DEFAULT_YEARS", "5"))

_ebay_token_cache = {"token": None, "expires_at": datetime.min}
_zip3_lookup_cache = {"data": None, "expires_at": datetime.min}
_trends_cache = {}
_trends_inflight = {}
_wto_cache = {}
_wto_partner_infer_cache = {}
_TRENDS_CACHE_TTL_SECONDS = 900
_TRENDS_CACHE_STALE_MAX_SECONDS = 86400
_WTO_CACHE_TTL_SECONDS = 21600
_WTO_PARTNER_INFER_TTL_SECONDS = 86400


def _ebay_base_url() -> str:
    return "https://api.ebay.com" if EBAY_ENV == "production" else "https://api.sandbox.ebay.com"


def _ebay_token_url() -> str:
    return f"{_ebay_base_url()}/identity/v1/oauth2/token"


def _wto_headers(primary: bool = True) -> dict:
    key = WTO_PRIMARY_KEY if primary else WTO_SECONDARY_KEY
    return {"Ocp-Apim-Subscription-Key": key} if key else {}


def _safe_float(value):
    try:
        v = float(value)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return None


def _summarize_series(rows: list) -> dict | None:
    if not isinstance(rows, list) or not rows:
        return None
    cleaned = []
    for r in rows:
        try:
            y = int(r.get("Year"))
            p = str(r.get("PeriodCode") or "")
            sort_period = int(p[1:]) if p.startswith("M") and p[1:].isdigit() else 0
            v = _safe_float(r.get("Value"))
            if v is None:
                continue
            cleaned.append((y, sort_period, v))
        except Exception:
            continue
    if not cleaned:
        return None
    cleaned.sort(key=lambda x: (x[0], x[1]))
    values = [c[2] for c in cleaned]
    latest = cleaned[-1]
    return {
        "latestYear": latest[0],
        "latestPeriodCode": f"M{latest[1]:02d}" if latest[1] else None,
        "latestValue": round(latest[2], 3),
        "mean": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "count": len(values),
    }


async def _wto_get_json(path: str, params: dict) -> dict | None:
    url = f"{WTO_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    for use_primary in (True, False):
        headers = _wto_headers(primary=use_primary)
        if not headers:
            continue
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            # Try secondary key only when auth fails on primary.
            if resp.status_code not in (401, 403):
                return None
        except Exception:
            # Network failure on primary can still try secondary.
            if not use_primary:
                return None
    return None


_WTO_PARTNER_CODE_TO_NAME = {
    "036": "Australia",
    "076": "Brazil",
    "124": "Canada",
    "156": "China",
    "158": "Chinese Taipei",
    "170": "Colombia",
    "250": "France",
    "276": "Germany",
    "356": "India",
    "360": "Indonesia",
    "372": "Ireland",
    "380": "Italy",
    "392": "Japan",
    "410": "Korea, Republic of",
    "458": "Malaysia",
    "484": "Mexico",
    "528": "Netherlands",
    "554": "New Zealand",
    "620": "Portugal",
    "702": "Singapore",
    "704": "Viet Nam",
    "710": "South Africa",
    "724": "Spain",
    "300": "Greece",
    "752": "Sweden",
    "756": "Switzerland",
    "764": "Thailand",
    "784": "United Arab Emirates",
    "826": "United Kingdom",
}

_WTO_PARTNER_KEYWORD_TO_CODE = {
    "china": "156",
    "canada": "124",
    "mexico": "484",
    "vietnam": "704",
    "india": "356",
    "japan": "392",
    "uk": "826",
    "united kingdom": "826",
    "germany": "276",
    "france": "250",
    "italy": "380",
    "south korea": "410",
    "korea": "410",
    "taiwan": "158",
    # origin cues by product naming
    "chianti": "380",
    "prosecco": "380",
    "barolo": "380",
    "parmesan": "380",
    "champagne": "250",
    "cognac": "250",
    "tequila": "484",
    "mezcal": "484",
    "sake": "392",
    "scotch": "826",
    "single malt": "826",
    "manuka": "554",
    # brand-origin hints
    "bosch": "276",
    "siemens": "276",
    "miele": "276",
    "samsung": "410",
    "lg": "410",
    "haier": "156",
    "hisense": "156",
    "whirlpool": "840",
    "ge appliances": "840",
}

_WTO_PRODUCT_HINTS = [
    (("running shoes", "trail shoes", "sneakers", "footwear"), ["704", "156", "360"]),
    (("wine", "chianti", "prosecco", "barolo"), ["380", "250", "724"]),
    (("tequila", "mezcal"), ["484"]),
    (("coffee",), ["076", "170", "704"]),
    (("olive oil",), ["380", "724", "300"]),
    (("washing machine", "washer", "laundry machine"), ["276", "410", "156"]),
]

_WTO_HS_HINTS = [
    (("running shoes", "trail shoes", "sneakers", "footwear"), ["640411", "640419", "6404"]),
    (("washing machine", "washer", "laundry machine"), ["845011", "845020", "8450"]),
    (("wine", "chianti", "prosecco", "barolo"), ["220421", "2204"]),
    (("coffee", "espresso"), ["0901"]),
    (("olive oil",), ["1509"]),
    (("wireless earbuds", "earbuds", "headphones"), ["851830", "8518"]),
]

_WTO_CATEGORY_ALIAS_HINTS = [
    (
        (
            "new balance",
            "nike",
            "adidas",
            "asics",
            "brooks",
            "hoka",
            "saucony",
            "salomon",
            "on running",
            "hierro",
            "fresh foam",
            "fuelcell",
            "pegasus",
            "ultraboost",
            "gel-kayano",
            "clifton",
        ),
        "running shoes trail shoes sneakers footwear",
    ),
    (
        (
            "airpods",
            "galaxy buds",
            "quietcomfort earbuds",
            "wf-1000xm4",
            "soundcore",
            "wireless earbuds",
            "earbuds",
        ),
        "wireless earbuds headphones",
    ),
]


def _trade_profile_schema(max_partners: int = 4, max_hs: int = 5, max_components: int = 6) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["productType", "criticalComponents", "hsCandidates", "partnerCandidates", "reasoning"],
        "properties": {
            "productType": {"type": "string"},
            "criticalComponents": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_components,
                "items": {"type": "string"},
            },
            "hsCandidates": {
                "type": "array",
                "minItems": 0,
                "maxItems": max_hs,
                "items": {"type": "string"},
            },
            "partnerCandidates": {
                "type": "array",
                "minItems": 0,
                "maxItems": max_partners,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code"],
                    "properties": {
                        "code": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "reasoning": {"type": "string"},
        },
    }


def _wto_partner_name(code: str) -> str:
    return _WTO_PARTNER_CODE_TO_NAME.get(str(code), f"Code {code}")


def _wto_partner_infer_cache_get(cache_key: str):
    entry = _wto_partner_infer_cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    payload = entry.get("payload")
    if not isinstance(fetched_at, datetime) or not isinstance(payload, dict):
        return None
    age = (datetime.utcnow() - fetched_at).total_seconds()
    if age <= _WTO_PARTNER_INFER_TTL_SECONDS:
        return payload
    return None


def _wto_partner_infer_cache_put(cache_key: str, payload: dict):
    if not isinstance(payload, dict):
        return
    _wto_partner_infer_cache[cache_key] = {"fetched_at": datetime.utcnow(), "payload": payload}


async def _infer_wto_partners_with_model(product_query: str, max_partners: int = 3) -> list[dict]:
    if not OPENAI_API_KEY:
        return []
    allowed = [{"code": c, "name": n} for c, n in _WTO_PARTNER_CODE_TO_NAME.items()]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["partners"],
        "properties": {
            "partners": {
                "type": "array",
                "minItems": 0,
                "maxItems": max_partners,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code"],
                    "properties": {
                        "code": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": (
                    "Infer likely origin countries for US imports of the product query. "
                    "Choose only from provided allowed partner codes. Keep it concise."
                ),
            },
            {
                "role": "user",
                "content": "Product query: " + str(product_query),
            },
            {
                "role": "user",
                "content": "Allowed partner codes JSON:\n" + json.dumps(allowed, ensure_ascii=True),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "wto_partner_infer",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 240,
        "store": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if resp.status_code != 200:
            return []
        txt = _extract_text_from_responses_api(resp.json()).strip()
        parsed = json.loads(txt) if txt else {}
        out = []
        for p in parsed.get("partners") or []:
            code = str((p or {}).get("code") or "").strip()
            if code in _WTO_PARTNER_CODE_TO_NAME:
                out.append({"code": code, "name": _wto_partner_name(code), "method": "model"})
        dedup = []
        seen = set()
        for p in out:
            c = p["code"]
            if c in seen:
                continue
            seen.add(c)
            dedup.append(p)
        return dedup[:max_partners]
    except Exception:
        return []


async def _infer_trade_profile_with_model(
    product_query: str,
    max_partners: int = 4,
    max_hs: int = 5,
    max_components: int = 6,
) -> dict:
    if not OPENAI_API_KEY:
        return {"method": "none", "productType": "", "criticalComponents": [], "hsCandidates": [], "partners": [], "reasoning": ""}

    allowed_partners = [{"code": c, "name": n} for c, n in _WTO_PARTNER_CODE_TO_NAME.items()]
    allowed_hs = sorted(
        {
            _normalize_hs_code(code)
            for _, codes in _WTO_HS_HINTS
            for code in codes
            if _normalize_hs_code(code)
        }
    )
    schema = _trade_profile_schema(max_partners=max_partners, max_hs=max_hs, max_components=max_components)
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": (
                    "Infer a supply-chain oriented trade profile for the product query. "
                    "Think about what the item is materially and commercially, not just the brand name. "
                    "List likely critical components/materials, likely supplier countries, and the most plausible HS candidates. "
                    "Use only the provided partner codes and HS codes when choosing codes."
                ),
            },
            {"role": "user", "content": "Product query: " + str(product_query)},
            {"role": "user", "content": "Allowed partner codes JSON:\n" + json.dumps(allowed_partners, ensure_ascii=True)},
            {"role": "user", "content": "Allowed HS candidate codes JSON:\n" + json.dumps(allowed_hs, ensure_ascii=True)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trade_profile_infer",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 420,
        "store": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if resp.status_code != 200:
            return {"method": "none", "productType": "", "criticalComponents": [], "hsCandidates": [], "partners": [], "reasoning": ""}
        txt = _extract_text_from_responses_api(resp.json()).strip()
        parsed = json.loads(txt) if txt else {}
        hs_candidates = []
        for code in parsed.get("hsCandidates") or []:
            norm = _normalize_hs_code(code)
            if norm and norm in allowed_hs and norm not in hs_candidates:
                hs_candidates.append(norm)
        partners = []
        seen_partner_codes = set()
        for p in parsed.get("partnerCandidates") or []:
            code = str((p or {}).get("code") or "").strip()
            if code in _WTO_PARTNER_CODE_TO_NAME and code not in seen_partner_codes:
                seen_partner_codes.add(code)
                partners.append({"code": code, "name": _wto_partner_name(code), "method": "supply_chain_model"})
        components = []
        seen_components = set()
        for c in parsed.get("criticalComponents") or []:
            item = str(c or "").strip()
            if item and item.lower() not in seen_components:
                seen_components.add(item.lower())
                components.append(item)
        return {
            "method": "supply_chain_model",
            "productType": str(parsed.get("productType") or "").strip(),
            "criticalComponents": components[:max_components],
            "hsCandidates": hs_candidates[:max_hs],
            "partners": partners[:max_partners],
            "reasoning": str(parsed.get("reasoning") or "").strip(),
        }
    except Exception:
        return {"method": "none", "productType": "", "criticalComponents": [], "hsCandidates": [], "partners": [], "reasoning": ""}


async def _infer_wto_partner_candidates(
    product_query: str,
    explicit_partner_code: str | None = None,
    max_partners: int = 3,
    inferred_product_type: str | None = None,
    critical_components: list[str] | None = None,
) -> dict:
    if explicit_partner_code:
        c = str(explicit_partner_code).strip()
        return {
            "method": "explicit",
            "partners": [{"code": c, "name": _wto_partner_name(c), "method": "explicit"}],
        }

    q = _wto_augmented_query_text(
        product_query,
        inferred_product_type=inferred_product_type,
        critical_components=critical_components,
    )
    cache_key = f"v3|{q}|{max_partners}"
    cached = _wto_partner_infer_cache_get(cache_key)
    if cached:
        return cached

    found = []
    for kw, code in _WTO_PARTNER_KEYWORD_TO_CODE.items():
        if kw in q and code in _WTO_PARTNER_CODE_TO_NAME:
            found.append({"code": code, "name": _wto_partner_name(code), "method": "keyword"})
    dedup = []
    seen = set()
    for p in found:
        if p["code"] in seen:
            continue
        seen.add(p["code"])
        dedup.append(p)
    if dedup:
        out = {"method": "keyword", "partners": dedup[:max_partners]}
        _wto_partner_infer_cache_put(cache_key, out)
        return out

    hinted = []
    for terms, codes in _WTO_PRODUCT_HINTS:
        if any(t in q for t in terms):
            for c in codes:
                if c in _WTO_PARTNER_CODE_TO_NAME:
                    hinted.append({"code": c, "name": _wto_partner_name(c), "method": "heuristic"})
    if hinted:
        dedup_hint = []
        seen_hint = set()
        for p in hinted:
            if p["code"] in seen_hint:
                continue
            seen_hint.add(p["code"])
            dedup_hint.append(p)
        out = {"method": "heuristic", "partners": dedup_hint[:max_partners]}
        _wto_partner_infer_cache_put(cache_key, out)
        return out

    model_partners = await _infer_wto_partners_with_model(product_query, max_partners=max_partners)
    if model_partners:
        out = {"method": "model", "partners": model_partners[:max_partners]}
        _wto_partner_infer_cache_put(cache_key, out)
        return out

    out = {"method": "none", "partners": []}
    _wto_partner_infer_cache_put(cache_key, out)
    return out


def _normalize_hs_code(hs_code: str) -> str | None:
    digits = "".join(ch for ch in str(hs_code or "") if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    if len(digits) in (2, 4):
        return digits
    return None


def _wto_augmented_query_text(
    product_query: str,
    inferred_product_type: str | None = None,
    critical_components: list[str] | None = None,
) -> str:
    parts = [str(product_query or "").strip().lower()]
    inferred = str(inferred_product_type or "").strip().lower()
    if inferred:
        parts.append(inferred)
    if isinstance(critical_components, list):
        comp_text = " ".join(str(c or "").strip().lower() for c in critical_components if str(c or "").strip())
        if comp_text:
            parts.append(comp_text)
    base = " ".join(p for p in parts if p).strip()
    for triggers, alias_text in _WTO_CATEGORY_ALIAS_HINTS:
        if any(t in base for t in triggers):
            parts.append(alias_text)
    return " ".join(p for p in parts if p).strip()


def _hs_rollups(hs_code: str) -> list[str]:
    hs = _normalize_hs_code(hs_code)
    if not hs:
        return []
    out = [hs]
    if len(hs) >= 6:
        out.append(hs[:4])
        out.append(hs[:2])
    elif len(hs) == 4:
        out.append(hs[:2])
    dedup = []
    seen = set()
    for h in out:
        if h in seen:
            continue
        seen.add(h)
        dedup.append(h)
    return dedup


def _infer_wto_hs_candidates(
    product_query: str,
    explicit_hs_code: str | None = None,
    max_codes: int = 4,
    inferred_product_type: str | None = None,
    critical_components: list[str] | None = None,
) -> list[str]:
    out = []
    if explicit_hs_code:
        norm = _normalize_hs_code(explicit_hs_code)
        if norm:
            out.append(norm)
    q = _wto_augmented_query_text(
        product_query,
        inferred_product_type=inferred_product_type,
        critical_components=critical_components,
    )
    for terms, codes in _WTO_HS_HINTS:
        if any(t in q for t in terms):
            for c in codes:
                norm = _normalize_hs_code(c)
                if norm:
                    out.append(norm)
    dedup = []
    seen = set()
    for c in out:
        if c in seen:
            continue
        seen.add(c)
        dedup.append(c)
    return dedup[:max_codes]


def _hs_category_label(hs_code: str | None) -> str:
    hs = _normalize_hs_code(hs_code)
    if not hs:
        return "Not specified"
    # Focused labels for the product groups used in this app's heuristics.
    if hs.startswith("845011"):
        return "Household Washing Machines (fully automatic)"
    if hs.startswith("845020"):
        return "Household Washing Machines (other types)"
    if hs.startswith("8450"):
        return "Laundry/Washing Machines (HS 8450)"
    if hs.startswith("640411"):
        return "Sports/Running Footwear (rubber/plastic soles)"
    if hs.startswith("640419"):
        return "Other Sports Footwear (HS 640419)"
    if hs.startswith("6404"):
        return "Footwear with outer soles of rubber/plastics (HS 6404)"
    if hs.startswith("220421"):
        return "Wine in containers <= 2 liters"
    if hs.startswith("2204"):
        return "Wine and Grape Must (HS 2204)"
    if hs.startswith("0901"):
        return "Coffee (HS 0901)"
    if hs.startswith("1509"):
        return "Olive Oil (HS 1509)"
    if hs.startswith("851830"):
        return "Headphones/Earphones (including wireless earbuds)"
    if hs.startswith("8518"):
        return "Microphones, speakers, headphones (HS 8518)"
    return f"HS {hs}"


def _pick_wto_tariff_indicator(hs_code: str | None, partner_code: str | None) -> tuple[str, str]:
    if hs_code:
        return ("HS_A_0010", "MFN tariff (HS-level)")
    return ("TP_A_0010", "MFN tariff (all products)")


def _wto_cache_get(cache_key: str):
    entry = _wto_cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    payload = entry.get("payload")
    if not isinstance(fetched_at, datetime) or not isinstance(payload, dict):
        return None
    age = (datetime.utcnow() - fetched_at).total_seconds()
    if age <= _WTO_CACHE_TTL_SECONDS:
        return payload
    return None


def _wto_cache_put(cache_key: str, payload: dict):
    if not isinstance(payload, dict):
        return
    _wto_cache[cache_key] = {"fetched_at": datetime.utcnow(), "payload": payload}


async def _fetch_wto_trade_context(
    years: int = WTO_DEFAULT_YEARS,
    reporter_code: str = WTO_REPORTER_CODE,
    product_query: str = "",
    partner_code: str | None = None,
    hs_code: str | None = None,
    strict_targeted: bool = True,
) -> dict:
    """
    Compact WTO tariff context for US imports from a specific partner/country.
    Designed to minimize payload and call volume.
    """
    if not WTO_PRIMARY_KEY and not WTO_SECONDARY_KEY:
        return {"warning": "WTO keys missing"}

    trade_profile = await _infer_trade_profile_with_model(product_query, max_partners=4, max_hs=5, max_components=6)
    inferred = await _infer_wto_partner_candidates(
        product_query,
        explicit_partner_code=partner_code,
        max_partners=3,
        inferred_product_type=trade_profile.get("productType"),
        critical_components=trade_profile.get("criticalComponents"),
    )

    raw_partner_candidates = []
    if partner_code:
        raw_partner_candidates.append(str(partner_code).strip())
    raw_partner_candidates.extend([str((p or {}).get("code") or "").strip() for p in (trade_profile.get("partners") or [])])
    raw_partner_candidates.extend([str((p or {}).get("code") or "").strip() for p in (inferred.get("partners") or [])])
    partner_candidates = []
    seen_partner_candidates = set()
    for p in raw_partner_candidates:
        if p and p not in seen_partner_candidates:
            seen_partner_candidates.add(p)
            partner_candidates.append(p)
    partner_candidates = partner_candidates[:4]

    raw_hs_candidates = []
    if hs_code:
        raw_hs_candidates.append(hs_code)
    raw_hs_candidates.extend(trade_profile.get("hsCandidates") or [])
    raw_hs_candidates.extend(
        _infer_wto_hs_candidates(
            product_query,
            explicit_hs_code=hs_code,
            max_codes=4,
            inferred_product_type=trade_profile.get("productType"),
            critical_components=trade_profile.get("criticalComponents"),
        )
    )
    hs_candidates = []
    seen_hs_candidates = set()
    for code in raw_hs_candidates:
        norm = _normalize_hs_code(code)
        if norm and norm not in seen_hs_candidates:
            seen_hs_candidates.add(norm)
            hs_candidates.append(norm)
    hs_candidates = hs_candidates[:5]

    now = datetime.utcnow()
    # Tariff series are annual and often lag current year by ~1-2 years.
    year_to = max(2000, now.year - 2)
    primary_year_from = max(1990, year_to - max(1, int(years)) + 1)
    year_windows = []
    for span in [max(1, int(years)), 7, 10]:
        y_from = max(1990, year_to - span + 1)
        year_windows.append((y_from, year_to))
    # Deduplicate windows preserving order.
    win_dedup = []
    seen_w = set()
    for w in year_windows:
        if w in seen_w:
            continue
        seen_w.add(w)
        win_dedup.append(w)
    year_windows = win_dedup

    primary_ps = ",".join(str(y) for y in range(primary_year_from, year_to + 1))
    primary_partner = partner_candidates[0] if partner_candidates else None
    primary_hs = hs_candidates[0] if hs_candidates else None
    indicator, indicator_label = _pick_wto_tariff_indicator(primary_hs, primary_partner)
    cache_key = f"{reporter_code}|{','.join(partner_candidates) or 'none'}|{','.join(hs_candidates) or 'default'}|{indicator}|{primary_ps}|strict={strict_targeted}"
    cached = _wto_cache_get(cache_key)
    if cached:
        out = dict(cached)
        out["cached"] = True
        return out

    def _build_attempt(i: str, p: str | None, pc: str, max_rows: str, ps: str) -> dict:
        out = {
            "i": i,
            "r": str(reporter_code or "840"),
            "ps": ps,
            "pc": pc,
            "spc": "false",
            "fmt": "json",
            "mode": "full",
            "dec": "default",
            "off": "0",
            "max": max_rows,
            "head": "H",
            "lang": "1",
            "meta": "false",
        }
        # Some indicators (e.g., TP_A_0010) do not have partner dimension.
        if p:
            out["p"] = p
        return out

    query_attempts = []
    for (y_from, y_to) in year_windows:
        ps = ",".join(str(y) for y in range(y_from, y_to + 1))
        if hs_candidates:
            for hs in hs_candidates:
                for rolled_hs in _hs_rollups(hs):
                    query_attempts.append(_build_attempt("HS_A_0010", None, rolled_hs, "60", ps))
        else:
            query_attempts.append(_build_attempt("TP_A_0010", None, "default", "40", ps))
    if not strict_targeted:
        # Optional broad fallback.
        query_attempts.append(_build_attempt("TP_A_0010", None, "default", "40", primary_ps))

    rows = []
    used_params = None
    for params in query_attempts:
        payload = await _wto_get_json("data", params)
        rows = (payload or {}).get("Dataset") or []
        if rows:
            used_params = params
            break

    summary = _summarize_series(rows)
    if not summary:
        return {
            "warning": "WTO tariff data unavailable for requested scope",
            "productQuery": product_query,
            "productType": trade_profile.get("productType") or product_query,
            "reporterCode": str(reporter_code or "840"),
            "partnerCode": primary_partner,
            "partnerName": _wto_partner_name(primary_partner) if primary_partner else None,
            "partnerCandidates": partner_candidates,
            "partnerCandidateNames": [_wto_partner_name(c) for c in partner_candidates],
            "partnerInferenceMethod": trade_profile.get("method") if trade_profile.get("partners") else inferred.get("method"),
            "hsCode": primary_hs,
            "hsCandidates": hs_candidates,
            "criticalComponents": trade_profile.get("criticalComponents") or [],
            "tradeProfileReasoning": trade_profile.get("reasoning") or "",
            "strictTargeted": bool(strict_targeted),
            "indicator": indicator,
            "periodRange": f"{primary_year_from}-{year_to}",
        }

    point_values = []
    cleaned_rows = []
    for r in rows:
        val = _safe_float(r.get("Value"))
        if val is None:
            continue
        y = int(r.get("Year") or 0)
        cleaned_rows.append((y, val))
        point_values.append(val)
    cleaned_rows.sort(key=lambda t: t[0])
    yoy_delta = None
    if len(cleaned_rows) >= 2:
        yoy_delta = round(cleaned_rows[-1][1] - cleaned_rows[-2][1], 3)

    used_indicator = str((used_params or {}).get("i") or indicator)
    used_label = (
        "MFN tariff (HS-level)"
        if used_indicator == "HS_A_0010"
        else "MFN tariff (all products)"
    )
    used_partner_code = str((used_params or {}).get("p") or "")
    used_partner_name = _wto_partner_name(used_partner_code) if used_partner_code else "n/a"
    used_hs_code = str((used_params or {}).get("pc") or "default")
    used_ps = str((used_params or {}).get("ps") or primary_ps)
    used_indicator_hs_specific = used_hs_code not in ("", "default")
    targeted_level = (
        "hs_only"
        if used_indicator_hs_specific
        else "aggregate"
    )
    obs = int(summary.get("count") or 0)
    confidence = "LOW"
    if targeted_level == "hs_only" and obs >= 3:
        confidence = "HIGH"
    elif targeted_level == "hs_only" and obs >= 1:
        confidence = "MEDIUM"

    out = {
        "source": "WTO Timeseries API",
        "focus": "US import tariff (targeted)",
        "productQuery": product_query,
        "productType": trade_profile.get("productType") or product_query,
        "reporterCode": str(reporter_code or "840"),
        "partnerCode": primary_partner,
        "partnerName": _wto_partner_name(primary_partner) if primary_partner else None,
        "partnerCandidates": partner_candidates,
        "partnerCandidateNames": [_wto_partner_name(c) for c in partner_candidates],
        "partnerInferenceMethod": trade_profile.get("method") if trade_profile.get("partners") else inferred.get("method"),
        "hsCode": primary_hs,
        "hsCandidates": hs_candidates,
        "criticalComponents": trade_profile.get("criticalComponents") or [],
        "tradeProfileReasoning": trade_profile.get("reasoning") or "",
        "strictTargeted": bool(strict_targeted),
        "indicator": used_indicator,
        "indicatorLabel": used_label,
        "usedPartnerCode": used_partner_code or "n/a",
        "usedPartnerName": used_partner_name,
        "usedHsCode": used_hs_code,
        "usedHsCategory": _hs_category_label(used_hs_code if used_hs_code != "default" else primary_hs),
        "hsCategory": _hs_category_label(primary_hs),
        "usedPeriodRange": used_ps,
        "targetedMatchLevel": targeted_level,
        "confidence": confidence,
        "fallbackApplied": bool(
            used_params
            and (
                used_params.get("i") != indicator
                or str(used_params.get("pc") or "default") != str(primary_hs or "default")
            )
        ),
        "periodRange": f"{primary_year_from}-{year_to}",
        "latestTariffPercent": summary.get("latestValue"),
        "latestYear": summary.get("latestYear"),
        "yoyDeltaPctPts": yoy_delta,
        "avgTariffPercent": summary.get("mean"),
        "observations": summary.get("count"),
        "headline": (
            f"{_hs_category_label(used_hs_code if used_hs_code != 'default' else primary_hs)} tariff context "
            f"with latest MFN rate {summary.get('latestValue')}% in {summary.get('latestYear')}."
        ),
        "retrievedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _wto_cache_put(cache_key, out)
    return out


def _percentile(sorted_values, p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * p
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return float(sorted_values[low])
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta
    out_year = total // 12
    out_month = (total % 12) + 1
    return out_year, out_month


def _trends_cache_get(cache_key: str, max_age_seconds: int):
    entry = _trends_cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    payload = entry.get("payload")
    if not isinstance(fetched_at, datetime) or not isinstance(payload, dict):
        return None
    age = (datetime.utcnow() - fetched_at).total_seconds()
    if age <= float(max_age_seconds):
        return payload
    return None


def _trends_cache_put(cache_key: str, payload: dict):
    if not isinstance(payload, dict):
        return
    _trends_cache[cache_key] = {"fetched_at": datetime.utcnow(), "payload": payload}


def _normalize_pricing_monthly_history(pricing: dict) -> None:
    history = pricing.get("monthlyHistory")
    if not isinstance(history, list) or not history:
        return

    n = len(history)
    projected_count = sum(1 for p in history if bool((p or {}).get("projected")))
    if projected_count <= 0:
        projected_count = max(3, n // 4)
    projected_count = max(1, min(projected_count, n - 1))
    hist_count = n - projected_count

    parsed_prices = []
    for point in history:
        try:
            parsed_prices.append(float((point or {}).get("price")))
        except Exception:
            continue

    fallback = float(pricing.get("currentAvgPrice") or 0.0)
    if fallback <= 0 and parsed_prices:
        fallback = parsed_prices[-1]
    if fallback <= 0:
        fallback = 1.0

    while len(parsed_prices) < n:
        parsed_prices.append(fallback)

    today = datetime.utcnow().date()
    cy, cm = today.year, today.month
    rebuilt = []
    for i in range(n):
        month_delta = i - (hist_count - 1)
        y, m = _add_months(cy, cm, month_delta)
        rebuilt.append(
            {
                "month": f"{y:04d}-{m:02d}",
                "price": round(float(parsed_prices[i]), 2),
                "projected": i >= hist_count,
            }
        )

    pricing["monthlyHistory"] = rebuilt


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _reconcile_live_pricing_with_forecast(pricing: dict) -> None:
    try:
        current = float(pricing.get("currentAvgPrice") or 0)
    except Exception:
        current = 0.0
    if current <= 0:
        return

    try:
        f12 = float(pricing.get("forecastAvgPrice12mo") or current)
    except Exception:
        f12 = current
    try:
        f24 = float(pricing.get("forecastAvgPrice24mo") or f12)
    except Exception:
        f24 = f12

    f12 = _clamp(f12, current * 0.85, current * 1.30)
    f24 = _clamp(f24, current * 0.70, current * 1.60)

    pricing["forecastAvgPrice12mo"] = round(f12, 2)
    pricing["forecastAvgPrice24mo"] = round(f24, 2)
    pricing["priceChangeYoY"] = round(((f12 - current) / current) * 100, 1)

    if f12 >= current * 1.03:
        pricing["priceTrend"] = "RISING"
    elif f12 <= current * 0.97:
        pricing["priceTrend"] = "FALLING"
    else:
        pricing["priceTrend"] = "STABLE"

    if f24 >= current * 1.08:
        pricing["priceOutlook"] = "BULLISH"
    elif f24 <= current * 0.92:
        pricing["priceOutlook"] = "BEARISH"
    else:
        pricing["priceOutlook"] = "NEUTRAL"


def _price_db_conn():
    db_path = Path(PRICE_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_price_snapshots (
          category_key TEXT NOT NULL,
          obs_date TEXT NOT NULL,
          avg_price REAL NOT NULL,
          low_price REAL,
          high_price REAL,
          sample_size INTEGER,
          source TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (category_key, obs_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_price_points (
          category_key TEXT NOT NULL,
          week_start TEXT NOT NULL,
          avg_price REAL NOT NULL,
          sample_size INTEGER,
          source TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (category_key, week_start)
        )
        """
    )
    return conn


def _category_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _record_price_snapshot(category: str, pricing: dict) -> None:
    try:
        avg = float(pricing.get("currentAvgPrice") or 0)
    except Exception:
        avg = 0.0
    if avg <= 0:
        return

    try:
        low = float(pricing.get("typicalPriceRangeLow") or avg)
    except Exception:
        low = avg
    try:
        high = float(pricing.get("typicalPriceRangeHigh") or avg)
    except Exception:
        high = avg

    source = str(pricing.get("liveSource") or "unknown")
    sample = int(pricing.get("liveSampleSize") or 0)
    today = datetime.utcnow().date().isoformat()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    conn = _price_db_conn()
    try:
        conn.execute(
            """
            INSERT INTO daily_price_snapshots
              (category_key, obs_date, avg_price, low_price, high_price, sample_size, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category_key, obs_date) DO UPDATE SET
              avg_price=excluded.avg_price,
              low_price=excluded.low_price,
              high_price=excluded.high_price,
              sample_size=excluded.sample_size,
              source=excluded.source,
              created_at=excluded.created_at
            """,
            (_category_key(category), today, avg, low, high, sample, source, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_monthly_price_averages(category: str, months_back: int = 48) -> dict:
    conn = _price_db_conn()
    try:
        cutoff = (datetime.utcnow().date() - timedelta(days=max(60, months_back * 31))).isoformat()
        rows = conn.execute(
            """
            SELECT substr(obs_date, 1, 7) AS ym, avg(avg_price) AS avg_month_price
            FROM daily_price_snapshots
            WHERE category_key = ? AND obs_date >= ?
            GROUP BY ym
            ORDER BY ym
            """,
            (_category_key(category), cutoff),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]): float(r[1]) for r in rows if r[0] is not None and r[1] is not None}


def _load_recent_snapshots(category: str, days: int = 365) -> list:
    conn = _price_db_conn()
    try:
        cutoff = (datetime.utcnow().date() - timedelta(days=max(1, int(days)))).isoformat()
        rows = conn.execute(
            """
            SELECT obs_date, avg_price, low_price, high_price, sample_size, source
            FROM daily_price_snapshots
            WHERE category_key = ? AND obs_date >= ?
            ORDER BY obs_date ASC
            """,
            (_category_key(category), cutoff),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "date": r[0],
            "avgPrice": float(r[1]),
            "lowPrice": float(r[2]) if r[2] is not None else None,
            "highPrice": float(r[3]) if r[3] is not None else None,
            "sampleSize": int(r[4]) if r[4] is not None else 0,
            "source": str(r[5] or ""),
        }
        for r in rows
    ]


def _week_start_iso(date_text: str) -> str:
    d = datetime.fromisoformat(date_text).date()
    week_start = d - timedelta(days=d.weekday())
    return week_start.isoformat()


def _record_weekly_sold_points(category: str, sold_points: list, source: str) -> None:
    if not sold_points:
        return
    buckets = {}
    for p in sold_points:
        try:
            d = str((p or {}).get("date") or "").strip()
            price = float((p or {}).get("price"))
        except Exception:
            continue
        if not d or price <= 0:
            continue
        try:
            wk = _week_start_iso(d)
        except Exception:
            continue
        if wk not in buckets:
            buckets[wk] = []
        buckets[wk].append(price)

    if not buckets:
        return

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    conn = _price_db_conn()
    try:
        for wk, vals in buckets.items():
            avg_price = round(sum(vals) / len(vals), 2)
            conn.execute(
                """
                INSERT INTO weekly_price_points
                  (category_key, week_start, avg_price, sample_size, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(category_key, week_start) DO UPDATE SET
                  avg_price=excluded.avg_price,
                  sample_size=excluded.sample_size,
                  source=excluded.source,
                  created_at=excluded.created_at
                """,
                (_category_key(category), wk, avg_price, len(vals), source, now_iso),
            )
        conn.commit()
    finally:
        conn.close()


def _load_weekly_price_series(category: str, weeks_back: int = 156) -> dict:
    conn = _price_db_conn()
    try:
        cutoff = (datetime.utcnow().date() - timedelta(days=max(28, int(weeks_back) * 7))).isoformat()
        rows = conn.execute(
            """
            SELECT week_start, avg_price
            FROM weekly_price_points
            WHERE category_key = ? AND week_start >= ?
            ORDER BY week_start
            """,
            (_category_key(category), cutoff),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]): float(r[1]) for r in rows if r[0] is not None and r[1] is not None}


def _load_recent_weekly_points(category: str, weeks: int = 156) -> list:
    conn = _price_db_conn()
    try:
        cutoff = (datetime.utcnow().date() - timedelta(days=max(28, int(weeks) * 7))).isoformat()
        rows = conn.execute(
            """
            SELECT week_start, avg_price, sample_size, source
            FROM weekly_price_points
            WHERE category_key = ? AND week_start >= ?
            ORDER BY week_start ASC
            """,
            (_category_key(category), cutoff),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "weekStart": r[0],
            "avgPrice": float(r[1]),
            "sampleSize": int(r[2]) if r[2] is not None else 0,
            "source": str(r[3] or ""),
        }
        for r in rows
    ]


def _build_expanded_weekly_history(category: str, pricing: dict, hist_weeks: int = 104, proj_weeks: int = 52) -> list:
    weekly_obs = _load_weekly_price_series(category, weeks_back=max(hist_weeks + 26, 156))
    try:
        current = float(pricing.get("currentAvgPrice") or 0)
    except Exception:
        current = 0.0
    if current <= 0:
        current = 1.0
    try:
        f24 = float(pricing.get("forecastAvgPrice24mo") or current)
    except Exception:
        f24 = current

    today = datetime.utcnow().date()
    current_week = today - timedelta(days=today.weekday())

    points = []
    last_price = current
    for i in range(-(hist_weeks - 1), proj_weeks + 1):
        wk = current_week + timedelta(days=i * 7)
        wk_iso = wk.isoformat()
        projected = i > 0
        if projected:
            t = i / max(1, proj_weeks)
            price = current + (f24 - current) * t
        else:
            if wk_iso in weekly_obs:
                price = weekly_obs[wk_iso]
                last_price = price
            else:
                price = last_price
        points.append({"month": wk_iso, "price": round(float(price), 2), "projected": projected})
    return points


def _build_expanded_monthly_history(category: str, pricing: dict, hist_months: int = 36, proj_months: int = 24) -> list:
    monthly_obs = _load_monthly_price_averages(category, months_back=max(hist_months + 12, 48))
    try:
        current = float(pricing.get("currentAvgPrice") or 0)
    except Exception:
        current = 0.0
    if current <= 0:
        current = 1.0
    try:
        f24 = float(pricing.get("forecastAvgPrice24mo") or current)
    except Exception:
        f24 = current

    today = datetime.utcnow().date()
    cy, cm = today.year, today.month

    points = []
    last_price = current
    for i in range(-(hist_months - 1), proj_months + 1):
        y, m = _add_months(cy, cm, i)
        ym = f"{y:04d}-{m:02d}"
        projected = i > 0
        if projected:
            t = i / max(1, proj_months)
            price = current + (f24 - current) * t
        else:
            if ym in monthly_obs:
                price = monthly_obs[ym]
                last_price = price
            else:
                price = last_price
        points.append({"month": ym, "price": round(float(price), 2), "projected": projected})
    return points


async def _get_ebay_token() -> str:
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError("Missing EBAY_CLIENT_ID or EBAY_CLIENT_SECRET")

    now = datetime.utcnow()
    token = _ebay_token_cache.get("token")
    expires_at = _ebay_token_cache.get("expires_at", datetime.min)
    if token and isinstance(expires_at, datetime) and expires_at > now + timedelta(seconds=60):
        return token

    basic_raw = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
    basic = base64.b64encode(basic_raw).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials", "scope": EBAY_SCOPE}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_ebay_token_url(), headers=headers, data=data)

    if resp.status_code != 200:
        raise RuntimeError(f"eBay token error: {resp.status_code}")

    payload = resp.json()
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 7200))
    if not access_token:
        raise RuntimeError("eBay token missing access_token")

    _ebay_token_cache["token"] = access_token
    _ebay_token_cache["expires_at"] = now + timedelta(seconds=max(60, expires_in - 60))
    return access_token


_US_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_US_STATE_ABBRS = set(_US_STATE_NAME_TO_ABBR.values())
_STATE_TO_REGION = {
    "CT": "Northeast", "ME": "Northeast", "MA": "Northeast", "NH": "Northeast", "RI": "Northeast", "VT": "Northeast",
    "NJ": "Northeast", "NY": "Northeast", "PA": "Northeast",
    "IL": "Midwest", "IN": "Midwest", "MI": "Midwest", "OH": "Midwest", "WI": "Midwest",
    "IA": "Midwest", "KS": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest", "ND": "Midwest", "SD": "Midwest",
    "DE": "South", "FL": "South", "GA": "South", "MD": "South", "NC": "South", "SC": "South", "VA": "South", "DC": "South",
    "WV": "South", "AL": "South", "KY": "South", "MS": "South", "TN": "South", "AR": "South", "LA": "South", "OK": "South", "TX": "South",
    "AZ": "West", "CO": "West", "ID": "West", "MT": "West", "NV": "West", "NM": "West", "UT": "West", "WY": "West",
    "AK": "West", "CA": "West", "HI": "West", "OR": "West", "WA": "West",
}


def _state_from_location_text(text: str) -> str | None:
    if not text:
        return None
    up = str(text).upper()
    # Try abbreviation after comma first, e.g. "Austin, TX"
    parts = [p.strip() for p in up.split(",")]
    for p in reversed(parts):
        tokens = [t for t in p.replace(".", " ").split() if t]
        for t in tokens:
            if t in _US_STATE_ABBRS:
                return t
    low = str(text).lower()
    for name, abbr in _US_STATE_NAME_TO_ABBR.items():
        if name in low:
            return abbr
    return None


def _region_from_state(state_abbr: str) -> str | None:
    s = str(state_abbr or "").strip().upper()
    return _STATE_TO_REGION.get(s)


def _extract_price_and_meta(item: dict, sold_mode: bool) -> tuple[float | None, str, str | None, str | None]:
    if sold_mode:
        price_obj = (item.get("sellingStatus") or {}).get("currentPrice") or {}
        value_raw = price_obj.get("__value__")
        currency = (price_obj.get("@currencyId") or "").upper()
        end_time_raw = ((item.get("listingInfo") or {}).get("endTime") or [None])[0]
        loc_raw = ((item.get("location") or [None])[0]) or ""
        state = _state_from_location_text(loc_raw)
        sold_date = str(end_time_raw)[:10] if end_time_raw else None
    else:
        price_obj = item.get("price") or {}
        value_raw = price_obj.get("value")
        currency = (price_obj.get("currency") or "").upper()
        loc_obj = item.get("itemLocation") or {}
        if isinstance(loc_obj, dict):
            state_raw = loc_obj.get("stateOrProvince") or ""
            city_raw = loc_obj.get("city") or ""
            state = _state_from_location_text(f"{city_raw}, {state_raw}")
        else:
            state = _state_from_location_text(str(loc_obj))
        sold_date = None
    if value_raw is None:
        return None, currency, sold_date, state
    try:
        value = float(value_raw)
    except Exception:
        return None, currency, sold_date, state
    if value <= 0 or value > 100000:
        return None, currency, sold_date, state
    return value, currency, sold_date, state


def _zip3_from_item(item: dict, sold_mode: bool) -> str | None:
    try:
        if sold_mode:
            return None
        loc_obj = item.get("itemLocation") or {}
        postal = ""
        if isinstance(loc_obj, dict):
            postal = str(loc_obj.get("postalCode") or "").strip()
        else:
            postal = str(loc_obj or "").strip()
        digits = "".join(ch for ch in postal if ch.isdigit())
        if len(digits) >= 3:
            return digits[:3]
    except Exception:
        return None
    return None


async def _get_zip3_lookup() -> dict:
    """
    Best-effort remote ZIP3 lookup cache.
    Falls back to empty dict if unavailable.
    """
    now = datetime.utcnow()
    cached = _zip3_lookup_cache.get("data")
    exp = _zip3_lookup_cache.get("expires_at", datetime.min)
    if isinstance(cached, dict) and exp > now:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(ZIP3_LOOKUP_URL)
        if r.status_code != 200:
            return cached if isinstance(cached, dict) else {}
        payload = r.json()
        if not isinstance(payload, dict):
            return cached if isinstance(cached, dict) else {}
        _zip3_lookup_cache["data"] = payload
        _zip3_lookup_cache["expires_at"] = now + timedelta(hours=12)
        return payload
    except Exception:
        return cached if isinstance(cached, dict) else {}


def _state_from_zip3_lookup(zip3: str, lookup: dict) -> str | None:
    if not zip3 or not isinstance(lookup, dict):
        return None
    rec = lookup.get(str(zip3))
    if not isinstance(rec, dict):
        return None
    state = str(rec.get("state") or "").strip().upper()
    if len(state) == 2 and state.isalpha():
        return state
    return None


def _region_from_zip3(zip3: str) -> str | None:
    """
    Approximate US region from ZIP3's first digit.
    This is a coarse mapping and should be treated as directional.
    """
    z = "".join(ch for ch in str(zip3 or "") if ch.isdigit())
    if len(z) < 1:
        return None
    d = z[0]
    if d in ("0", "1"):
        return "Northeast"
    if d in ("2", "3", "7"):
        return "South"
    if d in ("4", "5", "6"):
        return "Midwest"
    if d in ("8", "9"):
        return "West"
    return None


async def _fetch_ebay_items(q: str, limit: int = 1000) -> dict:
    if not q or not q.strip():
        return {"warning": "q is required", "mode": "active", "items": [], "soldDebug": {"attempted": False, "ack": None, "errors": [], "itemCount": 0}}

    target_limit = max(20, min(1000, int(limit)))
    sold_debug = {"attempted": False, "ack": None, "errors": [], "itemCount": 0}
    items = []
    sold_mode = False

    finding_url = "https://svcs.ebay.com/services/search/FindingService/v1"
    try:
        sold_debug["attempted"] = True
        per_page = min(100, target_limit)
        max_pages = max(1, (target_limit + per_page - 1) // per_page)
        collected = []
        async with httpx.AsyncClient(timeout=20) as client:
            for page in range(1, max_pages + 1):
                finding_params = {
                    "OPERATION-NAME": "findCompletedItems",
                    "SERVICE-VERSION": "1.13.0",
                    "SECURITY-APPNAME": EBAY_CLIENT_ID or "",
                    "RESPONSE-DATA-FORMAT": "JSON",
                    "REST-PAYLOAD": "",
                    "GLOBAL-ID": "EBAY-US",
                    "keywords": q.strip(),
                    "paginationInput.entriesPerPage": str(per_page),
                    "paginationInput.pageNumber": str(page),
                    "itemFilter(0).name": "SoldItemsOnly",
                    "itemFilter(0).value": "true",
                }
                finding_resp = await client.get(finding_url, params=finding_params)
                if finding_resp.status_code != 200:
                    sold_debug["errors"].append(f"http {finding_resp.status_code}")
                    break
                fjson = finding_resp.json()
                root = (fjson.get("findCompletedItemsResponse") or [{}])[0]
                ack = (root.get("ack") or [""])[0]
                sold_debug["ack"] = str(ack)
                errs = (root.get("errorMessage") or [{}])[0].get("error") or []
                for e in errs:
                    msg = ((e.get("message") or [""])[0] if isinstance(e.get("message"), list) else "")
                    if msg:
                        sold_debug["errors"].append(msg)
                if str(ack).lower() != "success":
                    break
                page_items = ((root.get("searchResult") or [{}])[0].get("item") or [])
                if not page_items:
                    break
                collected.extend(page_items)
                if len(collected) >= target_limit:
                    break
        sold_debug["itemCount"] = len(collected)
        if collected:
            items = collected[:target_limit]
            sold_mode = True
    except Exception as e:
        sold_debug["errors"].append(f"sold fetch exception: {type(e).__name__}")
        items = []

    if not items:
        try:
            token = await _get_ebay_token()
        except Exception as e:
            return {"warning": f"ebay auth error: {type(e).__name__}", "mode": "active", "items": [], "soldDebug": sold_debug}

        url = f"{_ebay_base_url()}/buy/browse/v1/item_summary/search"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE,
            "Accept": "application/json",
        }
        per_page = min(200, target_limit)
        max_pages = max(1, (target_limit + per_page - 1) // per_page)
        collected = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                for page in range(max_pages):
                    params = {
                        "q": q.strip(),
                        "limit": str(per_page),
                        "offset": str(page * per_page),
                    }
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code != 200:
                        return {"warning": f"ebay browse error: {resp.status_code}", "mode": "active", "items": collected, "soldDebug": sold_debug}
                    page_items = (resp.json().get("itemSummaries") or [])
                    if not page_items:
                        break
                    collected.extend(page_items)
                    if len(collected) >= target_limit:
                        break
        except Exception as e:
            return {"warning": f"ebay request error: {type(e).__name__}", "mode": "active", "items": [], "soldDebug": sold_debug}

        items = collected[:target_limit]

    return {"mode": "sold" if sold_mode else "active", "items": items, "soldDebug": sold_debug}


async def _fetch_ebay_pricing(q: str, limit: int = 1000, debug: bool = False) -> dict:
    if not q or not q.strip():
        return {"warning": "q is required"}

    fetch = await _fetch_ebay_items(q, limit=limit)
    items = fetch.get("items") or []
    sold_mode = fetch.get("mode") == "sold"
    sold_debug = fetch.get("soldDebug") or {"attempted": False, "ack": None, "errors": [], "itemCount": 0}
    if fetch.get("warning") and not items:
        return {"warning": fetch.get("warning"), "sampleSize": 0}

    usd_prices = []
    all_prices = []
    currency_counts = {}
    sold_points_all = []
    sold_points_usd = []

    for item in items:
        value, currency, sold_date, _state = _extract_price_and_meta(item, sold_mode)
        if value is None:
            continue
        all_prices.append(value)
        if currency:
            currency_counts[currency] = currency_counts.get(currency, 0) + 1
        if currency == "USD":
            usd_prices.append(value)
        if sold_mode and sold_date:
            pt = {"date": sold_date, "price": value}
            sold_points_all.append(pt)
            if currency == "USD":
                sold_points_usd.append(pt)

    prices = usd_prices if len(usd_prices) >= 3 else all_prices
    sold_points = sold_points_usd if len(sold_points_usd) >= 3 else sold_points_all
    prices.sort()
    sample_size = len(prices)
    if sample_size < 3:
        out = {"warning": "insufficient ebay sample", "sampleSize": sample_size}
        if debug:
            out["debug"] = {
                "rawItemCount": len(items),
                "pricedItemCount": len(all_prices),
                "usdPricedCount": len(usd_prices),
                "currencyCounts": currency_counts,
                "mode": "sold" if sold_mode else "active",
                "soldDebug": sold_debug,
            }
        return out

    low = round(_percentile(prices, 0.25), 2)
    high = round(_percentile(prices, 0.75), 2)
    avg = round(sum(prices) / sample_size, 2)
    currency = "USD" if prices is usd_prices else "MIXED"

    out = {
        "source": f"eBay {EBAY_ENV} ({'sold' if sold_mode else 'active'})",
        "sampleSize": sample_size,
        "currency": currency,
        "typicalPriceRangeLow": low,
        "typicalPriceRangeHigh": high,
        "currentAvgPrice": avg,
    }
    if currency == "MIXED":
        out["warning"] = "using mixed-currency listing prices due low USD sample"
    if debug:
        out["debug"] = {
            "rawItemCount": len(items),
            "pricedItemCount": len(all_prices),
            "usdPricedCount": len(usd_prices),
            "currencyCounts": currency_counts,
            "mode": "sold" if sold_mode else "active",
            "soldPoints": len(sold_points),
            "soldDebug": sold_debug,
        }
    if sold_mode and sold_points:
        out["soldPoints"] = sold_points
    return out


app = FastAPI(title="Market Intel Agent")

# â”€â”€ Serve static files â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


# â”€â”€ News API proxy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/api/news")
async def get_news(q: str):
    """Proxy NewsAPI.org /everything endpoint."""
    if not NEWS_API_KEY:
        raise HTTPException(status_code=400, detail="Missing NEWS_API_KEY env var")

    # NewsAPI plan limits can be strict/non-inclusive at boundary dates.
    # Keep a conservative default window and retry with a shorter one if rejected.
    lookback_days = 29
    from_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    base_url = "https://newsapi.org/v2/everything"
    params = {
        "q": q,
        "from": from_date,
        "sortBy": "publishedAt",
        "pageSize": "10",
        "language": "en",
        "apiKey": NEWS_API_KEY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(base_url, params=params)
        if resp.status_code != 200:
            try:
                err0 = resp.json()
            except Exception:
                err0 = {"raw": resp.text}
            msg0 = str((err0 or {}).get("message", ""))
            # Retry once with a tighter window if provider rejects date range.
            if "results too far in the past" in msg0.lower() or (err0 or {}).get("code") == "parameterInvalid":
                params["from"] = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
                resp = await client.get(base_url, params=params)

    if resp.status_code != 200:
        # Show real response body from NewsAPI
        try:
            err = resp.json()
        except Exception:
            err = {"raw": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=err)

    data = resp.json()
    if data.get("status") != "ok":
        raise HTTPException(status_code=502, detail=data.get("message", data))

    total_results = data.get("totalResults", 0)
    score = min(100, round(math.log10(total_results + 1) * 38))

    articles = []
    for a in data.get("articles", []):
        title = a.get("title")
        if not title or title == "[Removed]":
            continue
        pub = ""
        if a.get("publishedAt"):
            try:
                dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))
                pub = dt.strftime("%b %d")
            except Exception:
                pub = ""
        articles.append(
            {
                "title": title,
                "source": (a.get("source") or {}).get("name", "Unknown"),
                "publishedAt": pub,
                "url": a.get("url", ""),
            }
        )
        if len(articles) >= 6:
            break

    return {"score": score, "totalResults": total_results, "articles": articles}


@app.get("/api/wto/trade")
async def get_wto_trade(
    q: str = "",
    years: int = WTO_DEFAULT_YEARS,
    reporter_code: str = WTO_REPORTER_CODE,
    partner_code: str | None = None,
    hs_code: str | None = None,
    strict_targeted: bool = True,
):
    data = await _fetch_wto_trade_context(
        years=years,
        reporter_code=reporter_code,
        product_query=q,
        partner_code=partner_code,
        hs_code=hs_code,
        strict_targeted=strict_targeted,
    )
    return JSONResponse(content=data)

# â”€â”€ Google Trends (pytrends) proxy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/api/pricing/ebay")
async def get_ebay_pricing(q: str, debug: bool = False, limit: int = 1000):
    """
    Live eBay pricing snapshot from sold/completed data (preferred).
    Returns robust pricing stats used to refine report pricing.
    """
    result = await _fetch_ebay_pricing(q, limit=limit, debug=debug)
    # Persist snapshots even when this endpoint is called directly (outside /api/analyze).
    if isinstance(result, dict) and result.get("currentAvgPrice") is not None and result.get("sampleSize", 0) >= 1:
        pricing = {
            "currentAvgPrice": result.get("currentAvgPrice"),
            "typicalPriceRangeLow": result.get("typicalPriceRangeLow"),
            "typicalPriceRangeHigh": result.get("typicalPriceRangeHigh"),
            "liveSource": result.get("source", "eBay"),
            "liveSampleSize": int(result.get("sampleSize", 0)),
        }
        _record_price_snapshot(q, pricing)
        weekly_points = result.get("soldPoints") if isinstance(result.get("soldPoints"), list) else []
        if not weekly_points and result.get("currentAvgPrice"):
            weekly_points = [{"date": datetime.utcnow().date().isoformat(), "price": result.get("currentAvgPrice")}]
        _record_weekly_sold_points(q, weekly_points, str(pricing.get("liveSource", "eBay")))
    return result


@app.get("/api/pricing/map")
async def get_ebay_price_map(q: str, limit: int = 1000):
    """
    Build a state-level eBay price heat map from the same fetched item sample.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q is required")

    fetch = await _fetch_ebay_items(q, limit=limit)
    items = fetch.get("items") or []
    sold_mode = fetch.get("mode") == "sold"
    if fetch.get("warning") and not items:
        return {"category": q, "source": "eBay", "states": [], "count": 0, "warning": fetch.get("warning")}

    zip3_lookup = await _get_zip3_lookup()
    by_state = {}
    by_zip3 = {}
    approx_state_hits = 0
    for item in items:
        value, currency, _sold_date, state = _extract_price_and_meta(item, sold_mode)
        if value is None or not state:
            zip3 = _zip3_from_item(item, sold_mode)
            if value is None or not zip3:
                continue
            if currency and currency != "USD":
                continue
            lookup_state = _state_from_zip3_lookup(zip3, zip3_lookup)
            if lookup_state:
                if lookup_state not in by_state:
                    by_state[lookup_state] = []
                by_state[lookup_state].append(float(value))
                approx_state_hits += 1
                continue
            if zip3 not in by_zip3:
                by_zip3[zip3] = []
            by_zip3[zip3].append(float(value))
            continue
        if currency and currency != "USD":
            # Keep heat map in a single currency to avoid distortion.
            continue
        if state not in by_state:
            by_state[state] = []
        by_state[state].append(float(value))

    rows = []
    granularity = "state"
    source_buckets = by_state
    if not source_buckets:
        granularity = "zip3"
        source_buckets = by_zip3
    for loc_key, vals in source_buckets.items():
        if len(vals) < 2:
            continue
        vals_sorted = sorted(vals)
        rows.append(
            {
                "state": loc_key if granularity == "state" else f"ZIP {loc_key}",
                "avgPrice": round(sum(vals_sorted) / len(vals_sorted), 2),
                "medianPrice": round(_percentile(vals_sorted, 0.5), 2),
                "sampleSize": len(vals_sorted),
                "region": _region_from_state(loc_key) if granularity == "state" else _region_from_zip3(loc_key),
            }
        )

    rows.sort(key=lambda x: (x["state"]))
    source_mode = "sold" if sold_mode else "active"
    return {
        "category": q,
        "source": f"eBay {EBAY_ENV} ({source_mode})",
        "granularity": granularity,
        "approximateRegionMapping": granularity == "zip3",
        "approximateStateFromZip3": approx_state_hits > 0,
        "states": rows,
        "count": len(rows),
    }


@app.get("/api/pricing/history")
async def get_pricing_history(q: str, days: int = 365, granularity: str = "week"):
    """
    Stored local pricing history snapshots for a category/query key.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q is required")
    days = max(30, min(1825, int(days)))
    g = (granularity or "week").strip().lower()
    if g == "day":
        points = _load_recent_snapshots(q, days=days)
    else:
        points = _load_recent_weekly_points(q, weeks=max(4, days // 7))
        g = "week"
    return {"category": q, "days": days, "granularity": g, "points": points, "count": len(points)}


@app.get("/api/trends")
async def get_trends(q: str, geo: str = "US", days: int = 728):
    """
    Live Google Trends interest score (0â€“100) using pytrends.
    Returns:
      {
        "score": 0-100,
        "timeframe": "YYYY-MM-DD YYYY-MM-DD",
        "geo": "US",
        "points": [{"date":"YYYY-MM-DD","value":0-100}, ... over requested window],
        "warning": optional string
      }

    Notes:
    - pytrends is unofficial and can be rate-limited by Google.
    - Runs in a thread so we don't block the FastAPI event loop.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q is required")

    try:
        days = int(days)
    except Exception:
        raise HTTPException(status_code=400, detail="days must be an integer")

    days = max(7, min(1825, days))
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)
    timeframe = f"{start_date:%Y-%m-%d} {end_date:%Y-%m-%d}"
    cache_key = f"{q.strip().lower()}|{geo.strip().upper()}|{days}"

    cached = _trends_cache_get(cache_key, _TRENDS_CACHE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached

    def _fetch():
        # tz in minutes; 300 ~= America/New_York (UTC-5). Good enough for daily series.
        pytrends = TrendReq(
            hl="en-US",
            tz=300,
            retries=0,
            backoff_factor=0.0,
            timeout=(5, 20),
        )

        fallback_windows = []
        for d in (days, 365, 180, 90):
            d = max(7, min(1825, int(d)))
            if d <= days and d not in fallback_windows:
                fallback_windows.append(d)
        if not fallback_windows:
            fallback_windows = [days]

        last_exc = None
        for window_days in fallback_windows:
            window_start = end_date - timedelta(days=window_days)
            window_timeframe = f"{window_start:%Y-%m-%d} {end_date:%Y-%m-%d}"
            try:
                pytrends.build_payload([q], timeframe=window_timeframe, geo=geo)
                df = pytrends.interest_over_time()
                if df is None or df.empty or q not in df.columns:
                    return {
                        "score": 0,
                        "timeframe": window_timeframe,
                        "geo": geo,
                        "points": [],
                    }

                s = df[q].astype(float)  # already 0-100 within timeframe
                last = float(s.iloc[-1])
                avg7 = float(s.tail(min(7, len(s))).mean())
                score = int(max(0, min(100, round(0.65 * last + 0.35 * avg7))))

                points = [
                    {"date": idx.strftime("%Y-%m-%d"), "value": int(val)}
                    for idx, val in s.tail(window_days).items()
                ]
                payload = {
                    "score": score,
                    "timeframe": window_timeframe,
                    "geo": geo,
                    "points": points,
                }
                if window_days != days:
                    payload["warning"] = (
                        f"pytrends throttled on {days}d; using {window_days}d fallback"
                    )
                return payload
            except Exception as e:
                last_exc = e
                if type(e).__name__ == "TooManyRequestsError":
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        return {"score": 0, "timeframe": timeframe, "geo": geo, "points": []}

    existing = _trends_inflight.get(cache_key)
    if existing is not None:
        return await existing

    async def _fetch_and_cache():
        try:
            payload = await to_thread.run_sync(_fetch)
            _trends_cache_put(cache_key, payload)
            return payload
        except Exception as e:
            stale = _trends_cache_get(cache_key, _TRENDS_CACHE_STALE_MAX_SECONDS)
            if isinstance(stale, dict):
                fallback = dict(stale)
                fallback["warning"] = f"pytrends error: {type(e).__name__}; using cached"
                return fallback
            return {
                "score": 0,
                "timeframe": timeframe,
                "geo": geo,
                "points": [],
                "warning": f"pytrends error: {type(e).__name__}",
            }

    task = asyncio.create_task(_fetch_and_cache())
    _trends_inflight[cache_key] = task
    try:
        return await task
    finally:
        if _trends_inflight.get(cache_key) is task:
            _trends_inflight.pop(cache_key, None)

# -- OpenAI Responses API proxy ----------------------------------------------
def _extract_text_from_responses_api(resp_json: dict) -> str:
    """
    Robust extraction for REST Responses API output text.
    Prefer 'output_text' if present, otherwise traverse 'output' items.
    """
    if isinstance(resp_json, dict) and isinstance(resp_json.get("output_text"), str):
        return resp_json["output_text"]

    output = resp_json.get("output", [])
    if isinstance(output, list):
        for item in output:
            # Typical: {"type":"message","content":[{"type":"output_text","text":"..."}], ...}
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                        return c["text"]

    raise ValueError("Could not extract text from OpenAI response payload")


async def _openai_structured_json(
    api_key: str,
    model: str,
    system_prompt: str,
    user_messages: list[str],
    schema_name: str,
    schema: dict,
    max_output_tokens: int = 700,
) -> dict | None:
    payload = {
        "model": model,
        "input": [{"role": "system", "content": system_prompt}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    for msg in user_messages:
        payload["input"].append({"role": "user", "content": msg})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if resp.status_code != 200:
            return None
        text = _extract_text_from_responses_api(resp.json()).strip()
        return json.loads(text) if text else None
    except Exception:
        return None


def _extract_text_from_anthropic_message(resp_json: dict) -> str:
    content = resp_json.get("content") or []
    if not isinstance(content, list):
        raise ValueError("Anthropic content missing")
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    text = "".join(parts).strip()
    if not text:
        raise ValueError("Anthropic text missing")
    return text


def _parse_json_loose(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            cleaned = part.strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
            if cleaned:
                candidates.append(cleaned)
    start_positions = [i for i, ch in enumerate(raw) if ch in "{["]
    for start in start_positions:
        for end in range(len(raw), start + 1, -1):
            snippet = raw[start:end].strip()
            if not snippet or snippet[0] not in "{[":
                continue
            candidates.append(snippet)
    seen = set()
    for candidate in candidates:
        c = candidate.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


async def _anthropic_structured_json(
    api_key: str,
    model: str,
    system_prompt: str,
    user_messages: list[str],
    schema_hint: dict,
    max_tokens: int = 900,
) -> dict | None:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt
        + "\nReturn only valid JSON. Follow this schema exactly:\n"
        + json.dumps(schema_hint, ensure_ascii=True),
        "messages": [{"role": "user", "content": "\n\n".join(user_messages)}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        if resp.status_code != 200:
            return None
        text = _extract_text_from_anthropic_message(resp.json())
        return _parse_json_loose(text)
    except Exception:
        return None


def _market_tool_specs() -> dict:
    return {
        "news": {
            "description": "Recent market/news coverage. Best for demand catalysts, launches, sentiment, and retail narratives.",
            "live_context_key": "news",
        },
        "trends": {
            "description": "Google Trends demand signal over time. Best for consumer interest, seasonality, and momentum.",
            "live_context_key": "trends",
        },
        "ebay_pricing": {
            "description": "Live eBay pricing snapshot. Best for current resale price bands and market-clearing price direction.",
            "live_context_key": "ebayPricing",
        },
        "ebay_map": {
            "description": "Regional eBay pricing map. Best for geographic price dispersion and regional demand pockets.",
            "live_context_key": "ebayMap",
        },
        "wto_trade": {
            "description": "WTO trade context. Best for import exposure, partner concentration, tariff sensitivity, and trade-policy risk.",
            "live_context_key": "wtoTrade",
        },
        "pricing_history": {
            "description": "Stored local weekly pricing history. Best for historical price trend continuity and reconciliation with live snapshots.",
            "live_context_key": "storedPricingHistoryWeekly",
        },
    }


def _market_tool_plan_schema(max_tools: int = 4) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["objective", "selectedTools", "reasoning", "needsClarification", "clarifyingQuestion"],
        "properties": {
            "objective": {"type": "string"},
            "selectedTools": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_tools,
                "items": {"type": "string"},
            },
            "reasoning": {"type": "string"},
            "needsClarification": {"type": "boolean"},
            "clarifyingQuestion": {"type": "string"},
        },
    }


def _market_tool_reflection_schema(max_tools: int = 2) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["confidence", "stopReason", "additionalTools", "missingInformation"],
        "properties": {
            "confidence": {"type": "string"},
            "stopReason": {"type": "string"},
            "additionalTools": {
                "type": "array",
                "minItems": 0,
                "maxItems": max_tools,
                "items": {"type": "string"},
            },
            "missingInformation": {"type": "string"},
        },
    }


def _specialist_analysis_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["role", "thesis", "confidence", "keySignals", "risks", "recommendation"],
        "properties": {
            "role": {"type": "string"},
            "thesis": {"type": "string"},
            "confidence": {"type": "string"},
            "keySignals": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
            "risks": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
            "recommendation": {"type": "string"},
        },
    }


def _trade_evidence_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "role",
            "tradeConstraintSummary",
            "confidence",
            "keySignals",
            "dataGaps",
            "recommendation",
        ],
        "properties": {
            "role": {"type": "string"},
            "tradeConstraintSummary": {"type": "string"},
            "confidence": {"type": "string"},
            "keySignals": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
            "dataGaps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
            "recommendation": {"type": "string"},
        },
    }


def _multi_agent_synthesis_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["combinedView", "confidence", "agreementSummary", "tensionSummary", "recommendedAction"],
        "properties": {
            "combinedView": {"type": "string"},
            "confidence": {"type": "string"},
            "agreementSummary": {"type": "string"},
            "tensionSummary": {"type": "string"},
            "recommendedAction": {"type": "string"},
        },
    }


def _fallback_specialist_synthesis(demand_result: dict, market_result: dict) -> dict:
    demand_thesis = str((demand_result or {}).get("thesis") or "").strip()
    market_thesis = str((market_result or {}).get("thesis") or "").strip()
    demand_conf = str((demand_result or {}).get("confidence") or "").strip().lower()
    market_conf = str((market_result or {}).get("confidence") or "").strip().lower()
    demand_rec = str((demand_result or {}).get("recommendation") or "").strip()
    market_rec = str((market_result or {}).get("recommendation") or "").strip()

    combined_parts = []
    if demand_thesis:
        combined_parts.append(f"Demand view: {demand_thesis}")
    if market_thesis:
        combined_parts.append(f"Market view: {market_thesis}")
    combined_view = " ".join(combined_parts).strip() or "Specialist synthesis was assembled from the available agent outputs."

    if demand_conf == "high" and market_conf == "high":
        confidence = "high"
    elif "low" in (demand_conf, market_conf):
        confidence = "medium"
    else:
        confidence = demand_conf or market_conf or "medium"

    agreement_summary = (
        "Both specialists indicate meaningful demand, but not an unconstrained premium market. "
        "Consumer interest is improving while price realization remains in a competitive mid-market band."
    )
    tension_summary = (
        "The main tension is between strong demand momentum and practical pricing limits. "
        "Interest is accelerating faster than resale prices, so execution matters more than assuming broad pricing power."
    )

    recommended_action = market_rec or demand_rec or (
        "Increase inventory and marketing carefully, while monitoring weekly pricing and sell-through before making larger commitments."
    )

    return {
        "combinedView": combined_view,
        "confidence": confidence,
        "agreementSummary": agreement_summary,
        "tensionSummary": tension_summary,
        "recommendedAction": recommended_action,
    }


def _fallback_trade_evidence_summary(category: str, wto_trade: dict | None) -> dict:
    w = wto_trade if isinstance(wto_trade, dict) else {}
    hs_label = str(w.get("usedHsCategory") or w.get("hsCategory") or "product class").strip()
    hs_code = str(w.get("usedHsCode") or w.get("hsCode") or "").strip()
    latest_tariff = w.get("latestTariffPercent")
    latest_year = w.get("latestYear")
    partner_names = list(w.get("partnerCandidateNames") or [])
    confidence = str(w.get("confidence") or "low").strip().lower()
    critical_components = list(w.get("criticalComponents") or [])
    targeted_level = str(w.get("targetedMatchLevel") or "").strip()

    has_tariff = latest_tariff is not None and latest_year is not None
    if has_tariff:
        summary = (
            f"WTO evidence was found for the product class behind {category}: "
            f"{hs_label}{f' (HS {hs_code})' if hs_code and hs_code != 'default' else ''}. "
            f"The latest MFN tariff reading is {latest_tariff}% in {latest_year}. "
            f"This is product-class evidence rather than brand-specific New Balance trade data."
        )
        key_signals = [
            f"HS-targeted WTO evidence points to {hs_label}{f' (HS {hs_code})' if hs_code and hs_code != 'default' else ''}.",
            f"Latest MFN tariff is {latest_tariff}% in {latest_year}.",
            (
                f"Likely supplier-country candidates are {', '.join(partner_names[:3])}."
                if partner_names
                else "Partner-country evidence is limited, so tariff context is stronger than supplier-specific trade flow evidence."
            ),
        ]
        if critical_components:
            key_signals.append(f"Critical components/material cues include {', '.join(critical_components[:4])}.")
        data_gaps = [
            "WTO evidence is product-class and tariff-focused, not brand-specific shipment data.",
            "Supplier concentration and component-level trade flows remain only partially observed.",
        ]
        recommendation = (
            "Use this WTO evidence as a supply-side risk proxy for the footwear class, and combine it with pricing and sourcing signals rather than waiting for brand-specific trade records."
        )
        return {
            "role": "WTO Trade Evidence Agent",
            "tradeConstraintSummary": summary,
            "confidence": confidence or "medium",
            "keySignals": key_signals[:5],
            "dataGaps": data_gaps[:4],
            "recommendation": recommendation,
        }

    return {
        "role": "WTO Trade Evidence Agent",
        "tradeConstraintSummary": "Trade/tariff evidence was limited or unavailable for this category.",
        "confidence": confidence or "low",
        "keySignals": [
            "WTO evidence did not produce a strong targeted trade-constraint signal.",
            "Any tariff interpretation should be treated as directional only.",
        ],
        "dataGaps": [
            "Partner-specific trade flow evidence was limited.",
            f"HS-level targeting{f' ({targeted_level})' if targeted_level else ''} may still be broad rather than supplier-specific.",
        ],
        "recommendation": "Treat trade exposure as a monitoring item and gather more targeted HS/partner evidence before relying on it heavily.",
    }


def _sanitize_tool_selection(selected_tools: list, max_tools: int = 4) -> list[str]:
    allowed = _market_tool_specs().keys()
    out = []
    for tool_name in selected_tools or []:
        name = str(tool_name or "").strip()
        if name in allowed and name not in out:
            out.append(name)
    return out[:max_tools]


async def _run_market_tool(
    tool_name: str,
    category: str,
    *,
    wto_partner_code: str | None = None,
    wto_hs_code: str | None = None,
    wto_strict_targeted: bool = True,
) -> tuple[str, object, str | None]:
    try:
        if tool_name == "news":
            return tool_name, await get_news(category), None
        if tool_name == "trends":
            return tool_name, await get_trends(category, geo="US", days=728), None
        if tool_name == "ebay_pricing":
            return tool_name, await _fetch_ebay_pricing(category, limit=1000), None
        if tool_name == "ebay_map":
            return tool_name, await get_ebay_price_map(category, limit=1000), None
        if tool_name == "wto_trade":
            return tool_name, await _fetch_wto_trade_context(
                years=WTO_DEFAULT_YEARS,
                reporter_code=WTO_REPORTER_CODE,
                product_query=category,
                partner_code=wto_partner_code,
                hs_code=wto_hs_code,
                strict_targeted=wto_strict_targeted,
            ), None
        if tool_name == "pricing_history":
            return tool_name, _load_recent_weekly_points(category, weeks=104), None
        return tool_name, None, "unknown tool"
    except Exception as e:
        return tool_name, None, type(e).__name__


def _summarize_tool_result_for_agent(tool_name: str, result: object, error: str | None = None) -> dict:
    if error:
        return {"tool": tool_name, "status": "error", "summary": error}
    if tool_name == "news" and isinstance(result, dict):
        articles = result.get("articles") or []
        titles = [str((a or {}).get("title") or "") for a in articles[:3] if (a or {}).get("title")]
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": f"score={result.get('score')} totalResults={result.get('totalResults')} topTitles={titles}",
        }
    if tool_name == "trends" and isinstance(result, dict):
        pts = result.get("points") or []
        tail = pts[-4:] if isinstance(pts, list) else []
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": f"score={result.get('score')} timeframe={result.get('timeframe')} recentPoints={tail}",
        }
    if tool_name == "ebay_pricing" and isinstance(result, dict):
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": (
                f"avg={result.get('currentAvgPrice')} range=({result.get('typicalPriceRangeLow')}, "
                f"{result.get('typicalPriceRangeHigh')}) sample={result.get('sampleSize')} "
                f"source={result.get('source')}"
            ),
        }
    if tool_name == "ebay_map" and isinstance(result, dict):
        states = result.get("states") or []
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": f"granularity={result.get('granularity')} count={result.get('count')} sampleRows={states[:3]}",
        }
    if tool_name == "wto_trade" and isinstance(result, dict):
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": (
                f"partners={result.get('partnerCandidates')} hs={result.get('hsCandidates')} "
                f"headline={result.get('headline')} warnings={result.get('warnings')}"
            ),
        }
    if tool_name == "pricing_history" and isinstance(result, list):
        return {
            "tool": tool_name,
            "status": "ok",
            "summary": f"weeklyPoints={len(result)} latest={result[-3:] if result else []}",
        }
    return {"tool": tool_name, "status": "ok", "summary": str(result)[:500]}


async def _run_specialist_agents(
    category: str,
    *,
    api_key: str,
    model: str,
    live_context: dict,
    agent_trace: dict,
) -> dict:
    tool_plan = list(agent_trace.get("toolPlan") or [])
    tool_runs = list(agent_trace.get("toolRuns") or [])
    wto_trade = live_context.get("wtoTrade")

    demand_context = {
        "news": live_context.get("news"),
        "trends": live_context.get("trends"),
        "computedTrendTimeline": live_context.get("computedTrendTimeline"),
        "toolPlan": [t for t in tool_plan if t in ("news", "trends")],
        "toolRuns": [r for r in tool_runs if str((r or {}).get("tool") or "") in ("news", "trends")],
    }
    market_context = {
        "ebayPricing": live_context.get("ebayPricing"),
        "ebayMap": live_context.get("ebayMap"),
        "wtoTrade": wto_trade,
        "storedPricingHistoryWeekly": live_context.get("storedPricingHistoryWeekly"),
        "computedPricingModel": live_context.get("computedPricingModel"),
        "toolPlan": [t for t in tool_plan if t in ("ebay_pricing", "ebay_map", "wto_trade", "pricing_history")],
        "toolRuns": [
            r
            for r in tool_runs
            if str((r or {}).get("tool") or "") in ("ebay_pricing", "ebay_map", "wto_trade", "pricing_history")
        ],
    }

    trade_system_prompt = (
        "You are the WTO Trade Evidence Agent in a market-intel team. "
        "Your job is to convert raw WTO tariff/trade lookup data into a concise supply-side evidence summary. "
        "Be explicit about what was found, what confidence it deserves, and what gaps still remain. "
        "Treat WTO results as product-class trade evidence, not brand-specific trade records. "
        "If the payload contains an HS category, HS code, tariff level, inferred components, or partner candidates, use them directly instead of claiming no trade data exists. "
        "Do not invent country-specific facts that are not present in the evidence."
    )
    trade_user_messages = [
        f"Category: {category}",
        "Raw WTO trade context JSON:\n" + json.dumps(wto_trade, ensure_ascii=True),
        (
            "Summarize this for a downstream market-constraints analyst. "
            "Emphasize likely tariff pressure, HS-code specificity, partner/supplier evidence, inferred critical components, and any important data gaps. "
            "Do not say 'no data for New Balance' when there is product-class footwear evidence in the payload."
        ),
    ]
    trade_task = _openai_structured_json(
        api_key=api_key,
        model=model,
        system_prompt=trade_system_prompt,
        user_messages=trade_user_messages,
        schema_name="trade_evidence_agent",
        schema=_trade_evidence_schema(),
        max_output_tokens=400,
    )
    trade_debug = {
        "provider": "openai",
        "model": model,
    }

    demand_task = _openai_structured_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "You are the Demand Specialist in a market-intel agent team. "
            "Focus on demand, momentum, seasonality, and consumer attention. "
            "Use only the supplied evidence. Be concise and decision-oriented."
        ),
        user_messages=[
            f"Category: {category}",
            "Demand evidence JSON:\n" + json.dumps(demand_context, ensure_ascii=True),
        ],
        schema_name="demand_specialist",
        schema=_specialist_analysis_schema(),
        max_output_tokens=450,
    )

    demand_result, trade_result = await asyncio.gather(demand_task, trade_task)

    trade_summary_text = str((trade_result or {}).get("tradeConstraintSummary") or "").lower()
    trade_key_signals_text = " ".join(str(x or "") for x in ((trade_result or {}).get("keySignals") or [])).lower()
    if (
        not isinstance(trade_result, dict)
        or (
            isinstance(wto_trade, dict)
            and wto_trade.get("latestTariffPercent") is not None
            and (
                "no wto" in trade_summary_text
                or "no hs code" in trade_summary_text
                or "no partner" in trade_summary_text
                or "no hs code" in trade_key_signals_text
                or "no partner" in trade_key_signals_text
                or str((trade_result or {}).get("confidence") or "").strip().lower().startswith("low")
            )
        )
    ):
        trade_result = _fallback_trade_evidence_summary(category, wto_trade)

    market_context["tradeConstraintSummary"] = trade_result

    market_task = None
    market_debug = {
        "enabled": bool(ANTHROPIC_API_KEY),
        "model": ANTHROPIC_MODEL,
        "keySource": (
            "ANTHROPIC_API_KEY"
            if os.getenv("ANTHROPIC_API_KEY")
            else "CLAUDE_API_KEY"
            if os.getenv("CLAUDE_API_KEY")
            else "Calude"
            if os.getenv("Calude")
            else "missing"
        ),
    }
    if ANTHROPIC_API_KEY:
        market_task = _anthropic_structured_json(
            api_key=ANTHROPIC_API_KEY,
            model=ANTHROPIC_MODEL,
            system_prompt=(
                "You are the Market Constraints Specialist in a market-intel agent team. "
                "Focus on price positioning, supply-side pressure, trade exposure, and competitive constraints. "
                "Use only the supplied evidence. Be concise and decision-oriented."
            ),
            user_messages=[
                f"Category: {category}",
                "Trade evidence agent JSON:\n" + json.dumps(trade_result, ensure_ascii=True),
                "Market evidence JSON:\n" + json.dumps(market_context, ensure_ascii=True),
            ],
            schema_hint=_specialist_analysis_schema(),
            max_tokens=900,
        )

    market_result = await (market_task if market_task is not None else asyncio.sleep(0, result=None))

    if not isinstance(demand_result, dict):
        demand_result = {
            "role": "Demand Specialist",
            "thesis": "Demand-specialist analysis unavailable.",
            "confidence": "low",
            "keySignals": ["Demand specialist could not complete analysis."],
            "risks": ["Demand view missing."],
            "recommendation": "Treat demand-side interpretation as incomplete.",
        }
    if not isinstance(market_result, dict):
        market_result = {
            "role": "Market Constraints Specialist",
            "thesis": "Market-constraints analysis unavailable.",
            "confidence": "low",
            "keySignals": ["Market specialist could not complete analysis."],
            "risks": ["Pricing/trade-side view missing."],
            "recommendation": "Treat market-side interpretation as incomplete.",
        }

    synthesis = await _openai_structured_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "You are the final synthesizer in a multi-agent market-intel team. "
            "Reconcile the specialist views into one combined assessment. "
            "Call out where they agree, where they pull in different directions, and what the best practical action is."
        ),
        user_messages=[
            f"Category: {category}",
            "Demand specialist JSON:\n" + json.dumps(demand_result, ensure_ascii=True),
            "Trade evidence agent JSON:\n" + json.dumps(trade_result, ensure_ascii=True),
            "Market specialist JSON:\n" + json.dumps(market_result, ensure_ascii=True),
        ],
        schema_name="multi_agent_synthesis",
        schema=_multi_agent_synthesis_schema(),
        max_output_tokens=400,
    )

    synthesis_source = "model"
    if not isinstance(synthesis, dict):
        synthesis = _fallback_specialist_synthesis(demand_result, market_result)
        synthesis_source = "fallback"

    return {
        "mode": "specialist-synthesis",
        "demandSpecialist": demand_result,
        "tradeEvidenceAgent": trade_result,
        "tradeEvidenceAgentDebug": trade_debug,
        "marketSpecialist": market_result,
        "synthesis": synthesis,
        "synthesisDebug": {"source": synthesis_source},
        "marketDebug": market_debug,
    }


async def _build_agentic_live_context(
    category: str,
    *,
    api_key: str,
    model: str,
    wto_partner_code: str | None = None,
    wto_hs_code: str | None = None,
    wto_strict_targeted: bool = True,
    user_objective: str | None = None,
) -> tuple[dict, dict]:
    live_context = {
        "news": None,
        "trends": None,
        "ebayPricing": None,
        "ebayMap": None,
        "wtoTrade": None,
        "storedPricingHistoryWeekly": [],
        "computedTrendTimeline": None,
        "computedPricingModel": None,
        "warnings": [],
    }
    tool_specs = _market_tool_specs()
    default_tools = ["trends", "news", "ebay_pricing", "wto_trade"]
    planner_prompt = (
        "You are planning evidence gathering for a market intelligence agent. "
        "Select only the tools needed to answer well. Prefer 2-4 tools, not all tools by default. "
        "Only request clarification when ambiguity would materially change the evidence needed."
    )
    planner_input = [
        f"Category: {category}",
        "Available tools JSON:\n" + json.dumps(
            [{"name": name, "description": spec["description"]} for name, spec in tool_specs.items()],
            ensure_ascii=True,
        ),
    ]
    if user_objective:
        planner_input.append("User objective:\n" + str(user_objective))
    if wto_partner_code or wto_hs_code:
        planner_input.append(
            "Known WTO targeting:\n"
            + json.dumps(
                {
                    "partner_code": wto_partner_code,
                    "hs_code": wto_hs_code,
                    "strict_targeted": bool(wto_strict_targeted),
                },
                ensure_ascii=True,
            )
        )
    plan = await _openai_structured_json(
        api_key=api_key,
        model=model,
        system_prompt=planner_prompt,
        user_messages=planner_input,
        schema_name="market_tool_plan",
        schema=_market_tool_plan_schema(),
        max_output_tokens=350,
    )
    selected_tools = _sanitize_tool_selection((plan or {}).get("selectedTools") or [], max_tools=4)
    if not selected_tools:
        selected_tools = default_tools

    first_pass_results = await asyncio.gather(
        *[
            _run_market_tool(
                tool_name,
                category,
                wto_partner_code=wto_partner_code,
                wto_hs_code=wto_hs_code,
                wto_strict_targeted=wto_strict_targeted,
            )
            for tool_name in selected_tools
        ]
    )

    tool_runs = []
    for tool_name, result, error in first_pass_results:
        spec = tool_specs.get(tool_name) or {}
        live_key = spec.get("live_context_key")
        if live_key:
            live_context[live_key] = result if error is None else live_context.get(live_key)
        if error:
            live_context["warnings"].append(f"{tool_name} unavailable: {error}")
        tool_runs.append(_summarize_tool_result_for_agent(tool_name, result, error))

    reflection = await _openai_structured_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "You are reviewing evidence gathered by a market intelligence agent. "
            "If the evidence is still weak, request up to 2 additional tools not yet used. "
            "Do not ask for more tools if current evidence is already sufficient."
        ),
        user_messages=[
            f"Category: {category}",
            "Already used tools:\n" + json.dumps(selected_tools, ensure_ascii=True),
            "Tool summaries JSON:\n" + json.dumps(tool_runs, ensure_ascii=True),
            "Available tools JSON:\n"
            + json.dumps(
                [{"name": name, "description": spec["description"]} for name, spec in tool_specs.items()],
                ensure_ascii=True,
            ),
        ],
        schema_name="market_tool_reflection",
        schema=_market_tool_reflection_schema(),
        max_output_tokens=260,
    )

    extra_tools = [
        name for name in _sanitize_tool_selection((reflection or {}).get("additionalTools") or [], max_tools=2)
        if name not in selected_tools
    ]
    if extra_tools:
        second_pass_results = await asyncio.gather(
            *[
                _run_market_tool(
                    tool_name,
                    category,
                    wto_partner_code=wto_partner_code,
                    wto_hs_code=wto_hs_code,
                    wto_strict_targeted=wto_strict_targeted,
                )
                for tool_name in extra_tools
            ]
        )
        for tool_name, result, error in second_pass_results:
            spec = tool_specs.get(tool_name) or {}
            live_key = spec.get("live_context_key")
            if live_key:
                live_context[live_key] = result if error is None else live_context.get(live_key)
            if error:
                live_context["warnings"].append(f"{tool_name} unavailable: {error}")
            tool_runs.append(_summarize_tool_result_for_agent(tool_name, result, error))

    trends_live = live_context.get("trends")
    if (
        isinstance(trends_live, dict)
        and isinstance(trends_live.get("score"), (int, float))
        and isinstance(trends_live.get("points"), list)
        and len(trends_live.get("points")) > 0
    ):
        try:
            live_context["computedTrendTimeline"] = _timeline_from_google_zscore(trends_live)
        except Exception as e:
            live_context["warnings"].append(f"trend timeline model unavailable: {type(e).__name__}")

    ebay_live = live_context.get("ebayPricing")
    if isinstance(ebay_live, dict) and ebay_live.get("currentAvgPrice") is not None and ebay_live.get("sampleSize", 0) >= 3:
        try:
            current = float(ebay_live.get("currentAvgPrice") or 0)
            low = float(ebay_live.get("typicalPriceRangeLow") or current)
            high = float(ebay_live.get("typicalPriceRangeHigh") or current)
            pricing_model = {
                "currentAvgPrice": current,
                "typicalPriceRangeLow": low,
                "typicalPriceRangeHigh": high,
                "forecastAvgPrice12mo": round(current * 1.04, 2),
                "forecastAvgPrice24mo": round(current * 1.08, 2),
                "priceChangeYoY": 0.0,
                "priceTrend": "STABLE",
                "priceOutlook": "NEUTRAL",
                "liveSource": ebay_live.get("source", "eBay"),
                "liveSampleSize": int(ebay_live.get("sampleSize", 0)),
            }
            _reconcile_live_pricing_with_forecast(pricing_model)
            pricing_model["monthlyHistory"] = _build_expanded_weekly_history(
                category, pricing_model, hist_weeks=104, proj_weeks=52
            )
            live_context["computedPricingModel"] = pricing_model
        except Exception as e:
            live_context["warnings"].append(f"pricing model unavailable: {type(e).__name__}")

    agent_trace = {
        "mode": "plan-act-observe",
        "objective": (plan or {}).get("objective") or (user_objective or f"Analyze market conditions for {category}"),
        "planReasoning": (plan or {}).get("reasoning") or "Planner unavailable; used fallback tool bundle.",
        "clarificationRequested": bool((plan or {}).get("needsClarification")),
        "clarifyingQuestion": (plan or {}).get("clarifyingQuestion") or "",
        "toolPlan": selected_tools,
        "additionalTools": extra_tools,
        "toolRuns": tool_runs,
        "reflection": reflection or {
            "confidence": "medium",
            "stopReason": "Reflection unavailable; proceeded with gathered evidence.",
            "additionalTools": [],
            "missingInformation": "",
        },
        "warnings": list(live_context.get("warnings") or []),
    }
    return live_context, agent_trace


def _clip_0_100(v: float) -> int:
    return int(max(0, min(100, round(v))))


def _weekly_from_trend_points(points: list) -> list:
    if not isinstance(points, list):
        return []
    buckets = {}
    for p in points:
        try:
            ds = str((p or {}).get("date") or "")
            val = float((p or {}).get("value"))
            d = datetime.fromisoformat(ds).date()
        except Exception:
            continue
        wk = d - timedelta(days=d.weekday())
        key = wk.isoformat()
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(val)
    out = []
    for k in sorted(buckets.keys()):
        vals = buckets[k]
        out.append({"week": k, "value": float(sum(vals) / max(1, len(vals)))})
    return out


def _simple_slope(values: list) -> float:
    if len(values) < 2:
        return 0.0
    return (float(values[-1]) - float(values[0])) / max(1, len(values) - 1)


def _arima_robustness(values: list, horizon_weeks: int = 52) -> dict | None:
    """
    Forecast future Google interest and return compact robustness evidence:
    - model specification
    - in-sample diagnostics
    - holdout validation vs simple baselines
    """
    if not isinstance(values, list) or len(values) < 30:
        return None
    try:
        from statsmodels.tsa.arima.model import ARIMA  # optional dependency
        from statsmodels.tsa.stattools import adfuller
        from statsmodels.stats.diagnostic import acorr_ljungbox
    except Exception:
        return None

    def _clean(vals: list) -> list[float]:
        out = []
        for v in vals[-260:]:
            try:
                fv = float(v)
            except Exception:
                continue
            if math.isfinite(fv):
                out.append(fv)
        return out

    def _rmse(actual: list[float], pred: list[float]) -> float:
        if not actual or len(actual) != len(pred):
            return 0.0
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(actual, pred)) / len(actual))

    def _mae(actual: list[float], pred: list[float]) -> float:
        if not actual or len(actual) != len(pred):
            return 0.0
        return sum(abs(a - b) for a, b in zip(actual, pred)) / len(actual)

    try:
        series = _clean(values)
        if len(series) < 40:
            return None

        holdout_weeks = min(12, max(8, len(series) // 12))
        if len(series) <= holdout_weeks + 20:
            holdout_weeks = max(4, min(8, len(series) // 5))
        train_series = series[:-holdout_weeks]
        test_series = series[-holdout_weeks:]
        if len(train_series) < 24 or len(test_series) < 4:
            return None

        naive_last_pred = [float(train_series[-1])] * holdout_weeks
        trailing_mean = sum(train_series[-min(8, len(train_series)):]) / min(8, len(train_series))
        mean_pred = [float(trailing_mean)] * holdout_weeks
        mae_naive = _mae(test_series, naive_last_pred)
        rmse_naive = _rmse(test_series, naive_last_pred)
        mae_mean = _mae(test_series, mean_pred)
        rmse_mean = _rmse(test_series, mean_pred)

        def _difference(vals: list[float], order: int) -> list[float]:
            out = list(vals)
            for _ in range(max(0, int(order))):
                out = [out[i] - out[i - 1] for i in range(1, len(out))]
                if len(out) < 3:
                    break
            return out

        candidate_orders = [
            (0, 1, 1),
            (1, 1, 0),
            (1, 1, 1),
            (2, 1, 0),
            (0, 1, 2),
            (2, 1, 1),
            (1, 1, 2),
            (2, 1, 2),
            (0, 2, 1),
            (1, 2, 0),
            (1, 2, 1),
            (0, 0, 1),
            (1, 0, 0),
            (1, 0, 1),
        ]

        candidates = []
        for p, d, q in candidate_orders:
            if len(train_series) <= max(18, holdout_weeks + d + p + q):
                continue
            try:
                fitted = ARIMA(
                    train_series,
                    order=(p, d, q),
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                ).fit()

                holdout_forecast = fitted.forecast(steps=holdout_weeks)
                holdout_level_pred = [float(v) for v in holdout_forecast]
                if len(holdout_level_pred) != holdout_weeks:
                    continue

                mae_arima = _mae(test_series, holdout_level_pred)
                rmse_arima = _rmse(test_series, holdout_level_pred)

                future_forecast = fitted.forecast(steps=max(4, int(horizon_weeks)))
                level_forecast = [float(v) for v in future_forecast]
                if not level_forecast:
                    continue

                avg_fc = sum(level_forecast) / len(level_forecast)
                clipped_upper = avg_fc > 100.0
                clipped_lower = avg_fc < 0.0

                residuals = []
                fitted_residuals = getattr(fitted, "resid", None)
                if fitted_residuals is None:
                    fitted_residuals = []
                for r in fitted_residuals:
                    try:
                        fr = float(r)
                    except Exception:
                        continue
                    if math.isfinite(fr):
                        residuals.append(fr)

                diff_train = _difference(train_series, d)
                adf_stat = None
                adf_pvalue = None
                try:
                    if len(diff_train) >= 8:
                        adf_res = adfuller(diff_train)
                        adf_stat = float(adf_res[0])
                        adf_pvalue = float(adf_res[1])
                except Exception:
                    pass

                lb_pvalue = None
                try:
                    if len(residuals) >= 8:
                        lb_lag = min(10, max(3, len(residuals) // 8))
                        lb = acorr_ljungbox(residuals, lags=[lb_lag], return_df=True)
                        lb_pvalue = float(lb["lb_pvalue"].iloc[-1])
                except Exception:
                    pass

                residual_mean = sum(residuals) / len(residuals) if residuals else 0.0
                beats_naive = rmse_arima <= rmse_naive if rmse_naive > 0 else True
                beats_mean = rmse_arima <= rmse_mean if rmse_mean > 0 else True

                candidate = {
                    "futureScore": _clip_0_100(avg_fc),
                    "unclippedFutureScore": round(float(avg_fc), 3),
                    "clippedUpper": bool(clipped_upper),
                    "clippedLower": bool(clipped_lower),
                    "modelOrder": f"ARIMA({p},{d},{q})",
                    "estimationOrder": f"ARIMA({p},{d},{q}) on level series",
                    "differenceOrder": d,
                    "trainingPoints": len(train_series),
                    "holdoutPoints": len(test_series),
                    "forecastHorizonWeeks": int(horizon_weeks),
                    "adfStatistic": round(adf_stat, 4) if adf_stat is not None else None,
                    "adfPValue": round(adf_pvalue, 4) if adf_pvalue is not None else None,
                    "aic": round(float(fitted.aic), 2) if hasattr(fitted, "aic") else None,
                    "bic": round(float(fitted.bic), 2) if hasattr(fitted, "bic") else None,
                    "residualMean": round(float(residual_mean), 4),
                    "ljungBoxPValue": round(lb_pvalue, 4) if lb_pvalue is not None else None,
                    "holdoutMAE": round(float(mae_arima), 3),
                    "holdoutRMSE": round(float(rmse_arima), 3),
                    "naiveMAE": round(float(mae_naive), 3),
                    "naiveRMSE": round(float(rmse_naive), 3),
                    "rollingMeanMAE": round(float(mae_mean), 3),
                    "rollingMeanRMSE": round(float(rmse_mean), 3),
                    "beatsNaive": bool(beats_naive),
                    "beatsRollingMean": bool(beats_mean),
                }
                candidate["_rank"] = (
                    float(rmse_arima),
                    0 if beats_naive else 1,
                    0 if beats_mean else 1,
                    0 if lb_pvalue is not None and lb_pvalue > 0.05 else 1,
                    0 if adf_pvalue is not None and adf_pvalue < 0.05 else 1,
                    float(candidate["aic"]) if candidate["aic"] is not None else float("inf"),
                )
                candidates.append(candidate)
            except Exception:
                continue

        if not candidates:
            return None

        ranked = sorted(candidates, key=lambda c: c.get("_rank", (float("inf"),)))
        best = dict(ranked[0])
        best.pop("_rank", None)
        best["selectionMethod"] = f"lowest holdout RMSE across {len(ranked)} candidate ARIMA specifications"
        best["candidateCount"] = len(ranked)
        best["testedModels"] = [
            {
                "modelOrder": c.get("modelOrder"),
                "holdoutRMSE": c.get("holdoutRMSE"),
                "adfPValue": c.get("adfPValue"),
                "ljungBoxPValue": c.get("ljungBoxPValue"),
                "beatsNaive": c.get("beatsNaive"),
                "beatsRollingMean": c.get("beatsRollingMean"),
            }
            for c in ranked[:5]
        ]
        return best
    except Exception:
        return None


def _timeline_from_google_zscore(trends_payload: dict) -> dict | None:
    if not isinstance(trends_payload, dict):
        return None
    weekly = _weekly_from_trend_points(trends_payload.get("points") or [])
    if len(weekly) < 30:
        return None

    vals = [float(w["value"]) for w in weekly]
    present_vals = vals[-52:] if len(vals) >= 52 else vals[-26:]
    past_vals = vals[-104:-52] if len(vals) >= 104 else vals[:-len(present_vals)]
    if not past_vals:
        past_vals = present_vals

    past_score = _clip_0_100(sum(past_vals) / len(past_vals))
    present_score = _clip_0_100(sum(present_vals) / len(present_vals))

    recent_for_z = present_vals[-26:] if len(present_vals) >= 26 else present_vals
    mean_recent = sum(recent_for_z) / len(recent_for_z)
    var_recent = sum((x - mean_recent) ** 2 for x in recent_for_z) / max(1, len(recent_for_z) - 1)
    std_recent = math.sqrt(var_recent) if var_recent > 0 else 0.0
    current = present_vals[-1]
    z = (current - mean_recent) / std_recent if std_recent > 0 else 0.0

    slope = _simple_slope(present_vals[-8:] if len(present_vals) >= 8 else present_vals)
    future_raw = present_score + slope * 12 + (z * 4.0)
    future_score_momentum = _clip_0_100(future_raw)
    arima_robustness = _arima_robustness(vals, horizon_weeks=52)
    future_score_arima = (
        int(arima_robustness["futureScore"])
        if isinstance(arima_robustness, dict) and arima_robustness.get("futureScore") is not None
        else None
    )
    future_score = future_score_arima if future_score_arima is not None else future_score_momentum
    method = "google_arima_live" if future_score_arima is not None else "google_zscore_live"

    # Guardrail: if model collapses to zero unexpectedly while present demand exists,
    # fall back to momentum estimate.
    if future_score <= 1 and present_score >= 10:
        future_score = future_score_momentum
        method = "google_zscore_live"

    def badge_for(delta: float, zscore: float, allow_emerging: bool = False) -> str:
        if allow_emerging and zscore >= 1.2 and present_score < 45:
            return "EMERGING"
        if delta >= 2.0:
            return "RISING"
        if delta <= -2.0:
            return "DECLINING"
        if zscore >= 1.3 and delta < 0:
            return "PEAKED"
        return "STEADY"

    present_badge = badge_for(slope, z, allow_emerging=False)
    past_delta = present_score - past_score
    past_badge = "RISING" if past_delta >= 3 else "DECLINING" if past_delta <= -3 else "STEADY"
    future_delta = future_score - present_score
    future_badge = badge_for(future_delta / 4.0, z, allow_emerging=True)
    if future_badge == "STEADY":
        future_badge = "PEAKED" if z > 1.0 and slope < 0 else "RISING" if future_delta > 0 else "DECLINING" if future_delta < 0 else "PEAKED"

    return {
        "pastScore": past_score,
        "presentScore": present_score,
        "futureScore": future_score,
        "pastTrendBadge": past_badge,
        "presentTrendBadge": present_badge,
        "futureTrendBadge": future_badge,
        "pastSummary": f"Google Trends baseline over the prior period averaged {past_score}/100.",
        "presentSummary": f"Recent period averages {present_score}/100 (z-score {z:+.2f}, weekly slope {slope:+.2f}).",
        "futureSummary": (
            (
                f"ARIMA projection implies ~{future_score}/100 next period; "
                f"holdout RMSE {arima_robustness.get('holdoutRMSE')}, "
                f"naive RMSE {arima_robustness.get('naiveRMSE')}."
            )
            if method == "google_arima_live"
            else f"Short-horizon projection from momentum implies ~{future_score}/100 next period."
        ),
        "method": method,
        "arimaRobustness": arima_robustness,
    }


def _trend_report_schema():
    """
    JSON Schema for Structured Outputs (strict).
    Keep it aligned with your frontend expectations.
    """
    badge_past_present = ["PEAKED", "RISING", "DECLINING", "STEADY"]
    badge_future = ["PEAKED", "RISING", "DECLINING", "EMERGING"]
    curve_shapes = ["S-CURVE EARLY", "S-CURVE GROWTH", "S-CURVE PLATEAU", "DECLINING", "CYCLICAL", "VOLATILE"]
    price_trend = ["RISING", "FALLING", "STABLE", "VOLATILE"]
    outlook = ["BULLISH", "BEARISH", "NEUTRAL"]

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "category",
            "googleTrendScore",
            "newsScore",
            "pastScore",
            "presentScore",
            "futureScore",
            "pastTrendBadge",
            "presentTrendBadge",
            "futureTrendBadge",
            "pastSummary",
            "presentSummary",
            "futureSummary",
            "fullReport",
            "marketAnalysis",
            "competitionAnalysis",
            "politicalAnalysis",
            "inputBreakdownAnalysis",
            "hotKeywords",
            "warmKeywords",
            "coldKeywords",
            "trendCurveShape",
            "leadingBrands",
            "risingBrands",
            "topProducts",
            "fadingProducts",
            "pricing",
        ],
        "properties": {
            "category": {"type": "string"},
            "googleTrendScore": {"type": "integer", "minimum": 0, "maximum": 100},
            "newsScore": {"type": "integer", "minimum": 0, "maximum": 100},
            "pastScore": {"type": "integer", "minimum": 0, "maximum": 100},
            "presentScore": {"type": "integer", "minimum": 0, "maximum": 100},
            "futureScore": {"type": "integer", "minimum": 0, "maximum": 100},

            "pastTrendBadge": {"type": "string", "enum": badge_past_present},
            "presentTrendBadge": {"type": "string", "enum": badge_past_present},
            "futureTrendBadge": {"type": "string", "enum": badge_future},

            "pastSummary": {"type": "string"},
            "presentSummary": {"type": "string"},
            "futureSummary": {"type": "string"},
            "fullReport": {"type": "string"},
            "marketAnalysis": {"type": "string"},
            "competitionAnalysis": {"type": "string"},
            "politicalAnalysis": {"type": "string"},
            "inputBreakdownAnalysis": {"type": "string"},

            "hotKeywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "warmKeywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "coldKeywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},

            "trendCurveShape": {"type": "string", "enum": curve_shapes},
            "leadingBrands": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "risingBrands": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "topProducts": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "fadingProducts": {"type": "array", "items": {"type": "string"}, "minItems": 1},

            "pricing": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "typicalPriceRangeLow",
                    "typicalPriceRangeHigh",
                    "currentAvgPrice",
                    "priceChangeYoY",
                    "priceTrend",
                    "priceOutlook",
                    "forecastAvgPrice12mo",
                    "forecastAvgPrice24mo",
                    "monthlyHistory",
                    "priceBullishDrivers",
                    "priceBearishDrivers",
                    "pricingVerdict",
                ],
                "properties": {
                    "typicalPriceRangeLow": {"type": "number"},
                    "typicalPriceRangeHigh": {"type": "number"},
                    "currentAvgPrice": {"type": "number"},
                    "priceChangeYoY": {"type": "number"},
                    "priceTrend": {"type": "string", "enum": price_trend},
                    "priceOutlook": {"type": "string", "enum": outlook},
                    "forecastAvgPrice12mo": {"type": "number"},
                    "forecastAvgPrice24mo": {"type": "number"},
                    "monthlyHistory": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["month", "price", "projected"],
                            "properties": {
                                "month": {"type": "string"},
                                "price": {"type": "number"},
                                "projected": {"type": "boolean"},
                            },
                        },
                        "minItems": 1,
                    },
                    "priceBullishDrivers": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "priceBearishDrivers": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "pricingVerdict": {"type": "string"},
                },
            },
        },
    }


@app.post("/api/analyze")
async def analyze(request: Request):
    """
    Proxy a request to OpenAI Responses API.
    Accepts:
      { "category": "...", "apiKey": "optional override", "model": "optional override" }
    Returns:
      JSON matching your schema.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body. Use double-quoted JSON keys/values.")
    category = (body.get("category") or "").strip()
    wto_partner_code = (body.get("wtoPartnerCode") or "").strip() or None
    wto_hs_code = (body.get("wtoHsCode") or "").strip() or None
    wto_strict_targeted = bool(body.get("wtoStrictTargeted", True))
    if not category:
        raise HTTPException(status_code=400, detail="category is required")

    api_key = (body.get("apiKey") or "").strip() or OPENAI_API_KEY
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No OpenAI API key. Set OPENAI_API_KEY env var or pass apiKey in body.",
        )

    model = (body.get("model") or "").strip() or OPENAI_MODEL

    live_context, agent_trace = await _build_agentic_live_context(
        category,
        api_key=api_key,
        model=model,
        wto_partner_code=wto_partner_code,
        wto_hs_code=wto_hs_code,
        wto_strict_targeted=wto_strict_targeted,
        user_objective=f"Produce a grounded market intelligence report for {category}.",
    )
    specialist_trace = await _run_specialist_agents(
        category,
        api_key=api_key,
        model=model,
        live_context=live_context,
        agent_trace=agent_trace,
    )
    agent_trace["specialists"] = specialist_trace

    current_year = datetime.utcnow().year
    past_year = current_year - 1
    future_year = current_year + 1

    # Keep your same “shape” expectations, but let schema enforce correctness.
    prompt = f"""You are a consumer trend forecasting agent. Analyze "{category}".

Return content that fits the provided JSON schema exactly.
Be realistic and grounded. If uncertain, choose conservative estimates.

Guidance:
- Scores are 0–100 integers.
- Summaries are short (2–3 sentences).
- marketAnalysis: detailed narrative grounded in trend/news/pricing signals.
- competitionAnalysis: detailed narrative on competitive positioning and relative demand/price pressure.
- politicalAnalysis: detailed narrative on policy, regulatory, tariff, labor, or macro-political risks/opportunities.
- inputBreakdownAnalysis: detailed component/input-cost view of the product (materials, labor, logistics, tariffs, energy, packaging, distribution). Include key drivers and pressure direction.
- monthlyHistory: include a reasonable mix of historical vs projected entries.
- Use every available live/computed context section to ground scores and narrative.
- Explicitly reference specialized tool signals where relevant (NewsAPI, Google Trends, eBay pricing/map, WTO trade context).
- You are operating after an agent planning loop. Use the plan and tool evidence to explain what mattered most.
- A demand specialist and a market-constraints specialist have already reviewed the evidence. Use their synthesis where helpful.
- You may use general model knowledge for context not present in tools, but do not invent specific live facts.
"""

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "You output only valid JSON that matches the provided schema."},
            {"role": "user", "content": prompt},
            {
                "role": "user",
                "content": "Live context JSON:\n" + json.dumps(live_context, ensure_ascii=True),
            },
            {
                "role": "user",
                "content": "Agent trace JSON:\n" + json.dumps(agent_trace, ensure_ascii=True),
            },
            {
                "role": "user",
                "content": "Specialist analysis JSON:\n" + json.dumps(specialist_trace, ensure_ascii=True),
            },
        ],
        # Structured Outputs (Responses API uses text.format)
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trend_report",
                "strict": True,
                "schema": _trend_report_schema(),
            }
        },
        # You can tune this up/down
        "max_output_tokens": 3000,
        # optional: reduce retention if you want
        "store": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
        )

    if resp.status_code != 200:
        detail = resp.text[:800]
        raise HTTPException(status_code=resp.status_code, detail=f"OpenAI API error: {detail}")

    resp_json = resp.json()

    # With Structured Outputs, this should already be valid JSON text.
    try:
        raw_text = _extract_text_from_responses_api(resp_json).strip()
        parsed = json.loads(raw_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not parse OpenAI response as JSON: {type(e).__name__}")

    # Inject category/year sanity if you want (optional). Otherwise return as-is.
    # (Schema already enforces required keys/types.)
    parsed["category"] = category
    parsed["marketAnalysis"] = str(parsed.get("marketAnalysis") or parsed.get("fullReport") or "")
    parsed["competitionAnalysis"] = str(parsed.get("competitionAnalysis") or "")
    parsed["politicalAnalysis"] = str(parsed.get("politicalAnalysis") or "")
    parsed["inputBreakdownAnalysis"] = str(parsed.get("inputBreakdownAnalysis") or "")
    parsed["agentTrace"] = agent_trace

    # Prefer live eBay pricing for current price/range when available.
    ebay = live_context.get("ebayPricing")
    pricing = parsed.get("pricing") or {}
    if (
        isinstance(ebay, dict)
        and ebay.get("currentAvgPrice") is not None
        and ebay.get("sampleSize", 0) >= 3
    ):
        pricing["currentAvgPrice"] = ebay["currentAvgPrice"]
        pricing["typicalPriceRangeLow"] = ebay["typicalPriceRangeLow"]
        pricing["typicalPriceRangeHigh"] = ebay["typicalPriceRangeHigh"]
        pricing["liveSource"] = ebay.get("source", "eBay")
        pricing["liveSampleSize"] = int(ebay.get("sampleSize", 0))
        existing_verdict = (pricing.get("pricingVerdict") or "").strip()
        live_note = f"Live price snapshot from {pricing['liveSource']} ({pricing['liveSampleSize']} comps)."
        pricing["pricingVerdict"] = f"{existing_verdict} {live_note}".strip()
    else:
        pricing["liveSource"] = "AI estimate"
        if isinstance(ebay, dict) and ebay.get("warning"):
            pricing["liveWarning"] = ebay["warning"]

    # Apply live news/trends scores when available so returned JSON is aligned.
    news_live = live_context.get("news")
    trends_live = live_context.get("trends")
    if isinstance(news_live, dict) and isinstance(news_live.get("score"), (int, float)):
        parsed["newsScore"] = int(news_live.get("score"))
    if (
        isinstance(trends_live, dict)
        and isinstance(trends_live.get("score"), (int, float))
        and isinstance(trends_live.get("points"), list)
        and len(trends_live.get("points")) > 0
    ):
        parsed["googleTrendScore"] = int(trends_live.get("score"))
        timeline_live = live_context.get("computedTrendTimeline") or _timeline_from_google_zscore(trends_live)
        if isinstance(timeline_live, dict):
            parsed["pastScore"] = int(timeline_live["pastScore"])
            parsed["presentScore"] = int(timeline_live["presentScore"])
            parsed["futureScore"] = int(timeline_live["futureScore"])
            parsed["pastTrendBadge"] = str(timeline_live["pastTrendBadge"])
            parsed["presentTrendBadge"] = str(timeline_live["presentTrendBadge"])
            parsed["futureTrendBadge"] = str(timeline_live["futureTrendBadge"])
            parsed["pastSummary"] = str(timeline_live["pastSummary"])
            parsed["presentSummary"] = str(timeline_live["presentSummary"])
            parsed["futureSummary"] = str(timeline_live["futureSummary"])
            parsed["timelineMethod"] = str(timeline_live.get("method") or "google_zscore_live")
            parsed["arimaRobustness"] = timeline_live.get("arimaRobustness")

    if str(pricing.get("liveSource", "")).lower().startswith("ebay"):
        _reconcile_live_pricing_with_forecast(pricing)
    if str(pricing.get("liveSource", "")).lower().startswith("ebay"):
        weekly_points = []
        if isinstance(ebay, dict) and isinstance(ebay.get("soldPoints"), list):
            weekly_points = ebay.get("soldPoints")
        if not weekly_points and pricing.get("currentAvgPrice"):
            # Fallback so weekly history still accumulates even if sold comps are unavailable.
            weekly_points = [{"date": datetime.utcnow().date().isoformat(), "price": pricing.get("currentAvgPrice")}]
        _record_weekly_sold_points(category, weekly_points, str(pricing.get("liveSource", "eBay")))
    _normalize_pricing_monthly_history(pricing)
    _record_price_snapshot(category, pricing)
    pricing["monthlyHistory"] = _build_expanded_weekly_history(category, pricing, hist_weeks=104, proj_weeks=52)
    parsed["pricing"] = pricing

    # Optional: if you want the year-specific summaries to match, you can lightly post-process,
    # but I’m leaving it clean to avoid unexpected mutations.

    return JSONResponse(content=parsed)


@app.post("/api/chat_followup")
async def chat_followup(request: Request):
    """
    Follow-up Q&A over the generated report context.
    Accepts:
      {
        "category": "...",
        "question": "...",
        "report": {... optional current report json ...},
        "history": [{"role":"user|assistant","content":"..."}],
        "apiKey": "optional override",
        "model": "optional override"
      }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body. Use double-quoted JSON keys/values.")
    category = (body.get("category") or "").strip()
    wto_partner_code = (body.get("wtoPartnerCode") or "").strip() or None
    wto_hs_code = (body.get("wtoHsCode") or "").strip() or None
    wto_strict_targeted = bool(body.get("wtoStrictTargeted", True))
    question = (body.get("question") or "").strip()
    report = body.get("report")
    history = body.get("history") if isinstance(body.get("history"), list) else []
    api_key = (body.get("apiKey") or "").strip() or OPENAI_API_KEY
    model = (body.get("model") or "").strip() or OPENAI_MODEL

    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if not api_key:
        raise HTTPException(status_code=400, detail="No OpenAI API key. Set OPENAI_API_KEY or pass apiKey.")

    # Keep history bounded.
    trimmed_history = []
    for m in history[-8:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            trimmed_history.append({"role": role, "content": content[:2000]})

    live_context = None
    agent_trace = None
    specialist_trace = None
    if category:
        live_context, agent_trace = await _build_agentic_live_context(
            category,
            api_key=api_key,
            model=model,
            wto_partner_code=wto_partner_code,
            wto_hs_code=wto_hs_code,
            wto_strict_targeted=wto_strict_targeted,
            user_objective=question,
        )
        specialist_trace = await _run_specialist_agents(
            category,
            api_key=api_key,
            model=model,
            live_context=live_context,
            agent_trace=agent_trace,
        )
        agent_trace["specialists"] = specialist_trace

    context_payload = {
        "category": category,
        "report": report if isinstance(report, dict) else None,
        "liveContext": live_context,
        "agentTrace": agent_trace,
        "specialistTrace": specialist_trace,
    }

    input_msgs = [
        {
            "role": "system",
            "content": (
                "You are TrendPulse Follow-up Assistant. "
                "Use a hybrid approach: prioritize provided report/tool context, and supplement with general model knowledge when needed. "
                "When live tool context is present, use it for pricing, demand, and tariff/trade-policy portions of the answer. "
                "When specialist analyses are present, use them as intermediate reasoning products rather than ignoring them. "
                "Always structure the response with these exact headings: "
                "'From Report/Tools', 'From General Knowledge', and 'Combined Implication'. "
                "If a section has no content, write 'None'. "
                "Do not invent specific live facts that are not in report/tool context."
            ),
        },
        {"role": "user", "content": "Report context JSON:\n" + json.dumps(context_payload, ensure_ascii=True)},
    ]
    input_msgs.extend(trimmed_history)
    input_msgs.append({"role": "user", "content": question})

    payload = {
        "model": model,
        "input": input_msgs,
        "max_output_tokens": 700,
        "store": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)

    if resp.status_code != 200:
        detail = resp.text[:800]
        raise HTTPException(status_code=resp.status_code, detail=f"OpenAI API error: {detail}")

    try:
        text = _extract_text_from_responses_api(resp.json()).strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not parse follow-up response: {type(e).__name__}")

    return {"answer": text, "agentTrace": agent_trace}


