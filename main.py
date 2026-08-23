import io
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, status, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Worker, Shift, PolicyConfig, LayoutBlueprint
import crud
from ingest import ingest_from_excel
from solver import generate_schedule
from excel_exporter import generate_excel_schedule

app = FastAPI(
    title="Shift Scheduler & Workforce Engine", 
    version="1.0.0",
    description="CP-SAT Powered Shift Optimization Service with PostgreSQL Integration"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = (".xlsx", ".xls")

@app.get("/")
def read_root():
    return {"status": "online", "message": "Shift Builder PostgreSQL backend is running."}

@app.get("/workers", response_model=List[dict])
def get_workers(db: Session = Depends(get_db)):
    return crud.get_all_workers(db)

@app.get(
    "/download-latest-excel",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
            "description": "Download the latest generated schedule as an Excel spreadsheet",
        }
    }
)
def download_latest_excel(db: Session = Depends(get_db)):
    """
    Swagger-Friendly GET Endpoint:
    Fetches the most recent schedule run from PostgreSQL and triggers a direct browser file download.
    """
    latest_run = crud.get_latest_schedule_run(db)
    if not latest_run:
        raise HTTPException(status_code=404, detail="No previous schedule runs found in the database to export.")
    
    blueprint = LayoutBlueprint()
    excel_stream = generate_excel_schedule(latest_run, blueprint)
    
    return Response(
        content=excel_stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="Latest_Generated_Schedule.xlsx"',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )

@app.post("/generate-schedule-from-excel")
async def generate_schedule_from_excel(
    file: UploadFile = File(..., description="The scheduling Excel workbook (.xlsx or .xls)"), 
    db: Session = Depends(get_db)
):
    """
    Enterprise Endpoint: Processes uploaded Excel workbook in-memory,
    executes CP-SAT optimization, persists audit logs, and returns assignments.
    """
    # 1. Validate file extension early to prevent silent or unhelpful failures
    if not file.filename or not file.filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file format. Expected an Excel file ending with one of {ALLOWED_EXTENSIONS}."
        )

    try:
        # In-memory binary stream to prevent disk I/O bottlenecks under heavy load
        contents = await file.read()
        excel_buffer = io.BytesIO(contents)

        # 2. Ingest workbook data (Catches missing tabs, typo keys, or formatting issues)
        try:
            raw_workers, raw_shifts = ingest_from_excel(excel_buffer)
        except ValueError as ve:
            # Map data validation errors directly to 400 Bad Request for the user/employer
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Excel Validation Error: {str(ve)}"
            )

        if not raw_shifts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Ingestion failed: No valid shifts found in workbook grid."
            )
        if not raw_workers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Ingestion failed: No valid workers found in Employees tab."
            )

        workers = [Worker.from_dict(w) for w in raw_workers]
        shifts = [Shift.from_dict(s) for s in raw_shifts]

        # 3. Run Optimization Engine
        result = generate_schedule(workers, shifts, policy=PolicyConfig())

        if result.get("status") == "infeasible":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail={
                    "message": result.get("detail", "Optimization Infeasible"),
                    "diagnostics": result.get("diagnostics", [])
                }
            )

        # 4. Persistence
        crud.save_schedule_run(db, result)

        if "worker_summaries" in result:
            for w_summary in result["worker_summaries"]:
                crud.save_worker(db, w_summary)

        return result

    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Internal Server Error: {str(e)}"
        )