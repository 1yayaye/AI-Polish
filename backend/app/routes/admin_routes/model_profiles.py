from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ModelProfile
from app.schemas import ModelProfileCreate, ModelProfileResponse, ModelProfileUpdate

from .dependencies import get_admin_from_token

router = APIRouter()


@router.get("/model-profiles", response_model=List[ModelProfileResponse])
async def list_model_profiles(
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> List[ModelProfile]:
    return db.query(ModelProfile).order_by(ModelProfile.sort_order, ModelProfile.id).all()


@router.post("/model-profiles", response_model=ModelProfileResponse)
async def create_model_profile(
    data: ModelProfileCreate,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> ModelProfile:
    profile = ModelProfile(**data.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/model-profiles/{profile_id}", response_model=ModelProfileResponse)
async def update_model_profile(
    profile_id: int,
    data: ModelProfileUpdate,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> ModelProfile:
    profile = db.query(ModelProfile).filter(ModelProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Model profile not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(profile, key, value)

    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/model-profiles/{profile_id}")
async def delete_model_profile(
    profile_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    profile = db.query(ModelProfile).filter(ModelProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Model profile not found")

    db.delete(profile)
    db.commit()
    return {"message": "Model profile deleted"}


@router.post("/model-profiles/{profile_id}/set-default")
async def set_default_model_profile(
    profile_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    profile = db.query(ModelProfile).filter(ModelProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Model profile not found")

    # Clear all defaults
    db.query(ModelProfile).update({ModelProfile.is_default: False})
    # Set this one as default
    profile.is_default = True
    db.commit()
    return {"message": f"'{profile.name}' set as default"}
