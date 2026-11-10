#!/usr/bin/env python3
"""Minimal free job-search app. No LLM calls. Public APIs only."""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, Response, render_template, request
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

APP_UA = "JobSearchApp/1.0 (public job search; +https://github.com/swiftverse/job-search-app)"
FETCH_TIMEOUT = 12
MAX_RESULTS = 50
CACHE_TTL = 600  # seconds; raw source payloads only
IST = timezone(timedelta(hours=5, minutes=30))

LEVEL_CHOICES = [
    ("entry", "Entry / Junior"),
    ("mid", "Mid"),
    ("senior", "Senior"),
    ("lead", "Lead"),
]

LEVEL_WORDS = {
    "entry": ["entry", "junior", "intern", "internship", "graduate", "associate", "trainee", "apprentice", "jr", "entry-level", "entry level", "junior-level"],
    "mid": ["mid", "mid-level", "mid level", "midlevel", "intermediate"],
    "senior": ["senior", "sr", "staff", "senior-level", "sr."],
    "lead": ["lead", "principal", "staff", "director", "head of", "vp"],
}

# Title words that are a clear mismatch for the chosen experience level.
LEVEL_EXCLUDE = {
    "entry": ["senior", "sr", "lead", "principal", "staff", "director", "head of"],
    "mid": ["intern", "internship", "trainee", "junior", "entry", "entry-level", "graduate", "apprentice", "jr"],
    "senior": ["intern", "internship", "junior", "entry", "entry-level", "trainee", "graduate", "apprentice", "jr"],
    "lead": ["intern", "internship", "junior", "entry", "entry-level", "trainee", "graduate", "apprentice", "jr"],
}

# Role family words and close synonyms used when checking the job title.
ROLE_FAMILY = {
    "developer": ["developer", "dev", "engineer", "programmer"],
    "engineer": ["engineer", "developer", "dev", "programmer"],
    "programmer": ["programmer", "developer", "engineer", "dev"],
    "analyst": ["analyst"],
    "manager": ["manager"],
    "designer": ["designer"],
    "scientist": ["scientist"],
    "consultant": ["consultant"],
    "specialist": ["specialist"],
    "architect": ["architect"],
    "administrator": ["administrator", "admin"],
    "coordinator": ["coordinator"],
    "writer": ["writer"],
    "researcher": ["researcher"],
    "recruiter": ["recruiter"],
    "accountant": ["accountant"],
    "officer": ["officer"],
    "associate": ["associate"],
    "assistant": ["assistant"],
    "director": ["director"],
    "intern": ["intern", "internship"],
    "lead": ["lead"],
}

TOKEN_SYNONYMS = {
    "developer": ["developer", "dev", "engineer", "programmer"],
    "engineer": ["engineer", "developer", "dev", "programmer"],
    "programmer": ["programmer", "developer", "engineer", "dev"],
    "analyst": ["analyst", "analysis", "analytics"],
    "designer": ["designer", "design"],
}

# Words in the query that are experience, not search terms.
LEVEL_STOP = {"senior", "junior", "mid", "middle", "entry", "lead", "staff", "principal", "sr", "jr"}

# Skill words that form a compound job title with a role (so "product marketing manager" is not a product manager).
PHRASE_REQUIRED_SKILLS = {"product", "project", "program", "account"}

SCORE_TITLE_PHRASE = 40
SCORE_TITLE_WORDS = 25
SCORE_TAGS = 12
SCORE_DESCRIPTION = 8
SCORE_THRESHOLD = 12  # description-only mentions stay below this
NOISY_TAG_LIMIT = 8

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_cache: dict[str, tuple[float, object]] = {}

app = Flask(__name__)


def now_ist() -> datetime:
    return datetime.now(IST)


