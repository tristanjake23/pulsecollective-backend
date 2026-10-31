"""Verified administrator portrait uploads; decoded and re-encoded without metadata."""

from io import BytesIO
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Request, HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from ..auth import current_user, require, ADMIN
from ..db import connection, one
from ..config import settings
from ..limits import throttle

router = APIRouter(tags=["Doctor portraits"])
Image.MAX_IMAGE_PIXELS = 16000000


def clean_portrait(raw):
    if not raw or len(raw) > 2097152:
        raise HTTPException(413, "Choose an image smaller than 2 MB.")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError()
            if image.width * image.height > 16000000:
                raise ValueError()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((800, 800))
            out = BytesIO()
            image.save(out, "WEBP", quality=85)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(
            422, "Upload a valid JPG, PNG or WebP portrait (maximum 16 megapixels)."
        ) from None


@router.post("/doctors/{row_id}/photo")
async def upload(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    throttle(request, "doctor-photo", 10, subject=str(user.id))
    # Authorize before reading/decoding any file or making an external call.
    with connection(request, user, readonly=True) as conn:
        one(
            conn,
            "SELECT id FROM public.doctors WHERE id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
    import anyio

    image = await anyio.to_thread.run_sync(clean_portrait, await request.body())
    path = f"{user.clinic_id}/{row_id}/{uuid4()}.webp"

    def store():
        response = request.app.state.http.post(
            settings.supabase_url + "/storage/v1/object/doctor-photos/" + path,
            headers={
                "apikey": settings.supabase_publishable_key,
                "Authorization": "Bearer " + user.token,
                "Content-Type": "image/webp",
            },
            content=image,
        )
        if response.status_code not in (200, 201):
            raise HTTPException(
                503,
                "Portrait storage unavailable. Check the doctor-photo bucket policies.",
            )
        url = settings.supabase_url + "/storage/v1/object/public/doctor-photos/" + path
        with connection(request, user) as conn:
            return one(
                conn,
                "UPDATE public.doctors SET photo_url=%s WHERE id=%s AND clinic_id=%s RETURNING id,photo_url",
                (url, row_id, user.clinic_id),
            )

    return await anyio.to_thread.run_sync(store)
