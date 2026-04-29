from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.image_storyboard_service import generate_storyboard_images

router = APIRouter(prefix="/api/storyboard", tags=["Storyboard Images"])


class StoryboardRequest(BaseModel):
    text: str
    quantity: int = 15


@router.post("/generate-images")
def generate_images(request: StoryboardRequest):
    if not request.text or len(request.text.strip()) < 20:
        raise HTTPException(status_code=400, detail="Informe uma letra ou texto maior.")
    try:
        return generate_storyboard_images(text=request.text, quantity=request.quantity)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

