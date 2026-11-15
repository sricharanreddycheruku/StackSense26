"""Dataclasses shared by all modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from . import ENGINE_VERSION, KB_AS_OF  # noqa: F401  (re-exported for callers)

Severity = Literal["info", "warning", "critical"]
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Warn:
    code: str
    severity: Severity
    message: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


@dataclass
class Validated:
    spec: dict
    errors: list[FieldError]
    assumptions: list[str]
    warnings: list[Warn]
    provided: frozenset[str]


@dataclass(frozen=True)
class Signal:
    keyword: str
    field: str
    value: Any
    applied: bool
    reason: str


@dataclass
class Derived:
    users_peak: int
    scale_level: float
    req_scale_rating: float
    peak_concurrent: int
    peak_rps: float
    zone: str | None
    preferred_zone: str
    platforms: frozenset[str]
    needs_backend: bool
    needs_database: bool
    data_sensitivity: str
    consistency: str
    connectivity: str
    min_device_ram_gb: float | None
    notifications: tuple[str, ...]
    background_jobs: bool
    effort_points: float
    est_mvp_weeks: float
    weights: dict[str, float]
    signals: tuple[Signal, ...]
    existing: tuple[dict, ...]
    licences: frozenset[str]
    team_langs: frozenset[str]
    user_geography: tuple[str, ...] = ("global",)
    team_size_12m: int = 1
    lock_in_tolerance: str = "medium"
    rpo_minutes: int = 60
    collab: bool = False
    geo: bool = False
    static_hint: bool = False
    cms: bool = False
    assumptions: list[str] = field(default_factory=list)


@dataclass
class Constraints:
    zone: str | None
    allowed_providers: frozenset[str] | None
    cloud_lock: str | None
    no_hyperscaler: bool
    self_host_only: bool
    no_saas: bool
    no_cdn: bool
    no_internet: bool
    required_attestations: frozenset[str]
    payment_zone: str | None
    conflicts: list[Warn] = field(default_factory=list)
    relaxations: list[str] = field(default_factory=list)
    relaxed_requirements: dict = field(default_factory=dict)
    notes: list[Warn] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    deployment: str = "managed_cloud"
    auth_needed: bool = False
    commercial: bool = True
    no_free_tier: bool = False
    k8s_allowed: bool = False
    onprem_processors: bool = False


@dataclass(frozen=True)
class Mod:
    delta: float
    reason: str
    fields: tuple[str, ...] = ()


@dataclass
class Scored:
    entry_id: str
    offering_id: str | None
    name: str
    score: float
    dims: dict[str, float] = field(default_factory=dict)
    mods: list[Mod] = field(default_factory=list)
    region: str | None = None
    provider: str | None = None
    entry: dict | None = field(default=None, repr=False, compare=False)
    off: dict | None = field(default=None, repr=False, compare=False)
    vs: "Scored | None" = field(default=None, repr=False, compare=False)
    cost: tuple = field(default=(0.0, 0.0), repr=False, compare=False)
    stack_gap: float | None = field(default=None, repr=False, compare=False)
    bundled_in: str | None = field(default=None, repr=False, compare=False)
    team_lang: str | None = field(default=None, repr=False, compare=False)


def article(n: int) -> str:
    """'a' or 'an' for a number read aloud: an 8, an 11, an 18, an 80, but a 6 or a 1,100."""
    digits = str(abs(int(n)))
    lead = int(digits[:len(digits) % 3 or 3])
    return "an" if lead in (8, 11, 18) or 80 <= lead <= 89 or 800 <= lead <= 899 else "a"


@dataclass(frozen=True)
class Excluded:
    entry_id: str
    name: str
    reason: str
    fields: tuple[str, ...] = ()


@dataclass
class Pick:
    layer: str
    sub_layer: str | None = None
    chosen: Scored | None = None
    none_reason: str | None = None
    none_fields: tuple[str, ...] = ()
    runners_up: list[Scored] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    parts: list[Scored] = field(default_factory=list)
    note: str | None = None


@dataclass
class ArchDecision:
    style: str
    rule_id: str
    reason: str
    fields: tuple[str, ...] = ()
    rejected: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)


@dataclass
class Selection:
    arch: ArchDecision
    frontend: Pick
    backend: Pick
    database: Pick
    hosting: Pick
    supporting: list[Pick] = field(default_factory=list)
    zone: str | None = None
    region: str | None = None
    dr_region: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class CostBand:
    min: float
    max: float
    assumptions: list[str] = field(default_factory=list)
    breakdown: list[dict] = field(default_factory=list)
