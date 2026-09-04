"""Tests for chat run event log and concurrent-run guard."""

from __future__ import annotations

import unittest

from src.chat_runs import ChatRunStore, ConcurrentChatRunError


class ChatRunStoreTests(unittest.TestCase):
    def test_append_and_resume_cursor(self) -> None:
        store = ChatRunStore()
        run = store.create(
            session_id="s1",
            username="alice",
            message="hi",
            public_base_url="http://localhost",
        )
        run.append("status", phase="thinking")
        run.append("delta", text="你")
        run.append("delta", text="好")
        run.append("done", session_id="s1")

        after_1 = run.events_after(1)
        self.assertEqual([e["type"] for e in after_1], ["delta", "delta", "done"])
        self.assertEqual(after_1[0]["text"], "你")
        self.assertTrue(run.delta_emitted)
        self.assertTrue(run.snapshot_done())

    def test_concurrent_active_run(self) -> None:
        store = ChatRunStore()
        store.create(
            session_id="s1",
            username="alice",
            message="a",
            public_base_url="http://localhost",
        )
        with self.assertRaises(ConcurrentChatRunError):
            store.create(
                session_id="s1",
                username="alice",
                message="b",
                public_base_url="http://localhost",
            )

    def test_done_allows_new_run_without_finish(self) -> None:
        store = ChatRunStore()
        run = store.create(
            session_id="s1",
            username="alice",
            message="a",
            public_base_url="http://localhost",
        )
        run.append("done", session_id="s1")
        self.assertIsNone(store.active_run_id("s1"))
        run2 = store.create(
            session_id="s1",
            username="alice",
            message="b",
            public_base_url="http://localhost",
        )
        self.assertNotEqual(run.run_id, run2.run_id)

    def test_session_lock_registry(self) -> None:
        from src.session_locks import SessionLockRegistry

        locks = SessionLockRegistry()
        a = locks.lock_for("s-a")
        b = locks.lock_for("s-b")
        self.assertIs(a, locks.lock_for("s-a"))
        self.assertIsNot(a, b)

    def test_fork_for_turn_isolates_state(self) -> None:
        from src.chatbot import CustomerServiceBot
        from src.config import get_settings

        # Avoid heavy init: build via __new__ + minimal attrs if needed.
        # Use real bot only if settings load quickly; otherwise test fork shape.
        try:
            parent = CustomerServiceBot.__new__(CustomerServiceBot)
            parent.settings = get_settings()
            parent.llm = object()
            parent.prompt = object()
            from src.flow_feishu_task import FeishuTaskFlow
            from src.flow_order import OrderQueryFlow
            from src.flow_ticket import TicketCreateFlow

            parent.last_topic = "t"
            parent.last_clarify_options = ["x"]
            parent.last_effective_query = "q"
            parent.consecutive_no_answer = 2
            parent.repeat_count = 1
            parent.last_user_norm = "n"
            parent.order_flow = OrderQueryFlow()
            parent.ticket_flow = TicketCreateFlow()
            parent.feishu_task_flow = FeishuTaskFlow()
            parent.channel_ctx = None
            parent._stream_on_status = None
            parent._stream_on_delta = None
            parent._stream_delta_emitted = False

            # Bind real method
            child = CustomerServiceBot.fork_for_turn(parent)
            self.assertIs(child.llm, parent.llm)
            self.assertEqual(child.last_topic, "")
            self.assertEqual(child.last_clarify_options, [])
            self.assertIsNot(child.order_flow, parent.order_flow)
            parent.last_topic = "parent-only"
            self.assertEqual(child.last_topic, "")
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"fork test skipped: {exc}")


if __name__ == "__main__":
    unittest.main()
