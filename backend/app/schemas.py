from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime


class UserCreate(BaseModel):
    """创建用户"""
    card_key: str
    access_link: str


class UserResponse(BaseModel):
    """用户响应"""
    id: int
    card_key: str
    access_link: str
    is_active: bool
    created_at: datetime
    last_used: Optional[datetime] = None
    usage_limit: int
    usage_count: int
    workspace_balance_cents: int = 0
    workspace_total_spent_cents: int = 0
    
    class Config:
        from_attributes = True


class ModelConfig(BaseModel):
    """模型配置"""
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class ModelProfileCreate(BaseModel):
    """创建模型 Profile"""
    name: str
    model: str
    api_key: str
    base_url: str
    is_active: bool = True
    is_default: bool = False
    sort_order: int = 0


class ModelProfileUpdate(BaseModel):
    """更新模型 Profile"""
    name: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None


class ModelProfileResponse(BaseModel):
    """模型 Profile 响应（含 api_key，供 admin 使用）"""
    id: int
    name: str
    model: str
    api_key: str
    base_url: str
    is_active: bool
    is_default: bool
    sort_order: int
    created_at: datetime

    class Config:
        from_attributes = True


class ModelProfilePublic(BaseModel):
    """模型 Profile 公开响应（脱敏 api_key，供用户选择）"""
    id: int
    name: str
    model: str
    base_url: str
    is_default: bool
    sort_order: int

    class Config:
        from_attributes = True


class OptimizationCreate(BaseModel):
    """创建优化任务"""
    model_config = ConfigDict(protected_namespaces=())

    original_text: str
    processing_mode: str = Field(default='paper_polish_enhance',
                                  description='处理模式: paper_polish, paper_enhance, paper_polish_enhance, emotion_polish')
    model_profile_id: Optional[int] = None


class SegmentResponse(BaseModel):
    """段落响应"""
    id: int
    segment_index: int
    stage: str
    original_text: str
    polished_text: Optional[str] = None
    enhanced_text: Optional[str] = None
    status: str
    is_title: bool
    created_at: datetime
    completed_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class SessionResponse(BaseModel):
    """会话响应"""
    id: int
    session_id: str
    current_stage: str
    status: str
    progress: float
    current_position: int
    total_segments: int
    original_char_count: int = 0
    preview_text: Optional[str] = None
    error_message: Optional[str] = None
    processing_mode: str = 'paper_polish_enhance'
    billing_char_count: Optional[int] = None
    billing_amount_cents: Optional[int] = None
    billing_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class SessionDetailResponse(SessionResponse):
    """会话详细响应"""
    segments: List[SegmentResponse] = []


class QueueStatusResponse(BaseModel):
    """队列状态响应"""
    current_users: int
    max_users: int
    queue_length: int
    your_position: Optional[int] = None
    estimated_wait_time: Optional[int] = None  # 秒


class ProgressUpdate(BaseModel):
    """进度更新"""
    session_id: str
    status: str
    progress: float
    current_position: int
    total_segments: int
    current_stage: str
    error_message: Optional[str] = None


class ChangeLogResponse(BaseModel):
    """变更对照响应"""
    id: int
    segment_index: int
    stage: str
    before_text: str
    after_text: str
    changes_detail: Optional[Dict[str, Any]] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class ExportConfirmation(BaseModel):
    """导出确认"""
    session_id: str
    acknowledge_academic_integrity: bool
    export_format: str = Field(..., pattern="^(txt|docx|pdf)$")


class CardKeyGenerate(BaseModel):
    """生成卡密"""
    count: int = Field(1, ge=1, le=100)
    prefix: Optional[str] = None


class CardKeyResponse(BaseModel):
    """卡密响应"""
    card_key: str
    access_link: str
    created_at: datetime


class UserUsageUpdate(BaseModel):
    """更新用户使用限制"""
    usage_limit: int = Field(..., ge=0)  # 0 表示无限制
    reset_usage_count: bool = False


class WorkspaceBillingResponse(BaseModel):
    """Workspace billing info for the current card."""
    workspace_balance_cents: int
    workspace_total_spent_cents: int
    workspace_price_per_10k_cents: int
    price_configured: bool


class DatabaseUpdateRequest(BaseModel):
    """数据库记录更新请求"""
    data: Dict[str, Any]


class PromptCreate(BaseModel):
    """创建提示词"""
    name: str
    stage: str = Field(..., pattern="^(polish|enhance)$")
    content: str
    is_default: bool = False


class PromptUpdate(BaseModel):
    """更新提示词"""
    name: Optional[str] = None
    content: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


class PromptResponse(BaseModel):
    """提示词响应"""
    id: int
    user_id: Optional[int] = None
    name: str
    stage: str
    content: str
    is_default: bool
    is_system: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
