# docling-serve on NVIDIA Blackwell (GB10 / DGX Spark)

The Phase 3 PDF-ingest pipeline (OpenBravo → `/api/workflow/parse_pdf`)
delegates document parsing to a remote [`docling-serve`][1] instance.
This directory holds the **Dockerfile + runbook** for standing up a
GPU-accelerated `docling-serve` on an NVIDIA Blackwell GPU — the tricky
case — on a host configured like the DGX Spark the project was
originally deployed on.

For non-Blackwell hosts (H100, A100, L40, consumer RTX 4090, etc.) the
official images `quay.io/docling-project/docling-serve-cu124` (x86) or
a pip install in a plain Python venv will work fine; you don't need
what's in this directory.

[1]: https://github.com/docling-project/docling-serve

## Why this image exists

As of April 2026, `docling-serve` on Blackwell (compute capability
`sm_121`) crashes on the first kernel launch when installed via `pip`.
Triton invokes `ptxas --gpu-name=sm_121a`, but `ptxas` in CUDA 13
doesn't recognize `sm_121a` — it only knows up to `sm_120a`. The
resulting error is a giant dump of TorchInductor-generated CUDA/HIP
template code. Tracking:

- [triton-lang/triton#9181](https://github.com/triton-lang/triton/issues/9181)
- [pytorch/pytorch#164342](https://github.com/pytorch/pytorch/issues/164342)
- [NVIDIA forum: GB10 and Docling](https://forums.developer.nvidia.com/t/gb10-and-docling/360665)

The fix is to build on top of
**NVIDIA's NGC container `nvcr.io/nvidia/pytorch:26.01-py3`**, which
ships with pre-compiled kernels for `sm_120` / `sm_121` and avoids the
Triton compile path entirely. Revisit when PyTorch 2.10+ stable ships
Blackwell support (expected ~Q2 2026) — the pip-based path may become
viable again.

## Prerequisites on the host

- Docker ≥ 20.
- NVIDIA Container Toolkit (`docker info | grep nvidia` or `docker
  run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu22.04 nvidia-smi`
  should work; CDI is fine, a named `nvidia` runtime isn't required).
- NVIDIA driver ≥ 580 (Blackwell-aware). The CUDA forward-compat layer
  in the container handles the rest.

## Build

```bash
cd docker/docling-serve-gpu
docker build -t docling-serve-gpu .
```

Build takes ~80s on a DGX Spark. Image is ~15 GB (NGC base is big).

## Run

```bash
docker run -d \
  --name docling-serve \
  --restart unless-stopped \
  --gpus all \
  --ipc=host \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -p 5001:5001 \
  -v docling-cache:/root/.cache \
  -e DOCLING_SERVE_ENABLE_UI=true \
  docling-serve-gpu
```

Flag notes:

- `--restart unless-stopped` — container auto-starts on Docker daemon
  startup; no systemd wrapper needed.
- `--ipc=host --ulimit memlock=-1 --ulimit stack=67108864` — NVIDIA's
  recommended defaults for PyTorch containers; avoids shared-memory
  issues during multi-worker inference.
- `-v docling-cache:/root/.cache` — named volume persists the
  ~1 GB of model weights (RapidOCR PP-OCRv4, docling-layout-heron,
  tableformer, granite-docling) across container restarts. First parse
  after a fresh build warms this cache.
- `DOCLING_SERVE_ENABLE_UI=true` — exposes a Gradio UI at `/ui` for
  operators to drop a PDF in ad-hoc. Optional.

## Verify

```bash
sleep 15
docker ps | grep docling                     # should be 'Up'
docker logs docling-serve 2>&1 | tail -20    # look for 'Using GPU device with ID: 0'
curl -sS http://localhost:5001/health        # expect {"status":"ok"}
```

## Point OpenBravo at it

In the OpenBravo project's `.env` (on the dev/server host that runs
the FastAPI backend):

```
PYBRAVO_DOCLING_URL=http://<dgx-ip>:5001
```

See [`pybravo/workflow/drafter/paper_parser.py`](../../pybravo/workflow/drafter/paper_parser.py)
for the client.

## Performance baseline (Heliyon 8-page paper, April 2026)

| Config | First parse | Warm parse | Notes |
|---|---|---|---|
| Venv install, CPU only (`CUDA_VISIBLE_DEVICES=""`) | 30s | 30s | |
| **NGC container, Blackwell GPU** | **13s** | **7s** | 4.4× |

## Upgrade

```bash
# Stop + remove the current container (keeps the model cache volume).
docker rm -f docling-serve

# Rebuild against the latest docling-serve pip release.
docker build --no-cache -t docling-serve-gpu .

# Relaunch with the same command above.
```

## Troubleshooting

- **`ImportError: libxcb.so.1`** at container startup — NGC base
  upgraded and dropped the OpenCV runtime libs. Update the `apt-get
  install` line in the Dockerfile with whatever shared-library cv2
  complains about.
- **`ptxas fatal: architecture sm_121a not supported`** — you're NOT
  running on the NGC image; check the Dockerfile's `FROM` line and
  rebuild.
- **`no CUDA-capable device`** — the `--gpus all` flag isn't being
  honored. Run `docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu22.04 nvidia-smi`
  to verify GPU passthrough works at the Docker level.
