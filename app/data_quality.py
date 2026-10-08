"""Data-quality checks over the cached projects + time entries, plus a
natural-language query helper. Pure functions — no DB or network — given the
already-assembled projects (dashboard-shaped rows) and timespent (Redmine +
KSA entries), the same inputs the Analysis page uses. The route in main.py
applies the usual visibility rules before calling in.

The goal is to surface the recurring hygiene problems this app keeps tripping
over — projects with no country (so they fall into the "(no country)" bucket),
consultants whose name-order differs between Redmine and the KSA sheets (so
their hours split across two rows), and stale or missing project reviews — as
one actionable list, each item linking back to where it's fixed.
"""

from datetime import date, datetime


def _is_active(p: dict) -> bool:
    return "active" in (p.get("status") or "").lower()


def _parse_date(value: str):
    """Redmine's 'Last rev. date' comes back as YYYY-MM-DD (or blank)."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _token_key(name: str) -> str:
    """Order-independent key for a person's name: lowercased name tokens
    sorted, so 'Fatma El Raae' and 'El Raae Fatma' collapse to the same key.
    This is what catches a consultant entered in a different word order
    between Redmine and the KSA timesheet."""
    return " ".join(sorted((name or "").lower().split()))


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def no_country_projects(projects: list) -> list:
    """Active projects with a blank country — these land in the dashboard's
    "(no country)" bucket and distort every per-country figure."""
    out = []
    for p in projects:
        if not _is_active(p):
            continue
        if not (p.get("country") or "").strip():
            out.append({
                "id": p.get("id"), "name": p.get("name") or "",
                "rapportCode": p.get("rapportCode") or "",
                "manager": p.get("managerName") or "",
                "spent": round(float(p.get("spent") or 0)),
            })
    out.sort(key=lambda r: -r["spent"])
    return out


def name_flip_duplicates(timespent: list, name_map: dict) -> list:
    """Consultants whose hours are at risk of splitting across two rows:
    either the same Redmine user_id logged under two different display
    names, or two names that are anagrams of each other by word order
    (classic Redmine-vs-KSA 'First Last' / 'Last First'). Variants already
    unified by the admin name map are excluded — those are considered fixed.

    `name_map` is settings_store.get_consultant_name_map() ({normalized
    variant -> canonical}); a name already a key there is treated as mapped."""
    mapped = set(name_map or {})

    # Collect display names seen per user_id and per token-key.
    by_uid = {}            # user_id -> set(display names)
    by_tokenkey = {}       # token key -> set(display names)
    for t in timespent or []:
        name = (t.get("user_name") or "").strip()
        if not name:
            continue
        uid = t.get("user_id")
        if uid:
            by_uid.setdefault(uid, set()).add(name)
        by_tokenkey.setdefault(_token_key(name), set()).add(name)

    groups = []
    seen = set()

    # 1) Same user_id, more than one spelling.
    for uid, names in by_uid.items():
        if len(names) > 1 and not all(_norm(n) in mapped for n in names):
            key = tuple(sorted(names))
            if key not in seen:
                seen.add(key)
                groups.append({"reason": "same Redmine id, two spellings",
                               "names": sorted(names)})

    # 2) Different spellings that are the same name reordered.
    for tkey, names in by_tokenkey.items():
        if len(names) > 1 and not all(_norm(n) in mapped for n in names):
            key = tuple(sorted(names))
            if key not in seen:
                seen.add(key)
                groups.append({"reason": "same name, different word order",
                               "names": sorted(names)})

    groups.sort(key=lambda g: g["names"][0].lower())
    return groups


def stale_or_missing_reviews(projects: list, today: date = None, stale_days: int = 90) -> list:
    """Active projects whose last review is missing or older than
    `stale_days` — over-budget ones first, since those are where a stale
    review most matters. 'Over budget' = spent beyond estimate (same test
    the analysis uses)."""
    today = today or date.today()
    out = []
    for p in projects:
        if not _is_active(p):
            continue
        rev = _parse_date(p.get("revDate"))
        age = (today - rev).days if rev else None
        has_summary = bool((p.get("summary") or "").strip())
        missing = rev is None and not has_summary
        stale = rev is not None and age > stale_days
        if not (missing or stale):
            continue
        est = float(p.get("est") or 0)
        spent = float(p.get("spent") or 0)
        over = est > 0 and spent > est
        out.append({
            "id": p.get("id"), "name": p.get("name") or "",
            "rapportCode": p.get("rapportCode") or "",
            "manager": p.get("managerName") or "",
            "country": (p.get("country") or "").strip() or "(no country)",
            "revDate": p.get("revDate") or "",
            "age_days": age, "missing": missing, "over_budget": over,
            "spent": round(spent),
        })
    # Over-budget + missing to the top, then by age (missing counts as oldest).
    out.sort(key=lambda r: (not r["over_budget"], not r["missing"],
                            -(r["age_days"] or 10**6)))
    return out


def missing_estimate(projects: list) -> list:
    """Active, non-T&M projects that have logged time but no estimate — their
    consumed-% and remaining-hours figures are meaningless until an estimate
    is set."""
    out = []
    for p in projects:
        if not _is_active(p) or p.get("isTM"):
            continue
        est = float(p.get("est") or 0)
        spent = float(p.get("spent") or 0)
        if est <= 0 and spent > 0:
            out.append({
                "id": p.get("id"), "name": p.get("name") or "",
                "rapportCode": p.get("rapportCode") or "",
                "manager": p.get("managerName") or "",
                "spent": round(spent),
            })
    out.sort(key=lambda r: -r["spent"])
    return out


def build_report(projects: list, timespent: list, name_map: dict,
                 today: date = None, stale_days: int = 90) -> dict:
    """Assemble every check into one structure for the Data Quality page."""
    no_country = no_country_projects(projects)
    flips = name_flip_duplicates(timespent, name_map)
    reviews = stale_or_missing_reviews(projects, today, stale_days)
    no_est = missing_estimate(projects)
    checks = [
        {"key": "no_country", "label": "Projects with no country",
         "desc": "Active projects missing a country — they fall into the "
                 "“(no country)” bucket and skew per-country figures. "
                 "Fix the Country field in Redmine, then refresh.",
         "count": len(no_country), "rows": no_country, "fix": "project"},
        {"key": "name_flips", "label": "Possible consultant duplicates",
         "desc": "Names that likely refer to one person (same Redmine id, or "
                 "the same name in a different word order) and aren’t yet "
                 "unified. Add a line to the consultant name map so their "
                 "hours merge.",
         "count": len(flips), "rows": flips, "fix": "name_map"},
        {"key": "stale_reviews", "label": "Stale or missing reviews",
         "desc": f"Active projects whose last review is missing or older than "
                 f"{stale_days} days. Over-budget projects are listed first.",
         "count": len(reviews), "rows": reviews, "fix": "review"},
        {"key": "no_estimate", "label": "Logged time but no estimate",
         "desc": "Active fixed-price projects with hours logged but no "
                 "estimate — consumed-% and remaining are meaningless "
                 "until an estimate is set.",
         "count": len(no_est), "rows": no_est, "fix": "project"},
    ]
    total = sum(c["count"] for c in checks)
    return {"checks": checks, "total_issues": total}


# ---------------------------------------------------------------------------
# Natural-language query over the ledger (compact serialization for the model)
# ---------------------------------------------------------------------------

def build_query_context(projects: list, max_projects: int = 400) -> str:
    """A compact, token-frugal text snapshot of the portfolio for the AI query
    endpoint — one line per project with the figures a delivery question is
    likely to need. Capped so a very large portfolio can't blow the prompt;
    the biggest-spend projects are kept."""
    rows = sorted(projects, key=lambda p: -float(p.get("spent") or 0))[:max_projects]
    lines = []
    for p in rows:
        est = float(p.get("est") or 0)
        spent = float(p.get("spent") or 0)
        pct = round(spent / est * 100) if est else ""
        lines.append(
            f"#{p.get('id')} | {p.get('name') or ''} | country={(p.get('country') or '').strip() or '-'} "
            f"| mgr={p.get('managerName') or '-'} | status={p.get('status') or '-'} "
            f"| est={round(est)}h spent={round(spent)}h ({pct}%) "
            f"| remaining={round(float(p.get('remaining') or 0))}h "
            f"| order=${round(float(p.get('orderAmount') or 0))} | risk={p.get('risk') or '-'}"
        )
    header = (f"Portfolio snapshot: {len(projects)} projects"
              + (f" (showing top {len(rows)} by spend)" if len(rows) < len(projects) else "")
              + ". Columns: id | name | country | manager | status | estimate/spent hours "
                "(consumed%) | remaining hours | order amount | risk.")
    return header + "\n" + "\n".join(lines)
