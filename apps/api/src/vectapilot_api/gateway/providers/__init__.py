"""Model providers behind the gateway: the interface (`base`), a scripted fake for tests (`fake`)
and, from PR 5, the Anthropic adapter. Only the gateway calls a provider; callers never do."""
