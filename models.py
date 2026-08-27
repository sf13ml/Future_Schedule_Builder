from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime

# --- SQLAlchemy Database ORM Models ---
from sqlalchemy import Column, String, Float, Boolean, Integer, ForeignKey, JSON, DateTime
from sqlalchemy.orm import relationship
from database import Base


class WorkerModel(Base):
    __tablename__ = "workers"

    uid = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    talents = Column(JSON, default=list)
    despair = Column(Float, default=0.0)

    # State & Workload Tracking
    historical_hours = Column(Float, default=0.0)
    planned_hours = Column(Float, default=0.0)
    last_job = Column(String, nullable=True)
    last_shift_slot = Column(String, nullable=True)
    last_assigned_end_hour = Column(Float, default=-8.0)
    prefers_night = Column(Boolean, default=False)
    is_voluntary_grinder = Column(Boolean, default=False)

    history = relationship("WorkerHistoryModel", back_populates="worker", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "uid": self.uid,
            "name": self.name,
            "talents": self.talents,
            "despair": self.despair,
            "historical_hours": self.historical_hours,
            "planned_hours": self.planned_hours,
            "total_hours": self.historical_hours + self.planned_hours,
            "last_job": self.last_job,
            "last_shift_slot": self.last_shift_slot,
            "last_assigned_end_hour": self.last_assigned_end_hour,
            "prefers_night": self.prefers_night,
            "is_voluntary_grinder": self.is_voluntary_grinder,
            "previous_weeks_load_scores": [h.hours_worked for h in self.history]
        }


class WorkerHistoryModel(Base):
    __tablename__ = "worker_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    worker_uid = Column(String, ForeignKey("workers.uid", ondelete="CASCADE"), nullable=False)
    week_identifier = Column(String, nullable=False)
    hours_worked = Column(Float, default=0.0)

    worker = relationship("WorkerModel", back_populates="history")


class ScheduleAuditModel(Base):
    __tablename__ = "saved_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    schedule_json = Column(JSON, nullable=False)


# --- Pydantic Validation Schemas ---

class Job(BaseModel):
    name: str
    required_talents: List[str] = Field(default_factory=list)
    forbidden_talents: List[str] = Field(default_factory=list)
    required_number_workers: int = 1
    is_critical: bool = True
    min_rest_override: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict):
        return cls.model_validate(data)

    def to_dict(self):
        return self.model_dump()


class Shift(BaseModel):
    id: str
    slot_label: str = ""
    day: str
    job: Job
    time_slot: str
    time_block: str = "main"  # Carries block context (e.g., 'morning', 'afternoon', 'night')
    hours_duration: float
    abs_start_hour: float
    is_fixed: bool = False
    assigned_worker_uids: List[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict):
        return cls.model_validate(data)

    def to_dict(self):
        return self.model_dump()


class Worker(BaseModel):
    uid: str
    name: str
    talents: List[str] = Field(default_factory=list)
    despair: float = 0.0

    # State & Workload Tracking
    historical_hours: float = 0.0
    planned_hours: float = 0.0
    last_job: Optional[str] = None
    last_shift_slot: Optional[str] = None
    last_assigned_end_hour: float = -8.0
    prefers_night: bool = False
    is_voluntary_grinder: bool = False

    consecutive_days_worked: int = 0
    fatigue_multiplier_bonus: float = 1.0

    unavailable_days: List[str] = Field(default_factory=list)
    blocked_intervals: List[Tuple[float, float]] = Field(default_factory=list)
    previous_weeks_load_scores: List[float] = Field(default_factory=list)

    @property
    def total_hours(self) -> float:
        """Dynamically sums past fixed shifts and new solver-planned shifts."""
        return self.historical_hours + self.planned_hours

    @classmethod
    def from_dict(cls, data: dict):
        return cls.model_validate(data)

    def has_required_talents(self, required: List[str]) -> bool:
        worker_talents = {str(t).strip().lower() for t in self.talents}
        req_talents = {str(t).strip().lower() for t in required}
        return req_talents.issubset(worker_talents)

    def is_eligible_for_job(self, job: Job) -> bool:
        worker_talents = {str(t).strip().lower() for t in self.talents}
        req_talents = {str(t).strip().lower() for t in job.required_talents}
        forbidden_talents = {str(t).strip().lower() for t in job.forbidden_talents}

        has_positives = req_talents.issubset(worker_talents)
        has_no_negatives = not worker_talents.intersection(forbidden_talents)
        return has_positives and has_no_negatives

    def to_dict(self):
        d = self.model_dump()
        d["total_hours"] = self.total_hours
        return d


class PolicyConfig(BaseModel):
    # TODO: Connect this default to employer/tenant settings database model if custom per-client policies are loaded dynamically
    min_rest_hours_default: float = 8.0
    enforce_strict_rest: bool = True
    max_weekly_hours_per_worker: float = 168.0
    enforce_max_hours_cap: bool = False
    enforce_consecutive_day_limit: bool = False
    max_consecutive_days: int = 8

    night_shift_penalty_multiplier: float = 1.5
    night_owl_discount: float = 0.7
    historical_load_scaling_weight: float = 1.0
    turnaround_penalty_cost: int = 50
    tight_turnaround_threshold_hours: float = 8.0

    @classmethod
    def from_dict(cls, data: Optional[dict]):
        if not data:
            return cls()
        return cls.model_validate(data)

    def to_dict(self):
        return self.model_dump()


