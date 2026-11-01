"""Platform A (short- and long-term disability) and Platform B (dental and
vision): two independent admin systems, each with its own REST API, its own
MCP server over that API, and its own schema in PostgreSQL.

They share code, not data. A customer that buys from both exists on both,
under the same customer number, the way an enterprise customer number is
shared across a carrier's systems; nothing on Platform A can hold a dental
or vision plan, and nothing on Platform B a disability plan -- the API
refuses it and so does the database.
"""
