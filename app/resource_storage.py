"""Short-lived local delivery and an S3-compatible private storage adapter."""
import hashlib
import hmac
import os
import secrets
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import boto3
from botocore.config import Config

ACCESS_SECONDS = 300


class LocalDelivery:
    def __init__(self, root: Path, key: bytes | None = None):
        self.root = root.resolve()
        configured = os.getenv("MUSIYO_RESOURCE_SIGNING_KEY")
        self.key = key or (configured.encode() if configured else secrets.token_bytes(32))
        if len(self.key) < 32:
            raise ValueError("Resource signing key must contain at least 32 bytes")

    def path(self, resource: dict[str, Any]) -> Path | None:
        try:
            path = (self.root / resource["private_path"]).resolve()
            path.relative_to(self.root)
            return path if path.is_file() else None
        except (ValueError, OSError):
            return None

    def signature(self, resource_id: str, expires: int) -> str:
        return hmac.new(self.key, f"resource-v1\n{resource_id}\n{expires}".encode(), hashlib.sha256).hexdigest()

    def valid(self, resource_id: str, expires: int, signature: str, now: float) -> bool:
        return now < expires <= now + ACCESS_SECONDS and hmac.compare_digest(self.signature(resource_id, expires), signature)

    def access(self, resource: dict[str, Any], expires: int) -> str | None:
        if self.path(resource) is None:
            return None
        query = urlencode({"expires": expires, "signature": self.signature(resource["id"], expires)})
        return f"/api/v1/resource-delivery/{quote(resource['id'], safe='')}?{query}"


class S3Storage:
    def __init__(self, client, bucket: str, signer=None):
        self.client, self.bucket, self.signer = client, bucket, signer or client

    @classmethod
    def from_environment(cls):
        endpoint = os.getenv("MUSIYO_S3_ENDPOINT", "")
        key, secret = os.getenv("MUSIYO_S3_ACCESS_KEY"), os.getenv("MUSIYO_S3_SECRET_KEY")
        bucket = os.getenv("MUSIYO_S3_BUCKET", "")
        if not endpoint or not key or not secret or not bucket:
            raise ValueError("S3 endpoint, bucket and credentials must be configured")
        if urlsplit(endpoint).scheme not in ("https", "http"):
            raise ValueError("Invalid S3 endpoint")
        def connect(url):
            if urlsplit(url).scheme not in ("https", "http"):
                raise ValueError("Invalid S3 endpoint")
            return boto3.client("s3", endpoint_url=url,
            aws_access_key_id=key, aws_secret_access_key=secret,
            region_name=os.getenv("MUSIYO_S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                proxies={},
                connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))
        client = connect(endpoint)
        signer = connect(os.getenv("MUSIYO_S3_PUBLIC_ENDPOINT", endpoint))
        return cls(client, bucket, signer)

    @staticmethod
    def object_key(value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or not value:
            raise ValueError("Invalid private object key")
        return value

    def upload(self, path: Path, key: str, mime: str, digest: str) -> None:
        self.client.upload_file(str(path), self.bucket, self.object_key(key), ExtraArgs={
            "ContentType": mime, "CacheControl": "private, max-age=300",
            "Metadata": {"sha256": digest}})

    def upload_local(self, path: Path, key: str, mime: str, digest: str) -> None:
        endpoint = urlsplit(self.client.meta.endpoint_url)
        loopback = endpoint.hostname in ("localhost", "127.0.0.1", "::1")
        compose_service = os.getenv("MUSIYO_LOCAL_COMPOSE") == "1" and endpoint.hostname == "minio" and endpoint.port == 9000
        if not (loopback or compose_service):
            raise ValueError("Automatic package transfer is limited to local MinIO; remote uploads require a separate authorized workflow")
        self.upload(path, key, mime, digest)

    def access(self, resource: dict[str, Any], expires: int) -> str | None:
        key = self.object_key(resource["private_path"])
        # The caller has already checked publication and parent eligibility.
        metadata = self.client.head_object(Bucket=self.bucket, Key=key)
        if resource.get("sha256") and metadata.get("Metadata", {}).get("sha256") != resource["sha256"]:
            return None
        if resource.get("byte_count") is not None and metadata.get("ContentLength") != resource["byte_count"]:
            return None
        return self.signer.generate_presigned_url("get_object", Params={"Bucket": self.bucket,
            "Key": key, "ResponseContentType": resource["mime"], "ResponseCacheControl": "private, max-age=300"},
            ExpiresIn=ACCESS_SECONDS, HttpMethod="GET")

    def configure_cors(self, origin: str) -> None:
        url = urlsplit(origin)
        if url.scheme not in ("https", "http") or not url.netloc or url.path not in ("", "/") or url.query or url.fragment or url.username or "*" in origin:
            raise ValueError("Provide one explicit web origin, without a path or wildcard")
        self.client.put_bucket_cors(Bucket=self.bucket, CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": [origin.rstrip("/")], "AllowedMethods": ["GET", "HEAD"],
            "AllowedHeaders": ["Range"], "ExposeHeaders": ["Content-Length", "Content-Range", "Accept-Ranges"],
            "MaxAgeSeconds": ACCESS_SECONDS}]})


def configured_s3() -> S3Storage | None:
    backend = os.getenv("MUSIYO_STORAGE_BACKEND", "local")
    if backend not in ("local", "s3"):
        raise ValueError("Unknown storage backend")
    return S3Storage.from_environment() if backend == "s3" else None
