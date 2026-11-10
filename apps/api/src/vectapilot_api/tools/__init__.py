"""Tools: what the assistant may look up and propose (ADR 0016, M5).

PR 1 (`m5/sandbox`) adds the per-business tool configuration (`config.py`, read from
`VECTAPILOT_TOOLS_CONFIG_DIR`), the sandbox seed and its owner-only reset (`sandbox.py`) and the
command line (`cli.py`).

PR 2 (`m5/tool-core`) adds the tool core: the shared types (`types.py`), what a tool is
(`spec.py`), one module per tool (`check_slots`, `order_status`, `book`, `reschedule`,
`create_ticket`), the registry (`registry.py`) and the executor (`executor.py`), which runs read
tools and proposes write tools. Nothing here executes a write: that is PR 3's confirm route alone.
"""
