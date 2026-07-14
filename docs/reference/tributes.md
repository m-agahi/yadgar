# Tributes & Acknowledgments

Yadgar stands on the shoulders of extraordinary open-source work. None of this would exist without the researchers, engineers, and communities who built the models, databases, frameworks, and tools listed here. Thank you.

Linked from the [README](../README.md#tribute). Links are to the canonical upstream source for each project.

---

## Featured: Tom Aarsen

Tom Aarsen is a Machine Learning Engineer and Fellow at Hugging Face, and the lead maintainer of the [sentence-transformers](https://github.com/huggingface/sentence-transformers) library — the backbone of yadgar's embedding and cross-encoder reranking infrastructure since the very first version.

Beyond maintaining sentence-transformers, Tom authored the **[Ettin Reranker family](https://huggingface.co/blog/ettin-reranker)** (`cross-encoder/ettin-reranker-*-v1`): a family of five rerankers (17M–1B parameters) built on the ModernBERT architecture, released under Apache 2.0. The Ettin family is yadgar's adopted cross-encoder reranker (Train 2 of the recall pipeline overhaul), replacing the previous GTE-reranker-ModernBERT with a model that benchmarks at 2–8× faster throughput at equivalent or better quality on the memory-retrieval domain. Yadgar uses the 32M and 68M variants as primary and safety-fallback respectively.

The Ettin models are a collaborative work — the base encoders originate from Johns Hopkins University, training data from LightOn, distillation from a Mixedbread AI teacher model — but Tom drove the reranker integration, the sentence-transformers release, and the community-facing work that made these models easy to adopt.

More broadly, sentence-transformers underpins the entire open embeddings and reranking ecosystem that yadgar (and hundreds of other projects) are built on. Tom's sustained stewardship of that library is a gift to everyone doing local, reproducible, privacy-preserving ML inference.

---

## Models

| Model | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **sentence-transformers/all-MiniLM-L6-v2** | Hugging Face (community) | Apache 2.0 | Default embedding model; all memory and wiki vectors | [HF Hub](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) |
| **cross-encoder/ettin-reranker-32m-v1** (primary) | Tom Aarsen / Hugging Face Sentence Transformers team | Apache 2.0 | Cross-encoder reranker (CE stage) — Train 2 adoption | [HF Hub](https://huggingface.co/cross-encoder/ettin-reranker-32m-v1) |
| **cross-encoder/ettin-reranker-68m-v1** (fallback) | Tom Aarsen / Hugging Face Sentence Transformers team | Apache 2.0 | CE reranker safety fallback (68M) | [HF Hub](https://huggingface.co/cross-encoder/ettin-reranker-68m-v1) |
| **Alibaba-NLP/gte-reranker-modernbert-base** | Alibaba-NLP (Tongyi Lab, Alibaba Group) | Apache 2.0 | Current CE reranker (pre-Train-2; 149M params) | [HF Hub](https://huggingface.co/Alibaba-NLP/gte-reranker-modernbert-base) |
| **ModernBERT** (base architecture) | Answer.AI, LightOn, Johns Hopkins University, NVIDIA, Hugging Face | Apache 2.0 | Backbone architecture for both the GTE and Ettin rerankers | [HF Hub](https://huggingface.co/answerdotai/ModernBERT-base) |
| **cross-encoder/ms-marco-MiniLM-L-6-v2** | Hugging Face (community) | Apache 2.0 | Legacy cross-encoder (superseded; still in fallback chain) | [HF Hub](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2) |
| **cross-encoder/nli-deberta-v3-base** | Hugging Face (community) | Apache 2.0 | NLI reranking stage — filters hallucinated or contradictory results | [HF Hub](https://huggingface.co/cross-encoder/nli-deberta-v3-base) |

---

## Data & Storage

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **SurrealDB** | SurrealDB Ltd | BSL 1.1 (server binary); Python SDK: Apache 2.0 | Primary database — all memory, wiki, entity, episode, and vector data; HNSW vector indexes, BM25 FTS, schema migrations | [surrealdb.com](https://surrealdb.com) |
| **LongMemEval** dataset | Di Wu et al. (ICLR 2025) | MIT | Benchmark evaluation for recall quality (500 questions, 6 categories, ~50 sessions/query) | [arXiv](https://arxiv.org/abs/2410.10813) / [HF Hub](https://huggingface.co/datasets/xiaowu0162/longmemeval) |

---

## ML Runtime & Inference

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **sentence-transformers** | Tom Aarsen / Hugging Face | Apache 2.0 | Embedding and cross-encoder reranking inference; model loading and pooling | [GitHub](https://github.com/huggingface/sentence-transformers) |
| **PyTorch** | Meta AI / PyTorch contributors | BSD 3-Clause | Tensor compute backend for all ML inference (CPU-only in the backend container) | [pytorch.org](https://pytorch.org) |
| **ONNX Runtime** | Microsoft | MIT | Quantized (int8) inference path for CE models (evaluated; currently dormant — see ADR-0043) | [onnxruntime.ai](https://onnxruntime.ai) |
| **Hugging Face Hub** (`huggingface_hub`) | Hugging Face | Apache 2.0 | Model download and caching | [GitHub](https://github.com/huggingface/huggingface_hub) |
| **Hugging Face Transformers** (transitive) | Hugging Face | Apache 2.0 | Tokenizers and model config parsing (transitive via sentence-transformers) | [GitHub](https://github.com/huggingface/transformers) |
| **FlashRank** | Prithivi Damodaran | Apache 2.0 | Lightweight ONNX reranker fallback in the CE chain | [GitHub](https://github.com/PrithivirajDamodaran/FlashRank) |
| **hf-xet** | Hugging Face / XetHub | Apache 2.0 | Accelerated model file download from Hub | [GitHub](https://github.com/huggingface/hf-xet) |
| **msgpack** | Sadayuki Furuhashi / msgpack-python contributors | Apache 2.0 | Binary snapshot serialization for LRU caches (CE + embed) | [GitHub](https://github.com/msgpack/msgpack-python) |

---

## Graph & Compute

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **NetworkX** | NetworkX developers | BSD 3-Clause | Knowledge graph traversal; personalized PageRank (PPR) signal; spreading activation | [networkx.org](https://networkx.org) |
| **NumPy** | NumPy contributors | BSD 3-Clause | Array operations throughout retrieval scoring, thermodynamics, and embeddings | [numpy.org](https://numpy.org) |
| **SciPy** | SciPy contributors | BSD 3-Clause | Statistical utilities in scoring and thermodynamics | [scipy.org](https://scipy.org) |
| **regex** | Matthew Barnett | Apache 2.0 | Timeout-capable regex (replaces stdlib `re`) — guards against ReDoS in rules_engine | [PyPI](https://pypi.org/project/regex/) |

---

## Serving & Web

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **FastAPI** | Sebastián Ramírez | MIT | HTTP API for the backend embedding/reranking service | [fastapi.tiangolo.com](https://fastapi.tiangolo.com) |
| **Starlette** | Encode (Tom Christie) | BSD 3-Clause | ASGI foundation underlying FastAPI and the sse-starlette SSE transport | [GitHub](https://github.com/encode/starlette) |
| **Uvicorn** | Encode | BSD 3-Clause | ASGI server for both the MCP HTTP transport and the backend service | [uvicorn.org](https://www.uvicorn.org) |
| **sse-starlette** | Sysid | BSD 2-Clause | Server-Sent Events for the legacy SSE MCP transport | [GitHub](https://github.com/sysid/sse-starlette) |
| **httpx** | Encode | BSD 3-Clause | Async HTTP client — core↔backend communication and all outbound calls | [GitHub](https://github.com/encode/httpx) |
| **Pydantic** | Samuel Colvin | MIT | Data validation and settings management throughout | [docs.pydantic.dev](https://docs.pydantic.dev) |
| **pydantic-settings** | Samuel Colvin | MIT | Environment variable and YAML config loading | [PyPI](https://pypi.org/project/pydantic-settings/) |
| **MCP (Model Context Protocol)** | Anthropic | MIT | The tool protocol that exposes yadgar's tools to Claude Code | [modelcontextprotocol.io](https://modelcontextprotocol.io) |
| **python-multipart** | Andrew Dunstan | Apache 2.0 | Multipart form parsing | [PyPI](https://pypi.org/project/python-multipart/) |
| **urllib3** | Andrey Petrov / urllib3 contributors | MIT | HTTP connection pooling | [GitHub](https://github.com/urllib3/urllib3) |
| **watchdog** | Yesudeep Mangalapilly | Apache 2.0 | Filesystem event watching for the staleness detector | [GitHub](https://github.com/gorakhargosh/watchdog) |
| **ruamel.yaml** | Anthon van der Neut | MIT | YAML config parsing and diagram spec generation | [sourceforge](https://sourceforge.net/p/ruamel-yaml/code/ci/default/tree/) |

---

## Observability

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **OpenTelemetry** (API + SDK + exporters) | CNCF / OpenTelemetry contributors | Apache 2.0 | Distributed tracing (W3C traceparent propagation), span logs throughout the recall pipeline | [opentelemetry.io](https://opentelemetry.io) |
| **Prometheus client** | Prometheus Authors | Apache 2.0 | Metrics export — drainer cycle counters, heat distribution, backend liveness gauges | [GitHub](https://github.com/prometheus/client_python) |
| **Grafana** (dashboards) | Grafana Labs | AGPL 3.0 (OSS) | Visualization of yadgar metrics dashboards | [grafana.com](https://grafana.com) |
| **Tempo** (Grafana Tempo) | Grafana Labs | AGPL 3.0 (OSS) | Trace ingestion backend (OTLP/HTTP endpoint, opt-in via `YADGAR_OTLP_ENDPOINT`) | [grafana.com/oss/tempo](https://grafana.com/oss/tempo/) |

---

## Evaluation

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **LongMemEval** (benchmark harness) | Di Wu, Hongwei Wang et al. | MIT | Standard academic benchmark for long-term conversational memory retrieval quality | [arXiv](https://arxiv.org/abs/2410.10813) / [GitHub](https://github.com/xiaowu0162/LongMemEval) |

---

## Tooling & Infrastructure

| Project | Author / Org | License | What yadgar uses it for | Link |
|---|---|---|---|---|
| **uv** | Astral | MIT / Apache 2.0 | Fast Python package installer and virtual environment manager | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |
| **ruff** | Astral | MIT | Python linter and formatter (lint gate in CI and pre-commit) | [docs.astral.sh/ruff](https://docs.astral.sh/ruff/) |
| **pytest** | Holger Krekel / pytest-dev | MIT | Test runner for the full yadgar test suite | [pytest.org](https://pytest.org) |
| **pytest-xdist** | pytest-dev | MIT | Parallel test execution across workers | [GitHub](https://github.com/pytest-dev/pytest-xdist) |
| **pytest-split** | Jerry Pussinen | MIT | CI matrix splitting for distributed test runs | [GitHub](https://github.com/jerry-git/pytest-split) |
| **pytest-asyncio** | pytest-asyncio contributors | Apache 2.0 | Async test support (auto mode) | [GitHub](https://github.com/pytest-dev/pytest-asyncio) |
| **Hypothesis** | David MacIver / HypothesisWorks | MPL 2.0 | Property-based fuzz testing | [hypothesis.works](https://hypothesis.works) |
| **Nix / nixpkgs** | NixOS Foundation / nixpkgs contributors | MIT | Reproducible build environment; homeManagerModules for systemd unit wiring | [nixos.org](https://nixos.org) |
| **flake-utils** | numtide | MIT | Nix flake output helpers | [GitHub](https://github.com/numtide/flake-utils) |
| **Hatchling** | Ofek Lev / PyPA | MIT | Build backend (pyproject.toml) | [hatch.pypa.io](https://hatch.pypa.io) |
| **Graphviz** | AT&T / Graphviz contributors | CPL 1.0 | Diagram rendering for `docs/diagrams/` (pipeline architecture visuals) | [graphviz.org](https://graphviz.org) |
| **Podman / Docker** | Red Hat / Docker Inc. | Apache 2.0 | Container runtime for the two-container deploy (yadgar-core + yadgar-backend) | [podman.io](https://podman.io) / [docker.com](https://docker.com) |
| **CycloneDX** (`cyclonedx-bom`) | CycloneDX community | Apache 2.0 | SBOM generation for supply-chain transparency | [cyclonedx.org](https://cyclonedx.org) |
| **defusedxml** | Christian Heimes | PSF-2.0 | Safe XML parsing in tests (XXE defence-in-depth) | [GitHub](https://github.com/tiran/defusedxml) |

---

*Licenses are as confirmed at time of writing. Check upstream for the authoritative current license before redistribution. SurrealDB's BSL 1.1 has a four-year conversion clause to Apache 2.0; yadgar's use (embedded personal memory engine, not a DBaaS offering) is within the permitted non-production-competition scope.*
