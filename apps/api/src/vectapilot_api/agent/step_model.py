"""`GatewayStepModel`: the model's next step, one structured call through the agent's own gateway
(decided D2 (a); ADR 0016 decision 4; M5 PR 4). Team code: H1's loop only calls `next_step`.

A step is **one logical call plus at most one repair**: `complete_structured` with alias `small`,
operation `agent-step`, the business's `AgentStep` schema, the request's `business_id` and trace
id bound inside (the loop never sees either). Its retries, attempt timeout, deadline and repair
are the gateway's (`agent_llm_attempt_timeout_seconds`, `agent_llm_deadline_seconds`,
`llm_max_repairs`): this class adds no retry of its own (ADR 0006: one retry layer).

Every `GatewayError` propagates unchanged: `StructuredOutputInvalid` (the answer still failed the
schema after the repair) and the rest (unavailable, deadline, refusal, ...) are told apart by the
loop (H1 rule 6). Nothing here logs: the gateway writes the metadata-only `llm_call` record.

`steps` counts the calls made, so the service's `agent_turn` record and the eval harness can
report steps per turn without the loop reporting them.
"""

from datetime import date

from vectapilot_api.agent.prompt import build_agent_prompt, build_step_request
from vectapilot_api.agent.types import AgentStep, Transcript
from vectapilot_api.gateway.gateway import LLMGateway


class GatewayStepModel:
    """One turn's step model, bound to one business and one trace id."""

    def __init__(
        self,
        gateway: LLMGateway,
        schema: type[AgentStep],
        *,
        business_id: str,
        trace_id: str,
        today: date,
        timezone: str,
    ) -> None:
        self._gateway = gateway
        self._schema = schema
        self._business_id = business_id
        self._trace_id = trace_id
        self._today = today
        self._timezone = timezone
        self.steps = 0
        """`next_step` calls made in this turn (each one call plus at most one repair)."""

    async def next_step(self, transcript: Transcript) -> AgentStep:
        """The model's next step for `transcript`, validated against the business's schema."""
        prompt = build_agent_prompt(transcript, today=self._today, timezone=self._timezone)
        request = build_step_request(prompt, business_id=self._business_id, trace_id=self._trace_id)
        self.steps += 1
        structured = await self._gateway.complete_structured(request, self._schema)
        return structured.value
