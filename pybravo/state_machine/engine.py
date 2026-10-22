"""Async task engine with abort/retry/ignore error handling.

Executes StateMachineTask instances step-by-step. On error, pauses and
waits for an ErrorAction (abort, retry, or ignore) before proceeding.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

# A step stopped by the safety interlock may be retried at most this many
# times per task, and only after the operator has pressed Recover.
SAFETY_STOP_MAX_RETRIES = 2

SAFETY_STOP_HELP = (
    "Safety stop: the light curtain was crossed or the E-stop was pressed, and "
    "the instrument disabled the axes. Clear the light curtain / release the "
    "E-stop, then press Recover."
)

# Collects the tasks the engine ends as ABORTED while it is set (see
# ``watch_aborted_tasks``). A context variable, so a caller only sees the tasks
# its own call chain ran, not those of a concurrent caller.
_aborted_sink: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "pybravo_aborted_sink", default=None,
)


@contextlib.contextmanager
def watch_aborted_tasks() -> Iterator[list]:
    """Collect the tasks that end ABORTED inside this block.

    ``StateMachineEngine.execute`` returns normally for an aborted task, and
    the Bravo facade methods then mostly return None, so a caller that runs a
    sequence of operations (the workflow executor) uses this to tell an
    aborted operation from a completed one.
    """
    sink: list = []
    token = _aborted_sink.set(sink)
    try:
        yield sink
    finally:
        _aborted_sink.reset(token)


def is_safety_stop(exc: BaseException | None) -> bool:
    """True when ``exc`` (or anything in its cause chain) is a safety stop.

    A light-curtain trip or E-stop latches the safety interlock and the
    controller disables the axes (Darwin broadcasts STOP_DISABLE, mapped to
    ``ErrorType.ROBOT_DISABLE``). Nothing after such a step may assume the
    motion happened.
    """
    from pybravo.protocol.errors import BravoError, ErrorType

    safety_types = {ErrorType.ROBOT_DISABLE, ErrorType.ROBOT_DISABLE_BUTTON}
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, BravoError) and exc.error_type in safety_types:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class TaskStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    ABORTED = auto()
    PAUSED = auto()


class ErrorAction(Enum):
    ABORT = auto()
    RETRY = auto()
    IGNORE = auto()


@dataclass
class TaskError:
    message: str
    step_name: str
    original_exception: Exception | None = None


class StateMachineTask(ABC):
    """Base class for all Bravo operation tasks.

    Each task defines a sequence of async steps. The engine executes them
    in order. If a step raises an exception, the engine pauses and waits
    for an ErrorAction (abort, retry, or ignore).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = TaskStatus.PENDING
        self._current_step_index = 0
        self._current_step_name: str | None = None
        self.error: TaskError | None = None
        # Universal operator-prompt slot. Step handlers can set this to a
        # dict like {kind, title, message, choices: [retry, ignore, abort]}
        # before raising, to show a task-specific modal. If left None, the
        # engine will synthesize a generic step_failed prompt so every
        # failure is recoverable.
        self._operator_prompt: dict[str, Any] | None = None

    def status_payload(self) -> dict:
        # Default behavior: surface the operator_prompt (task-specific or
        # engine-synthesized) when the task is in a failed state. Subclasses
        # that compose richer payloads should call super().status_payload()
        # and merge.
        if self.status == TaskStatus.FAILED and self._operator_prompt:
            return {"operator_prompt": dict(self._operator_prompt)}
        return {}

    def on_error_action(self, action: ErrorAction) -> None:
        """Optional hook invoked when the operator chooses retry/ignore/abort.

        Base implementation clears the operator prompt so a subsequent
        distinct failure can populate its own. Subclasses should call
        super().on_error_action(action) before their custom logic.
        """
        self._operator_prompt = None

    @abstractmethod
    def get_steps(self) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
        """Return ordered list of (step_name, async_callable) pairs."""
        ...


