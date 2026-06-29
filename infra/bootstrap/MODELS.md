# Local model bootstrap

The default local stack uses Ollama Qwen3 8B Q4_K_M for generation, BGE-small-en-v1.5 for dense embeddings, Qdrant BM25 for sparse retrieval, and MiniLM-L6-v2 for reranking. Generation token budgeting uses the separately pinned Qwen tokenizer; the pinned o200k tokenizer is also prepared for the optional hosted adapter. No paid key or paid model call is needed by bootstrap or the local probe loop.

The application image runs this one-shot initializer after the private `ollama` service is healthy:

```sh
python -m infra.bootstrap.models --download --ollama-url http://ollama:11434
```

Mount the persistent `models` volume at `/models`, writable for UID/GID10001 during initialization; set `ED_MODEL_CACHE_PATH=/models`, `HF_HUB_OFFLINE=0`, and `TRANSFORMERS_OFFLINE=0` for this initializer only. Serving workers mount this same cache read-only and retain offline mode. Ollama owns its separate persistent `/root/.ollama` volume. The initializer needs neither database credentials nor the Docker socket. It does not assume any host service or default loopback port.

`scripts/prepare_models.py --cache PATH --download` downloads only missing files at the reviewed Hugging Face commit IDs, verifies every SHA256, and publishes the generation tokenizer atomically after verification. Existing corrupt files cause failure and are not silently replaced. Running without `--download` performs verification only. The cache path also honors `ED_MODEL_CACHE_PATH`; the host default remains `.cache/models`.

The initializer sets Hugging Face's auxiliary home and Xet cache to `PATH/.huggingface` before importing the download libraries. An explicit snapshot `cache_dir` does not relocate Xet's metadata and logging files; without this setting a numeric container user tries to write `/.cache` on the read-only root filesystem. These auxiliary files stay on the writable model volume. Download failures report their exception class without printing remote response bodies or signed URLs. See the upstream [Hugging Face cache environment variables](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables).

The initializer checks the exact Ollama runtime version, model digest and size, prompt-template and license hashes, and capabilities from `local-runtime.lock.json`. If the pinned alias is absent, it obtains the upstream model through the native `/api/pull` API, checks the returned digest and size, and copies it to the pinned alias. Existing aliases with the wrong digest are rejected. A successful rerun needs no pull or copy. Model locks are never rewritten by bootstrap. Network requests have bounded connect/read timeouts and each preparation stage has a wall-clock limit of3600seconds by default. A cache-level file lock rejects concurrent initializers. Interrupted downloads can be resumed by rerunning the job; partial/failed pulls do not create the pinned alias.

Ollama's reviewed release does not accept a digest in its ordinary model-name parser, and the registry manifest URL using the locked digest returned404 during verification. The published `qwen3:8b` and `qwen3:8b-q4_K_M` manifests both still matched the locked digest on2026-10-19. A changed upstream tag therefore fails closed. An operator must preserve a verified volume or review a replacement model lock if the exact artifact becomes unavailable. This is a reproducibility limit, not permission to follow an updated tag. The implementation uses the supported [pull API](https://docs.ollama.com/api/pull), [copy API](https://docs.ollama.com/api/copy), and [model inventory](https://docs.ollama.com/api/tags); it does not synthesize GGUF imports or depend on writing Ollama's private manifest format.

## Ongoing readiness

After initialization, the `probe` credential profile starts:

```sh
python -m infra.bootstrap.entrypoint --profile probe -- python -m infra.bootstrap.probe_loop
```

It checks every30seconds and invokes only the existing native `probe_model("local_standard", allow_paid=False, max_cost_micro_usd=0)` path. A successful probe becomes eligible for refresh after4minutes; API admission still requires success younger than5minutes. Probe leases, the global local-inference slot, streaming/schema/usage/cancellation checks, and failed-probe backoff remain authoritative. An active probe temporarily uses `half_open` state and the shared inference slot; this is not a zero-interruption availability guarantee. No hosted model can be selected through this loop.

The loop writes `/tmp/local-model-readiness.json` without prompts or credentials. Its healthcheck checks both heartbeat age and the actual database-derived readiness expiry:

```sh
python -m infra.bootstrap.probe_loop --healthcheck
```

A skipped or busy probe does not count as a successful capability test. The loop reads the current configuration-bound database report before marking readiness. SIGTERM/SIGINT cancel an active probe and remove the local readiness file.

## Verification boundary

The2026-10-19 check used a disposable, isolated Ollama0.40.3 container capped at512MiB and1CPU. It began with no model manifests, mounted existing blob files read-only, fetched the live registry manifest through `/api/pull`, created the pinned alias, and passed metadata verification. The second initializer invocation passed without fetching the model again. No generation took place in this packaging check. A separate fixture copied from the host's old mutable manifest had a different digest and was correctly rejected without replacement.

For the integrated Compose smoke, the existing model cache was copied into the project volumes only after checking the exact pinned hashes:40 embedding/reranking/tokenizer files and6 Ollama blobs. Mutable Ollama manifests were deliberately excluded; the initializer must still fetch and verify the current registry manifest. **This is a warm-cache integration setup. A complete cold-cache download and a fresh integrated native generation probe are not yet evidenced by these checks.** See [the machine-readable report](../../docs/10-testing/model-bootstrap-2026-10-19.json) for tested scope.
