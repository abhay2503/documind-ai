"""Helper for local testing: does what the Node API does when a user uploads a file and asks for a summary.

    1. puts the file into S3 (LocalStack)
    2. inserts a row in `documents` and a 'queued' row in `summary_jobs` (local PostgreSQL)

    uv run python upload_to_s3.py samples/small.pdf            # concise
    uv run python upload_to_s3.py samples/small.pdf detailed
"""

import mimetypes
import os
import sys
import uuid

import boto3
from dotenv import load_dotenv

import db

load_dotenv()

s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
    endpoint_url=os.environ.get("LOCAL_AWS_ENDPOINT") or None,
)

local_file = sys.argv[1]
style = sys.argv[2] if len(sys.argv) > 2 else "concise"

file_name = os.path.basename(local_file)
document_id = "doc_" + uuid.uuid4().hex                     # same ID format as the Node API
job_id = "job_" + uuid.uuid4().hex
key = f"documents/{document_id}/{file_name}"

# 1. S3 (locally the bucket may not exist yet: create it once)
bucket = os.environ["S3_BUCKET_NAME"]
if os.environ.get("LOCAL_AWS_ENDPOINT"):
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=bucket,
                         CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_REGION"]})
s3.upload_file(local_file, bucket, key)

# 2. PostgreSQL
db.run(
    """INSERT INTO documents (document_id, file_name, content_type, file_size, s3_key, status, uploaded_at)
       VALUES (%s, %s, %s, %s, %s, 'uploaded', CURRENT_TIMESTAMP(3))""",
    (document_id, file_name, mimetypes.guess_type(file_name)[0] or "application/octet-stream",
     os.path.getsize(local_file), key),
)
db.run(
    "INSERT INTO summary_jobs (job_id, document_id, status, summary_style) VALUES (%s, %s, 'queued', %s)",
    (job_id, document_id, style),
)

print("S3 key:     ", key)
print("documentId: ", document_id)
print("jobId:      ", job_id)
print("\nNext:")
print(f"  uv run python handler.py {job_id} {document_id}")
