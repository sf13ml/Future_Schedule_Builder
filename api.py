import csv
import io
import json
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Import CRUD functions & Database configuration
from crud import get_all_workers, save_schedule_run, save_worker, get_latest_schedule_run
from database import get_db, init_db
from excel_exporter import generate_excel_schedule
from ingest import ingest_from_excel
from models import LayoutBlueprint, PolicyConfig, Shift, Worker
from solver import generate_schedule


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Schedule Builder API",
    description="CP-SAT Powered Shift Optimization Service (PostgreSQL)",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for frontend connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- SCHEMAS ---

class JobSchema(BaseModel):
    name: str
    required_talents: List[str] = Field(default_factory=list)
    forbidden_talents: List[str] = Field(default_factory=list)
    required_number_workers: int = 1
    is_critical: bool = False
    min_rest_override: Optional[float] = 4.0


class ShiftSchema(BaseModel):
    id: str
    day: str
    job: JobSchema
    time_slot: str
    hours_duration: float
    abs_start_hour: float = 0.0


class WorkerSchema(BaseModel):
    uid: str
    name: str
    talents: List[str] = Field(default_factory=list)
    despair: float = 0.0
    total_hours: float = 0.0
    last_job: Optional[str] = None
    last_shift_slot: Optional[str] = None
    last_shift_end_time: Optional[float] = -20.0
    unavailable_days: List[str] = Field(default_factory=list)
    blocked_intervals: List[Tuple[float, float]] = Field(default_factory=list)


class ScheduleRequest(BaseModel):
    policy: Optional[dict] = None
    workers: List[WorkerSchema] = Field(default_factory=list)
    shifts: List[ShiftSchema]


class ExportExcelRequest(BaseModel):
    schedule_payload: dict
    blueprint: Optional[LayoutBlueprint] = None


# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "online", "message": "Schedule Builder API is running!"}


@app.get("/workers")
def fetch_workers(db: Session = Depends(get_db)):
    return get_all_workers(db)


@app.post("/workers")
def add_or_update_worker(worker: WorkerSchema, db: Session = Depends(get_db)):
    save_worker(db, worker.model_dump())
    return {"status": "success", "message": f"Worker {worker.name} saved."}


@app.post("/workers/upload-csv")
async def upload_workers_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    buffer = io.StringIO(contents.decode('utf-8'))
    reader = csv.DictReader(buffer)
    
    count = 0
    for row in reader:
        talents_list = [t.strip() for t in row.get("talents", "").split(",") if t.strip()]
        worker_dict = {
            "uid": str(row["uid"]).strip(),
            "name": row["name"].strip(),
            "talents": talents_list,
            "despair": float(row.get("despair", 0.0)),
            "total_hours": float(row.get("total_hours", 0.0))
        }
        save_worker(db, worker_dict)
        count += 1
        
    return {"status": "success", "imported_count": count}


@app.post("/shifts/upload-csv")
async def upload_shifts_csv(file: UploadFile = File(...)):
    """Uploads a CSV file of shifts and returns them in a format ready for the solver."""
    contents = await file.read()
    buffer = io.StringIO(contents.decode('utf-8'))
    reader = csv.DictReader(buffer)
    
    parsed_shifts = []
    for row in reader:
        raw_talents = row.get("required_talents", "")
        talents_list = [t.strip() for t in raw_talents.split(",") if t.strip()]

        raw_forbidden = row.get("forbidden_talents", "")
        forbidden_list = [f.strip() for f in raw_forbidden.split(",") if f.strip()]

        shift_dict = {
            "id": row["id"],
            "day": row["day"],
            "job": {
                "name": row["job_name"],
                "required_talents": talents_list,
                "forbidden_talents": forbidden_list,
                "required_number_workers": int(row.get("required_number_workers", 1)),
                "is_critical": str(row.get("is_critical", "true")).lower() == "true",
                "min_rest_override": float(row["min_rest_override"]) if row.get("min_rest_override") else None
            },
            "time_slot": row["time_slot"],
            "hours_duration": float(row["hours_duration"]),
            "abs_start_hour": float(row["abs_start_hour"])
        }
        parsed_shifts.append(shift_dict)
        
    return {"status": "success", "shift_count": len(parsed_shifts), "shifts": parsed_shifts}


