import os

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from mypy_boto3_s3 import S3Client

# Exercises the minio + createbucket services from
# docker/compose.test(.public).yaml directly -- not audio_fetcher, which
# is still a stand-in SDK whose S3 calls all raise NotImplementedError (see
# packages/audio_fetcher's __init__.py).
pytestmark = pytest.mark.integration

MINIO_ROOT_USER = os.environ.get("MINIO_ROOT_USER", "fina_test")
MINIO_ROOT_PASSWORD = os.environ.get("MINIO_ROOT_PASSWORD", "fina_test")
MINIO_BUCKET_NAME = os.environ.get("MINIO_BUCKET_NAME", "audio-fetcher-dev")
S3_TEST_REGION = "us-east-1"


def _minio_client(endpoint_url: str) -> S3Client:
    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=MINIO_ROOT_USER,
        aws_secret_access_key=MINIO_ROOT_PASSWORD,
        region_name=S3_TEST_REGION,
    )


def test_minio_health_live_returns_200(minio_endpoint_url: str) -> None:
    response = httpx.get(f"{minio_endpoint_url}/minio/health/live", timeout=5.0)
    assert response.status_code == 200


def test_createbucket_created_the_dev_bucket(minio_endpoint_url: str) -> None:
    client = _minio_client(minio_endpoint_url)
    try:
        client.head_bucket(Bucket=MINIO_BUCKET_NAME)
    except ClientError as exc:
        pytest.fail(
            f"Bucket {MINIO_BUCKET_NAME!r} is not reachable at {minio_endpoint_url} "
            f"(did the createbucket service fail?): {exc}"
        )
