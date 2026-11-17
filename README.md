# Learning project: document summarizer

| File | What it does |
|---|---|
| `main.py` | Simplest version: local PDF → Docling → HybridChunker → LLM → summary |
| `s3_main.py` | Same pipeline, but the file comes from S3 (simple production style) |
| `handler.py` | Lambda entry point: SQS message → find file in S3 → summarize |
| `upload_to_s3.py` | Puts a test file into S3 (LocalStack), like the Node API does |

## Setup

```bash
cd learn-phase1
cp .env.example .env          # put your Groq key in .env
uv sync
```

## 1. Local file

```bash
uv run python main.py ../ai/tests/fixtures/generated/small.pdf
```

## 2. From S3

Start LocalStack first (from the API project): `cd ../api && npm run localstack:up`

Also start the Node API's MySQL, and put its DB settings in `.env`.

```bash
uv run python upload_to_s3.py ../ai/tests/fixtures/generated/small.pdf
# -> S3 key: documents/doc_<id>/small.pdf, documentId, jobId (a 'queued' row in MySQL)

uv run python s3_main.py documents/doc_<id>/small.pdf
```

## 3. Like Lambda (SQS message + MySQL)

```bash
uv run python handler.py job_<id> doc_<id>
# builds a fake SQS event {"jobId", "documentId"}, calls handler(event, context),
# and saves the result in summary_jobs (status: queued -> processing -> completed)
```
