# AWS Lambda container image for the summarizer.
#
#   docker build -t documind-ai .
#
# Lambda's base image already contains Python 3.12 and a small web server that waits for events
# (locally it listens on port 8080, so we can test it like the real Lambda).
FROM public.ecr.aws/lambda/python:3.12

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

# 1) Install the Python packages (exact versions from uv.lock, CPU-only torch)
COPY pyproject.toml uv.lock /build/
RUN cd /build \
 && uv export --frozen --no-hashes -o requirements.txt \
 && uv pip install --system --no-cache --python /var/lang/bin/python3.12 \
      --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements.txt \
 && rm -rf /build

# 2) Download the AI models NOW (at build time), so Lambda never downloads anything at runtime
ENV HF_HOME=/opt/models/hf \
    DOCLING_ARTIFACTS_PATH=/opt/models/docling
RUN docling-tools models download layout tableformer rapidocr -o /opt/models/docling \
 && python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('sentence-transformers/all-MiniLM-L6-v2')" \
 && chmod -R a+rX /opt/models

# 3) Runtime settings: no internet for models; Lambda can only write to /tmp
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.cache/matplotlib

# 4) Check the models really work offline (fails the build if something is missing)
COPY samples/small.pdf /check/small.pdf
RUN python -c "from docling.document_converter import DocumentConverter; \
print('build check:', len(DocumentConverter().convert('/check/small.pdf').document.texts), 'texts')" \
 && rm -rf /check /tmp/.cache

# 5) Our code (last, because it changes most often -> faster rebuilds)
COPY s3_main.py db.py handler.py errors.py mapreduce.py ${LAMBDA_TASK_ROOT}/

# Lambda calls handler.py -> handler(event, context)
CMD ["handler.handler"]
