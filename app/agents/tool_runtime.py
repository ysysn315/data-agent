import asyncio
import inspect
import json
import time
from dataclasses import dataclass, replace
from threading import Lock
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ToolExecutionPolicy:
    retry_attempts: int = 1
    timeout_seconds: float = 10.0
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 60.0
    backoff_seconds: float = 0.2
    enabled: bool = True


@dataclass
class CircuitState:
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: Optional[float] = None


@dataclass
class ToolExecutionResult:
    tool_name: str
    status: str
    content: str
    attempts: int
    error: Optional[str] = None
    degraded: bool = False
    circuit_state: str = "closed"

    @property
    def success(self) -> bool:
        return self.status == "success"


DEFAULT_POLICY = ToolExecutionPolicy()

TOOL_POLICIES: Dict[str, ToolExecutionPolicy] = {
    "query_internal_docs": replace(
        DEFAULT_POLICY,
        retry_attempts=1,
        timeout_seconds=15.0,
        failure_threshold=3,
        recovery_timeout_seconds=60.0,
    ),
    "tavily_search": replace(
        DEFAULT_POLICY,
        retry_attempts=1,
        timeout_seconds=12.0,
        failure_threshold=3,
        recovery_timeout_seconds=60.0,
    ),
    "query_log": replace(
        DEFAULT_POLICY,
        retry_attempts=1,
        timeout_seconds=10.0,
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
    ),
    "query_prometheus_alerts": replace(
        DEFAULT_POLICY,
        retry_attempts=1,
        timeout_seconds=8.0,
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
    ),
    "get_current_datetime": replace(
        DEFAULT_POLICY,
        retry_attempts=0,
        timeout_seconds=2.0,
        failure_threshold=5,
        recovery_timeout_seconds=10.0,
    ),
}

TOOL_FALLBACK_MESSAGES: Dict[str, str] = {
    "query_internal_docs": (
        "Internal docs lookup is temporarily unavailable. Continue with existing context "
        "and state that document retrieval did not succeed."
    ),
    "tavily_search": (
        "Web search is temporarily unavailable. Continue without live web results and "
        "state that online lookup did not succeed."
    ),
    "query_log": (
        "Log retrieval is temporarily unavailable. Try another tool or continue with the "
        "current evidence."
    ),
    "query_prometheus_alerts": (
        "Alert retrieval is temporarily unavailable. Try another tool or continue with the "
        "current evidence."
    ),
    "default": "The requested tool is temporarily unavailable. Continue with the current context.",
}

_CIRCUIT_STATES: Dict[str, CircuitState] = {}
_STATE_LOCK = Lock()


def get_tool_policy(tool_name: str) -> ToolExecutionPolicy:
    return TOOL_POLICIES.get(tool_name, DEFAULT_POLICY)


def override_tool_policy(tool_name: str, **kwargs: Any) -> ToolExecutionPolicy:
    policy = replace(get_tool_policy(tool_name), **kwargs)
    TOOL_POLICIES[tool_name] = policy
    return policy


def reset_tool_runtime_state() -> None:
    with _STATE_LOCK:
        _CIRCUIT_STATES.clear()


def get_circuit_snapshot(tool_name: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    with _STATE_LOCK:
        if tool_name is not None:
            state = _CIRCUIT_STATES.get(tool_name, CircuitState())
            return {
                tool_name: {
                    "state": state.state,
                    "consecutive_failures": state.consecutive_failures,
                    "opened_at": state.opened_at,
                }
            }

        return {
            name: {
                "state": state.state,
                "consecutive_failures": state.consecutive_failures,
                "opened_at": state.opened_at,
            }
            for name, state in _CIRCUIT_STATES.items()
        }


def _get_or_create_circuit(tool_name: str) -> CircuitState:
    with _STATE_LOCK:
        state = _CIRCUIT_STATES.get(tool_name)
        if state is None:
            state = CircuitState()
            _CIRCUIT_STATES[tool_name] = state
        return state


def _allow_execution(tool_name: str, policy: ToolExecutionPolicy) -> Optional[CircuitState]:
    state = _get_or_create_circuit(tool_name)
    now = time.monotonic()

    with _STATE_LOCK:
        if state.state != "open":
            return None

        if state.opened_at is None:
            state.opened_at = now

        if now - state.opened_at >= policy.recovery_timeout_seconds:
            state.state = "half_open"
            return None

        return CircuitState(
            state=state.state,
            consecutive_failures=state.consecutive_failures,
            opened_at=state.opened_at,
        )


def _mark_success(tool_name: str) -> CircuitState:
    state = _get_or_create_circuit(tool_name)
    with _STATE_LOCK:
        state.state = "closed"
        state.consecutive_failures = 0
        state.opened_at = None
        return CircuitState(
            state=state.state,
            consecutive_failures=state.consecutive_failures,
            opened_at=state.opened_at,
        )


def _mark_failure(tool_name: str, policy: ToolExecutionPolicy) -> CircuitState:
    state = _get_or_create_circuit(tool_name)
    now = time.monotonic()

    with _STATE_LOCK:
        state.consecutive_failures += 1
        if state.state == "half_open" or state.consecutive_failures >= policy.failure_threshold:
            state.state = "open"
            state.opened_at = now
        return CircuitState(
            state=state.state,
            consecutive_failures=state.consecutive_failures,
            opened_at=state.opened_at,
        )


def _fallback_message(tool_name: str, reason: Optional[str] = None) -> str:
    base = TOOL_FALLBACK_MESSAGES.get(tool_name, TOOL_FALLBACK_MESSAGES["default"])
    if not reason:
        return base
    return f"{base} Reason: {reason}"


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True

    name = exc.__class__.__name__
    if name in {
        "ReadTimeout",
        "PoolTimeout",
        "ConnectTimeout",
        "ConnectError",
        "HTTPStatusError",
        "RemoteProtocolError",
    }:
        return True

    message = str(exc).lower()
    retryable_markers = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "503",
        "502",
        "504",
        "429",
    )
    return any(marker in message for marker in retryable_markers)


