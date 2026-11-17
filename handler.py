"""
Lambda entry point:  SQS message -> find the file in S3 -> summarize (s3_main.process)

What SQS gives Lambda:
    {"Records": [{"messageId": "...", "body": "{\"jobId\": \"job_...\", \"documentId\": \"doc_...\"}"}]}

Test locally (no AWS needed, uses LocalStack S3):
    uv run python handler.py doc_<32 hex>      # the documentId printed by upload_to_s3.py
"""

import json
import sys
import uuid

from pydantic import BaseModel, Field

from s3_main import BUCKET, process, s3   # models load once here, reused on warm Lambda calls


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
# documentId -> S3 key
# The Node API stores files at documents/{documentId}/{fileName}.
# (Next step: read the key from MySQL instead of listing S3.)
# ---------------------------------------------------------------
def find_s3_key(document_id):
    response = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"documents/{document_id}/")
    files = response.get("Contents", [])
    if not files:
        raise FileNotFoundError(f"No file in S3 for {document_id}")
    return files[0]["Key"]


# ---------------------------------------------------------------
# One message = one job
# ---------------------------------------------------------------
def handle_record(record):
    message = parse_message(record["body"])
    print(f"[{message.jobId}] started, document {message.documentId}")

    s3_key = find_s3_key(message.documentId)
    summary = process(s3_key, style="concise")      # next step: style comes from MySQL

    print(f"[{message.jobId}] done")
    print(summary.model_dump_json(indent=2))        # next step: save to MySQL instead


# ---------------------------------------------------------------
# Lambda calls this function
# ---------------------------------------------------------------
def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            handle_record(record)
        except Exception as error:
            # Tell SQS "this message failed": it comes back later for a retry,
            # and after 3 failures it goes to the dead-letter queue (DLQ).
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
            "body": json.dumps({"jobId": "job_" + uuid.uuid4().hex, "documentId": sys.argv[1]}),
        }]
    }
    print(handler(fake_event, None))
