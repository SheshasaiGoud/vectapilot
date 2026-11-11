"""VectaPilot's MCP server (M5 PR 5, ADR 0016 decision 8, D1 (a)).

A thin stdio adapter over the API's tool core, in a uv project of its own, so the MCP SDK never
enters the API's lock or image. One process serves one business and one customer, both from the
command line (`--business-id`, `--customer-ref`), sandbox only:

- **Context is injected, never chosen (W7).** The business and the customer come from the flags,
  exactly as `X-Business-Id` and `customer_ref` do in the API, and are refused outside local, test
  and ci. No listed input schema has a field for either (the tools' input models forbid them).
- **Reads run through the executor** (`ToolExecutor.run`): snapshot transactions, server
  timeouts, the observation caps, the leak fence and an audit row per call, as for the agent.
- **Writes are only proposed** (`ToolExecutor.propose`): the result is the summary and
  `confirmation_required`, with no action id, no token and no pending action. MCP cannot confirm
  in M5, so nothing it returns is confirmable (W1, W2; "Review notes" in the M5 plan).
- **Least privilege.** It connects only as `vectapilot_app`, checked by `Database.verify_role`
  before it serves anything: the owner's DSN stops startup.

`server.py` builds the MCP server, `startup.py` checks the flags, the settings and the database,
and `cli.py` is the `vectapilot-mcp` command.
"""
