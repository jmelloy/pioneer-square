"""Pioneer Square standalone Foreman API proxy.

This process is a thin proxy: it holds no durable state and makes no product
decisions of its own. Its only job is to execute LLM API requests across the
network/firewall boundary that the backend can't otherwise cross. All state,
trigger routing, history, and tool logic live in the backend. See
https://github.com/jmelloy/pioneer-square/blob/main/docs/foreman-split-plan.md
for the full protocol.
"""
