"""Switching a scheduled task to Event or Webhook must cancel its old timer (#6588).

``TaskScheduler._check_due_tasks`` dispatches every active task whose
``next_run`` is due, whatever its trigger type (deferred event runs use
``next_run`` too). ``PUT /api/tasks/{id}`` changed ``trigger_type`` but left the
old ``next_run`` in place, so a task switched to a webhook or event trigger
still fired at its old scheduled time without any webhook or event.
"""

import asyncio
import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
import routes.task_routes as task_routes  # noqa: E402
from core.database import ScheduledTask  # noqa: E402

OLD_RUN = datetime(2030, 1, 2, 10, 0, 0)


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(task_routes, "SessionLocal", factory)
    monkeypatch.setattr(task_routes, "get_current_user", lambda _request: "alice")
    try:
        yield factory
    finally:
        engine.dispose()


def _update_endpoint():
    router = task_routes.setup_task_routes(
        task_scheduler=SimpleNamespace(pop_notifications=lambda owner: [])
    )
    for route in router.routes:
        if route.path == "/api/tasks/{task_id}" and "PUT" in route.methods:
            return route.endpoint
    raise RuntimeError("PUT /api/tasks/{task_id} not found")


def _add_scheduled_task(factory, task_id="t1"):
    db = factory()
    try:
        db.add(ScheduledTask(
            id=task_id,
            owner="alice",
            name="Morning digest",
            prompt="Summarise my inbox",
            task_type="llm",
            schedule="once",
            scheduled_date=OLD_RUN,
            trigger_type="schedule",
            status="active",
            next_run=OLD_RUN,
        ))
        db.commit()
    finally:
        db.close()
    return task_id


def _task(factory, task_id="t1"):
    db = factory()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).one()
        db.expunge(task)
        return task
    finally:
        db.close()


async def test_switching_to_webhook_clears_old_timer(session_factory):
    task_id = _add_scheduled_task(session_factory)

    await _update_endpoint()(
        SimpleNamespace(), task_id, task_routes.TaskUpdate(trigger_type="webhook"),
    )

    task = _task(session_factory)
    assert task.trigger_type == "webhook"
    assert task.webhook_token, "switching to webhook still issues a token"
    assert task.next_run is None, "the old schedule must not fire a webhook task"


async def test_switching_to_event_clears_old_timer(session_factory):
    task_id = _add_scheduled_task(session_factory)

    await _update_endpoint()(
        SimpleNamespace(),
        task_id,
        task_routes.TaskUpdate(trigger_type="event", trigger_event="message_sent", trigger_count=3),
    )

    task = _task(session_factory)
    assert task.trigger_type == "event"
    assert task.next_run is None, "an event task waits for its events, not the old timer"


async def test_switching_back_to_schedule_restarts_timer(session_factory, monkeypatch):
    task_id = _add_scheduled_task(session_factory)
    update = _update_endpoint()
    await update(SimpleNamespace(), task_id, task_routes.TaskUpdate(trigger_type="webhook"))

    expected = datetime(2031, 5, 6, 7, 8, 0)
    monkeypatch.setattr(task_routes, "compute_next_run", lambda *a, **k: expected)
    await update(SimpleNamespace(), task_id, task_routes.TaskUpdate(trigger_type="schedule"))

    task = _task(session_factory)
    assert task.trigger_type == "schedule"
    assert task.next_run == expected


async def test_unchanged_trigger_keeps_existing_timer(session_factory):
    """Editing other fields of a scheduled task must not cancel its run."""
    task_id = _add_scheduled_task(session_factory)

    await _update_endpoint()(
        SimpleNamespace(), task_id, task_routes.TaskUpdate(name="Renamed digest"),
    )

    task = _task(session_factory)
    assert task.name == "Renamed digest"
    assert task.next_run == OLD_RUN


async def test_deferred_event_run_survives_unrelated_edit(session_factory):
    """Event tasks keep a scheduler-deferred next_run when the trigger stays the same."""
    task_id = _add_scheduled_task(session_factory)
    db = session_factory()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).one()
        task.trigger_type = "event"
        task.trigger_event = "message_sent"
        task.trigger_count = 3
        deferred = datetime.utcnow() + timedelta(minutes=20)
        task.next_run = deferred
        db.commit()
    finally:
        db.close()

    await _update_endpoint()(
        SimpleNamespace(), task_id, task_routes.TaskUpdate(trigger_type="event", trigger_count=5),
    )

    task = _task(session_factory)
    assert task.trigger_count == 5
    assert task.next_run is not None


