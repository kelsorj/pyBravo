# docling-serve on NVIDIA Blackwell (GB10 / DGX Spark, compute cap sm_121).
#
# Why NGC 26.01-py3 specifically (as of April 2026):
# Triton issue #9181 + PyTorch issue #164342 — Triton invokes `ptxas` with
# `sm_121a`, but ptxas in CUDA 13 only recognizes up to `sm_120a`, so the
# default pip `torch` nightly crashes on first kernel launch. NGC
# 26.01-py3 ships with pre-compiled kernels for sm_120/sm_121 that
# sidestep the Triton toolchain entirely. Revisit when PyTorch 2.10+
# stable lands full Blackwell support (expected ~Q2 2026); after that the
# pip-based install may become viable again.

FROM nvcr.io/nvidia/pytorch:26.01-py3

# docling-ibm-models imports cv2 at module load; NGC's minimal base image
# lacks the X11/Mesa runtime libs that opencv-python links against.
# Install the smallest set that satisfies cv2's dynamic loader.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libxcb-shm0 libxcb-render0 libxrender1 \
    libglib2.0-0 libgl1 libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# docling-serve + its pipeline (docling, docling-core, docling-ibm-models,
# docling-parse, rapidocr, transformers, etc.). Installs cleanly on top
# of NGC's torch.
RUN pip install --no-cache-dir docling-serve

# Central HuggingFace cache location inside the container; the run
# command mounts a named volume here so downloaded model weights
# (RapidOCR PP-OCRv4, docling-layout-heron, tableformer, ...) survive
# container restarts.
ENV HF_HOME=/root/.cache/huggingface

EXPOSE 5001
CMD ["docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"]
