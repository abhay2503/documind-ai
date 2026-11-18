#!/usr/bin/env bash
# Deploy the AI worker to AWS Lambda. Safe to run again: it creates what is missing and updates the rest.
#
#   aws sso login --profile document-ai
#   ./deploy.sh
#
# Needs: the Node API already deployed (it owns the S3 bucket and the SQS queue),
#        GROQ_API_KEY in ai/.env, and the Aiven DB_* values in ../api/.env.prod.
# Secrets are read from those files and sent only to Lambda; they are never printed or committed.
set -euo pipefail
cd "$(dirname "$0")"

PROFILE=document-ai
REGION=ap-south-1
export AWS_PROFILE=$PROFILE AWS_REGION=$REGION AWS_PAGER=""

FUNCTION=documind-ai-worker
ROLE=documind-ai-worker-role
REPO=documind-ai
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# The bucket and queue come from the Node API stack
output() { aws cloudformation describe-stacks --stack-name documind-api-prod \
             --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET=$(output DocumentsBucketName)
QUEUE_ARN=$(output SummaryQueueArn)
echo "bucket: $BUCKET"
echo "queue:  $QUEUE_ARN"

# ---------------------------------------------------------------
# 1. ECR repo + push the image
# ---------------------------------------------------------------
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
IMAGE=$REGISTRY/$REPO:$(git rev-parse --short HEAD)

if ! aws ecr describe-repositories --repository-names $REPO >/dev/null 2>&1; then
  aws ecr create-repository --repository-name $REPO >/dev/null
  # keep only the 2 newest images (each is ~5 GB, storage costs money)
  aws ecr put-lifecycle-policy --repository-name $REPO --lifecycle-policy-text \
    '{"rules":[{"rulePriority":1,"selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":2},"action":{"type":"expire"}}]}' >/dev/null
  echo "created ECR repo $REPO"
fi

aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY >/dev/null
# --provenance/--sbom=false: plain image without extra metadata, which Lambda can refuse
docker buildx build --platform linux/amd64 --provenance=false --sbom=false -t $IMAGE .
docker push $IMAGE

# ---------------------------------------------------------------
# 2. IAM role: write logs, read uploaded documents, read/delete queue messages. Nothing else.
# ---------------------------------------------------------------
if ! aws iam get-role --role-name $ROLE >/dev/null 2>&1; then
  aws iam create-role --role-name $ROLE --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name $ROLE \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "created IAM role $ROLE"
  sleep 10   # a new role takes a few seconds before Lambda can use it
fi
aws iam put-role-policy --role-name $ROLE --policy-name worker-access --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": \"s3:GetObject\", \"Resource\": \"arn:aws:s3:::$BUCKET/documents/*\"},
    {\"Effect\": \"Allow\",
     \"Action\": [\"sqs:ReceiveMessage\", \"sqs:DeleteMessage\", \"sqs:GetQueueAttributes\", \"sqs:ChangeMessageVisibility\"],
     \"Resource\": \"$QUEUE_ARN\"},
    {\"Effect\": \"Allow\", \"Action\": \"bedrock:InvokeModel\",
     \"Resource\": [\"arn:aws:bedrock:$REGION:$ACCOUNT:inference-profile/*anthropic.claude-*\",
                  \"arn:aws:bedrock:*::foundation-model/anthropic.claude-*\",
                  \"arn:aws:bedrock:::foundation-model/anthropic.claude-*\"]}
  ]}"
ROLE_ARN=arn:aws:iam::$ACCOUNT:role/$ROLE

# ---------------------------------------------------------------
# 3. Lambda settings (environment variables) - written to a private temp file, deleted at the end
# ---------------------------------------------------------------
ENV_FILE=$(mktemp)
trap 'rm -f "$ENV_FILE"' EXIT
BUCKET=$BUCKET uv run --no-project --with python-dotenv python - "$ENV_FILE" <<'EOF'
import json, os, sys
from dotenv import dotenv_values
ai, api = dotenv_values(".env"), dotenv_values("../api/.env.prod")
variables = {
    "S3_BUCKET_NAME": os.environ["BUCKET"],
    "GROQ_API_KEY": ai["GROQ_API_KEY"],
    "GROQ_MODEL": ai.get("GROQ_MODEL") or "openai/gpt-oss-20b",
    "BEDROCK_MODEL_ID": ai.get("BEDROCK_MODEL_ID") or "",   # empty = Groq only
    "DB_HOST": api["DB_HOST"], "DB_PORT": api["DB_PORT"], "DB_USER": api["DB_USER"],
    "DB_PASSWORD": api["DB_PASSWORD"], "DB_NAME": api["DB_NAME"], "DB_SSLMODE": "require",
}
missing = [k for k, v in variables.items() if not v and k != "BEDROCK_MODEL_ID"]
if missing:
    sys.exit(f"missing values: {missing}")
json.dump({"Variables": variables}, open(sys.argv[1], "w"))
EOF

# ---------------------------------------------------------------
# 4. Lambda function (3 GB memory for Docling, 15 min = Lambda maximum)
# ---------------------------------------------------------------
SETTINGS=(--role $ROLE_ARN --memory-size 3008 --timeout 900 --ephemeral-storage Size=2048
          --environment file://$ENV_FILE)

if aws lambda get-function --function-name $FUNCTION >/dev/null 2>&1; then
  aws lambda update-function-code --function-name $FUNCTION --image-uri $IMAGE >/dev/null
  aws lambda wait function-updated-v2 --function-name $FUNCTION
  aws lambda update-function-configuration --function-name $FUNCTION "${SETTINGS[@]}" >/dev/null
  echo "updated Lambda $FUNCTION"
else
  # keep logs 14 days only (cost)
  aws logs create-log-group --log-group-name /aws/lambda/$FUNCTION 2>/dev/null || true
  aws logs put-retention-policy --log-group-name /aws/lambda/$FUNCTION --retention-in-days 14
  aws lambda create-function --function-name $FUNCTION --package-type Image \
    --code ImageUri=$IMAGE --architectures x86_64 "${SETTINGS[@]}" >/dev/null
  echo "created Lambda $FUNCTION"
fi
aws lambda wait function-updated-v2 --function-name $FUNCTION

# ---------------------------------------------------------------
# 5. SQS -> Lambda: 1 message at a time, failed messages retried, max 2 Lambdas at once
# ---------------------------------------------------------------
if [ -z "$(aws lambda list-event-source-mappings --function-name $FUNCTION --event-source-arn $QUEUE_ARN \
             --query 'EventSourceMappings[0].UUID' --output text | grep -v None)" ]; then
  aws lambda create-event-source-mapping --function-name $FUNCTION --event-source-arn $QUEUE_ARN \
    --batch-size 1 --function-response-types ReportBatchItemFailures \
    --scaling-config MaximumConcurrency=2 >/dev/null
  echo "connected SQS queue -> $FUNCTION"
fi

echo "done: $IMAGE"
