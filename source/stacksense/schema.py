"""Input field table: standard fields (spec section 3.1) plus extra research inputs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROJECT_TYPES = (
    "content_site", "web_app", "saas_b2b", "ecommerce", "marketplace", "social",
    "realtime_collab", "fintech", "healthtech", "data_analytics", "iot",
    "ai_ml_product", "internal_tool", "gaming", "api_platform", "other",
)
PLATFORMS = ("web", "android", "ios", "desktop", "api_only")
COMPLIANCE = ("gdpr", "dpdp", "hipaa", "pci_dss", "rbi_localisation", "soc2", "iso27001")
PRIORITY_KEYS = ("cost", "time_to_market", "scalability", "performance", "maintainability", "security")
KNOWN_LANGUAGES = (
    "python", "javascript", "typescript", "java", "csharp", "php", "go", "kotlin",
    "swift", "dart", "rust", "ruby", "elixir", "scala", "c", "cpp", "lua", "sql",
    "html", "css",
)
KNOWN_LICENCES = (
    "microsoft_365", "google_workspace", "aws_credits", "gcp_credits", "azure_credits",
    "github_enterprise", "gitlab", "atlassian", "sql_server", "oracle", "red_hat",
)
GEOGRAPHIES = ("india", "eu", "us", "apac", "latam", "africa", "middle_east", "global")
SECTIONS = (
    "project", "scale", "team", "budget", "timeline", "infrastructure",
    "requirements", "priorities",
)
SECTION_TITLES = {
    "": "Scenario",
    "project": "Project",
    "scale": "Scale",
    "team": "Team",
    "budget": "Budget",
    "timeline": "Timeline",
    "infrastructure": "Infrastructure",
    "requirements": "Requirements",
    "compliance": "Compliance",
    "priorities": "Priorities",
}
TOP_LEVEL_KEYS = ("scenario_id",) + SECTIONS[:7] + ("compliance", "priorities")


@dataclass(frozen=True)
class FieldSpec:
    path: str
    type: str              # string integer number boolean enum list_string list_enum object
    required: bool = False
    enum: tuple = ()
    default: Any = None
    min: float | None = None
    max: float | None = None
    help: str = ""
    affects: tuple = ()
    extra: bool = False    # not in spec section 3.1
    nullable: bool = False
    default_note: str = "optional field, documented default"


def _f(path, type_, **kw):
    return FieldSpec(path, type_, **kw)


FIELDS: list[FieldSpec] = [
    _f("scenario_id", "string", default="unnamed",
       help="Label echoed in the output so results can be matched to inputs.",
       affects=("output scenario_id",), default_note="no id supplied"),
    # project
    _f("project", "object", help="Project basics."),
    _f("project.name", "string", default="Untitled project",
       help="Display name only; it never changes the recommendation.",
       affects=("summary",)),
    _f("project.type", "enum", required=True, enum=PROJECT_TYPES,
       help="The kind of product sets the architecture family, effort estimate and which components fit.",
       affects=("architecture", "frontend", "backend", "database", "timeline")),
    _f("project.description", "string", default="",
       help="Free text. Keyword rules suggest requirements you may have forgotten and fill absent extra inputs.",
       affects=("suggestions", "extras defaults")),
    _f("project.platforms", "list_enum", required=True, enum=PLATFORMS,
       help="Client platforms to ship. Each extra platform adds effort and limits frontend choices.",
       affects=("frontend", "architecture", "timeline")),
    # scale
    _f("scale", "object", help="How many users and how they use the system."),
    _f("scale.users_at_launch", "integer", required=True, min=0, max=10**11,
       help="Users on day one. Together with the 12-month figure it sets the sizing and cost.",
       affects=("cost", "scalability", "hosting")),
    _f("scale.users_12_months", "integer", required=True, min=0, max=10**11,
       help="Expected users after a year. The larger of the two user counts drives scalability needs.",
       affects=("scalability", "architecture", "cost", "database")),
    _f("scale.peak_concurrent_users", "integer", min=0, max=10**11,
       help="Simultaneous users at peak; decides cache, queue and realtime sizing.",
       affects=("cache", "hosting", "architecture"),
       default_note="5% of the larger user count, minimum 10"),
    _f("scale.traffic_pattern", "enum", enum=("steady", "spiky", "seasonal"), default="steady",
       help="Spiky or seasonal load favours elastic hosting and adds a cost buffer.",
       affects=("architecture", "hosting", "cost")),
    _f("scale.workload", "enum", enum=("read_heavy", "balanced", "write_heavy"), default="balanced",
       help="Read-heavy suits caching and static delivery; write-heavy needs a database that scales writes.",
       affects=("database", "cache", "queue", "architecture")),
    _f("scale.data_volume_gb_12_months", "number", min=0, max=10**9,
       help="Stored data after a year; sizes database and object storage cost.",
       affects=("database", "storage", "cost", "search"),
       default_note="0.002 GB per 12-month user, minimum 1 GB"),
    _f("scale.peak_rps", "number", min=0, max=10**9, extra=True,
       help="Requests per second at peak. Separates compute-bound from user-count-bound designs.",
       affects=("serverless vs container", "cache", "queue", "cost"),
       default_note="0.2 requests per second per concurrent user, x1.5 when traffic is spiky"),
    _f("scale.user_geography", "list_enum", enum=GEOGRAPHIES, extra=True,
       help="Where users are. Picks the region, CDN need and the payment provider.",
       affects=("region", "cdn", "payments", "latency warnings"),
       default_note="from the description, else from compliance, else global"),
    # team
    _f("team", "object", help="Who builds and runs it."),
    _f("team.size", "integer", required=True, min=1, max=10000,
       help="Engineers available. Small teams get simpler architectures.",
       affects=("architecture", "timeline", "ops burden")),
    _f("team.experience", "enum", required=True, enum=("beginner", "intermediate", "senior", "mixed"),
       help="Experience level sets how steep a learning curve we can accept.",
       affects=("scoring", "timeline", "BEGINNER_COMPLEXITY")),
    _f("team.languages", "list_string", required=True,
       help="Languages the team already knows; matching them is the biggest speed-to-market lever.",
       affects=("frontend", "backend", "SKILL_MISMATCH")),
    _f("team.has_devops", "boolean", default=False,
       help="Whether someone can operate infrastructure. Without it we avoid self-run clusters.",
       affects=("hosting", "architecture", "NO_DEVOPS_SELF_HOSTED")),
    _f("team.size_in_12_months", "integer", min=1, max=10000, extra=True,
       help="Planned team size in a year; growth favours modular structure and bigger ecosystems.",
       affects=("architecture", "ecosystem weight"), default_note="same as team.size"),
    # budget, timeline
    _f("budget", "object", help="Money available."),
    _f("budget.monthly_infra_usd", "number", required=True, min=0, max=10_000_000,
       help="Monthly infrastructure and service budget. Never silently exceeded.",
       affects=("hosting", "database", "supporting services", "BUDGET_EXCEEDED")),
    _f("timeline", "object", help="Delivery window."),
    _f("timeline.mvp_weeks", "integer", required=True, min=1, max=520,
       help="Weeks until the first usable release; compared with an effort estimate.",
       affects=("TIMELINE_TIGHT", "frameworks", "BaaS vs custom")),
    # infrastructure
    _f("infrastructure", "object", help="Where and how it runs."),
    _f("infrastructure.cloud_preference", "enum", enum=("any", "aws", "gcp", "azure", "none"),
       default="any",
       help="A named cloud locks hosting, database and storage to it; none excludes hyperscalers.",
       affects=("hosting", "database", "supporting services")),
    _f("infrastructure.deployment_model", "enum",
       enum=("managed_cloud", "self_hosted_cloud_vm", "on_premise", "air_gapped"),
       default="managed_cloud",
       help="Self-hosted models rule out SaaS components; air-gapped rules out anything online.",
       affects=("hosting", "supporting services", "NO_DEVOPS_SELF_HOSTED")),
    _f("infrastructure.data_residency", "enum", enum=("none", "india", "eu", "us", "other"),
       default="none",
       help="Where data must stay. Filters regions and vendors that cannot pin data there.",
       affects=("hosting", "database", "region", "auth provider")),
    _f("infrastructure.existing_systems", "list_string", default=[],
       help="Systems to reuse or integrate. Matches are reused; end-of-life ones are flagged.",
       affects=("auth", "database", "EOL_EXISTING_SYSTEM")),
    _f("infrastructure.vendor_lock_in_tolerance", "enum", enum=("low", "medium", "high"), extra=True,
       help="How much proprietary lock-in is acceptable.",
       affects=("backend", "database", "hosting"), default_note="medium"),
    _f("infrastructure.existing_licences", "list_string", extra=True,
       help="Licences or credits already owned (for example microsoft_365 or aws_credits).",
       affects=("provider affinity", "auth", "ci_cd", "cost notes"), default_note="none"),
    # requirements
    _f("requirements", "object", help="Functional and quality requirements."),
    _f("requirements.realtime", "boolean", default=False,
       help="Live updates or chat; needs websocket-capable components.",
       affects=("backend", "realtime service", "cache")),
    _f("requirements.offline_support", "boolean", default=False,
       help="Must work without connectivity; adds a local store and sync design.",
       affects=("frontend", "offline_sync", "timeline")),
    _f("requirements.full_text_search", "boolean", default=False,
       help="Search over text content; picks database search or a search service.",
       affects=("database", "search")),
    _f("requirements.file_media_storage", "boolean", default=False,
       help="User uploads; adds object storage, a CDN and background processing.",
       affects=("storage", "cdn", "queue")),
    _f("requirements.payments", "boolean", default=False,
       help="Takes money; needs a payment provider, strong consistency and often PCI scope.",
       affects=("payments", "database", "backend")),
    _f("requirements.auth", "enum",
       enum=("none", "email_password", "social", "phone_otp", "enterprise_sso"), default="none",
       help="Sign-in method; picks the identity service and may add an SMS provider.",
       affects=("auth", "SMS_DLT_REQUIRED")),
    _f("requirements.multi_tenant", "boolean", default=False,
       help="Several customer organisations share the system; affects data isolation design.",
       affects=("database", "auth", "backend")),
    _f("requirements.ml_features", "enum", enum=("none", "inference_api", "llm_rag", "training"),
       default="none",
       help="Machine learning needs; picks the ML service and vector storage.",
       affects=("ml", "database", "timeline")),
    _f("requirements.seo_critical", "boolean", default=False,
       help="Public pages must rank in search engines; needs server or static rendering.",
       affects=("frontend", "cdn")),
    _f("requirements.availability", "enum", enum=("99", "99.9", "99.95", "99.99"), default="99.9",
       help="Uptime target as a string. Higher targets need multi-AZ or multi-region and cost more.",
       affects=("hosting", "database", "cost", "AVAILABILITY_UNREALISTIC")),
    _f("requirements.p95_latency_ms", "integer", min=1, max=3_600_000, nullable=True, default=None,
       help="Target 95th percentile response time; low values penalise cold starts and far regions.",
       affects=("performance weight", "serverless", "cache", "region"),
       default_note="no latency target"),
    _f("requirements.data_sensitivity", "enum",
       enum=("public", "internal", "personal", "sensitive", "financial", "health"), extra=True,
       help="How sensitive the stored data is; raises the security weight and adds a security baseline.",
       affects=("security weight", "security services", "telemetry"),
       default_note="from compliance, project type and description"),
    _f("requirements.consistency", "enum", enum=("strong", "eventual"), extra=True,
       help="Strong consistency needs ACID transactions; eventual allows document or key-value stores.",
       affects=("database",), default_note="from payments and project type"),
    _f("requirements.background_jobs", "boolean", extra=True,
       help="Scheduled or asynchronous work; adds a queue.",
       affects=("queue", "backend"), default_note="from payments, notifications, ML and uploads"),
    _f("requirements.notifications", "list_enum", enum=("email", "sms", "push"), extra=True,
       help="Message channels; adds a notifications component.",
       affects=("notifications service",), default_note="from platforms, auth and payments"),
    _f("requirements.connectivity", "enum", enum=("reliable", "intermittent", "poor"), extra=True,
       help="Network quality of end users; drives offline design and payload size.",
       affects=("offline_sync", "frontend"), default_note="from the description, else reliable"),
    _f("requirements.min_device_ram_gb", "number", min=0.1, max=1024, extra=True,
       help="RAM of the weakest target device; heavy runtimes are penalised on small devices.",
       affects=("frontend",), default_note="from the description, else unknown"),
    _f("requirements.rpo_minutes", "integer", min=0, max=10**6, extra=True,
       help="Data loss tolerated in a disaster; short values need point-in-time recovery.",
       affects=("database", "backups", "cost"), default_note="from the availability target"),
    # compliance, priorities
    _f("compliance", "list_enum", enum=COMPLIANCE, default=[],
       help="Regulations that apply; each filters vendors by attestation or region.",
       affects=("hosting", "database", "payments", "warnings")),
    _f("priorities", "object", help="How much each quality matters, 1 (low) to 5 (high)."),
]

_PRIORITY_HELP = {
    "cost": ("Weight of low running cost.", ("hosting", "database", "supporting services")),
    "time_to_market": ("Weight of shipping quickly.", ("frameworks", "BaaS vs custom")),
    "scalability": ("Weight of headroom for growth.", ("architecture", "database", "hosting")),
    "performance": ("Weight of raw speed.", ("backend", "frontend", "region")),
    "maintainability": ("Weight of long-term upkeep and hiring.", ("maturity", "ecosystem")),
    "security": ("Weight of security posture.", ("auth", "hosting", "security services")),
}
for _k in PRIORITY_KEYS:
    FIELDS.append(_f(f"priorities.{_k}", "integer", min=1, max=5, default=3,
                     help=_PRIORITY_HELP[_k][0], affects=_PRIORITY_HELP[_k][1]))

FIELD_INDEX: dict[str, FieldSpec] = {f.path: f for f in FIELDS}
ENUMS: dict[str, tuple] = {f.path: f.enum for f in FIELDS if f.enum}
REQUIRED_PATHS: tuple[str, ...] = tuple(f.path for f in FIELDS if f.required)


def children(section: str) -> list[FieldSpec]:
    prefix = section + "."
    return [f for f in FIELDS if f.path.startswith(prefix)]


def schema_for_ui() -> dict:
    """JSON-able description of every input field, grouped by section."""
    groups = []
    for key in ("",) + SECTIONS[:7] + ("compliance", "priorities"):
        if key == "":
            members = [FIELD_INDEX["scenario_id"]]
        elif key == "compliance":
            members = [FIELD_INDEX["compliance"]]
        else:
            members = children(key)
        groups.append({
            "id": key or "scenario",
            "title": SECTION_TITLES[key],
            "help": FIELD_INDEX[key].help if key in FIELD_INDEX else "",
            "fields": [{
                "path": f.path, "type": f.type, "required": f.required,
                "enum": list(f.enum), "default": f.default, "min": f.min, "max": f.max,
                "help": f.help, "affects": list(f.affects), "extra": f.extra,
                "nullable": f.nullable,
            } for f in members],
        })
    return {"sections": groups, "known_languages": list(KNOWN_LANGUAGES),
            "known_licences": list(KNOWN_LICENCES)}
