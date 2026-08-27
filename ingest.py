import pandas as pd
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional
from models import Worker, Shift, Job, PolicyConfig

DAY_OFFSETS = {
    "Sunday": 0.0, "Monday": 24.0, "Tuesday": 48.0, 
    "Wednesday": 72.0, "Thursday": 96.0, "Friday": 120.0, "Saturday": 144.0
}

# Pre-compiled Regex patterns for execution speed
TIME_SLOT_PATTERN = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?")
SLUG_CLEAN_1 = re.compile(r'[^a-zA-Z0-9]')
SLUG_CLEAN_2 = re.compile(r'_+')


def slugify(text: str) -> str:
    clean = SLUG_CLEAN_1.sub('_', str(text).strip())
    return SLUG_CLEAN_2.sub('_', clean).strip('_').upper() or "DEFAULT"


def parse_time_to_hours(time_str: Any) -> float:
    if pd.isna(time_str):
        return 0.0
    if hasattr(time_str, "hour") and hasattr(time_str, "minute"):
        return float(time_str.hour) + (float(time_str.minute) / 60.0)
    parts = str(time_str).strip().split(":")
    try:
        return float(parts[0]) + (float(parts[1]) / 60.0 if len(parts) > 1 else 0.0)
    except ValueError:
        return 0.0


def format_time_slot_label(slot_str: str) -> str:
    """Dynamically converts '06-10' or '06:00-10:00' to standardized 'HH:MM-HH:MM' format."""
    if not slot_str or str(slot_str).strip().lower() in ("main", "standard", "none", "", "nan"):
        return ""
    clean_str = str(slot_str).strip().replace("–", "-")
    match = TIME_SLOT_PATTERN.search(clean_str)
    if not match:
        return clean_str
    sh, sm, eh, em = match.groups()
    start_fmt = f"{int(sh):02d}:{sm if sm else '00'}"
    end_fmt = f"{int(eh):02d}:{em if em else '00'}"
    return f"{start_fmt}-{end_fmt}"


def parse_time_slot_duration(slot_str: str, default_block_hours: float = 8.0) -> float:
    if not slot_str or str(slot_str).strip().lower() in ("main", "standard", "none", "", "nan"):
        return default_block_hours

    clean_str = str(slot_str).strip().replace("–", "-")
    match = TIME_SLOT_PATTERN.search(clean_str)
    if not match:
        return default_block_hours

    sh, sm, eh, em = match.groups()
    start = float(sh) + (float(sm) / 60.0 if sm else 0.0)
    end = float(eh) + (float(em) / 60.0 if em else 0.0)

    if end <= start:
        end += 24.0

    return round(end - start, 2)


def parse_time_slot_start_hour(time_slot_str: str, default_block_start: float) -> float:
    if not time_slot_str or str(time_slot_str).strip().lower() in ("main", "standard", "none", "", "nan"):
        return default_block_start

    clean_str = str(time_slot_str).strip().replace("–", "-")
    match = TIME_SLOT_PATTERN.search(clean_str)
    if not match:
        return default_block_start

    sh, sm, _, _ = match.groups()
    return float(sh) + (float(sm) / 60.0 if sm else 0.0)


