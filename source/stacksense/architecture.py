"""Architecture style rule table; the first rule that matches wins."""
from __future__ import annotations

from .model import ArchDecision, Derived

STYLES = ("static", "microservices", "service_oriented", "serverless", "modular_monolith", "monolith")
HEAVY_TYPES = ("iot", "data_analytics", "gaming")
BUSINESS_TYPES = ("fintech", "healthtech", "saas_b2b", "marketplace", "ecommerce")


def _get(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def choose(spec: dict, d: Derived) -> ArchDecision:
    size = _get(spec, "team.size", 1)
    size12 = d.team_size_12m
    devops = bool(_get(spec, "team.has_devops", False))
    exp = _get(spec, "team.experience", "intermediate")
    users12 = _get(spec, "scale.users_12_months", 0)
    traffic = _get(spec, "scale.traffic_pattern", "steady")
    workload = _get(spec, "scale.workload", "balanced")
    ptype = _get(spec, "project.type")
    p95 = _get(spec, "requirements.p95_latency_ms")
    scal_prio = _get(spec, "priorities.scalability", 3)
    realtime = bool(_get(spec, "requirements.realtime", False))
    ml = _get(spec, "requirements.ml_features", "none")
    deployment = _get(spec, "infrastructure.deployment_model", "managed_cloud")

    rejected: list[tuple[str, str, tuple[str, ...]]] = []

    # S1
    if not d.needs_backend:
        if "web" in d.platforms:
            return ArchDecision(
                "static", "S1",
                "No server-side logic or stored user data is needed (project.type is "
                f"{ptype}, platforms include web), so the site is built ahead of time and served as static files.",
                ("project.type", "project.platforms", "requirements.auth"),
                _rejections(rejected, "microservices", "service_oriented", "serverless", "modular_monolith",
                            "monolith", why="there is no server component to split or scale"))
        return ArchDecision(
            "other", "S1b",
            "No server-side logic is needed and the app is not a web site, so it runs fully on the device "
            "(client-only app).", ("project.platforms", "requirements.auth"), rejected)

    # S2
    micro_ok = ((size >= 12 or size12 >= 15) and devops and users12 >= 1_000_000 and scal_prio >= 4
                and exp in ("senior", "mixed"))
    if micro_ok:
        return ArchDecision(
            "microservices", "S2",
            f"Team of {size} ({size12} in 12 months) with a devops engineer, {users12:,} users in 12 months and "
            f"scalability priority {scal_prio} justify independently scaled services.",
            ("team.size", "team.has_devops", "scale.users_12_months", "priorities.scalability"), rejected)
    why_not_micro = _why_not_micro(size, size12, devops, users12, scal_prio, exp)
    rejected.append(("microservices", why_not_micro, ("team.size", "team.has_devops", "scale.users_12_months")))

    # S3
    heavy = []
    if realtime and d.peak_concurrent >= 2000:
        heavy.append(f"realtime with {d.peak_concurrent:,} concurrent users")
    if ml == "training":
        heavy.append("model training")
    if ptype in HEAVY_TYPES and workload == "write_heavy":
        heavy.append(f"{ptype} with a write-heavy workload")
    if devops and size >= 5 and heavy:
        return ArchDecision(
            "service_oriented", "S3",
            f"A devops engineer and a team of {size} can run a separate service for {'; '.join(heavy)}, "
            "which scales differently from the main API.",
            ("team.has_devops", "team.size", "requirements.realtime", "scale.peak_concurrent_users"), rejected)
    if heavy:
        rejected.append(("service_oriented", f"there is a separable heavy workload ({'; '.join(heavy)}) but "
                         f"team.has_devops is {str(devops).lower()} and team.size is {size} "
                         "(needs a devops engineer and 5 or more people)", ("team.has_devops", "team.size")))
    else:
        rejected.append(("service_oriented", "no workload is heavy enough to need its own service "
                         "(realtime under 2,000 concurrent users, no model training, no write-heavy "
                         "iot, analytics or gaming)", ("requirements.realtime", "scale.peak_concurrent_users")))

    # S4
    managed = deployment == "managed_cloud"
    big_realtime = ptype in ("gaming", "realtime_collab") and d.peak_concurrent >= 2000
    fast = p95 is not None and p95 <= 100
    if managed and not devops and size <= 4 and traffic in ("spiky", "seasonal") and not fast and not big_realtime:
        return ArchDecision(
            "serverless", "S4",
            f"No devops engineer, a team of {size} and {traffic} traffic suit pay-per-use serverless "
            "or backend-as-a-service, which scales to zero between peaks.",
            ("team.has_devops", "team.size", "scale.traffic_pattern"), rejected)
    rejected.append(("serverless", _why_not_serverless(managed, devops, size, traffic, fast, p95, big_realtime),
                     ("infrastructure.deployment_model", "team.has_devops", "scale.traffic_pattern",
                      "requirements.p95_latency_ms")))

    # S5
    if size >= 4 or size12 >= 6 or (ptype in BUSINESS_TYPES and size >= 3 and exp != "beginner"):
        why = (f"a team of {size}" if size >= 4 else f"{size12} people in 12 months" if size12 >= 6
               else f"a {ptype} project with a team of {size}")
        return ArchDecision(
            "modular_monolith", "S5",
            f"{why[0].upper() + why[1:]} benefits from clear module boundaries inside one deployable unit, "
            "without the operating cost of separate services.",
            ("team.size", "project.type", "team.experience"), rejected)
    rejected.append(("modular_monolith", f"a team of {size} does not need enforced module boundaries yet",
                     ("team.size",)))
    return ArchDecision(
        "monolith", "S6",
        f"A team of {size} ships fastest with one deployable application and one database; modules can be "
        "split later if needed.", ("team.size", "timeline.mvp_weeks"), rejected)


def _rejections(rejected, *styles, why):
    out = list(rejected)
    for s in styles:
        out.append((s, why, ("requirements.auth", "project.platforms")))
    return out


def _why_not_micro(size, size12, devops, users12, scal_prio, exp) -> str:
    miss = []
    if not (size >= 12 or size12 >= 15):
        miss.append(f"team.size is {size}")
    if not devops:
        miss.append("team.has_devops is false")
    if users12 < 1_000_000:
        miss.append(f"scale.users_12_months is {users12:,}")
    if scal_prio < 4:
        miss.append(f"priorities.scalability is {scal_prio}")
    if exp not in ("senior", "mixed"):
        miss.append(f"team.experience is {exp}")
    return "needs 12+ people, a devops engineer, 1,000,000+ users and scalability priority 4+; here " + ", ".join(miss)


def _why_not_serverless(managed, devops, size, traffic, fast, p95, big_realtime) -> str:
    if not managed:
        return "serverless needs a managed cloud deployment"
    if devops or size > 4:
        return f"a team of {size} with devops={str(devops).lower()} can run long-lived servers, which cost less at steady load"
    if traffic == "steady":
        return "traffic is steady, so always-on servers are cheaper than pay-per-request"
    if fast:
        return f"requirements.p95_latency_ms is {p95}, and cold starts would break it"
    if big_realtime:
        return "long-lived connections at this concurrency do not fit functions"
    return "not suited to this workload"
