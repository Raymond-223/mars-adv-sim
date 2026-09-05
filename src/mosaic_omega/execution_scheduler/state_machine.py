"""Strict task-state transitions."""

from __future__ import annotations

from .models import TaskState


class IllegalTransition(ValueError):
    pass


class StateMachine:
    _forward = {
        TaskState.CREATED: {TaskState.PLANNED},
        TaskState.PLANNED: {TaskState.READY},
        TaskState.READY: {TaskState.RUNNING},
        TaskState.RUNNING: {TaskState.VERIFYING},
        TaskState.VERIFYING: {TaskState.SUCCEEDED},
        TaskState.SUCCEEDED: set(),
        TaskState.FAILED: set(),
        TaskState.PAUSED: {TaskState.PLANNED, TaskState.READY, TaskState.RUNNING, TaskState.VERIFYING},
    }
    _pausable = {TaskState.PLANNED, TaskState.READY, TaskState.RUNNING, TaskState.VERIFYING}
    _failable = {TaskState.CREATED, TaskState.PLANNED, TaskState.READY, TaskState.RUNNING, TaskState.VERIFYING, TaskState.PAUSED}

    @classmethod
    def validate(cls, current: TaskState, target: TaskState, *, paused_from: TaskState | None = None) -> None:
        if target is TaskState.PAUSED and current in cls._pausable:
            return
        if target is TaskState.FAILED and current in cls._failable:
            return
        if current is TaskState.PAUSED and target is not paused_from:
            raise IllegalTransition(f"PAUSED task must resume to {paused_from}, not {target}")
        if target not in cls._forward[current]:
            raise IllegalTransition(f"illegal task transition: {current.value} -> {target.value}")

    @classmethod
    def transition(cls, current: TaskState, target: TaskState, *, paused_from: TaskState | None = None) -> tuple[TaskState, TaskState | None]:
        cls.validate(current, target, paused_from=paused_from)
        if target is TaskState.PAUSED:
            return target, current
        if current is TaskState.PAUSED:
            return target, None
        return target, paused_from
