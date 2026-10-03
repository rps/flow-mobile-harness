"""Shared contracts for the harness.

Standard library only. Every type that goes into a trace has to_dict() and
from_dict() and survives a JSON round trip; enums serialise as their values.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# --- Enums -------------------------------------------------------------------


class Verdict(StrEnum):
    """The agent's own verdict, given in its finish call."""

    DONE = "done"
    INFEASIBLE = "infeasible"
    FAILED = "failed"


class TerminationReason(StrEnum):
    """Why the harness ended the run."""

    FINISHED = "finished"
    STEP_CAP = "step_cap"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    ERROR = "error"


class FlowType(StrEnum):
    A = "a"  # goal-based completion
    B = "b"  # cross-app information transfer
    C = "c"  # UI variation robustness
    D = "d"  # multi-app orchestration
    E = "e"  # hybrid routing
    F = "f"  # stop-and-confirm
    G = "g"  # interruption recovery
    H = "h"  # infeasible goal
    DRIFT = "drift"
    FREEFORM = "freeform"


class OracleTier(IntEnum):
    """What a task's score rests on, strongest first (PRODUCT.md)."""

    OWN_STORAGE = 1
    APP_EXPORT_API = 2
    DOWNSTREAM_EFFECT = 3
    ROOT_PRIVATE_DB = 4
    SCRIPTED_READBACK = 5
    MODEL_JUDGE = 6

    @property
    def is_headline(self) -> bool:
        return self <= OracleTier.DOWNSTREAM_EFFECT


class ConfirmationDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


# --- Serialisation helper ----------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _opt(cls: Callable[[Any], Any], value: Any) -> Any:
    return None if value is None else cls(value)


# --- Device (agent-facing) ---------------------------------------------------


class DeviceError(Exception):
    """An action or read on the device failed."""


class QueryNotAllowed(DeviceError):
    """query_structured was called with a name outside the allow-list."""


@dataclass(frozen=True)
class Screenshot:
    """A downscaled PNG plus the real screen size, for coordinate mapping.

    `png` is the downscaled image the model sees. `width`/`height` are real
    device pixels; `scaled_width`/`scaled_height` are the PNG's pixels.
    Not stored in the trace; the trace keeps a file path instead.
    """

    png: bytes
    width: int
    height: int
    scaled_width: int
    scaled_height: int

    def to_device(self, x: int, y: int) -> tuple[int, int]:
        """Map a point in screenshot (scaled) space to real device pixels."""
        return (
            round(x * self.width / self.scaled_width),
            round(y * self.height / self.scaled_height),
        )


