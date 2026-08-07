"""Async task engine with abort/retry/ignore error handling.

Executes StateMachineTask instances step-by-step. On error, pauses and
waits for an ErrorAction (abort, retry, or ignore) before proceeding.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


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
        self._on_error: Callable[[TaskError], None] | None = None
        self._on_step_complete: Callable[[str, str], None] | None = None
        self._on_task_complete: Callable[[StateMachineTask], None] | None = None

    def set_error_handler(self, handler: Callable[[TaskError], None]) -> None:
        self._on_error = handler

    def set_step_handler(self, handler: Callable[[str, str], None]) -> None:
        self._on_step_complete = handler

    def set_completion_handler(self, handler: Callable[[StateMachineTask], None]) -> None:
        self._on_task_complete = handler

    async def execute(self, task: StateMachineTask) -> None:
        async with self._lock:
            self._current_task = task
            task.status = TaskStatus.RUNNING
            steps = task.get_steps()

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
                    if not payload.get("operator_prompt"):
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

                    action = await self._wait_for_error_action()
                    try:
                        task.on_error_action(action)
                    except Exception as hook_exc:
                        logger.error("Task '%s' error-action hook failed: %s", task.name, hook_exc)
                        task.status = TaskStatus.ABORTED
                        self._current_task = None
                        raise

                    if action == ErrorAction.ABORT:
                        task.status = TaskStatus.ABORTED
                        self._current_task = None
                        return
                    elif action == ErrorAction.RETRY:
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

    async def _wait_for_error_action(self) -> ErrorAction:
        self._error_action_event.clear()
        self._pending_action = None
        self._awaiting_error_action = True
        try:
            await self._error_action_event.wait()
            return self._pending_action or ErrorAction.ABORT
        finally:
            self._awaiting_error_action = False

    def resolve_error(self, action: ErrorAction) -> bool:
        if not self._awaiting_error_action:
            return False
        if self._pending_action is not None:
            return False
        self._pending_action = action
        self._error_action_event.set()
        return True

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