@app.post("/generate-schedule")
def api_generate_schedule(payload: ScheduleRequest, db: Session = Depends(get_db)):
    try:
        # 1. Resolve workers
        if not payload.workers:
            db_workers = get_all_workers(db)
            if not db_workers:
                raise HTTPException(status_code=400, detail="No workers provided and none found in DB.")
            workers = [
                Worker.from_dict({
                    "uid": getattr(w, "uid", w.get("uid") if isinstance(w, dict) else None),
                    "name": getattr(w, "name", w.get("name") if isinstance(w, dict) else ""),
                    "talents": getattr(w, "talents", w.get("talents") if isinstance(w, dict) else []) or [],
                    "despair": getattr(w, "despair", w.get("despair") if isinstance(w, dict) else 0.0),
                    "total_hours": getattr(w, "total_hours", w.get("total_hours") if isinstance(w, dict) else 0.0),
                    "last_job": getattr(w, "last_job", None),
                    "last_shift_slot": getattr(w, "last_shift_slot", None),
                    "last_shift_end_time": getattr(w, "last_shift_end_time", -20.0),
                    "prefers_night": getattr(w, "prefers_night", False),
                    "is_voluntary_grinder": getattr(w, "is_voluntary_grinder", False)
                })
                for w in db_workers
            ]
        else:
            workers = [Worker.from_dict(w.model_dump()) for w in payload.workers]

        # 2. Parse shifts and policy
        shifts = [Shift.from_dict(s.model_dump()) for s in payload.shifts]
        policy_config = PolicyConfig.from_dict(payload.policy) if payload.policy else PolicyConfig()

        # 3. Solve
        result = generate_schedule(workers, shifts, policy=policy_config)

        if result.get("status") == "infeasible":
            raise HTTPException(
                status_code=422, 
                detail={
                    "message": result.get("detail", "Schedule generation infeasible."),
                    "diagnostics": result.get("diagnostics", [])
                }
            )

        # 4. Update worker state in database
        if "worker_summaries" in result:
            for w_data in result["worker_summaries"]:
                save_worker(db, w_data)

        # 5. Save schedule run for export and history
        audit_record = save_schedule_run(db, result)
        if "meta" in result and isinstance(result["meta"], dict):
            result["meta"]["run_id"] = getattr(audit_record, "id", getattr(audit_record, "run_id", 1))

        return result

    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download-latest-excel")
def download_latest_excel(db: Session = Depends(get_db)):
    """Fetches the latest stored schedule run from the database and returns it as an Excel download."""
    latest_run = get_latest_schedule_run(db)
    if not latest_run:
        raise HTTPException(status_code=404, detail="No previous schedule runs found to export.")
    
    payload = latest_run.payload if hasattr(latest_run, "payload") else latest_run
    blueprint = LayoutBlueprint()
    excel_stream = generate_excel_schedule(payload, blueprint)
    
    return Response(
        content=excel_stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="Latest_Generated_Schedule.xlsx"',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )


@app.post("/export-schedule-csv")
def export_schedule_csv(payload: ScheduleRequest, db: Session = Depends(get_db)):
    """Generates the schedule and directly returns a downloadable CSV file."""
    result = api_generate_schedule(payload, db)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Shift ID", "Day", "Time Slot", "Job Category", "Assigned Worker"])
    
    for item in result.get("assignments", []):
        writer.writerow([
            item.get("shift_id"),
            item.get("day_name", item.get("day")),
            item.get("time_slot"),
            item.get("job_category", item.get("job")),
            item.get("worker_name")
        ])
    
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=generated_schedule.csv"}
    )


@app.post("/export-schedule-excel")
def export_schedule_excel(payload: ExportExcelRequest):
    """Generates an Excel spreadsheet dynamically using the Layout Blueprint configuration."""
    blueprint = payload.blueprint or LayoutBlueprint()
    excel_stream = generate_excel_schedule(payload.schedule_payload, blueprint)
    
    layout_type = getattr(blueprint, "layout_type", "hierarchical")
    filename = f"Schedule_{layout_type}.xlsx"
    
    return Response(
        content=excel_stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )


@app.post(
    "/process-excel-schedule",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
            "description": "Download processed Excel schedule",
        }
    }
)
async def process_excel_schedule(
    file: UploadFile = File(...),
    blueprint_json: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Full End-to-End Pipeline:
    Upload Excel -> Parse -> Run CP-SAT Solver -> Render Dynamic Layout -> Download Excel
    """
    try:
        # 1. Read file into memory buffer
        contents = await file.read()
        excel_buffer = io.BytesIO(contents)

        # 2. Parse Excel sheets
        raw_workers, raw_shifts = ingest_from_excel(excel_buffer)
        workers = [Worker.from_dict(w) for w in raw_workers]
        shifts = [Shift.from_dict(s) for s in raw_shifts]

        # 3. Generate Schedule via CP-SAT
        schedule_result = generate_schedule(workers, shifts, policy=PolicyConfig())
        if schedule_result.get("status") == "infeasible":
            raise HTTPException(
                status_code=422, 
                detail={
                    "message": schedule_result.get("detail", "Optimization infeasible."),
                    "diagnostics": schedule_result.get("diagnostics", [])
                }
            )

        # 4. Audit Log & Persist to Postgres
        save_schedule_run(db, schedule_result)

        # 5. Parse Blueprint Form Data if provided
        active_blueprint = LayoutBlueprint()
        if blueprint_json:
            active_blueprint = LayoutBlueprint.model_validate_json(blueprint_json)

        # 6. Render Excel Download
        excel_out = generate_excel_schedule(schedule_result, active_blueprint)

        return Response(
            content=excel_out.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="Generated_{file.filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition"
            }
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))