@runtime_checkable
class Device(Protocol):
    """What the agent can do to the device.

    tap/swipe coordinates are in screenshot (scaled) space; implementations
    map them to device pixels. Actions return None and raise DeviceError on
    failure. Verifier-only reads live elsewhere and are not part of this.
    """

    def screenshot(self) -> Screenshot: ...

    def ui_tree(self) -> str: ...

    def tap(self, x: int, y: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...

    def back(self) -> None: ...

    def home(self) -> None: ...

    def open_app(self, package: str) -> None: ...

    def allowed_queries(self) -> list[str]: ...

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run an allow-listed, read-only query. Raises QueryNotAllowed."""
        ...


# --- Confirmation ------------------------------------------------------------


@dataclass
class ConfirmationRequest:
    action: str
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ConfirmationRequest:
        return cls(action=d["action"], summary=dict(d["summary"]))


ConfirmationHandler = Callable[[ConfirmationRequest], ConfirmationDecision]


# --- Verifier results --------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CheckResult:
        return cls(name=d["name"], passed=d["passed"], detail=d.get("detail", ""))


@dataclass
class VerifierResult:
    passed: bool
    oracle_tier: OracleTier
    end_state: list[CheckResult] = field(default_factory=list)
    side_effects: list[CheckResult] = field(default_factory=list)
    process: list[CheckResult] = field(default_factory=list)
    self_report_agrees: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> VerifierResult:
        return cls(
            passed=d["passed"],
            oracle_tier=OracleTier(d["oracle_tier"]),
            end_state=[CheckResult.from_dict(c) for c in d.get("end_state", [])],
            side_effects=[CheckResult.from_dict(c) for c in d.get("side_effects", [])],
            process=[CheckResult.from_dict(c) for c in d.get("process", [])],
            self_report_agrees=d.get("self_report_agrees"),
        )


# --- Trace -------------------------------------------------------------------


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TokenUsage:
        return cls(**{f.name: d.get(f.name, 0) for f in dataclasses.fields(cls)})


@dataclass
class StepRecord:
    """One agent step. Timestamps are ISO-8601 UTC strings; paths are
    relative to the run folder."""

    index: int
    started_at: str
    ended_at: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_result: dict[str, Any]
    reasoning: str = ""
    screenshot_path: str | None = None
    ui_tree_path: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)  # harness measurements, e.g. settle_s; never shown to the agent

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> StepRecord:
        return cls(
            index=d["index"],
            started_at=d["started_at"],
            ended_at=d["ended_at"],
            tool_name=d["tool_name"],
            tool_input=dict(d["tool_input"]),
            tool_result=dict(d["tool_result"]),
            reasoning=d.get("reasoning", ""),
            screenshot_path=d.get("screenshot_path"),
            ui_tree_path=d.get("ui_tree_path"),
            usage=TokenUsage.from_dict(d.get("usage", {})),
            duration_ms=d.get("duration_ms", 0),
            meta=dict(d.get("meta", {})),
        )


@dataclass
class RunRecord:
    """One run. agent_verdict is None when the harness ended the run before
    a finish call; verifier_result is None for freeform runs; seed is filled
    in by the runner only after the run ends; meta holds runner bookkeeping
    (confirmation policy, wall time) and is never shown to the agent."""

    run_id: str
    task_id: str | None
    flow_type: FlowType
    goal: str
    model: str
    started_at: str
    ended_at: str | None = None
    agent_verdict: Verdict | None = None
    agent_summary: str | None = None
    termination_reason: TerminationReason | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float | None = None
    verifier_result: VerifierResult | None = None
    seed: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RunRecord:
        return cls(
            run_id=d["run_id"],
            task_id=d.get("task_id"),
            flow_type=FlowType(d["flow_type"]),
            goal=d["goal"],
            model=d["model"],
            started_at=d["started_at"],
            ended_at=d.get("ended_at"),
            agent_verdict=_opt(Verdict, d.get("agent_verdict")),
            agent_summary=d.get("agent_summary"),
            termination_reason=_opt(TerminationReason, d.get("termination_reason")),
            usage=TokenUsage.from_dict(d.get("usage", {})),
            estimated_cost_usd=d.get("estimated_cost_usd"),
            verifier_result=_opt(VerifierResult.from_dict, d.get("verifier_result")),
            seed=d.get("seed"),
            meta=dict(d.get("meta", {})),
        )


# --- Task definition ---------------------------------------------------------


@dataclass(frozen=True)
class RunLimits:
    """Per-task caps on the agent's run. They replace Config.max_steps,
    Config.wall_clock_s and Config.run_cap_usd for that task, so a slow task
    gets headroom and a short one cannot spend long in an action loop."""

    max_steps: int
    wall_clock_s: float
    run_cap_usd: float


@dataclass
class TaskSpec:
    """A predefined task. Agent-side code gets `goal` only.

    seed_spec and checks are for the runner and verifier and may hold
    callables, so TaskSpec itself is not serialised into the trace; the
    trace records task_id, flow_type and the verifier's oracle_tier.
    """

    id: str
    flow_type: FlowType
    goal: str
    oracle_tier: OracleTier
    seed_spec: Any = None
    checks: list[Any] = field(default_factory=list)
    sensitive_actions: list[str] = field(default_factory=list)
    limits: RunLimits | None = None  # None: the Config limits apply


# --- Config ------------------------------------------------------------------


class ConfigError(Exception):
    pass


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse KEY=VALUE lines. Skips blanks and # comments, strips an
    `export ` prefix and matching quotes. Missing file -> {}. Does not touch
    os.environ."""
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    max_steps: int = 40
    wall_clock_s: float = 600.0
    budget_usd: float = 2.0
    screenshot_max_px: int = 1280
    runs_dir: str = "runs"
    run_cap_usd: float = 1.50  # per-run model spend cap (the agent stops when reached)
    api_key: str | None = field(default=None, repr=False)

    _ENV = {
        "model": "HARNESS_MODEL",
        "max_steps": "HARNESS_MAX_STEPS",
        "wall_clock_s": "HARNESS_WALL_CLOCK_S",
        "budget_usd": "HARNESS_BUDGET_USD",
        "screenshot_max_px": "HARNESS_SCREENSHOT_MAX_PX",
        "runs_dir": "HARNESS_RUNS_DIR",
        "run_cap_usd": "HARNESS_RUN_CAP_USD",
        "api_key": "ANTHROPIC_API_KEY",
    }

    @classmethod
    def from_env(
        cls,
        env_file: str | os.PathLike[str] | None = ".env",
        environ: Mapping[str, str] | None = None,
    ) -> Config:
        """Defaults, overridden by env_file, overridden by environ
        (os.environ when None)."""
        merged = load_env_file(env_file) if env_file is not None else {}
        merged.update(os.environ if environ is None else environ)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            raw = merged.get(cls._ENV[f.name])
            if raw is None or raw == "":
                continue
            conv = {int: int, float: float}.get(type(f.default), str)
            try:
                kwargs[f.name] = conv(raw)
            except ValueError:
                raise ConfigError(f"{cls._ENV[f.name]} must be {conv.__name__}") from None
        config = cls(**kwargs)
        for name in ("max_steps", "wall_clock_s", "budget_usd", "screenshot_max_px", "run_cap_usd"):
            value = getattr(config, name)
            if not value > 0 or value == float("inf"):  # also rejects NaN
                raise ConfigError(f"{cls._ENV[name]} must be positive")
        return config

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError("ANTHROPIC_API_KEY is not set")
        return self.api_key

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe settings for the trace. Never includes the API key."""
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "api_key"}
