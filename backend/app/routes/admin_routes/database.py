from typing import Any, Dict, List, Type

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import (
    BillingTransaction,
    ChangeLog,
    OptimizationSegment,
    OptimizationSession,
    SessionHistory,
    SystemSetting,
    User,
)
from app.schemas import DatabaseUpdateRequest

from .dependencies import get_admin_from_token

router = APIRouter()


ALLOWED_TABLES: Dict[str, Type] = {
    "users": User,
    "optimization_sessions": OptimizationSession,
    "optimization_segments": OptimizationSegment,
    "session_history": SessionHistory,
    "change_logs": ChangeLog,
    "billing_transactions": BillingTransaction,
    "system_settings": SystemSetting,
}


def _model_to_dict(record: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    mapper = inspect(record).mapper
    for column in mapper.columns:
        data[column.key] = getattr(record, column.key)
    return data


@router.get("/database/tables")
async def list_tables(_: str = Depends(get_admin_from_token)) -> Dict[str, List[str]]:
    return {"tables": list(ALLOWED_TABLES.keys())}


@router.get("/database/{table_name}")
async def fetch_table_records(
    table_name: str,
    skip: int = 0,
    limit: int = 50,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail="表不存在或不允许访问")

    model = ALLOWED_TABLES[table_name]
    page_size = max(min(limit, 200), 1)
    query = db.query(model).offset(max(skip, 0)).limit(page_size)
    records = [_model_to_dict(row) for row in query.all()]
    total = db.query(model).count()
    return {"total": total, "items": records}


@router.put("/database/{table_name}/{record_id}")
async def update_table_record(
    table_name: str,
    record_id: int,
    payload: DatabaseUpdateRequest,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail="表不存在或不允许访问")

    model = ALLOWED_TABLES[table_name]
    record = db.query(model).filter(getattr(model, "id") == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")

    mapper = inspect(model)
    allowed_columns = {column.key for column in mapper.columns if not column.primary_key}

    for key, value in payload.data.items():
        if key in allowed_columns:
            setattr(record, key, value)

    db.commit()
    db.refresh(record)
    return {"message": "记录已更新", "record": _model_to_dict(record)}


@router.delete("/database/{table_name}/{record_id}")
async def delete_table_record(
    table_name: str,
    record_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail="表不存在或不允许访问")

    model = ALLOWED_TABLES[table_name]
    record = db.query(model).filter(getattr(model, "id") == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")

    db.delete(record)
    db.commit()
    return {"message": "记录已删除"}
