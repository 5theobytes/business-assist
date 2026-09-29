"""Tests for v4 decision_log population throughout workflow lifecycle."""
import pytest

from app.eval.mock_llm import mock_responder
from app.llm import LLMResponse, MockLLM
from app.state import Phase, WorkflowState
from app.workflows import build
from app.workflows.base import WorkflowSession
from app.workflows.v4_clarity import ClarityWorkflow


class TestDecisionLogPopulation:
    """Verify decision_log is populated at key decision points."""

    def _make_session(self, phase=Phase.POINT_A):
        """Create a test v4 session in given phase."""
        state = WorkflowState()
        state.conversation.phase = phase
        state.conversation.language = "en"
        session = WorkflowSession(id="test-decision-log", workflow_name="v4")
        session.state = state
        return session

    def _build_workflow(self, responder=None):
        """Build a v4 ClarityWorkflow with the given (or default mock) responder."""
        if responder is None:
            responder = mock_responder
        llm = MockLLM(responder=responder)
        return build("v4", llm=llm)

    # ------------------------------------------------------------------
    # Test: extractor log_decision tool call records into decision_log
    # ------------------------------------------------------------------

    def test_decision_logged_when_extractor_calls_log_decision(self):
        """When extractor LLM returns a log_decision tool call, it's recorded."""
        call_count = {"n": 0}

        def responder(system, messages, tools):
            call_count["n"] += 1
            # First call is the extractor (has update_state tool)
            if tools and any(t.get("name") == "update_state" for t in tools):
                return LLMResponse(
                    text="",
                    tool_use={
                        "name": "log_decision",
                        "input": {
                            "question": "What is the main business channel?",
                            "answer": "Instagram direct messages",
                            "alternatives": ["WhatsApp", "Telegram"],
                            "rationale": "Most clients reach out via Instagram DMs",
                        },
                    },
                )
            # Session memory extractor — no signal
            if "retractions" in (system if isinstance(system, str) else ""):
                return LLMResponse(text="{}")
            # Question-first handler
            if "has_question" in (system if isinstance(system, str) else ""):
                return LLMResponse(text='{"has_question": false}')
            # Asker fallback
            return LLMResponse(text="Tell me more about your business.")

        wf = self._build_workflow(responder=responder)
        session = self._make_session(phase=Phase.POINT_A)
        # Seed transcript so respond() works
        session.append("assistant", "Hi! Where shall we start?")

        wf.respond(session, "We mostly get clients through Instagram DMs")

        log = session.state.solution.decision_log
        assert len(log) >= 1
        entry = log[0]
        assert entry["question"] == "What is the main business channel?"
        assert entry["answer"] == "Instagram direct messages"
        assert "WhatsApp" in entry["alternatives"]
        assert entry["rationale"] == "Most clients reach out via Instagram DMs"

    # ------------------------------------------------------------------
    # Test: ANALYSIS option selection logs a decision
    # ------------------------------------------------------------------

    def test_analysis_option_selection_logs_decision(self):
        """When user selects an analysis option, it's logged as a decision."""

        def responder(system, messages, tools):
            return LLMResponse(text="Got it.")

        wf = self._build_workflow(responder=responder)
        session = self._make_session(phase=Phase.ANALYSIS)

        # Pre-load offered_options like the scanner+generator already ran
        session.state.solution.offered_options = [
            {
                "shape": "Telegram bot for bookings",
                "description": "Clients book via TG, owner sees calendar",
                "needs_ai": True,
                "needs_chat_ui": True,
                "needs_external_integrations": True,
                "recommended": True,
            },
            {
                "shape": "Simple Google Sheets reminder",
                "description": "A spreadsheet with scheduled notifications",
                "needs_ai": False,
                "needs_chat_ui": False,
                "needs_external_integrations": False,
                "recommended": False,
            },
            {
                "shape": "WhatsApp auto-reply",
                "description": "Auto-responses for common questions",
                "needs_ai": True,
                "needs_chat_ui": True,
                "needs_external_integrations": True,
                "recommended": False,
            },
        ]

        wf._lock_solution_and_ack(session, 0)

        log = session.state.solution.decision_log
        assert len(log) == 1
        entry = log[0]
        assert entry["question"] == "Which solution shape to build?"
        assert entry["answer"] == "Telegram bot for bookings"
        # Alternatives should be the other two options
        assert "Simple Google Sheets reminder" in entry["alternatives"]
        assert "WhatsApp auto-reply" in entry["alternatives"]
        assert entry["rationale"] == "Clients book via TG, owner sees calendar"

    # ------------------------------------------------------------------
    # Test: DEV_COVERAGE QA merged into decision_log
    # ------------------------------------------------------------------

    def test_dev_coverage_qa_merged_into_decision_log(self):
        """When DEV_COVERAGE completes, QA entries become decision_log entries."""

        def responder(system, messages, tools):
            return LLMResponse(text="Summary complete.")

        wf = self._build_workflow(responder=responder)
        session = self._make_session(phase=Phase.DEV_COVERAGE)
        # Seed transcript
        session.append("assistant", "Let me check if we have everything...")

        # Pre-fill dev_coverage_qa as if the scanner collected answers
        session.state.resources.dev_coverage_qa = [
            {
                "question": "Should the bot reply in English or Russian?",
                "answer": "Russian only",
                "rationale": "All clients speak Russian",
            },
            {
                "question": "What happens if payment fails?",
                "answer": "Notify the owner manually",
                "rationale": "Simple fallback for MVP",
            },
        ]

        wf._proceed_after_dev_coverage_complete(session)

        log = session.state.solution.decision_log
        assert len(log) == 2
        # Check first entry
        assert log[0]["question"] == "Should the bot reply in English or Russian?"
        assert log[0]["answer"] == "Russian only"
        assert "[Technical clarification]" in log[0]["rationale"]
        # Check second entry
        assert log[1]["question"] == "What happens if payment fails?"
        assert log[1]["answer"] == "Notify the owner manually"
        assert "[Technical clarification]" in log[1]["rationale"]

    # ------------------------------------------------------------------
    # Test: decision_log entry structure
    # ------------------------------------------------------------------

    def test_decision_log_entry_structure(self):
        """Each decision_log entry has required fields."""

        def responder(system, messages, tools):
            return LLMResponse(text="ok")

        wf = self._build_workflow(responder=responder)
        session = self._make_session()

        wf._record_decision(session, {
            "question": "Which CRM to use?",
            "answer": "AmoCRM",
            "alternatives": ["Bitrix24", "HubSpot"],
            "rationale": "Already has an account",
        })

        log = session.state.solution.decision_log
        assert len(log) == 1
        entry = log[0]
        # Required fields
        assert isinstance(entry["question"], str) and entry["question"]
        assert isinstance(entry["answer"], str) and entry["answer"]
        assert isinstance(entry["alternatives"], list)
        assert isinstance(entry["rationale"], str)
        # Specific values
        assert entry["question"] == "Which CRM to use?"
        assert entry["answer"] == "AmoCRM"
        assert "Bitrix24" in entry["alternatives"]
        assert "HubSpot" in entry["alternatives"]
        assert entry["rationale"] == "Already has an account"

    # ------------------------------------------------------------------
    # Test: empty entries filtered out
    # ------------------------------------------------------------------

    def test_empty_entries_filtered_out(self):
        """Entries with empty question or answer are not added."""

        def responder(system, messages, tools):
            return LLMResponse(text="ok")

        wf = self._build_workflow(responder=responder)
        session = self._make_session()

        # Empty question — should not be added
        wf._record_decision(session, {
            "question": "",
            "answer": "some answer",
            "alternatives": [],
            "rationale": "",
        })
        assert len(session.state.solution.decision_log) == 0

        # Empty answer — should not be added
        wf._record_decision(session, {
            "question": "some question",
            "answer": "",
            "alternatives": [],
            "rationale": "",
        })
        assert len(session.state.solution.decision_log) == 0

        # Whitespace-only question — should not be added
        wf._record_decision(session, {
            "question": "   ",
            "answer": "answer",
            "alternatives": [],
            "rationale": "",
        })
        assert len(session.state.solution.decision_log) == 0

        # Whitespace-only answer — should not be added
        wf._record_decision(session, {
            "question": "question",
            "answer": "   ",
            "alternatives": [],
            "rationale": "",
        })
        assert len(session.state.solution.decision_log) == 0

        # Valid entry — should be added
        wf._record_decision(session, {
            "question": "Valid question?",
            "answer": "Valid answer",
            "alternatives": [],
            "rationale": "",
        })
        assert len(session.state.solution.decision_log) == 1

    # ------------------------------------------------------------------
    # Test: multiple decisions accumulate in order
    # ------------------------------------------------------------------

    def test_multiple_decisions_accumulate(self):
        """Multiple decisions across turns accumulate in order."""

        def responder(system, messages, tools):
            return LLMResponse(text="ok")

        wf = self._build_workflow(responder=responder)
        session = self._make_session()

        decisions = [
            {
                "question": "What is your business?",
                "answer": "Online beauty shop",
                "alternatives": [],
                "rationale": "Direct statement",
            },
            {
                "question": "Primary value?",
                "answer": "Save time",
                "alternatives": ["Save money", "Better CX"],
                "rationale": "Owner spends 4h/day on admin",
            },
            {
                "question": "Which integration?",
                "answer": "Telegram",
                "alternatives": ["WhatsApp", "Instagram"],
                "rationale": "80% of clients use Telegram",
            },
        ]

        for d in decisions:
            wf._record_decision(session, d)

        log = session.state.solution.decision_log
        assert len(log) == 3
        # Verify order
        assert log[0]["question"] == "What is your business?"
        assert log[1]["question"] == "Primary value?"
        assert log[2]["question"] == "Which integration?"
        # Verify content preserved
        assert log[1]["alternatives"] == ["Save money", "Better CX"]
        assert log[2]["rationale"] == "80% of clients use Telegram"

    # ------------------------------------------------------------------
    # Test: decision_log survives missing optional fields
    # ------------------------------------------------------------------

    def test_record_decision_handles_missing_optional_fields(self):
        """_record_decision works with minimal data (no alternatives/rationale)."""

        def responder(system, messages, tools):
            return LLMResponse(text="ok")

        wf = self._build_workflow(responder=responder)
        session = self._make_session()

        # Only required fields provided
        wf._record_decision(session, {
            "question": "Budget range?",
            "answer": "Up to 5000 RUB/month",
        })

        log = session.state.solution.decision_log
        assert len(log) == 1
        entry = log[0]
        assert entry["question"] == "Budget range?"
        assert entry["answer"] == "Up to 5000 RUB/month"
        assert entry["alternatives"] == []
        assert entry["rationale"] == ""

    # ------------------------------------------------------------------
    # Test: full respond() call populates decision_log via extraction
    # ------------------------------------------------------------------

    def test_respond_populates_decision_log_via_extractor(self):
        """A full respond() call that triggers the extractor path records decisions."""
        call_idx = {"n": 0}

        def responder(system, messages, tools):
            call_idx["n"] += 1
            sys_text = system if isinstance(system, str) else ""

            # Extractor call — return update_state + log_decision via tool_use
            if tools and any(t.get("name") == "update_state" for t in tools):
                return LLMResponse(
                    text="",
                    tool_use={
                        "name": "update_state",
                        "input": {
                            "point_a": {"business_type": "online store"},
                        },
                    },
                )
            # All other calls — simple text response
            return LLMResponse(text="What else can you tell me?")

        wf = self._build_workflow(responder=responder)
        session = self._make_session(phase=Phase.POINT_A)
        session.append("assistant", "Where shall we start?")

        wf.respond(session, "I have an online store selling handmade items")

        # State should be updated
        assert session.state.point_a.business_type == "online store"
