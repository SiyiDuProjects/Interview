from __future__ import annotations

import asyncio
import json
import unittest

from app.services.candidate_transcript import CandidateTranscriptRelay
from app.services.realtime_history import InterviewHistory


class FakeRuntime:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.contexts: list[dict] = []
        self.history = InterviewHistory()
        self.inject_lock = asyncio.Lock()
        self.inject_started = asyncio.Event()

    async def update_candidate_transcript(self, turn_id, text, status, *, delta=""):
        turn = self.history.add_turn("candidate", text, turn_id=turn_id)
        turn.update(text=text, status=status)
        self.updates.append({**turn, "delta": delta})

    async def append_candidate_context(self, text):
        self.inject_started.set()
        async with self.inject_lock:
            self.contexts.append(json.loads(text))


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


class CandidateTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_delta_is_forwarded_before_any_pause_or_final(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        for index, delta in enumerate(["I", " ", "use", " a map"]):
            await relay.add(delta, "a")
            await wait_until(lambda: len(runtime.contexts) == index + 1)
            self.assertEqual(runtime.contexts[-1]["delta"], delta)
        self.assertEqual(len(runtime.history.turns), 1)
        self.assertEqual(runtime.history.turns[0]["text"], "I use a map")
        self.assertEqual(runtime.history.turns[0]["status"], "streaming")
        await relay.complete("I use a map.", "a")
        await relay.close()
        self.assertEqual(len(runtime.contexts), 5)
        self.assertEqual(runtime.contexts[-1]["status"], "completed")

    async def test_native_stop_does_not_finalize_or_wait_for_an_application_timer(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.handle({"type": "input_audio_buffer.speech_started", "item_id": "a"})
        await relay.add("Think", "a")
        await relay.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "a"})
        await wait_until(lambda: len(runtime.contexts) == 1)
        self.assertEqual(runtime.history.turns[0]["status"], "streaming")
        await relay.add(" again.", "a")
        await relay.complete("Think again.", "a")
        await relay.close()
        self.assertEqual([item.get("delta") for item in runtime.contexts[:2]], ["Think", " again."])
        self.assertEqual(len(runtime.history.turns), 1)

    async def test_out_of_order_finals_keep_native_start_positions_and_interviewer_order(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.start("a")
        runtime.history.add_turn("interviewer", "Follow up", turn_id="question")
        await relay.start("b")
        await relay.complete("Second candidate turn.", "b")
        await relay.complete("First candidate turn.", "a")
        await relay.close()
        snapshot = runtime.history.transcript_snapshot()
        self.assertEqual([turn["text"] for turn in snapshot],
                         ["First candidate turn.", "Follow up", "Second candidate turn."])
        self.assertEqual(len({turn["turn_id"] for turn in snapshot}), 3)

    async def test_final_correction_supersedes_partial_without_an_extra_candidate_turn(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.add("Forty people", "a")
        created = runtime.history.turns[0]["created_at"]
        await relay.complete("Four people", "a")
        await relay.complete("Four people", "a")
        await relay.add(" late replay", "a")
        await relay.close()
        self.assertEqual(len(runtime.contexts), 2)
        self.assertEqual(runtime.contexts[-1]["text"], "Four people")
        self.assertIn("not a new candidate choice", runtime.contexts[-1]["meaning"])
        self.assertEqual(runtime.contexts[0]["turn_id"], runtime.contexts[1]["turn_id"])
        self.assertEqual(len(runtime.history.turns), 1)
        self.assertEqual(runtime.history.turns[0]["created_at"], created)
        self.assertEqual(runtime.history.turns[0]["text"], "Four people")

    async def test_empty_final_can_retract_a_provisional_recognition(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.add("Spurious text", "a")
        await relay.complete("", "a")
        await relay.close()
        self.assertEqual(runtime.history.transcript_snapshot(), [])
        self.assertEqual(runtime.contexts[-1]["text"], "")
        self.assertEqual(runtime.contexts[-1]["status"], "completed")

    async def test_event_replays_are_deduplicated_but_repeated_speech_is_not(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        event = {"type": "conversation.item.input_audio_transcription.delta",
                 "event_id": "event-a", "item_id": "a", "delta": "Yes."}
        await relay.handle(event)
        await relay.handle(event)
        await relay.handle({**event, "event_id": "event-b", "delta": " Yes."})
        await relay.complete("Yes. Yes.", "a")
        await relay.complete("Yes.", "b")
        await relay.close()
        self.assertEqual([turn["text"] for turn in runtime.history.turns], ["Yes. Yes.", "Yes."])
        self.assertEqual(len(runtime.contexts), 4)
        self.assertEqual([item["event_id"] for item in runtime.contexts[:2]], ["event-a", "event-b"])

    async def test_collection_and_finals_continue_while_forwarding_is_blocked(self):
        runtime = FakeRuntime()
        await runtime.inject_lock.acquire()
        relay = CandidateTranscriptRelay(runtime)
        await relay.add("First", "a")
        await asyncio.wait_for(runtime.inject_started.wait(), 1)
        await relay.add("Second", "b")
        await relay.complete("First.", "a")
        await relay.complete("Second.", "b")
        self.assertEqual([turn["text"] for turn in runtime.history.turns], ["First.", "Second."])
        self.assertEqual(runtime.contexts, [])
        closing = asyncio.create_task(relay.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        runtime.inject_lock.release()
        await asyncio.wait_for(closing, 1)
        self.assertEqual([item.get("delta", item.get("text")) for item in runtime.contexts],
                         ["First", "Second", "First.", "Second."])

    async def test_disconnect_retains_partial_without_claiming_native_completion(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.add("Still speaking", "a")
        await relay.close()
        await relay.close()
        self.assertEqual(runtime.history.turns[0]["status"], "interrupted")
        self.assertEqual(runtime.contexts[-1]["status"], "interrupted")
        self.assertIn("no", runtime.contexts[-1]["meaning"])
        with self.assertRaises(RuntimeError):
            await relay.add("Too late", "a")

    async def test_cancelled_close_caller_does_not_cancel_owned_text(self):
        runtime = FakeRuntime()
        await runtime.inject_lock.acquire()
        relay = CandidateTranscriptRelay(runtime)
        await relay.add("Keep this", "a")
        closing = asyncio.create_task(relay.close())
        await asyncio.wait_for(runtime.inject_started.wait(), 1)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        runtime.inject_lock.release()
        await asyncio.wait_for(relay.close(), 1)
        self.assertEqual(runtime.contexts[0]["delta"], "Keep this")
        self.assertEqual(runtime.contexts[-1]["status"], "interrupted")

    async def test_forwarding_failure_keeps_local_record_and_propagates(self):
        class FailingRuntime(FakeRuntime):
            async def append_candidate_context(self, text):
                raise RuntimeError("synthetic failure")

        runtime = FailingRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.complete("Keep this visible", "a")
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            await asyncio.wait_for(relay.close(), 1)
        self.assertEqual(runtime.history.turns[0]["text"], "Keep this visible")

    async def test_long_turns_and_many_items_are_not_trimmed(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        for index in range(60):
            await relay.complete(f"statement {index}", str(index))
        long_text = "完整陈述" * 1000 + "FINAL-MARKER"
        await relay.add(long_text, "long")
        await relay.complete(long_text, "long")
        await relay.close()
        self.assertEqual(len(runtime.history.turns), 61)
        self.assertEqual(runtime.history.turns[-1]["text"], long_text)
        self.assertEqual(runtime.contexts[-2]["delta"], long_text)
        self.assertEqual(runtime.contexts[-1]["text"], long_text)

    async def test_blank_native_start_is_in_snapshot_until_its_text_arrives(self):
        runtime = FakeRuntime()
        relay = CandidateTranscriptRelay(runtime)
        await relay.start("a")
        snapshot = runtime.history.transcript_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["status"], "streaming")
        self.assertEqual(snapshot[0]["text"], "")
        await relay.close()

    async def test_provider_item_ids_are_scoped_to_each_transcription_connection(self):
        runtime = FakeRuntime()
        first = CandidateTranscriptRelay(runtime)
        await first.complete("First session", "a")
        await first.close()
        second = CandidateTranscriptRelay(runtime)
        await second.complete("Second session", "a")
        await second.close()
        self.assertEqual(len(runtime.history.turns), 2)
        self.assertNotEqual(runtime.contexts[0]["turn_id"], runtime.contexts[1]["turn_id"])

    async def test_missing_item_identity_is_rejected_instead_of_guessing_boundaries(self):
        relay = CandidateTranscriptRelay(FakeRuntime())
        with self.assertRaisesRegex(ValueError, "item_id"):
            await relay.add("No identity", "")
        await relay.close()
