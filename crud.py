# crud.py
from sqlalchemy.orm import Session
from models import WorkerModel, WorkerHistoryModel, ScheduleAuditModel

def get_all_workers(db: Session) -> list[dict]:
    """Retrieves all workers and formats them into dictionaries for the solver."""
    db_workers = db.query(WorkerModel).all()
    workers = []
    for w in db_workers:
        worker_dict = w.to_dict()
        # previous_weeks_load_scores is already calculated by the to_dict() model method
        workers.append(worker_dict)
    return workers

def save_worker(db: Session, worker_dict: dict):
    """Inserts or updates a worker in the PostgreSQL database."""
    uid_str = str(worker_dict["uid"])
    db_worker = db.query(WorkerModel).filter(WorkerModel.uid == uid_str).first()
    
    if not db_worker:
        db_worker = WorkerModel(uid=uid_str)
        db.add(db_worker)
    
    db_worker.name = worker_dict["name"]
    db_worker.talents = worker_dict.get("talents", [])
    db_worker.despair = worker_dict.get("despair", 0.0)
    db_worker.total_hours = worker_dict.get("total_hours", 0.0)
    db_worker.last_job = worker_dict.get("last_job")
    db_worker.last_shift_slot = worker_dict.get("last_shift_slot")
    db_worker.last_shift_end_time = worker_dict.get("last_shift_end_time", -20.0)
    db_worker.prefers_night = worker_dict.get("prefers_night", False)
    db_worker.is_voluntary_grinder = worker_dict.get("is_voluntary_grinder", False)
    
    db.commit()

def record_worker_week_history(db: Session, worker_uid: str, week_id: str, hours_worked: float):
    """Logs a worker's weekly load for the 4-week rolling delta engine."""
    history_entry = WorkerHistoryModel(
        worker_uid=str(worker_uid),
        week_identifier=week_id,
        hours_worked=hours_worked
    )
    db.add(history_entry)
    db.commit()

def save_schedule_run(db: Session, schedule_dict: dict) -> int:
    """Saves an audit log of a generated schedule run and returns its assigned ID."""
    audit_entry = ScheduleAuditModel(schedule_json=schedule_dict)
    db.add(audit_entry)
    db.commit()
    db.refresh(audit_entry)
    return audit_entry.id

def get_latest_schedule_run(db: Session) -> dict | None:
    """Retrieves the most recent schedule JSON."""
    audit_entry = db.query(ScheduleAuditModel).order_by(ScheduleAuditModel.id.desc()).first()
    return audit_entry.schedule_json if audit_entry else None