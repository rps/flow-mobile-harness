"""Goal-driven tool-use loop over the Messages API.

Each turn the model sees the goal, the conversation so far, and the newest
screenshot and UI tree, and answers with one tool call. Only the latest
`keep_screenshots` images stay in the history; older ones become a text
placeholder. Because that edits earlier turns, thinking blocks are not kept
in the history (an edited prefix would invalidate them); text and tool_use
blocks are.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import anthropic

from harness.agent import prompts
from harness.agent.pricing import Ledger, estimate_cost, model_price
from harness.agent.tools import build_tools, execute, validate_input
from harness.contracts import (
    Config,
    ConfirmationDecision,
    ConfirmationHandler,
    ConfirmationRequest,
    Device,
    DeviceError,
    QueryNotAllowed,
    Screenshot,
    StepRecord,
    TerminationReason,
    TokenUsage,
    Verdict,
    _plain,
)

log = logging.getLogger(__name__)

OnStep = Callable[[StepRecord, bytes | None, str | None], None]

REASONING_MAX_CHARS = 2000
_EPHEMERAL = {"type": "ephemeral"}
# Tools that can change the screen; after a successful one the device settles.
UI_ACTIONS = frozenset({"tap", "type_text", "swipe", "back", "home", "open_app"})


@dataclass(frozen=True)
class AgentSettings:
    """Loop knobs that are not part of Config."""

    keep_screenshots: int = 3
    per_run_cap_usd: float = 1.50
    run_reserve_usd: float | None = None  # None: use per_run_cap_usd
    effort: str = "medium"
    max_tokens: int = 16000
    api_retries: int = 2
    device_retries: int = 1  # for screenshot/ui_tree reads; actions are not retried
    max_consecutive_device_errors: int = 3
    max_confirm_rerequests: int = 2
    no_tool_call_retries: int = 2
    allow_unpriced: bool = False
    settle_max_s: float = 8.0  # cap on waiting for the UI to settle after an action

    @property
    def reserve_usd(self) -> float:
        return self.per_run_cap_usd if self.run_reserve_usd is None else self.run_reserve_usd


@dataclass(frozen=True)
class AgentOutcome:
    verdict: Verdict | None
    summary: str
    termination_reason: TerminationReason
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float | None = None
    steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


class _Stop(Exception):
    def __init__(self, reason: TerminationReason, summary: str, verdict: Verdict | None = None):
        super().__init__(summary)
        self.reason = reason
        self.summary = summary
        self.verdict = verdict


@dataclass
class _Observation:
    screenshot: Screenshot
    ui_tree: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _norm_action(action: str) -> str:
    return " ".join(action.casefold().split())


def _usage_of(response: Any) -> TokenUsage:
    u = response.usage
    return TokenUsage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )


def _reasoning_of(content: list[Any]) -> str:
    parts = []
    for block in content:
        if block.type == "thinking" and block.thinking:
            parts.append(block.thinking)
        elif block.type == "text" and block.text:
            parts.append(block.text)
    text = "\n".join(parts)
    return text if len(text) <= REASONING_MAX_CHARS else text[: REASONING_MAX_CHARS - 1] + "…"


def _history_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Assistant content to replay: text and tool_use only, no thinking."""
    out: list[dict[str, Any]] = []
    for block in content:
        if block.type == "text" and block.text:
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return out