async def _agent_update(task_id, **fields):
    from src.tools.system import do_manage_tasks

    result = await do_manage_tasks(
        json.dumps({"action": "edit", "task_id": task_id, **fields}), owner="alice",
    )
    assert result["exit_code"] == 0, result


async def test_agent_tool_switching_trigger_clears_old_timer(session_factory):
    """manage_tasks writes trigger_type too, so it must cancel the timer as well."""
    task_id = _add_scheduled_task(session_factory)

    await _agent_update(task_id, trigger_type="webhook")

    assert _task(session_factory).next_run is None


async def test_agent_tool_switching_back_to_schedule_restarts_timer(session_factory, monkeypatch):
    import src.task_scheduler as task_scheduler

    task_id = _add_scheduled_task(session_factory)
    await _agent_update(task_id, trigger_type="webhook")

    expected = datetime(2031, 5, 6, 7, 8, 0)
    monkeypatch.setattr(task_scheduler, "compute_next_run", lambda *a, **k: expected)
    await _agent_update(task_id, trigger_type="schedule")

    assert _task(session_factory).next_run == expected


async def test_scheduler_does_not_dispatch_task_switched_to_webhook(session_factory, monkeypatch):
    """The reported symptom: the old time passes and the task runs anyway."""
    from src import interactive_gate, task_scheduler

    task_id = _add_scheduled_task(session_factory)
    await _update_endpoint()(
        SimpleNamespace(), task_id, task_routes.TaskUpdate(trigger_type="webhook"),
    )

    monkeypatch.setattr(task_scheduler, "_utcnow", lambda: OLD_RUN + timedelta(hours=1))
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda: False)
    scheduler = task_scheduler.TaskScheduler.__new__(task_scheduler.TaskScheduler)
    scheduler._executing = set()
    scheduler._executing_lock = threading.RLock()
    dispatched = []

    async def record_execution(dispatched_id):
        dispatched.append(dispatched_id)

    scheduler._execute_task = record_execution
    await scheduler._check_due_tasks()
    await asyncio.sleep(0)

    assert dispatched == []


async def test_restoring_an_event_builtin_clears_its_old_timer(session_factory, monkeypatch):
    """ensure_defaults writes trigger_type too: a built-in edited to a schedule goes back to event."""
    from unittest.mock import AsyncMock

    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler(session_manager=None)
    monkeypatch.setattr(scheduler, "ensure_assistant_defaults", AsyncMock())
    db = session_factory()
    try:
        db.add(ScheduledTask(
            id="builtin",
            owner="alice",
            name="Chat Sessions Tidy",
            task_type="action",
            action="tidy_sessions",
            schedule="daily",
            scheduled_time="03:00",
            trigger_type="schedule",
            status="active",
            next_run=OLD_RUN,
        ))
        db.commit()
    finally:
        db.close()

    await scheduler.ensure_defaults("alice")

    task = _task(session_factory, "builtin")
    assert task.trigger_type == "event"
    assert task.next_run is None


async def test_saving_assistant_settings_does_not_rearm_a_webhook_check_in(session_factory, monkeypatch):
    from core.database import CrewMember
    from routes import assistant_routes

    monkeypatch.setattr(assistant_routes, "SessionLocal", session_factory)
    monkeypatch.setattr(assistant_routes, "get_current_user", lambda _request: "alice")
    db = session_factory()
    try:
        db.add(CrewMember(id="crew", owner="alice", name="Assistant", is_default_assistant=True))
        db.add(ScheduledTask(
            id="check-in",
            owner="alice",
            crew_member_id="crew",
            name="Morning check-in",
            task_type="llm",
            schedule="daily",
            scheduled_time="08:00",
            trigger_type="webhook",
            status="active",
            next_run=None,
        ))
        db.commit()
    finally:
        db.close()

    router = assistant_routes.setup_assistant_routes(task_scheduler=SimpleNamespace())
    save = next(
        route.endpoint for route in router.routes
        if route.path == "/api/assistant/settings" and "PATCH" in route.methods
    )
    await save(
        assistant_routes.AssistantSettingsUpdate(
            timezone="UTC",
            check_ins=[assistant_routes.CheckInUpdate(id="check-in", enabled=True)],
        ),
        SimpleNamespace(),
    )

    assert _task(session_factory, "check-in").next_run is None