class StateMachineEngine:
    """Async engine that executes StateMachineTask instances.

    Runs tasks step-by-step. On error, fires an error callback and waits
    for an ErrorAction before proceeding.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current_task: StateMachineTask | None = None
        self._error_action_event = asyncio.Event()
        self._pending_action: ErrorAction | None = None
        self._awaiting_error_action = False
        # What the pending error prompt allows. A safety stop never allows
        # Ignore, and allows Retry only after Recover and within the limit.
        self._ignore_allowed = True
        self._retry_allowed = True
        self._awaiting_safety_stop = False
        self._recovered_since_safety_stop = False
        self.last_refusal: str | None = None
        self._on_error: Callable[[TaskError], None] | None = None
        self._on_step_complete: Callable[[str, str], None] | None = None
        self._on_task_complete: Callable[[StateMachineTask], None] | None = None

    def set_error_handler(self, handler: Callable[[TaskError], None]) -> None:
        self._on_error = handler

    def set_step_handler(self, handler: Callable[[str, str], None]) -> None:
        self._on_step_complete = handler

    def set_completion_handler(self, handler: Callable[[StateMachineTask], None]) -> None:
        self._on_task_complete = handler

    def get_handlers(self) -> tuple[Any, Any, Any]:
        """The (error, step, completion) handlers, for a caller that swaps them temporarily."""
        return self._on_error, self._on_step_complete, self._on_task_complete

    async def execute(self, task: StateMachineTask) -> None:
        async with self._lock:
            self._current_task = task
            task.status = TaskStatus.RUNNING
            steps = task.get_steps()
            safety_retries = 0

            while task._current_step_index < len(steps):
                step_name, step_fn = steps[task._current_step_index]
                task._current_step_name = step_name
                try:
                    await step_fn()
                    if self._on_step_complete:
                        self._on_step_complete(task.name, step_name)
                    task._current_step_index += 1
                except Exception as exc:
                    task.error = TaskError(
                        message=str(exc),
                        step_name=step_name,
                        original_exception=exc,
                    )
                    task.status = TaskStatus.FAILED
                    logger.error(
                        "Task '%s' step '%s' failed: %s",
                        task.name, step_name, exc,
                    )

                    if self._on_error:
                        self._on_error(task.error)
                    else:
                        self._current_task = None
                        raise

                    try:
                        payload = task.status_payload() or {}
                    except Exception:
                        payload = {}
                    safety_stop = is_safety_stop(exc)
                    retry_left = SAFETY_STOP_MAX_RETRIES - safety_retries
                    if safety_stop:
                        # Always the safety-stop prompt, whatever the task set:
                        # a task prompt may offer Ignore, and skipping a step
                        # the interlock interrupted is never safe.
                        fallback_step = step_name or "<unknown step>"
                        logger.error(
                            "Task '%s' step '%s' was stopped by the safety interlock "
                            "(light curtain / E-stop); waiting for Recover, then Retry or Abort",
                            task.name, fallback_step,
                        )
                        if retry_left > 0:
                            choice_text = (
                                "Then Retry re-runs the step (only after Recover; "
                                f"{retry_left} retr{'y' if retry_left == 1 else 'ies'} left), "
                                "or Abort stops the task and the workflow. After Abort, "
                                "retract Z and Home All before running again."
                            )
                        else:
                            choice_text = (
                                "The retry limit for this task is used up: Abort, then "
                                "retract Z and Home All before running again."
                            )
                        task._operator_prompt = {
                            "kind": "safety_stop",
                            "title": f"{task.name} stopped by the safety interlock",
                            "message": (
                                f"{SAFETY_STOP_HELP}\n{choice_text}\n\n"
                                f"Step '{fallback_step}' raised:\n{exc!s}"
                            ),
                            "choices": ["recover", "retry", "abort"] if retry_left > 0 else ["recover", "abort"],
                            "step": fallback_step,
                        }
                    elif not payload.get("operator_prompt"):
                        # No task-specific prompt was set. Synthesize a
                        # generic Retry/Ignore/Abort prompt so every state
                        # machine failure is recoverable from the UI.
                        fallback_step = step_name or "<unknown step>"
                        task._operator_prompt = {
                            "kind": "step_failed",
                            "title": f"{task.name} failed",
                            "message": (
                                f"Step '{fallback_step}' raised:\n{exc!s}\n\n"
                                "Retry re-runs the same step.\n"
                                "Ignore skips this step and continues.\n"
                                "Abort stops the workflow."
                            ),
                            "choices": ["retry", "ignore", "abort"],
                            "step": fallback_step,
                        }

                    action = await self._wait_for_error_action(
                        safety_stop=safety_stop,
                        allow_retry=(not safety_stop) or retry_left > 0,
                    )
                    try:
                        task.on_error_action(action)
                    except Exception as hook_exc:
                        logger.error("Task '%s' error-action hook failed: %s", task.name, hook_exc)
                        self._mark_aborted(task)
                        raise

                    if action == ErrorAction.ABORT:
                        self._mark_aborted(task)
                        return
                    elif action == ErrorAction.RETRY:
                        if safety_stop:
                            safety_retries += 1
                        task.status = TaskStatus.RUNNING
                        continue
                    elif action == ErrorAction.IGNORE:
                        task.status = TaskStatus.RUNNING
                        task._current_step_index += 1
                        continue

            task._current_step_name = None
            task.status = TaskStatus.COMPLETED
            if self._on_task_complete:
                self._on_task_complete(task)
            self._current_task = None

    def _mark_aborted(self, task: StateMachineTask) -> None:
        task.status = TaskStatus.ABORTED
        self._current_task = None
        sink = _aborted_sink.get()
        if sink is not None:
            sink.append(task)

    async def _wait_for_error_action(
        self, *, safety_stop: bool = False, allow_retry: bool = True,
    ) -> ErrorAction:
        self._error_action_event.clear()
        self._pending_action = None
        self._ignore_allowed = not safety_stop
        self._retry_allowed = allow_retry
        self._awaiting_safety_stop = safety_stop
        if safety_stop:
            # Every safety stop needs its own Recover before a Retry.
            self._recovered_since_safety_stop = False
        self._awaiting_error_action = True
        try:
            await self._error_action_event.wait()
            return self._pending_action or ErrorAction.ABORT
        finally:
            self._awaiting_error_action = False
            self._awaiting_safety_stop = False
            self._ignore_allowed = True
            self._retry_allowed = True

    def resolve_error(self, action: ErrorAction) -> bool:
        self.last_refusal = None
        if not self._awaiting_error_action:
            return False
        if self._pending_action is not None:
            return False
        refusal = self._refusal(action)
        if refusal is not None:
            self.last_refusal = refusal
            logger.warning("%s refused: %s", action.name.capitalize(), refusal)
            return False
        self._pending_action = action
        self._error_action_event.set()
        return True

    def _refusal(self, action: ErrorAction) -> str | None:
        if action == ErrorAction.IGNORE and not self._ignore_allowed:
            # Skipping a step the safety interlock interrupted would let the
            # next steps assume a motion that never finished.
            return "a step stopped by the safety interlock cannot be ignored; Recover, then Retry or Abort"
        if action == ErrorAction.RETRY:
            if not self._retry_allowed:
                return "the retry limit after a safety stop is used up; Abort, then retract Z and Home All"
            if self._awaiting_safety_stop and not self._recovered_since_safety_stop:
                return "press Recover first: the axes are still disabled after the safety stop"
        return None

    def note_recovered(self) -> None:
        """Record that the controller was recovered after a safety stop, so Retry is allowed."""
        if self._awaiting_safety_stop:
            self._recovered_since_safety_stop = True

    @property
    def awaiting_safety_stop(self) -> bool:
        """True while a task waits on the operator after a safety stop."""
        return self._awaiting_error_action and self._awaiting_safety_stop

    def abort(self) -> bool:
        return self.resolve_error(ErrorAction.ABORT)

    def retry(self) -> bool:
        return self.resolve_error(ErrorAction.RETRY)

    def ignore(self) -> bool:
        return self.resolve_error(ErrorAction.IGNORE)

    @property
    def current_task(self) -> StateMachineTask | None:
        return self._current_task

    @property
    def is_busy(self) -> bool:
        return self._current_task is not None

    @property
    def awaiting_error_action(self) -> bool:
        return self._awaiting_error_action
