# DocuMind AI: document summarizer

| File | What it does |
|---|---|
| `main.py` | Simplest version: local PDF → Docling → HybridChunker → LLM → summary |
| `s3_main.py` | Same pipeline, but the file comes from S3 (simple production style) |
| `handler.py` | Lambda entry point: SQS message → find file in S3 → summarize |
| `upload_to_s3.py` | Puts a test file into S3 (LocalStack), like the Node API does |

## Setup

```bash
cd ai
cp .env.example .env          # put your Groq key in .env
uv sync
```

## 1. Local file

```bash
uv run python main.py samples/small.pdf
```

## 2. From S3

Start local MySQL + S3 first (Docker): `docker compose up -d --wait`

```bash
uv run python upload_to_s3.py samples/small.pdf
# -> S3 key: documents/doc_<id>/small.pdf, documentId, jobId (a 'queued' row in MySQL)

uv run python s3_main.py documents/doc_<id>/small.pdf
```

## 3. Like Lambda (SQS message + MySQL)

```bash
uv run python handler.py job_<id> doc_<id>
# builds a fake SQS event {"jobId", "documentId"}, calls handler(event, context),
# and saves the result in summary_jobs (status: queued -> processing -> completed)
```

## 4. Like real Lambda (Docker)

```bash
docker build -t documind-ai .                                   # once (10-15 min, ~5 GB)

# start the "Lambda" (uses your .env; --network host so it can reach local MySQL + S3)
docker run --rm --network host --env-file .env documind-ai

# in a second terminal: create a job, then send the SQS event to the Lambda
uv run python upload_to_s3.py samples/small.pdf
curl -s localhost:8080/2015-03-31/functions/function/invocations -d '{
  "Records": [{"messageId": "m1", "attributes": {"ApproximateReceiveCount": "1"},
               "body": "{\"jobId\": \"job_<id>\", \"documentId\": \"doc_<id>\"}"}]}'
# -> {"batchItemFailures": []}  and the job is 'completed' in MySQL
```
