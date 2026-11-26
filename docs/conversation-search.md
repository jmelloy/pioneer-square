# Conversation search

`GET /api/guilds/{guild_id}/conversations?q=deployment+failure` searches conversation names and individual message bodies. Authentication and guild membership are required, just as for the unfiltered conversation list. The optional `status` filter, newest-created-first order, 200-conversation cap, previews, and participants remain unchanged. Search includes active, archived, and closed conversations unless filtered by status.

## Indexing decision (#1531)

Use PostgreSQL full-text search with English stemming and GIN expression indexes on conversation names and linked message content. PostgreSQL maintains these indexes on inserts, edits, and renames; building them also indexes existing rows. Concurrent index creation avoids blocking ongoing writes, but requires extra disk space and database work during deployment.

The existing `q` API is reused so current clients receive indexed search without a second endpoint. Queries use `plainto_tsquery`: words are ANDed within a name or a single message, case-insensitively, with English stemming and stop-word removal. Punctuation is not search syntax. Blank queries list conversations normally; nonblank queries containing only stop words or punctuation return no matches. Unlike the previous substring scan, partial words do not match, and punctuation such as `%` and `_` is not matched literally. Results are conversations, not individual message hits, and duplicate message matches do not duplicate conversations.

pgvector is deferred: semantic search needs an embedding model/provider, generation and retry handling, an existing-message backfill, and model/version lifecycle management. None is needed for the core keyword-search use case. No extensions, external API calls, or new dependencies are required for FTS.
