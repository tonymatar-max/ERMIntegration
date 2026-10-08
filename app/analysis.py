"""Live Q3-style analysis computed from the cached projects + timespent, so the
Analysis page refreshes with every "Refresh from Redmine". Pure functions — no
DB or network — given already-assembled projects (dashboard-shaped rows) and
timespent (Redmine + KSA entries).
"""

from . import resource_planning as rp


def _month_window(report_month: str, count: int = 3) -> list:
    """The `count` months ending at report_month (oldest first)."""
    months = [report_month]
    for _ in range(count - 1):
        months.insert(0, rp.previous_month_str(months[0]))
    return months


def build_trends(projects: list, timespent: list, report_month: str, months_back: int = 12) -> dict:
    """Monthly time-series (oldest→newest) derived from the cached time entries,
    so history is available immediately without waiting to accumulate. Covers
    hours (Redmine vs KSA), utilization %, active consultants and cross-team
    count per month, for the `months_back` months ending at report_month."""
    months = [report_month]
    for _ in range(months_back - 1):
        months.insert(0, rp.previous_month_str(months[0]))
    mset = set(months)
    pmap = {p["id"]: p for p in projects}

    hours = {m: {"total": 0.0, "redmine": 0.0, "ksa": 0.0} for m in months}
    users = {m: {} for m in months}
    user_mgrs = {m: {} for m in months}
    for t in timespent or []:
        m = (t.get("spent_on") or "")[:7]
        if m not in mset:
            continue
        h = float(t.get("hours") or 0)
        is_ksa = (t.get("project_id") or 0) < 0
        hours[m]["total"] += h
        hours[m]["ksa" if is_ksa else "redmine"] += h
        u = t.get("user_name") or "?"
        users[m][u] = users[m].get(u, 0.0) + h
        mgr = "KSA" if is_ksa else ((pmap.get(t.get("project_id")) or {}).get("managerName") or "(unknown)")
        user_mgrs[m].setdefault(u, set()).add(mgr)

    series = []
    for m in months:
        cap = rp.capacity_hours(m)
        actives = [v for v in users[m].values() if v > 0]
        total = sum(actives)
        series.append({
            "month": m, "label": rp.format_month_label(m),
            "total": round(total), "redmine": round(hours[m]["redmine"]), "ksa": round(hours[m]["ksa"]),
            "active": len(actives),
            "util_pct": round(100 * total / (len(actives) * cap)) if actives and cap else 0,
            "cross_team": sum(1 for s in user_mgrs[m].values() if len(s) > 1),
        })
    return {"months": months, "series": series}


def build_analysis(projects: list, timespent: list, report_month: str, excluded_codes=None) -> dict:
    excluded_codes = {c.upper() for c in (excluded_codes or set())}
    months = _month_window(report_month, 3)
    pmap = {p["id"]: p for p in projects}

    # ---- Portfolio ----
    est = spent = order = 0.0
    over = neg = active = 0
    by_country = {}
    for p in projects:
        pe, ps, po = float(p.get("est") or 0), float(p.get("spent") or 0), float(p.get("orderAmount") or 0)
        est += pe; spent += ps; order += po
        if pe > 0 and ps > pe:
            over += 1
        if float(p.get("remaining") or 0) < 0:
            neg += 1
        if "active" in (p.get("status") or "").lower():
            active += 1
        c = (p.get("country") or "").strip() or "(no country)"
        b = by_country.setdefault(c, {"projects": 0, "est": 0.0, "spent": 0.0, "order": 0.0})
        b["projects"] += 1; b["est"] += pe; b["spent"] += ps; b["order"] += po
    for c, b in by_country.items():
        b["consumed_pct"] = round(100 * b["spent"] / b["est"]) if b["est"] else 0
        b["rate_per_spent_h"] = round(b["order"] / b["spent"]) if b["spent"] else 0

    # ---- Time: by month, KSA split, utilization, cross-team, country/manager ----
    hours = {m: {"total": 0.0, "redmine": 0.0, "ksa": 0.0} for m in months}
    user_hours = {m: {} for m in months}            # consultant -> hours
    user_mgrs = {m: {} for m in months}             # consultant -> set(managers)
    ksa_split = {"project": 0.0, "leave_support": 0.0}
    for t in timespent or []:
        m = (t.get("spent_on") or "")[:7]
        if m not in hours:
            continue
        h = float(t.get("hours") or 0)
        is_ksa = (t.get("project_id") or 0) < 0
        hours[m]["total"] += h
        hours[m]["ksa" if is_ksa else "redmine"] += h
        u = t.get("user_name") or "?"
        user_hours[m][u] = user_hours[m].get(u, 0.0) + h
        mgr = "KSA" if is_ksa else ((pmap.get(t.get("project_id")) or {}).get("managerName") or "(unknown)")
        user_mgrs[m].setdefault(u, set()).add(mgr)
        if is_ksa:
            code = (t.get("project_code") or "").upper()
            if code and code in excluded_codes:
                ksa_split["leave_support"] += h
            else:
                ksa_split["project"] += h

    util = {}
    for m in months:
        cap = rp.capacity_hours(m)
        actives = [v for v in user_hours[m].values() if v > 0]
        total = sum(actives)
        util[m] = {
            "capacity": round(cap),
            "active": len(actives),
            "avg_pct": round(100 * total / (len(actives) * cap)) if actives and cap else 0,
            "total": round(total),
        }
    cross_team = {m: sum(1 for s in user_mgrs[m].values() if len(s) > 1) for m in months}

    # ---- Top over-budget (by overrun hours) ----
    top_over = sorted(
        ({
            "name": p.get("name") or "",
            "country": p.get("country") or "",
            "pm": p.get("managerName") or "",
            "est": round(float(p.get("est") or 0)),
            "spent": round(float(p.get("spent") or 0)),
            "overrun": round(float(p.get("spent") or 0) - float(p.get("est") or 0)),
            "pct": round(100 * float(p.get("spent") or 0) / float(p.get("est") or 1)),
        } for p in projects if float(p.get("est") or 0) > 0 and float(p.get("spent") or 0) > float(p.get("est") or 0)),
        key=lambda r: r["overrun"], reverse=True,
    )[:8]
    total_overrun = sum(r["overrun"] for r in top_over)

    # ---- Top consultants (window total) ----
    totals = {}
    for m in months:
        for u, h in user_hours[m].items():
            totals[u] = totals.get(u, 0.0) + h
    top_consultants = sorted(
        ({"name": u, "months": [round(user_hours[m].get(u, 0.0)) for m in months], "total": round(tot)}
         for u, tot in totals.items()),
        key=lambda r: r["total"], reverse=True,
    )[:12]

    return {
        "months": months,
        "month_labels": [rp.format_month_label(m) for m in months],
        "portfolio": {
            "projects": len(projects), "active": active,
            "est": round(est), "spent": round(spent), "order": round(order),
            "consumed_pct": round(100 * spent / est) if est else 0,
            "over_budget": over, "negative_remaining": neg,
            "window_hours": round(sum(hours[m]["total"] for m in months)),
        },
        "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1]["order"])),
        "hours_by_month": {m: {k: round(v) for k, v in hours[m].items()} for m in months},
        "utilization": util,
        "cross_team": cross_team,
        "top_over_budget": top_over,
        "total_overrun": total_overrun,
        "ksa_split": {k: round(v) for k, v in ksa_split.items()},
        "top_consultants": top_consultants,
    }