def _maybe_fix_mojibake(text: str) -> str:
    """Repair text that is UTF-8 decoded as Latin-1 (common in some job feeds)."""
    if not any(ch in text for ch in ("Ã", "Â", "ð", "â", "ï")):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def strip_html(text: str) -> str:
    if not text:
        return ""
    # Unescape first so already-escaped markup (e.g. &lt;div&gt;) can be stripped.
    text = html.unescape(str(text))
    text = _maybe_fix_mojibake(text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def snippet(text: str, limit: int = 220) -> str:
    clean = strip_html(text)
    if len(clean) <= limit:
        return clean
    cut = clean[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def format_date(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""
    text = str(value).strip()
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text[:10]), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text[:16]


def tokenize(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", query.lower().strip()) if t and t not in {"and", "or", "the", "a", "an", "for"}]


def variants(token: str) -> list[str]:
    token = token.lower().strip()
    return TOKEN_SYNONYMS.get(token, [token])


def word_in(text: str, word: str) -> bool:
    if not word or not text:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", text.lower()) is not None


def token_in(text: str, token: str) -> bool:
    return any(word_in(text, v) for v in variants(token))


def haystack(job: dict) -> str:
    tags = " ".join(job.get("tags") or [])
    return " ".join(
        [
            job.get("title") or "",
            job.get("company") or "",
            tags,
            job.get("location") or "",
            strip_html(job.get("description") or ""),
        ]
    ).lower()


def split_query(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split a query into skill terms (python, data, product) and role terms (developer, analyst)."""
    skills, roles = [], []
    role_keys = set(ROLE_FAMILY)
    for vs in ROLE_FAMILY.values():
        role_keys.update(vs)
    for tok in tokens:
        if tok in LEVEL_STOP:
            continue
        if tok in role_keys:
            roles.append(tok)
        else:
            skills.append(tok)
    return skills, roles


def role_in_title(title: str, roles: list[str]) -> bool:
    if not roles:
        return True
    for role in roles:
        for variant in ROLE_FAMILY.get(role, [role]):
            if word_in(title, variant):
                return True
    return False


def all_terms_in(text: str, terms: list[str]) -> bool:
    return bool(terms) and all(token_in(text, term) for term in terms)


def tight_phrase_in_description(desc: str, skills: list[str], roles: list[str], phrase: str) -> bool:
    """True only when the query phrase, or a skill sitting next to a role word, appears in the description."""
    if not desc:
        return False
    if phrase and phrase in desc:
        return True
    role_variants: list[str] = []
    for role in roles:
        role_variants.extend(ROLE_FAMILY.get(role, [role]))
    if not skills or not role_variants:
        return False
    for skill in skills:
        for sv in variants(skill):
            for rv in role_variants:
                pat_a = r"(?<![a-z0-9])" + re.escape(sv) + r"[- /]?" + re.escape(rv) + r"(?![a-z0-9])"
                pat_b = r"(?<![a-z0-9])" + re.escape(rv) + r"[- /]?" + re.escape(sv) + r"(?![a-z0-9])"
                if re.search(pat_a, desc) or re.search(pat_b, desc):
                    return True
    return False


def keyword_score(job: dict, tokens: list[str]) -> float | None:
    """Rank a job, or return None if it is not a match.

    Key terms (e.g. 'python', or both words of 'data analyst') must appear in
    the title or in a short, non-noisy tag list. A title role word is also
    required when the query includes one (developer/engineer/analyst/...).
    Description-only hits score below the publish threshold unless they
    contain the exact query phrase or a skill+role phrase.
    """
    if not tokens:
        return 0.0
    title = (job.get("title") or "").lower()
    tag_list = [str(t).strip() for t in (job.get("tags") or []) if str(t).strip()]
    tags_text = " ".join(tag_list).lower()
    desc = strip_html(job.get("description") or "").lower()[:4000]
    skills, roles = split_query(tokens)
    key_terms = skills if skills else [t for t in tokens if t not in LEVEL_STOP]
    phrase = " ".join(t for t in tokens if t not in LEVEL_STOP)

    # Compound titles such as "product manager" must appear as a phrase in the title.
    require_phrase = bool(skills) and bool(roles) and any(s in PHRASE_REQUIRED_SKILLS for s in skills)
    if phrase and phrase in title and role_in_title(title, roles):
        return float(SCORE_TITLE_PHRASE)
    if require_phrase:
        # Tags and loose word matches are not enough for these titles.
        if roles and role_in_title(title, roles) and tight_phrase_in_description(desc, skills, roles, phrase):
            return float(SCORE_DESCRIPTION)
        return None
    # Key skill terms must be visible in the title (tags alone are often noisy).
    if key_terms and all_terms_in(title, key_terms) and role_in_title(title, roles):
        return float(SCORE_TITLE_WORDS)
    if roles and role_in_title(title, roles) and tight_phrase_in_description(desc, skills, roles, phrase):
        return float(SCORE_DESCRIPTION)
    return None


def _term_in_title(title: str, term: str) -> bool:
    return word_in(title, term) or (term in title if " " in term else False)


def level_adjust(job: dict, level: str) -> float | None:
    """Bonus for a matching level word; None drops an obvious mismatch."""
    title = (job.get("title") or "").lower()
    listed = (job.get("listed_level") or "").lower()

    exclude = LEVEL_EXCLUDE.get(level, [])
    if any(_term_in_title(title, term) for term in exclude):
        return None

    if listed:
        if level == "entry" and any(w in listed for w in ("senior", "director", "principal", "staff", "lead", "executive")):
            return None
        if level in {"senior", "lead"} and any(w in listed for w in ("entry", "junior", "intern")):
            return None
        if level == "mid" and any(w in listed for w in ("entry", "junior", "intern", "director", "executive")):
            return None

    bonus = 0.0
    want = LEVEL_WORDS.get(level, [])
    if any(_term_in_title(title, term) for term in want):
        bonus += 4
    elif listed and any(term in listed for term in want):
        bonus += 2
    return bonus


def is_remote(job: dict) -> bool:
    if job.get("remote"):
        return True
    if job.get("source") in {"Remotive", "RemoteOK", "Jobicy", "Himalayas", "Working Nomads"}:
        return True
    loc = (job.get("location") or "").lower()
    return any(w in loc for w in ("remote", "worldwide", "anywhere", "distributed", "global", "work from home"))


def location_ok(job: dict, location: str, remote_only: bool) -> bool:
    if remote_only and not is_remote(job):
        return False
    if not location:
        return True
    loc = (job.get("location") or "").lower()
    needle = location.lower().strip()
    if needle in loc:
        return True
    # Worldwide/remote listings can still match a preferred region if mentioned in the text.
    if remote_only and is_remote(job) and needle in haystack(job):
        return True
    return False


def dedupe_key(job: dict) -> str:
    title = NON_ALNUM_RE.sub(" ", (job.get("title") or "").lower()).strip()
    company = NON_ALNUM_RE.sub(" ", (job.get("company") or "").lower()).strip()
    return f"{title}|{company}"


def http_get_json(url: str, extra_headers: dict | None = None, timeout: int | None = None):
    headers = {"User-Agent": APP_UA, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def cached_json(cache_key: str, url: str, extra_headers: dict | None = None, timeout: int | None = None):
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data = http_get_json(url, extra_headers, timeout=timeout)
    _cache[cache_key] = (now, data)
    return data


def make_job(title, company, location, source, date, description, url, tags=None, remote=False, listed_level: str = "") -> dict:
    title = strip_html(title or "")
    company = strip_html(company or "")
    location = strip_html(location or "")
    clean_tags = []
    for tag in tags or []:
        tag = strip_html(str(tag)).strip()
        if tag and tag not in clean_tags:
            clean_tags.append(tag)
    return {
        "title": title or "Untitled role",
        "company": company or "Unknown company",
        "location": location or "Not specified",
        "source": source,
        "date": format_date(date),
        "description": description or "",
        "snippet": snippet(description or ""),
        "url": (url or "").strip(),
        "tags": clean_tags,
        "remote": bool(remote),
        "listed_level": (listed_level or "").strip(),
    }


def fetch_remotive(query: str) -> list[dict]:
    q = urllib.parse.quote_plus(query or "")
    url = f"https://remotive.com/api/remote-jobs?search={q}" if query else "https://remotive.com/api/remote-jobs"
    data = cached_json(f"remotive:{query}", url)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = []
    for item in jobs or []:
        out.append(
            make_job(
                title=item.get("title"),
                company=item.get("company_name"),
                location=item.get("candidate_required_location") or "Remote",
                source="Remotive",
                date=item.get("publication_date"),
                description=item.get("description") or "",
                url=item.get("url") or "",
                tags=item.get("tags") or [],
                remote=True,
            )
        )
    return out


def fetch_arbeitnow(query: str) -> list[dict]:
    # Public board; search param is accepted but filtering is done locally.
    url = "https://www.arbeitnow.com/api/job-board-api"
    if query:
        url += "?" + urllib.parse.urlencode({"search": query})
    data = cached_json(f"arbeitnow:{query}", url)
    jobs = data.get("data") if isinstance(data, dict) else data
    out = []
    for item in jobs or []:
        out.append(
            make_job(
                title=item.get("title"),
                company=item.get("company_name"),
                location=item.get("location") or ("Remote" if item.get("remote") else ""),
                source="Arbeitnow",
                date=item.get("created_at"),
                description=item.get("description") or "",
                url=item.get("url") or "",
                tags=item.get("tags") or [],
                remote=bool(item.get("remote")),
            )
        )
    return out


def fetch_remoteok(_query: str) -> list[dict]:
    data = cached_json("remoteok", "https://remoteok.com/api")
    out = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("position") or item.get("title")
        if not title:
            continue
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        out.append(
            make_job(
                title=title,
                company=item.get("company"),
                location=item.get("location") or "Remote",
                source="RemoteOK",
                date=item.get("date") or item.get("epoch"),
                description=item.get("description") or "",
                url=item.get("apply_url") or item.get("url") or "",
                tags=list(tags),
                remote=True,
            )
        )
    return out


def fetch_muse(query: str, level: str) -> list[dict]:
    # Optional: public API has been intermittently unavailable. Failures are ignored.
    params = {"page": 1, "descending": "true"}
    if query:
        params["q"] = query
    muse_level = {
        "entry": "Entry Level",
        "mid": "Mid Level",
        "senior": "Senior Level",
        "lead": "Senior Level",
    }.get(level)
    if muse_level:
        params["level"] = muse_level
    url = "https://www.themuse.com/api/public/jobs?" + urllib.parse.urlencode(params)
    data = cached_json(f"muse:{query}:{level}", url)
    jobs = data.get("results") if isinstance(data, dict) else data
    out = []
    for item in jobs or []:
        company = item.get("company") or {}
        company_name = company.get("name") if isinstance(company, dict) else (company or "")
        locs = item.get("locations") or []
        loc_names = []
        for loc in locs:
            if isinstance(loc, dict):
                loc_names.append(loc.get("name") or "")
            else:
                loc_names.append(str(loc))
        refs = item.get("refs") or {}
        url_ = (refs.get("landing_page") if isinstance(refs, dict) else "") or item.get("url") or ""
        levels = item.get("levels") or []
        level_names = [lv.get("name") if isinstance(lv, dict) else str(lv) for lv in levels]
        out.append(
            make_job(
                title=item.get("name") or item.get("title"),
                company=company_name,
                location=", ".join(n for n in loc_names if n) or "Not specified",
                source="The Muse",
                date=item.get("publication_date"),
                description=item.get("contents") or item.get("description") or "",
                url=url_,
                tags=level_names,
                remote=any("remote" in (n or "").lower() for n in loc_names),
            )
        )
    return out


def fetch_adzuna(query: str) -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        raise RuntimeError("skipped: ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
    country = os.environ.get("ADZUNA_COUNTRY", "us").strip().lower() or "us"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": 50,
        "what": query or "",
        "content-type": "application/json",
    }
    url = f"https://api.adzuna.com/v1/api/jobs/{urllib.parse.quote(country)}/search/1?" + urllib.parse.urlencode(params)
    data = cached_json(f"adzuna:{country}:{query}", url)
    jobs = data.get("results") if isinstance(data, dict) else []
    out = []
    for item in jobs or []:
        company = item.get("company") or {}
        company_name = company.get("display_name") if isinstance(company, dict) else (company or "")
        loc = item.get("location") or {}
        loc_name = loc.get("display_name") if isinstance(loc, dict) else (loc or "")
        out.append(
            make_job(
                title=item.get("title"),
                company=company_name,
                location=loc_name,
                source="Adzuna",
                date=item.get("created"),
                description=item.get("description") or "",
                url=item.get("redirect_url") or item.get("adref") or "",
                tags=[],
                remote="remote" in (loc_name or "").lower() or "remote" in (item.get("title") or "").lower(),
            )
        )
    return out



def _as_tag_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def jobicy_tag(query: str) -> str:
    tokens = tokenize(query)
    skills, _roles = split_query(tokens)
    raw = (skills[0] if skills else (tokens[0] if tokens else "")).lower()
    return re.sub(r"[^a-z0-9]+", "", raw)


def fetch_jobicy(query: str) -> list[dict]:
    tag = jobicy_tag(query)
    url = "https://jobicy.com/api/v2/remote-jobs?count=100"
    if tag:
        url += "&tag=" + urllib.parse.quote(tag)
    data = cached_json(f"jobicy:{tag}", url)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = []
    for item in jobs or []:
        tags = _as_tag_list(item.get("jobIndustry"))
        tags.extend(_as_tag_list(item.get("jobType")))
        out.append(
            make_job(
                title=item.get("jobTitle"),
                company=item.get("companyName"),
                location=item.get("jobGeo") or "Remote",
                source="Jobicy",
                date=item.get("pubDate"),
                description=item.get("jobDescription") or item.get("jobExcerpt") or "",
                url=item.get("url") or "",
                tags=tags,
                remote=True,
                listed_level=item.get("jobLevel") or "",
            )
        )
    return out


def fetch_himalayas(_query: str) -> list[dict]:
    # Public feed does not reliably honour search params; filter locally.
    # The API caps each page at 20 even when limit=100 is requested.
    data = cached_json("himalayas", "https://himalayas.app/jobs/api?limit=100", timeout=15)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = []
    for item in jobs or []:
        tags = _as_tag_list(item.get("categories")) + _as_tag_list(item.get("parentCategories"))
        seniority = item.get("seniority") or []
        if isinstance(seniority, str):
            listed = seniority
        else:
            listed = ", ".join(str(s) for s in seniority if s)
        locs = item.get("locationRestrictions") or []
        if isinstance(locs, list):
            location = ", ".join(str(x) for x in locs if x) or "Remote"
        else:
            location = str(locs) if locs else "Remote"
        out.append(
            make_job(
                title=item.get("title"),
                company=item.get("companyName"),
                location=location,
                source="Himalayas",
                date=item.get("pubDate"),
                description=item.get("description") or item.get("excerpt") or "",
                url=item.get("applicationLink") or item.get("guid") or "",
                tags=tags,
                remote=True,
                listed_level=listed,
            )
        )
    return out


def fetch_working_nomads(_query: str) -> list[dict]:
    data = cached_json("workingnomads", "https://www.workingnomads.com/api/exposed_jobs/")
    jobs = data if isinstance(data, list) else (data.get("jobs") if isinstance(data, dict) else [])
    out = []
    for item in jobs or []:
        if not isinstance(item, dict):
            continue
        tags = _as_tag_list(item.get("tags"))
        if item.get("category_name"):
            tags.append(item["category_name"])
        out.append(
            make_job(
                title=item.get("title"),
                company=item.get("company_name"),
                location=item.get("location") or "Remote",
                source="Working Nomads",
                date=item.get("pub_date"),
                description=item.get("description") or "",
                url=item.get("url") or "",
                tags=tags,
                remote=True,
            )
        )
    return out


SOURCES = [
    ("Remotive", fetch_remotive),
    ("Arbeitnow", fetch_arbeitnow),
    ("RemoteOK", fetch_remoteok),
    ("Jobicy", fetch_jobicy),
    ("Himalayas", fetch_himalayas),
    ("Working Nomads", fetch_working_nomads),
    ("The Muse", fetch_muse),
    ("Adzuna", fetch_adzuna),
]


def search_jobs(query: str, level: str, location: str, remote_only: bool) -> tuple[list[dict], list[dict]]:
    """Return (ranked jobs, per-source status rows)."""
    tokens = tokenize(query)
    status: list[dict] = []
    collected: list[dict] = []

    def run(name, fn):
        start = time.time()
        try:
            if name == "The Muse":
                jobs = fn(query, level)
            else:
                jobs = fn(query)
            elapsed = round(time.time() - start, 2)
            return name, jobs, None, elapsed
        except Exception as exc:  # noqa: BLE001 — a failed source must not break results
            elapsed = round(time.time() - start, 2)
            return name, [], str(exc), elapsed

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = [pool.submit(run, name, fn) for name, fn in SOURCES]
        for fut in as_completed(futures):
            name, jobs, error, elapsed = fut.result()
            if error:
                # Adzuna without keys is expected, not a failure to show as an error.
                skipped = error.lower().startswith("skipped:")
                status.append(
                    {
                        "name": name,
                        "ok": False,
                        "skipped": skipped,
                        "count": 0,
                        "matched": 0,
                        "error": error,
                        "elapsed": elapsed,
                    }
                )
            else:
                status.append(
                    {
                        "name": name,
                        "ok": True,
                        "skipped": False,
                        "count": len(jobs),
                        "matched": 0,
                        "error": "",
                        "elapsed": elapsed,
                    }
                )
                collected.extend(jobs)

    ranked: list[dict] = []
    seen: set[str] = set()
    for job in collected:
        if not job.get("url"):
            continue
        if not location_ok(job, location, remote_only):
            continue
        ks = keyword_score(job, tokens)
        if ks is None or ks < SCORE_THRESHOLD:
            continue
        adj = level_adjust(job, level)
        if adj is None:
            continue
        key = dedupe_key(job)
        if key in seen:
            continue
        seen.add(key)
        job = dict(job)
        job["score"] = ks + adj
        job["display_tags"] = (job.get("tags") or [])[:6]
        ranked.append(job)

    ranked.sort(key=lambda j: (-j["score"], j.get("date") or "", j.get("title") or ""))
    ranked = ranked[:MAX_RESULTS]

    matched_counts: dict[str, int] = {}
    for job in ranked:
        matched_counts[job["source"]] = matched_counts.get(job["source"], 0) + 1
    for row in status:
        row["matched"] = matched_counts.get(row["name"], 0)
    status.sort(key=lambda r: r["name"])
    return ranked, status


def build_pdf(jobs: list[dict], query: str, level_label: str, location: str, remote_only: bool) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Job results: {query}",
        author="Job Search App",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "JobTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#14233a"),
        spaceAfter=1,
        spaceBefore=8,
    )
    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#334155"),
        spaceAfter=1,
    )
    link_style = ParagraphStyle(
        "ApplyLink",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#1d4ed8"),
        spaceAfter=2,
    )
    head_style = ParagraphStyle(
        "DocHead",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=4,
        alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "DocSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#475569"),
        spaceAfter=2,
    )
    story = []
    generated = now_ist().strftime("%Y-%m-%d %H:%M IST")
    loc_bit = xml_escape(location) if location else "Any location"
    if remote_only:
        loc_bit += " · remote only"
    story.append(Paragraph("Job search results", head_style))
    story.append(
        Paragraph(
            f"{xml_escape(query or 'All roles')} · {xml_escape(level_label)} · {loc_bit}",
            sub_style,
        )
    )
    story.append(Paragraph(f"{len(jobs)} listing{'s' if len(jobs) != 1 else ''} · generated {generated}", sub_style))
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceAfter=2 * mm))

    if not jobs:
        story.append(Paragraph("No matching jobs were found for this search.", meta_style))
    for i, job in enumerate(jobs, 1):
        story.append(Paragraph(f"{i}. {xml_escape(job['title'])}", title_style))
        meta = " · ".join(
            [
                xml_escape(job["company"]),
                xml_escape(job["location"]),
                xml_escape(job["date"] or "Date not listed"),
                xml_escape(job["source"]),
            ]
        )
        story.append(Paragraph(meta, meta_style))
        href = xml_escape(job["url"], entities={'"': "&quot;"})
        story.append(Paragraph(f'<link href="{href}" color="blue">Apply: {xml_escape(job["url"])}</link>', link_style))

    doc.build(story)
    return buf.getvalue()


def form_args():
    query = (request.args.get("q") or request.form.get("q") or "").strip()
    level = (request.args.get("level") or request.form.get("level") or "mid").strip().lower()
    if level not in {k for k, _ in LEVEL_CHOICES}:
        level = "mid"
    location = (request.args.get("location") or request.form.get("location") or "").strip()
    remote_only = (request.args.get("remote") or request.form.get("remote") or "") in {"1", "on", "true", "yes"}
    return query, level, location, remote_only


def level_label(level: str) -> str:
    return dict(LEVEL_CHOICES).get(level, "Mid")


@app.get("/")
def index():
    query, level, location, remote_only = form_args()
    jobs, status, searched = [], [], False
    if query:
        searched = True
        jobs, status = search_jobs(query, level, location, remote_only)
    return render_template(
        "index.html",
        query=query,
        level=level,
        location=location,
        remote_only=remote_only,
        levels=LEVEL_CHOICES,
        jobs=jobs,
        status=status,
        searched=searched,
    )


@app.get("/pdf")
def pdf():
    query, level, location, remote_only = form_args()
    if not query:
        jobs, status = [], []
    else:
        jobs, status = search_jobs(query, level, location, remote_only)
    payload = build_pdf(jobs, query, level_label(level), location, remote_only)
    filename = "job-search-results.pdf"
    return Response(
        payload,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False)
