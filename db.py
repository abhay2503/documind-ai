"""
All MySQL code in one place. Same database as the Node API (tables: documents, summary_jobs).

Job status flow:   queued -> processing -> completed   (or -> failed)
"""

import json
import os

import pymysql
from dotenv import load_dotenv

load_dotenv()


def connect():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "documind"),
        cursorclass=pymysql.cursors.DictCursor,   # rows come back as dicts: row["s3_key"]
        autocommit=True,                          # each UPDATE is saved immediately
        connect_timeout=5,                        # DB down? fail fast instead of hanging
    )


def run(sql, params):
    """Run one SQL statement. Returns (rows, number_of_rows_changed)."""
    connection = connect()
    try:
        with connection.cursor() as cursor:
            changed = cursor.execute(sql, params)
            return cursor.fetchall(), changed
    finally:
        connection.close()


# ---------------------------------------------------------------
# READ: the job + its document (s3_key, style, status)
# ---------------------------------------------------------------
def get_job(job_id):
    rows, _ = run(
        """SELECT j.job_id, j.document_id, j.status, j.summary_style, d.s3_key
           FROM summary_jobs j
           JOIN documents d ON d.document_id = j.document_id
           WHERE j.job_id = %s""",
        (job_id,),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------
# queued -> processing
# The WHERE makes this safe: if SQS delivers the same message twice,
# only ONE worker changes the row (changed == 1). The other gets 0 and stops.
#
# Crash rule: Lambda can run at most 15 minutes. So a job that has been 'processing'
# for more than 20 minutes means the Lambda crashed -> allow the retry to take it over.
# ---------------------------------------------------------------
STUCK_AFTER_MINUTES = 20


def start_job(job_id):
    _, changed = run(
        """UPDATE summary_jobs
           SET status = 'processing', started_at = UTC_TIMESTAMP(3), retry_count = retry_count + 1
           WHERE job_id = %s
             AND (status = 'queued'
                  OR (status = 'processing'
                      AND updated_at < UTC_TIMESTAMP(3) - INTERVAL %s MINUTE))""",
        (job_id, STUCK_AFTER_MINUTES),
    )
    return changed == 1


# ---------------------------------------------------------------
# processing -> completed (save the summary JSON)
# ---------------------------------------------------------------
def complete_job(job_id, result):
    run(
        """UPDATE summary_jobs
           SET status = 'completed', result = %s, completed_at = UTC_TIMESTAMP(3),
               error_code = NULL, error_message = NULL
           WHERE job_id = %s AND status = 'processing'""",
        (json.dumps(result), job_id),
    )


# ---------------------------------------------------------------
# processing -> failed (the message is shown to the user, so keep it friendly)
# ---------------------------------------------------------------
def fail_job(job_id, error_code, error_message):
    run(
        """UPDATE summary_jobs
           SET status = 'failed', error_code = %s, error_message = %s, completed_at = UTC_TIMESTAMP(3)
           WHERE job_id = %s AND status = 'processing'""",
        (error_code, error_message, job_id),
    )


# ---------------------------------------------------------------
# processing -> queued (give it back, so the SQS retry can start it again)
# ---------------------------------------------------------------
def release_job(job_id):
    run(
        "UPDATE summary_jobs SET status = 'queued' WHERE job_id = %s AND status = 'processing'",
        (job_id,),
    )
