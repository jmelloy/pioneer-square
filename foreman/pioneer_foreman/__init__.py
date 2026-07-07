"""Pioneer Square standalone foreman agent.

This process is a thin proxy: it holds no durable state and makes no product
decisions of its own. Its only job is to run the LLM call and relay tool
execution across the network/firewall boundary that the backend can't
otherwise cross — all state lives in the backend's database, and all tool
logic lives in the backend. See
https://github.com/jmelloy/pioneer-square/blob/main/docs/foreman-split-plan.md
for the full protocol.
"""