def _extract_failure_reason(tool_name: str, result: Any) -> Optional[str]:
    if isinstance(result, dict):
        if result.get("success") is False:
            return str(result.get("error") or result.get("message") or "tool returned success=false")
        return None

    if not isinstance(result, str):
        return None

    text = result.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        lower = text.lower()
        if tool_name == "query_internal_docs" and (
            "temporarily unavailable" in lower or "not yet initialized" in lower
        ):
            return text
        if "not configured" in lower:
            return text
        return None

    if isinstance(payload, dict) and payload.get("success") is False:
        return str(payload.get("error") or payload.get("message") or "tool returned success=false")

    return None


async def _invoke_tool(tool: Any, tool_args: Optional[Dict[str, Any]], timeout_seconds: float) -> Any:
    args = tool_args or {}

    if hasattr(tool, "ainvoke"):
        return await asyncio.wait_for(tool.ainvoke(args), timeout=timeout_seconds)

    if callable(tool):
        maybe_result = tool(**args) if isinstance(args, dict) else tool(args)
        if inspect.isawaitable(maybe_result):
            return await asyncio.wait_for(maybe_result, timeout=timeout_seconds)
        return maybe_result

    raise TypeError("tool must provide ainvoke() or be callable")


async def safe_tool_execute(
    tool_name: str,
    tool: Any,
    tool_args: Optional[Dict[str, Any]] = None,
    *,
    policy: Optional[ToolExecutionPolicy] = None,
) -> ToolExecutionResult:
    resolved_policy = policy or get_tool_policy(tool_name)

    if not resolved_policy.enabled:
        content = str(await _invoke_tool(tool, tool_args, resolved_policy.timeout_seconds))
        return ToolExecutionResult(
            tool_name=tool_name,
            status="success",
            content=content,
            attempts=1,
            circuit_state="closed",
        )

    blocked_state = _allow_execution(tool_name, resolved_policy)
    if blocked_state is not None:
        return ToolExecutionResult(
            tool_name=tool_name,
            status="circuit_open",
            content=_fallback_message(tool_name, "circuit is open"),
            attempts=0,
            error="circuit is open",
            degraded=True,
            circuit_state=blocked_state.state,
        )

    last_error: Optional[str] = None
    total_attempts = max(1, resolved_policy.retry_attempts + 1)

    for attempt in range(1, total_attempts + 1):
        try:
            raw_result = await _invoke_tool(tool, tool_args, resolved_policy.timeout_seconds)
            failure_reason = _extract_failure_reason(tool_name, raw_result)
            if failure_reason is None:
                state = _mark_success(tool_name)
                return ToolExecutionResult(
                    tool_name=tool_name,
                    status="success",
                    content=str(raw_result),
                    attempts=attempt,
                    circuit_state=state.state,
                )
            last_error = failure_reason
            should_retry = attempt < total_attempts
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            should_retry = attempt < total_attempts and _is_retryable_exception(exc)

        if should_retry:
            await asyncio.sleep(resolved_policy.backoff_seconds * attempt)
            continue

        break

    state = _mark_failure(tool_name, resolved_policy)
    status = "circuit_open" if state.state == "open" else "degraded"
    return ToolExecutionResult(
        tool_name=tool_name,
        status=status,
        content=_fallback_message(tool_name, last_error),
        attempts=total_attempts,
        error=last_error,
        degraded=True,
        circuit_state=state.state,
    )