class _Run:
    def __init__(
        self,
        goal: str,
        device: Device,
        config: Config,
        confirm: ConfirmationHandler,
        on_step: OnStep,
        client: Any,
        settings: AgentSettings,
        clock: Callable[[], float],
    ):
        self.goal = goal
        self.device = device
        self.config = config
        self.confirm = confirm
        self.on_step = on_step
        self.client = client
        self.s = settings
        self.clock = clock
        self.started = clock()
        self.ledger = Ledger(Path(config.runs_dir) / "ledger.json")
        self.priced = model_price(config.model) is not None
        self.usage = TokenUsage()
        self.cost = 0.0
        self.steps = 0
        self.messages: list[dict[str, Any]] = []
        # (container list, index in it, message index, step) per image still in history
        self.images: list[tuple[list[dict[str, Any]], int, int, int]] = []
        self.rejected: dict[str, int] = {}  # normalised action -> re-requests since reject
        self.device_errors = 0  # consecutive DeviceErrors from tool calls
        self.tools = build_tools(self.query_names())

    def query_names(self) -> list[str] | None:
        try:
            return list(self.device.allowed_queries())
        except Exception as e:  # fall back to the static list in tools.py
            log.warning("allowed_queries() failed, using default query names: %s", e)
            return None

    # --- guards ---

    def check_budget_at_start(self) -> None:
        if not self.priced and not self.s.allow_unpriced:
            raise _Stop(
                TerminationReason.BUDGET,
                f"model {self.config.model!r} has no price entry; budget guard cannot run",
            )
        spent = self.ledger.total()
        if spent + self.s.reserve_usd > self.config.budget_usd:
            raise _Stop(
                TerminationReason.BUDGET,
                f"budget refused: spent ${spent:.4f} + reserve ${self.s.reserve_usd:.4f} "
                f"exceeds ${self.config.budget_usd:.4f}",
            )

    def remaining_s(self) -> float:
        return self.config.wall_clock_s - (self.clock() - self.started)

    def check_time(self) -> None:
        if self.remaining_s() <= 0:
            raise _Stop(TerminationReason.TIMEOUT, f"wall-clock cap of {self.config.wall_clock_s}s reached")

    # --- device ---

    def with_retries(self, what: str, fn: Callable[[], Any]) -> Any:
        attempts = 1 + self.s.device_retries
        for attempt in range(attempts):
            try:
                return fn()
            except QueryNotAllowed:
                raise
            except DeviceError as e:
                log.warning("%s failed (attempt %d/%d): %s", what, attempt + 1, attempts, e)
                if attempt + 1 == attempts:
                    raise _Stop(TerminationReason.ERROR, f"device error during {what}: {e}") from e

    def settle(self) -> dict[str, Any]:
        """Wait for the UI to settle after an action, if the device can.
        Returns step meta: settle_s, plus the device's reason when it settles."""
        fn = getattr(self.device, "settle", None)
        if fn is None:
            return {"settle_s": 0.0}
        t0 = time.monotonic()
        try:
            result = fn(max_s=max(0.0, min(self.s.settle_max_s, self.remaining_s())))
        except DeviceError as e:  # observing anyway beats ending the run
            log.warning("settle failed: %s", e)
            result = None
            reason: str | None = "error"
        else:
            if result is None:  # a wrapper over a device without settle
                return {"settle_s": 0.0}
            reason = getattr(result, "reason", None)
        meta: dict[str, Any] = {"settle_s": round(time.monotonic() - t0, 3)}
        if reason is not None:
            meta["settle"] = reason
        return meta

    def observe(self) -> _Observation:
        self.check_time()
        shot = self.with_retries("screenshot", self.device.screenshot)
        tree = self.with_retries("ui_tree", self.device.ui_tree)
        return _Observation(shot, tree)

    # --- history ---

    def observation_blocks(self, obs: _Observation, msg_index: int, container: list[dict[str, Any]]) -> None:
        image = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(obs.screenshot.png).decode("ascii"),
            },
        }
        container.append(image)
        self.images.append((container, len(container) - 1, msg_index, self.steps))
        container.append({"type": "text", "text": prompts.UI_TREE_TEMPLATE.format(ui_tree=obs.ui_tree)})

    def prune_images(self) -> None:
        while len(self.images) > self.s.keep_screenshots:
            container, idx, _, step = self.images.pop(0)
            container[idx] = {"type": "text", "text": prompts.SCREENSHOT_OMITTED.format(step=step)}

    def request_messages(self) -> list[dict[str, Any]]:
        """Messages with a cache breakpoint on the newest message that lies
        wholly before the image window (that prefix no longer changes)."""
        msgs = list(self.messages)
        if not self.images:
            return msgs
        stable = self.images[0][2] - 1
        if stable < 0 or stable >= len(msgs) or not msgs[stable]["content"]:
            return msgs
        content = list(msgs[stable]["content"])
        content[-1] = {**content[-1], "cache_control": _EPHEMERAL}
        msgs[stable] = {**msgs[stable], "content": content}
        return msgs

    # --- model ---

    def call_model(self) -> Any:
        self.check_time()
        self.prune_images()
        try:
            return self.client.messages.create(
                model=self.config.model,
                max_tokens=self.s.max_tokens,
                system=[{"type": "text", "text": prompts.SYSTEM_PROMPT, "cache_control": _EPHEMERAL}],
                tools=self.tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.s.effort},
                messages=self.request_messages(),
                timeout=max(1.0, self.remaining_s()),
            )
        except anthropic.APIError as e:
            rid = getattr(e, "request_id", None)
            raise _Stop(TerminationReason.ERROR, f"API error ({type(e).__name__}, request_id={rid}): {e}") from e

    def account(self, response: Any) -> TokenUsage:
        u = _usage_of(response)
        self.usage = self.usage + u
        cost = estimate_cost(self.config.model, u) if self.priced else None
        if cost is not None:
            self.cost += cost
            self.ledger.add(cost, run_label=self.goal[:60])
        log.info(
            "model call: in=%d out=%d cache_read=%d cache_write=%d cost=%s run_total=%s",
            u.input_tokens, u.output_tokens, u.cache_read_input_tokens,
            u.cache_creation_input_tokens, cost, self.cost if self.priced else None,
        )
        return u

    # --- main loop ---

    def run(self) -> AgentOutcome:
        try:
            self.check_budget_at_start()
            obs = self.observe()
            first: list[dict[str, Any]] = [{"type": "text", "text": prompts.GOAL_TEMPLATE.format(goal=self.goal)}]
            self.messages.append({"role": "user", "content": first})
            self.observation_blocks(obs, 0, first)
            self.loop(obs)
        except _Stop as stop:
            return self.outcome(stop.reason, stop.summary, stop.verdict)

    def outcome(self, reason: TerminationReason, summary: str, verdict: Verdict | None) -> AgentOutcome:
        return AgentOutcome(
            verdict=verdict,
            summary=summary,
            termination_reason=reason,
            usage=self.usage,
            estimated_cost_usd=round(self.cost, 6) if self.priced else None,
            steps=self.steps,
        )

    def loop(self, obs: _Observation) -> NoReturn:
        """Runs until a _Stop is raised."""
        nudges = 0
        while True:
            if self.steps >= self.config.max_steps:
                raise _Stop(TerminationReason.STEP_CAP, f"step cap of {self.config.max_steps} reached")
            started_at, t0 = _now(), time.monotonic()
            step_usage = TokenUsage()
            while True:
                response = self.call_model()
                step_usage = step_usage + self.account(response)
                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    category = getattr(details, "category", None)
                    raise _Stop(TerminationReason.ERROR, f"model refused (category={category})")
                history = _history_blocks(response.content)
                if history:
                    self.messages.append({"role": "assistant", "content": history})
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if tool_uses:
                    break
                nudges += 1
                if nudges > self.s.no_tool_call_retries:
                    raise _Stop(TerminationReason.ERROR, "model gave no tool call after nudges")
                self.messages.append({"role": "user", "content": [{"type": "text", "text": prompts.NUDGE}]})
            nudges = 0
            obs = self.step(response, tool_uses, obs, started_at, t0, step_usage)

    def step(
        self,
        response: Any,
        tool_uses: list[Any],
        obs: _Observation,
        started_at: str,
        t0: float,
        step_usage: TokenUsage,
    ) -> _Observation:
        call = tool_uses[0]
        name, tool_input = call.name, call.input
        over_cap = self.priced and self.cost >= self.s.per_run_cap_usd
        if over_cap and name != "finish":
            raise _Stop(
                TerminationReason.BUDGET,
                f"per-run cap ${self.s.per_run_cap_usd:.4f} reached (run cost ${self.cost:.4f})",
            )

        result: dict[str, Any]
        model_extra: list[str] = []
        meta: dict[str, Any] = {}
        is_error = False
        finish: tuple[Verdict, str] | None = None

        error = validate_input(name, tool_input)
        if error is not None:
            result, is_error = {"error": error}, True
        elif name == "finish":
            finish = (Verdict(tool_input["verdict"]), tool_input["summary"])
            result = {"ok": True}
        elif name == "request_confirmation":
            result = self.ask(tool_input)
            if result["decision"] == ConfirmationDecision.REJECT.value:
                model_extra.append(prompts.REJECT_NOTE)
        else:
            self.check_time()
            try:
                result = execute(self.device, name, tool_input)
                self.device_errors = 0
                if name in UI_ACTIONS:
                    meta = self.settle()
            except QueryNotAllowed as e:
                result = {"error": str(e), "allowed_queries": self.device.allowed_queries()}
                is_error = True
            except DeviceError as e:
                # Bad parameters and transport failures look alike; report it
                # to the model and stop only after repeated failures.
                self.device_errors += 1
                log.warning("%s failed (%d in a row): %s", name, self.device_errors, e)
                result = {"error": f"{name} failed: {e}"}
                is_error = True

        self.steps += 1
        record = StepRecord(
            index=self.steps - 1,
            started_at=started_at,
            ended_at=_now(),
            tool_name=name,
            tool_input=dict(tool_input) if isinstance(tool_input, dict) else {"raw": tool_input},
            tool_result=result,
            reasoning=_reasoning_of(response.content),
            usage=step_usage,
            duration_ms=int((time.monotonic() - t0) * 1000),
            meta=meta,
        )
        try:
            self.on_step(record, obs.screenshot.png, obs.ui_tree)
        except Exception as e:  # the trace sink failing must stop the run
            raise _Stop(TerminationReason.ERROR, f"on_step failed: {e}") from e

        if finish is not None:
            raise _Stop(TerminationReason.FINISHED, finish[1], finish[0])
        if self.device_errors >= self.s.max_consecutive_device_errors:
            raise _Stop(
                TerminationReason.ERROR,
                f"{self.device_errors} device errors in a row; last: {result['error']}",
            )

        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(result)}]
        content += [{"type": "text", "text": t} for t in model_extra]
        results = [{"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error}]
        for extra in tool_uses[1:]:
            results.append(
                {"type": "tool_result", "tool_use_id": extra.id, "content": prompts.ONE_ACTION_ONLY, "is_error": True}
            )
        self.messages.append({"role": "user", "content": results})
        if is_error:
            # The API requires text-only content in an is_error tool_result,
            # so no new screenshot; the model keeps the previous one.
            content.append({"type": "text", "text": prompts.SCREEN_UNCHANGED})
            return obs
        new_obs = self.observe()
        self.observation_blocks(new_obs, len(self.messages) - 1, content)
        return new_obs

    def ask(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        key = _norm_action(tool_input["action"])
        if key in self.rejected:
            self.rejected[key] += 1
            if self.rejected[key] > self.s.max_confirm_rerequests:
                raise _Stop(
                    TerminationReason.FINISHED,
                    f"harness stopped the run: confirmation for {tool_input['action']!r} was rejected "
                    f"and requested again {self.rejected[key]} times",
                    Verdict.FAILED,
                )
        try:
            decision = ConfirmationDecision(
                self.confirm(ConfirmationRequest(action=tool_input["action"], summary=dict(tool_input["summary"])))
            )
        except Exception as e:
            raise _Stop(TerminationReason.ERROR, f"confirmation handler failed: {e}") from e
        if decision is ConfirmationDecision.REJECT:
            self.rejected.setdefault(key, 0)
        return {"decision": decision.value}


def run_agent(
    goal: str,
    device: Device,
    config: Config,
    confirm: ConfirmationHandler,
    on_step: OnStep,
    model_client: Any = None,
    *,
    settings: AgentSettings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgentOutcome:
    """Drive `device` toward `goal`. Each call is a fresh conversation."""
    settings = settings or AgentSettings()
    if model_client is None:
        model_client = anthropic.Anthropic(api_key=config.require_api_key(), max_retries=settings.api_retries)
    return _Run(goal, device, config, confirm, on_step, model_client, settings, clock).run()
