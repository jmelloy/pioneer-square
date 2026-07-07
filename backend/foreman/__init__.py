"""Backend Foreman implementation package.

Keep this module lightweight: the standalone proxy imports ``backend.foreman.llm``
for shared provider/client helpers, and importing that should not pull in the
backend runner, database models, or tool executor.
"""