class AssingmentOutput(BaseModel):
    shift_id: str
    day: str
    time_slot: str
    job_name: str
    abs_start_hour: float
    hours_duration: float
    worker_uid: str
    worker_name: str


AssignmentOutput = AssingmentOutput


class CanonicalAssignment(BaseModel):
    shift_id: str
    day: str
    time_block: str
    time_slot: str
    job_name: str
    worker_uid: str
    worker_name: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalAssignment":
        job_raw = (
            data.get("job_name")
            or data.get("job_group")
            or data.get("role")
            or data.get("job_category")
            or (data.get("job", {}).get("name") if isinstance(data.get("job"), dict) else data.get("job"))
            or "General Shift"
        )

        block_raw = (
            data.get("block_label")
            or data.get("time_block")
            or "Main"
        )

        slot_raw = (
            data.get("time_slot")
            or data.get("shift_hours")
            or ""
        )

        day_raw = data.get("day_name") or data.get("day") or ""

        worker_obj = data.get("worker") if isinstance(data.get("worker"), dict) else {}
        worker_uid_raw = (
            data.get("worker_uid")
            or data.get("worker_id")
            or worker_obj.get("uid")
            or worker_obj.get("id")
            or ""
        )
        worker_name_raw = (
            data.get("worker_name")
            or worker_obj.get("name")
            or (str(data.get("worker")) if data.get("worker") and not isinstance(data.get("worker"), dict) else "")
        )

        shift_id_raw = data.get("id") or data.get("shift_id") or ""

        return cls(
            shift_id=str(shift_id_raw).strip(),
            day=str(day_raw).strip().capitalize(),
            time_block=str(block_raw).strip().lower(),
            time_slot=str(slot_raw).strip(),
            job_name=str(job_raw).strip(),
            worker_uid=str(worker_uid_raw).strip(),
            worker_name=str(worker_name_raw).strip() if str(worker_name_raw) != "None" else ""
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class ScheduleMeta(BaseModel):
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str
    total_assignments: int
    policy_applied: Optional[Dict[str, Any]] = None


class ScheduleResponse(BaseModel):
    meta: ScheduleMeta
    assignments: List[AssingmentOutput]
    worker_summaries: List[Dict[str, Any]]


class LayoutBlueprint(BaseModel):
    layout_type: str = "hierarchical"
    x_axis_field: str = "day_name"
    y_axis_hierarchy: List[str] = Field(
        default_factory=lambda: ["time_block", "job_name"]
    )
    auto_merge_parent_cells: bool = True
    show_gridlines: bool = True
    title: str = "Operational Shift Schedule"

    @classmethod
    def from_dict(cls, data: Optional[dict]):
        if not data:
            return cls()
        return cls.model_validate(data)

    def to_dict(self):
        return self.model_dump()


# --- TOP-LEVEL PAYLOAD SCHEMAS FOR INGESTION & EXPORT ---

class ShiftBlockMeta(BaseModel):
    block_id: str
    name: str
    shift_hours: Optional[str] = ""


class SchedulePayload(BaseModel):
    assignments: List[Dict[str, Any]] = Field(default_factory=list)
    shift_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    metrics: Optional[Dict[str, Any]] = Field(default_factory=dict)
    diagnostics: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    status: Optional[str] = "optimal"

    @model_validator(mode="before")
    @classmethod
    def auto_discover_shift_blocks(cls, data: Any) -> Any:
        """
        Pre-validator that intercepts raw input payload data before validation.
        Scans all assignments for time_block labels (e.g. 'morning') and 
        dynamically registers them into shift_blocks if missing.
        """
        if not isinstance(data, dict):
            return data

        raw_assignments = data.get("assignments") or data.get("schedule") or []
        raw_blocks = data.get("shift_blocks") or data.get("blocks") or []

        # 1. Collect all unique time_block labels from incoming assignments
        discovered_blocks = set()
        for assignment in raw_assignments:
            if isinstance(assignment, dict):
                block = (
                    assignment.get("time_block") 
                    or assignment.get("block_label") 
                    or assignment.get("block")
                )
                if block and str(block).strip():
                    discovered_blocks.add(str(block).strip().lower())

        # 2. Extract existing block IDs/names from payload metadata
        existing_block_keys = set()
        for b in raw_blocks:
            if isinstance(b, dict):
                b_key = b.get("block_id") or b.get("id") or b.get("name") or ""
                if b_key:
                    existing_block_keys.add(str(b_key).strip().lower())

        # 3. Inject any missing block label (e.g. 'morning') automatically
        updated_blocks = list(raw_blocks)
        for block_name in discovered_blocks:
            if block_name not in existing_block_keys:
                updated_blocks.append({
                    "block_id": block_name,
                    "name": block_name.capitalize(),
                    "shift_hours": ""
                })

        data["shift_blocks"] = updated_blocks
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SchedulePayload":
        return cls.model_validate(data)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()