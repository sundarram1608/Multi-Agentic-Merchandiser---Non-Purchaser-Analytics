"""
Agents package for the Merchandising chat workflow.

The package houses nine LLM agents plus the rule-based Orchestrator that
wires them together. See `docs/00_system_overview.md` for the canonical
architecture description and `docs/09_agents_and_tools_reference.md` for
the per-agent contract table.

Modules:
  schemas.py             shared dataclasses (PlannerOutput, AgentResponse,
                         StepEvent, OutputReviewerResult, ...)
  llm.py                 single aisuite entry point (text + JSON + vision)
                         and `load_prompt` helper
  orchestrator.py        the state machine — calls Planner, runs the SQL
                         retry loops, drives the hard-block groundedness
                         loop on the Writer, dispatches the viz pipeline,
                         persists every turn to chat_trace via trace_logger
  planner.py             routes turns into sql/direct/clarify; resolves
                         chat-history references
  coder.py               NL question → SELECT SQL
  code_reviewer.py       static + post-exec SQL review
  output_reviewer.py     semantic fit + visualization decision
  writer.py              rows → grounded English answer; called inside the
                         orchestrator's `_write_grounded_answer` loop
  viz_coder.py           rows → matplotlib snippet
  viz_code_reviewer.py   static review of viz code before sandbox exec
  viz_reviewer.py        multimodal review of rendered PNG
  supervisor.py          recovery brain when a retry loop exhausts

  prompts/               all system prompts as .txt files (one per agent)
"""
