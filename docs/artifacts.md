# Worker / foreman artifact API

Workers and coding agents upload artifacts to the backend's shared API:

```http
POST /guilds/{guild_id}/artifacts
Authorization: Bearer <worker token, member token, or foreman JWT>
Content-Type: multipart/form-data
```

Send `file` and optionally `task_id`. This is the endpoint used by
`worker/pioneer_worker/artifact_upload.py`; there is no separate artifact upload
handler in `backend/foreman/` or the standalone foreman LLM proxy.

Both the upload response and `GET /guilds/{guild_id}/artifacts/{artifact_id}`
return metadata including:

```json
{
  "id": "<artifact UUID>",
  "url": "/api/artifacts/<artifact UUID>/public",
  "public_url": "https://pioneer.example/api/artifacts/<artifact UUID>/public",
  "artifactUri": "https://pioneer.example/api/artifacts/<artifact UUID>/public?type=text",
  "markdown": "[report.txt](https://pioneer.example/api/artifacts/<artifact UUID>/public?type=text)"
}
```

`public_url` is an absolute, permanent download link, not an expiring S3
presigned URL. Its origin uses `FRONTEND_URL`, falling back to the request's
base URL. Set `FRONTEND_URL` to the externally reachable app origin when workers
connect through an internal hostname. That origin must route `/api/artifacts/`
to the backend.

Downloads require no authentication: anyone with the UUID link can access the
file. Use `public_url` for external references. `artifactUri` (kept for API
compatibility) and `markdown` also use public URLs, with a `?type=` rendering hint
for non-image artifacts. The app still renders legacy `artifact://` references
in historical messages. Inline images externalized from messages also use the
public origin, falling back to `http://localhost:5173` when `FRONTEND_URL` is unset. The same response applies with or without `task_id` and for all
supported artifact types. The worker folder watcher uploads automatically but
does not currently expose the JSON response to the running coding agent.
