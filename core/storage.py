"""Acceso unico al almacenamiento de objetos (S3 o compatible).

Antes cada modulo creaba su propio boto3.client("s3", region_name=...) y
armaba URLs publicas con el dominio de AWS a mano. En Railway el bucket es
S3-compatible pero con otro endpoint, asi que todo pasa por aqui:

- AWS_S3_ENDPOINT_URL: endpoint del proveedor (vacio = AWS S3).
- AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY: boto3 las lee solas del entorno.
- MEDIA_PUBLIC_BASE_URL: base publica para mostrar imagenes/comprobantes
  (CDN o bucket con lectura publica). Vacio = URL derivada del endpoint.
"""

import boto3
from botocore.config import Config
from django.conf import settings


def get_s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_S3_REGION,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL or None,
        # Firma v4 con URLs estilo path: es lo que aceptan todos los
        # proveedores S3-compatibles, no solo AWS.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.AWS_S3_ADDRESSING_STYLE},
        ),
    )


def presigned_upload_url(key: str, content_type: str, expires_in: int) -> str:
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def presigned_download_url(key: str, expires_in: int) -> str:
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )


def put_object(key: str, body: bytes) -> None:
    get_s3_client().put_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key, Body=body
    )


def public_object_url(key: str) -> str:
    """URL con la que el frontend muestra un objeto ya subido."""
    if settings.MEDIA_PUBLIC_BASE_URL:
        return f"{settings.MEDIA_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    if settings.AWS_S3_ENDPOINT_URL:
        return f"{settings.AWS_S3_ENDPOINT_URL.rstrip('/')}/{bucket}/{key}"
    return f"https://{bucket}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{key}"
