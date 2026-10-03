# harness.contracts — exported names

Standard library only. Import as `from harness.contracts import ...`.
Records with `to_dict()` / `from_dict()` survive a JSON round trip; enums serialise as their values.

## Enums

- `Verdict(StrEnum)`: DONE="done", INFEASIBLE="infeasible", FAILED="failed" (agent's finish call)
- `TerminationReason(StrEnum)`: FINISHED, STEP_CAP, TIMEOUT, BUDGET, ERROR (values are the lowercase names; set by the harness)
- `FlowType(StrEnum)`: A..H = "a".."h", DRIFT="drift", FREEFORM="freeform"
- `OracleTier(IntEnum)`: OWN_STORAGE=1, APP_EXPORT_API=2, DOWNSTREAM_EFFECT=3, ROOT_PRIVATE_DB=4, SCRIPTED_READBACK=5, MODEL_JUDGE=6; `.is_headline -> bool` (tiers 1–3)
- `ConfirmationDecision(StrEnum)`: APPROVE="approve", REJECT="reject"

## Device (agent-facing)

- `DeviceError(Exception)`: an action or read failed
- `QueryNotAllowed(DeviceError)`: query name not on the allow-list
- `Screenshot(png: bytes, width: int, height: int, scaled_width: int, scaled_height: int)`, frozen. `png` is downscaled; `width`/`height` are real device pixels. Not serialised.
  - `.to_device(x: int, y: int) -> tuple[int, int]`: scaled to real pixels
- `Device(Protocol)`, `runtime_checkable`. tap and swipe coordinates are in **screenshot (scaled) space**; actions return None and raise `DeviceError`:
  - `screenshot() -> Screenshot`
  - `ui_tree() -> str` (trimmed text)
  - `tap(x: int, y: int) -> None`
  - `type_text(text: str) -> None`
  - `swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None`
  - `back() -> None`, `home() -> None`
  - `open_app(package: str) -> None`
  - `allowed_queries() -> list[str]`
  - `query_structured(name: str, params: dict[str, Any]) -> dict[str, Any]` (read-only, raises `QueryNotAllowed`)
  - Optional, not part of the Protocol: `settle(max_s: float) -> SettleResult` (`harness.device.adb`). The agent loop calls it, when present, after each successful UI action and before the next observation. `AdbDevice` waits until the focused window is non-null and steady; if that window is the one the previous settle ended on, the wait ends there without a dump (`focus_only`), otherwise two consecutive `ui_tree()` dumps must be identical and list the window's package when they list any (`stable`); or `max_s` elapses (`cap`; default 8 s). Devices without it (the fakes) settle instantly.

## Confirmation

- `ConfirmationRequest(action: str, summary: dict[str, Any])`, with `to_dict` / `from_dict`
- `ConfirmationHandler = Callable[[ConfirmationRequest], ConfirmationDecision]`

## Verifier

- `CheckResult(name: str, passed: bool, detail: str = "")`, with `to_dict` / `from_dict`
- `VerifierResult(passed: bool, oracle_tier: OracleTier, end_state: list[CheckResult] = [], side_effects: list[CheckResult] = [], process: list[CheckResult] = [], self_report_agrees: bool | None = None)`, with `to_dict` / `from_dict`
  - `self_report_agrees`: `(agent_verdict == expected_verdict(task)) == passed`, where the expected verdict is `INFEASIBLE` for a flow-H (infeasible goal) task and `DONE` otherwise; `None` when the agent gave no verdict. An agent that correctly reports an infeasible goal and leaves the device unchanged therefore agrees. The verdict never affects `passed`.

## Trace

- `TokenUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)`, supports `+`, with `to_dict` / `from_dict`
- `StepRecord(index: int, started_at: str, ended_at: str, tool_name: str, tool_input: dict, tool_result: dict, reasoning: str = "", screenshot_path: str | None = None, ui_tree_path: str | None = None, usage: TokenUsage = TokenUsage(), duration_ms: int = 0, meta: dict = {})`, with `to_dict` / `from_dict`. Timestamps are ISO-8601 UTC strings; paths are relative to the run folder. `meta` holds harness measurements the agent never sees; after a successful UI action it has `settle_s` (seconds spent waiting for the UI to settle before the next observation; 0.0 on devices without `settle`) and, when the device settles, `settle` (`stable`, `focus_only`, `cap` or `error`).
- `RunRecord(run_id: str, task_id: str | None, flow_type: FlowType, goal: str, model: str, started_at: str, ended_at: str | None = None, agent_verdict: Verdict | None = None, agent_summary: str | None = None, termination_reason: TerminationReason | None = None, usage: TokenUsage = TokenUsage(), estimated_cost_usd: float | None = None, verifier_result: VerifierResult | None = None, seed: int | None = None, meta: dict = {})`, with `to_dict` / `from_dict`. `agent_verdict` is None if the harness ended the run first. `verifier_result` is None for freeform runs. The runner fills in `seed` only after the run ends. `meta` holds runner bookkeeping (confirmation policy, wall time, steps); older run.json files without it load with `{}`.

## Task

- `TaskSpec(id: str, flow_type: FlowType, goal: str, oracle_tier: OracleTier, seed_spec: Any = None, checks: list[Any] = [], sensitive_actions: list[str] = [])`. Agent-side code receives `goal` only. `seed_spec` and `checks` may hold callables, so a `TaskSpec` is not serialised.

## Config

- `ConfigError(Exception)`
- `load_env_file(path) -> dict[str, str]`: parses KEY=VALUE lines and skips comments. Handles an `export` prefix and quotes. A missing file returns {}. Never modifies `os.environ`.
- `Config(model="claude-opus-5-5", max_steps=40, wall_clock_s=600.0, budget_usd=2.0, screenshot_max_px=1280, runs_dir="runs", api_key=None)`. `api_key` is excluded from repr.
  - `Config.from_env(env_file=".env", environ=None) -> Config`: precedence is defaults < env_file < environ (`os.environ` when None). Variables: `HARNESS_MODEL`, `HARNESS_MAX_STEPS`, `HARNESS_WALL_CLOCK_S`, `HARNESS_BUDGET_USD`, `HARNESS_SCREENSHOT_MAX_PX`, `HARNESS_RUNS_DIR`, `HARNESS_RUN_CAP_USD` (per-run model spend cap, default 1.50; used by `run_agent` when no explicit `AgentSettings` is passed), `ANTHROPIC_API_KEY`. Raises `ConfigError` on non-numeric, non-positive or non-finite values.
  - `.require_api_key() -> str`: raises `ConfigError` if unset
  - `.to_dict() -> dict`: never includes the key
