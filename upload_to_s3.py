"""Helper for local testing: puts a file into S3 (LocalStack), like the Node API does after an upload.

    uv run python upload_to_s3.py ../ai/tests/fixtures/generated/small.pdf
"""

import os
import sys

import boto3
from dotenv import load_dotenv

load_dotenv()

s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
    endpoint_url=os.environ.get("LOCAL_AWS_ENDPOINT") or None,
)

local_file = sys.argv[1]
key = "documents/test/" + os.path.basename(local_file)

s3.upload_file(local_file, os.environ["S3_BUCKET_NAME"], key)
print("Uploaded. S3 key:", key)
