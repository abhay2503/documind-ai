"""
Lambda entry point:

    SQS {jobId, documentId} -> PostgreSQL (s3_key, style) -> s3_main.process -> PostgreSQL (result, status)

What SQS gives Lambda:
    {"Records": [{"messageId": "...", "body": "{\"jobId\": \"job_...\", \"documentId\": \"doc_...\"}",
                  "attributes": {"ApproximateReceiveCount": "1"}}]}

Test locally (LocalStack S3 + the Node API's local PostgreSQL):
    uv run python handler.py job_<32 hex> doc_<32 hex>     # both printed by upload_to_s3.py
"""

import json
import sys

from pydantic import BaseModel, Field

import db
from s3_main import process   # models load once here, reused on warm Lambda calls

MAX_ATTEMPTS = 3   # must match the queue's maxReceiveCount (after 3 tries SQS moves it to the DLQ)


# ---------------------------------------------------------------
# The message the Node API puts on the queue
# ---------------------------------------------------------------
class SummaryJobMessage(BaseModel):
    jobId: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    documentId: str = Field(pattern=r"^doc_[0-9a-f]{32}$")


def parse_message(body):
    # Raises ValidationError if the message is not valid JSON or the IDs look wrong
    return SummaryJobMessage.model_validate_json(body)


# ---------------------------------------------------------------
# One message = one job
# Return normally  -> message is done (deleted from the queue)
# Raise an error   -> message comes back later for a retry
# ---------------------------------------------------------------
def handle_record(record):
    message = parse_message(record["body"])
    job_id = message.jobId

    # 1. Read the job from PostgreSQL
    job = db.get_job(job_id)
    if job is None or job["document_id"] != message.documentId:
        raise ValueError(f"Job {job_id} not found for document {message.documentId}")

    if job["status"] in ("completed", "failed"):
        print(f"[{job_id}] already {job['status']}, nothing to do")   # duplicate message
        return

    # 2. queued -> processing (only one worker can win this)
    if not db.start_job(job_id):
        print(f"[{job_id}] already being processed by another worker")
        return

    print(f"[{job_id}] processing {job['s3_key']} ({job['summary_style']})")

    # 3. Do the work, then save the result
    try:
        summary = process(job["s3_key"], job["summary_style"])
    except Exception as error:
        print(f"[{job_id}] error: {type(error).__name__}: {error}")

        attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
        if attempt >= MAX_ATTEMPTS:
            # Last try: tell the user it failed, and stop retrying
            db.fail_job(job_id, "PROCESSING_FAILED",
                        "We could not generate a summary for this document. Please try again.")
            print(f"[{job_id}] failed after {attempt} attempts")
            return

        # Not the last try: put the job back to 'queued' so the retry can start it again
        db.release_job(job_id)
        raise

    db.complete_job(job_id, summary.model_dump())
    print(f"[{job_id}] completed")


# ---------------------------------------------------------------
# Lambda calls this function
# ---------------------------------------------------------------
def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            handle_record(record)
        except Exception as error:
            # Any error here (including "PostgreSQL is down") -> SQS retries this message.
            # We never lose a message just because the database was unavailable.
            print(f"[{record['messageId']}] FAILED: {type(error).__name__}: {error}")
            failures.append({"itemIdentifier": record["messageId"]})

    # Messages NOT listed here are deleted from the queue (= done)
    return {"batchItemFailures": failures}


# ---------------------------------------------------------------
# Local test: build a fake SQS event, like Lambda would receive
# ---------------------------------------------------------------
if __name__ == "__main__":
    fake_event = {
        "Records": [{
            "messageId": "local-test-1",
            "body": json.dumps({"jobId": sys.argv[1], "documentId": sys.argv[2]}),
            "attributes": {"ApproximateReceiveCount": "1"},
        }]
    }
    print(handler(fake_event, None))
