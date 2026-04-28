"""Formatting spec endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import SavedSpec, User
from app.services.resource_guard import ensure_memory_available

from ..services import (
    ai_generate_spec,
    builtin_specs,
    export_spec_to_json,
    get_spec_schema,
    validate_custom_spec,
)
from .dependencies import (
    get_ai_service,
    get_current_user,
    release_usage,
    reserve_usage,
)
from .schemas import (
    GenerateSpecRequest,
    SaveSpecRequest,
    SavedSpecListResponse,
    SavedSpecResponse,
    SpecListResponse,
    SpecSchemaResponse,
)

router = APIRouter()


@router.get("/specs", response_model=SpecListResponse)
async def list_specs():
    """List available built-in formatting specs."""
    return SpecListResponse(specs=list(builtin_specs().keys()))


@router.get("/specs/schema", response_model=SpecSchemaResponse)
async def get_schema():
    """Get JSON schema for custom spec validation."""
    return SpecSchemaResponse(schema=get_spec_schema())


@router.post("/specs/validate")
async def validate_spec(spec_json: str):
    """Validate a custom spec JSON."""
    try:
        spec = validate_custom_spec(spec_json)
        return {"valid": True, "spec_name": spec.meta.get("name", "Custom")}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/specs/generate")
async def generate_spec(
    request: GenerateSpecRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a formatting spec from user requirements using AI."""
    ensure_memory_available("word formatter spec generation")
    reserve_usage(user, db)
    db.commit()

    print(f"\n[WORD-FORMATTER] ========== AI 规范生成请求 ==========", flush=True)
    print(f"[WORD-FORMATTER] 用户ID: {user.id}", flush=True)
    print(f"[WORD-FORMATTER] 需求长度: {len(request.requirements)} 字符", flush=True)

    try:
        ai_service = get_ai_service()
        spec = await ai_generate_spec(request.requirements, ai_service)


        print(f"[WORD-FORMATTER] ✅ 规范生成成功: {spec.meta.get('name', 'AI_Generated')}", flush=True)
        print(f"[WORD-FORMATTER] ===========================================\n", flush=True)

        return {
            "success": True,
            "spec_json": export_spec_to_json(spec),
            "spec_name": spec.meta.get("name", "AI_Generated"),
        }
    except Exception as e:
        release_usage(user, db)
        db.commit()
        print(f"[WORD-FORMATTER] ❌ 规范生成失败: {e}", flush=True)
        print(f"[WORD-FORMATTER] ===========================================\n", flush=True)
        raise HTTPException(status_code=500, detail=f"生成规范失败: {str(e)}")


@router.post("/specs/save", response_model=SavedSpecResponse)
async def save_spec(
    request: SaveSpecRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save a user's custom spec."""

    # Validate spec JSON
    try:
        validate_custom_spec(request.spec_json)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"规范 JSON 无效: {e}")

    # Check if name already exists for this user
    existing = db.query(SavedSpec).filter(
        SavedSpec.user_id == user.id,
        SavedSpec.name == request.name
    ).first()

    if existing:
        # Update existing spec
        existing.spec_json = request.spec_json
        existing.description = request.description
        db.commit()
        db.refresh(existing)

        print(f"[WORD-FORMATTER] 更新规范 user_id={user.id} name={request.name}", flush=True)

        return SavedSpecResponse(
            id=existing.id,
            name=existing.name,
            description=existing.description,
            spec_json=existing.spec_json,
            created_at=existing.created_at.isoformat(),
            updated_at=existing.updated_at.isoformat(),
        )

    # Create new spec
    new_spec = SavedSpec(
        user_id=user.id,
        name=request.name,
        description=request.description,
        spec_json=request.spec_json,
    )
    db.add(new_spec)
    db.commit()
    db.refresh(new_spec)

    print(f"[WORD-FORMATTER] 保存规范 user_id={user.id} name={request.name} id={new_spec.id}", flush=True)

    return SavedSpecResponse(
        id=new_spec.id,
        name=new_spec.name,
        description=new_spec.description,
        spec_json=new_spec.spec_json,
        created_at=new_spec.created_at.isoformat(),
        updated_at=new_spec.updated_at.isoformat(),
    )


@router.get("/specs/saved", response_model=SavedSpecListResponse)
async def list_saved_specs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List user's saved specs."""

    specs = db.query(SavedSpec).filter(
        SavedSpec.user_id == user.id
    ).order_by(SavedSpec.updated_at.desc()).all()

    return SavedSpecListResponse(
        specs=[
            SavedSpecResponse(
                id=s.id,
                name=s.name,
                description=s.description,
                spec_json=s.spec_json,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
            )
            for s in specs
        ]
    )


@router.get("/specs/saved/{spec_id}", response_model=SavedSpecResponse)
async def get_saved_spec(
    spec_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific saved spec."""

    spec = db.query(SavedSpec).filter(
        SavedSpec.id == spec_id,
        SavedSpec.user_id == user.id
    ).first()

    if not spec:
        raise HTTPException(status_code=404, detail="规范不存在")

    return SavedSpecResponse(
        id=spec.id,
        name=spec.name,
        description=spec.description,
        spec_json=spec.spec_json,
        created_at=spec.created_at.isoformat(),
        updated_at=spec.updated_at.isoformat(),
    )


@router.delete("/specs/saved/{spec_id}")
async def delete_saved_spec(
    spec_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a saved spec."""

    spec = db.query(SavedSpec).filter(
        SavedSpec.id == spec_id,
        SavedSpec.user_id == user.id
    ).first()

    if not spec:
        raise HTTPException(status_code=404, detail="规范不存在")

    db.delete(spec)
    db.commit()

    print(f"[WORD-FORMATTER] 删除规范 user_id={user.id} spec_id={spec_id}", flush=True)

    return {"message": "规范已删除"}


__all__ = [
    "delete_saved_spec",
    "generate_spec",
    "get_saved_spec",
    "get_schema",
    "list_saved_specs",
    "list_specs",
    "router",
    "save_spec",
    "validate_spec",
]
