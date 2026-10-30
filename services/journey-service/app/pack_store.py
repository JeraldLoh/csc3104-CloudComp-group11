"""Where route packs and step photos are kept: S3 (LocalStack locally).

A pack is the offline file the traveller's phone downloads (contracts/examples/route-pack.json). It is
written to S3 and handed out as a temporary presigned link, so the bucket itself stays private. Step photos
(media/streetview/..., media/uploads/...) live in the same bucket and are handed out the same way.

The link is signed for `public_client`'s endpoint. Locally that matters: the service reaches LocalStack as
http://cloud-localstack:4566, but a browser can only reach http://localhost:4566, and the host is part of the
signature. On real AWS both clients are the same.
"""


class PackUnavailable(Exception):
    """S3 couldn't be reached or refused the request."""


class S3PackStorage:
    def __init__(self, s3_client, bucket: str, public_client=None, url_ttl_s: int = 3600):
        self.client = s3_client
        self._s3 = s3_client
        self._public = public_client or s3_client
        self._bucket = bucket
        self._ttl = url_ttl_s

    @classmethod
    def from_settings(cls, s) -> "S3PackStorage":
        """Clients for the configured endpoint (LocalStack locally, AWS otherwise). Links use the current
        signing method (SigV4; the old one is refused by newer AWS regions) and the bucket in the path, which
        works for LocalStack and AWS alike."""
        import boto3
        from botocore.config import Config
        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        s3 = boto3.client("s3", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url, config=cfg)
        public = boto3.client("s3", region_name=s.aws_region, config=cfg,
                              endpoint_url=s.s3_public_endpoint_url or s.aws_endpoint_url)
        return cls(s3, s.packs_bucket, public, s.pack_url_ttl_s)

    def ping(self) -> None:
        self._s3.head_bucket(Bucket=self._bucket)

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=content_type)
        except (BotoCoreError, ClientError) as e:
            raise PackUnavailable(type(e).__name__) from e

    def exists(self, key: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise PackUnavailable(type(e).__name__) from e
        except BotoCoreError as e:
            raise PackUnavailable(type(e).__name__) from e

    def url(self, key: str, ttl_s: int | None = None) -> str:
        """A link that works for `ttl_s` seconds (default `url_ttl_s`). Signing is local; no request is made."""
        return self._public.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl_s or self._ttl)
