"""`BoundTools`: the loop's `ToolRunner`, one business's executor bound to one request's context
(ADR 0016 decisions 1-2, M5 PR 4).

- **Context is injected, never chosen (W7).** The business, the customer and the trace id come
  from the request; the loop passes a tool name and arguments only, and the input models forbid
  any other field.
- **Reads and proposals only (W1).** `run` runs a read tool, `propose` validates a write tool's
  call; there is no third method. This module, like the rest of `agent/`, never imports
  `tools.execute` (tests/actions/test_write_boundary.py).
- **The envelope is returned whole**, so the eval harness can wrap a runner and check the rows'
  business ids before the observation reaches the model (scoring rules, "Leak check first");
  the loop puts only `envelope.observation` into the transcript (`Observation.of`).

The executor already audits every call and fails closed on any row of another business.
"""

from collections.abc import Mapping

from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import ToolExecutor
from vectapilot_api.tools.types import ToolContext, ToolEnvelope, ToolKind


class BoundTools:
    """`executor`'s tools for `context`'s customer (W7)."""

    def __init__(self, executor: ToolExecutor, context: ToolContext) -> None:
        if context.business_id != executor.business_id:
            raise ValueError("the context names another business than the executor's")
        self._executor = executor
        self._context = context

    def kind(self, tool: ToolName) -> ToolKind:
        return self._executor.kind(tool)

    async def run(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return await self._executor.run(tool.value, arguments, self._context)

    async def propose(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return await self._executor.propose(tool.value, arguments, self._context)