def ingest_from_excel(file_path_or_buffer) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    xls = pd.ExcelFile(file_path_or_buffer)

    # 1. SHIFT BLOCKS
    blocks_map = {}
    block_id_counts = {}

    if "Shift_Blocks" in xls.sheet_names:
        df_blocks = pd.read_excel(xls, "Shift_Blocks")
        for row in df_blocks.to_dict("records"):
            label = str(row.get("block label", "")).strip()
            if not label or pd.isna(label):
                continue
            base_slug = slugify(label)

            if base_slug in block_id_counts:
                block_id_counts[base_slug] += 1
                unique_block_id = f"{base_slug}_{block_id_counts[base_slug]}"
            else:
                block_id_counts[base_slug] = 1
                unique_block_id = base_slug

            start_h = parse_time_to_hours(row.get("start time", "00:00"))
            end_h = parse_time_to_hours(row.get("end time", "08:00"))
            duration = end_h - start_h if end_h > start_h else (24.0 - start_h) + end_h

            blocks_map[label.lower()] = {
                "block_id": unique_block_id,
                "label": label,
                "start_hour": start_h,
                "duration": duration if duration > 0 else 8.0
            }

    # 2. JOBS
    jobs_map = {}
    job_id_counts = {}

    if "Jobs" in xls.sheet_names:
        df_jobs = pd.read_excel(xls, "Jobs")
        for row in df_jobs.to_dict("records"):
            display_name = str(row.get("job name", "")).strip()
            if not display_name or pd.isna(display_name):
                continue
            base_slug = slugify(display_name)
            
            if base_slug in job_id_counts:
                job_id_counts[base_slug] += 1
                unique_job_id = f"{base_slug}_{job_id_counts[base_slug]}"
            else:
                job_id_counts[base_slug] = 1
                unique_job_id = base_slug

            raw_quals = str(row.get("required talents", "")) if pd.notna(row.get("required talents")) else ""
            priority_val = str(row.get("priority", "High")).strip().capitalize()
            
            jobs_map[display_name.lower()] = {
                "job_id": unique_job_id,
                "name": display_name,
                "required_talents": [q.strip().lower() for q in raw_quals.split(",") if q.strip()],
                "is_critical": priority_val in ["High", "Critical", "1"],
                "min_rest_override": float(row["min res override"]) if pd.notna(row.get("min res override")) else None
            }

    # 3. WEEKLY GRID (Vectorized Melt + Filtering)
    df_grid = pd.read_excel(xls, "Weekly_Grid")
    
    # Unpivot day columns into rows to eliminate inner day loops
    day_cols = [d for d in DAY_OFFSETS.keys() if d in df_grid.columns]
    df_melted = df_grid.melt(
        id_vars=[c for c in df_grid.columns if c not in day_cols],
        value_vars=day_cols,
        var_name="day_name",
        value_name="workers_needed"
    )

    # Discard non-numeric, null, or zero demand immediately
    df_melted = df_melted[pd.to_numeric(df_melted["workers_needed"], errors="coerce").fillna(0) > 0].copy()
    df_melted["workers_needed"] = df_melted["workers_needed"].astype(int)

    unrolled_shifts = []

    for row in df_melted.to_dict("records"):
        base_job_name = str(row.get("job name", "")).strip()
        time_slot_val = str(row.get("Time slot", "")).strip() if pd.notna(row.get("Time slot")) else "Main"
        block_label_val = str(row.get("block label", "General")).strip()
        day_name = row["day_name"]
        workers_needed = row["workers_needed"]

        block_key = block_label_val.lower()
        job_key = base_job_name.lower()

        # Dynamic Auto-Discovery for Shift Blocks
        if block_key not in blocks_map:
            base_slug = slugify(block_label_val)
            if base_slug in block_id_counts:
                block_id_counts[base_slug] += 1
                unique_block_id = f"{base_slug}_{block_id_counts[base_slug]}"
            else:
                block_id_counts[base_slug] = 1
                unique_block_id = base_slug

            blocks_map[block_key] = {
                "block_id": unique_block_id,
                "label": block_label_val,
                "start_hour": 0.0,
                "duration": 8.0
            }

        # Dynamic Auto-Discovery for Jobs
        if job_key not in jobs_map:
            base_slug = slugify(base_job_name)
            if base_slug in job_id_counts:
                job_id_counts[base_slug] += 1
                unique_job_id = f"{base_slug}_{job_id_counts[base_slug]}"
            else:
                job_id_counts[base_slug] = 1
                unique_job_id = base_slug

            jobs_map[job_key] = {
                "job_id": unique_job_id,
                "name": base_job_name,
                "required_talents": [],
                "is_critical": True,
                "min_rest_override": None
            }

        block_def = blocks_map[block_key]
        job_def = jobs_map[job_key]

        formatted_slot = format_time_slot_label(time_slot_val)
        full_job_name = f"{base_job_name} {formatted_slot}".strip() if formatted_slot else base_job_name
        shift_duration = parse_time_slot_duration(time_slot_val, default_block_hours=block_def["duration"])
        slot_start_hour = parse_time_slot_start_hour(time_slot_val, default_block_start=block_def["start_hour"])

        day_offset = DAY_OFFSETS[day_name]
        abs_start = day_offset + slot_start_hour
        slot_slug = slugify(time_slot_val) if time_slot_val else "MAIN"
        day_short = day_name[:3]

        for slot_idx in range(1, workers_needed + 1):
            shift_id = f"S_{job_def['job_id']}_{slot_slug}_{day_short}_{block_def['block_id']}_{slot_idx}"

            unrolled_shifts.append({
                "id": shift_id,
                "slot_label": f"{full_job_name}_slot_{slot_idx}",
                "day": day_name,
                "job": job_def,
                "job_group": base_job_name,
                "job_name": full_job_name,
                "time_block": block_label_val,
                "time_slot": time_slot_val,
                "hours_duration": shift_duration,
                "abs_start_hour": abs_start
            })

    # 4. EMPLOYEES
    workers = []
    if "Employees" in xls.sheet_names:
        df_emp = pd.read_excel(xls, "Employees")
        for row in df_emp.to_dict("records"):
            raw_quals = str(row.get("qualifications", "")) if pd.notna(row.get("qualifications")) else ""
            workers.append({
                "uid": str(row.get("uid", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "talents": [t.strip().lower() for t in raw_quals.split(",") if t.strip()],
                "prefers_night": str(row.get("prefers_night", "N")).strip().upper() in ["Y", "YES", "1", "TRUE"],
                "unavailable_days": [d.strip().capitalize() for d in str(row.get("unavailable_days", "")).split(",") if d.strip()]
            })

    return workers, unrolled_shifts


def parse_schedule_excel(
    file_path_or_bytes: Any,
    workers_db: List[Worker],
    jobs_db: List[Job],
    policy: PolicyConfig
) -> Tuple[List[Worker], List[Shift], float]:
    """
    Parses an Excel schedule grid using a dual-pass approach.
    """
    df = pd.read_excel(file_path_or_bytes, sheet_name=0)

    worker_map: Dict[str, Worker] = {w.name.strip().lower(): w for w in workers_db}
    job_map: Dict[str, Job] = {j.title.strip().lower(): j for j in jobs_db}

    time_columns = list(df.columns[1:])
    
    col_to_abs_hour: Dict[Any, float] = {}
    for idx, col in enumerate(time_columns):
        col_to_abs_hour[col] = float(idx * policy.standard_shift_duration)

    t_cutoff: float = float('inf')
    raw_cell_matrix: List[Dict[str, Any]] = []

    for row_idx, row in df.iterrows():
        job_title = str(row.iloc[0]).strip().lower()
        if job_title not in job_map:
            continue
            
        job = job_map[job_title]

        for col in time_columns:
            cell_val = row[col]
            abs_start = col_to_abs_hour[col]
            shift_duration = policy.standard_shift_duration
            abs_end = abs_start + shift_duration
            
            is_assigned = pd.notna(cell_val) and str(cell_val).strip() != ""
            
            raw_cell_matrix.append({
                "job": job,
                "abs_start": abs_start,
                "abs_end": abs_end,
                "duration": shift_duration,
                "assigned_raw": str(cell_val).strip() if is_assigned else None,
                "is_assigned": is_assigned
            })

            if not is_assigned and abs_start < t_cutoff:
                t_cutoff = abs_start

    if t_cutoff == float('inf'):
        t_cutoff = max(col_to_abs_hour.values(), default=0.0) + policy.standard_shift_duration

    for cell in raw_cell_matrix:
        if cell["abs_start"] < t_cutoff and cell["is_assigned"]:
            worker_name_key = cell["assigned_raw"].lower()
            if worker_name_key in worker_map:
                worker = worker_map[worker_name_key]
                worker.historical_hours += cell["duration"]
                if cell["abs_end"] > worker.last_assigned_end_hour:
                    worker.last_assigned_end_hour = cell["abs_end"]
                    worker.last_job = cell["job"].title

    all_shifts: List[Shift] = []

    for idx, cell in enumerate(raw_cell_matrix):
        is_historical = cell["abs_start"] < t_cutoff
        
        assigned_uids = []
        if is_historical and cell["is_assigned"]:
            worker_name_key = cell["assigned_raw"].lower()
            if worker_name_key in worker_map:
                assigned_uids.append(worker_map[worker_name_key].uid)

        shift = Shift(
            id=f"shift_{idx}_{int(cell['abs_start'])}",
            job=cell["job"],
            abs_start_hour=cell["abs_start"],
            hours_duration=cell["duration"],
            is_fixed=is_historical,
            assigned_worker_uids=assigned_uids
        )
        all_shifts.append(shift)

    return list(worker_map.values()), all_shifts, t_cutoff