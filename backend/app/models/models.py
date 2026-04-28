from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base
from app.config import settings


class User(Base):
    """用户表"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    card_key = Column(String(255), unique=True, index=True, nullable=False)
    access_link = Column(String(255), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, nullable=True)
    usage_limit = Column(Integer, default=settings.DEFAULT_USAGE_LIMIT)
    usage_count = Column(Integer, default=0)
    workspace_balance_cents = Column(Integer, default=0)
    workspace_total_spent_cents = Column(Integer, default=0)
    
    # 关系
    sessions = relationship("OptimizationSession", back_populates="user")
    prompts = relationship("CustomPrompt", back_populates="user")
    saved_specs = relationship("SavedSpec", back_populates="user", cascade="all, delete-orphan")
    billing_transactions = relationship("BillingTransaction", back_populates="user", cascade="all, delete-orphan")


class CustomPrompt(Base):
    """自定义提示词表"""
    __tablename__ = "custom_prompts"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=False)
    stage = Column(String(50), nullable=False)  # 'polish' 或 'enhance'
    content = Column(Text, nullable=False)
    is_default = Column(Boolean, default=False)
    is_system = Column(Boolean, default=False)  # 系统预设提示词
    is_active = Column(Boolean, default=True)  # 是否启用
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="prompts")


class OptimizationSession(Base):
    """优化会话表"""
    __tablename__ = "optimization_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    session_id = Column(String(255), unique=True, index=True)
    original_text = Column(Text)
    current_stage = Column(String(50))  # 'polish' 或 'enhance'
    status = Column(String(50), index=True)  # 'queued', 'processing', 'completed', 'failed'
    progress = Column(Float, default=0.0)
    current_position = Column(Integer, default=0)  # 当前处理的段落位置
    total_segments = Column(Integer, default=0)  # 总段落数
    error_message = Column(Text, nullable=True)
    failed_segment_index = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # 模型配置
    polish_model = Column(String(100), nullable=True)
    polish_api_key = Column(String(255), nullable=True)
    polish_base_url = Column(String(255), nullable=True)
    enhance_model = Column(String(100), nullable=True)
    enhance_api_key = Column(String(255), nullable=True)
    enhance_base_url = Column(String(255), nullable=True)
    emotion_model = Column(String(100), nullable=True)
    emotion_api_key = Column(String(255), nullable=True)
    emotion_base_url = Column(String(255), nullable=True)
    
    # 处理模式: 'paper_polish', 'paper_enhance', 'paper_polish_enhance', 'emotion_polish'
    processing_mode = Column(String(50), default='paper_polish_enhance')

    billing_char_count = Column(Integer, nullable=True)
    billing_amount_cents = Column(Integer, nullable=True)
    billing_price_per_10k_cents = Column(Integer, nullable=True)
    billing_status = Column(String(50), nullable=True)
    billing_refunded_at = Column(DateTime, nullable=True)
    
    # 关系
    user = relationship("User", back_populates="sessions")
    segments = relationship("OptimizationSegment", back_populates="session", cascade="all, delete-orphan")
    history = relationship("SessionHistory", back_populates="session", cascade="all, delete-orphan")
    billing_transactions = relationship("BillingTransaction", back_populates="session", cascade="all, delete-orphan")

    @property
    def completed_segments(self) -> int:
        """Return how many segments finished successfully."""
        return sum(1 for segment in self.segments if segment.status == "completed")


class OptimizationSegment(Base):
    """优化段落表"""
    __tablename__ = "optimization_segments"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("optimization_sessions.id"), index=True)
    segment_index = Column(Integer, index=True)  # 段落序号
    stage = Column(String(50))  # 'polish' 或 'enhance'
    original_text = Column(Text)
    polished_text = Column(Text, nullable=True)
    enhanced_text = Column(Text, nullable=True)
    status = Column(String(50), index=True)  # 'pending', 'processing', 'completed', 'failed'
    is_title = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # 关系
    session = relationship("OptimizationSession", back_populates="segments")


class SessionHistory(Base):
    """会话历史表 (用于AI上下文)"""
    __tablename__ = "session_history"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("optimization_sessions.id"))
    stage = Column(String(50))  # 'polish' 或 'enhance'
    history_data = Column(Text)  # JSON格式的历史会话
    is_compressed = Column(Boolean, default=False)
    character_count = Column(Integer, default=0)  # 汉字数量
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    session = relationship("OptimizationSession", back_populates="history")


class ChangeLog(Base):
    """变更对照记录表 (用于学术审计)"""
    __tablename__ = "change_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("optimization_sessions.id"), index=True)
    segment_index = Column(Integer, index=True)
    stage = Column(String(50), index=True)  # 'polish' 或 'enhance'
    before_text = Column(Text)
    after_text = Column(Text)
    changes_detail = Column(Text)  # JSON格式的详细变更
    created_at = Column(DateTime, default=datetime.utcnow)


class QueueStatus(Base):
    """队列状态表"""
    __tablename__ = "queue_status"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(255), unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    position = Column(Integer)  # 队列位置
    status = Column(String(50))  # 'queued' 或 'processing'
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)


class BillingTransaction(Base):
    """Workspace balance ledger."""
    __tablename__ = "billing_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    optimization_session_id = Column(Integer, ForeignKey("optimization_sessions.id"), nullable=True, index=True)
    transaction_type = Column(String(50), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    balance_after_cents = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="billing_transactions")
    session = relationship("OptimizationSession", back_populates="billing_transactions")


class SystemSetting(Base):
    """系统设置表"""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelProfile(Base):
    """模型配置表 - 管理员预配的可用模型"""
    __tablename__ = "model_profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)          # 显示名: "Gemini 2.5 Pro"
    model = Column(String(100), nullable=False)          # 模型ID: "gemini-2.5-pro"
    api_key = Column(String(255), nullable=False)
    base_url = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)            # 是否启用
    is_default = Column(Boolean, default=False)          # 是否默认选中
    sort_order = Column(Integer, default=0)              # 排序
    created_at = Column(DateTime, default=datetime.utcnow)


class SavedSpec(Base):
    """用户保存的排版规范表"""
    __tablename__ = "saved_specs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    spec_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    user = relationship("User", back_populates="saved_specs")
