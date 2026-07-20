# process-gpt-deepagents

## Operational notes

- **Chat SSE reconnect (`/chat/stream/attach`) requires a single uvicorn worker.** The in-progress-run registry (`core/chat/stream_registry.py`) lives in process memory. If this service is ever scaled to multiple uvicorn workers or replicas, a reattach request can land on a worker that doesn't know about the run and will report "no active stream" even though another worker is still streaming. This degrades gracefully (the client falls back to loading saved messages + Supabase Realtime) but silently loses the live catch-up/streaming behavior. Re-evaluate this design (e.g. move the registry to Redis or another shared store) before adding workers.
