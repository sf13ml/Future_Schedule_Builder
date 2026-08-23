import pandas as pd
import re
from typing import List, Dict, Any, Tuple

DAY_OFFSETS = {
    "Sunday": 0.0, "Monday": 24.0, "Tuesday": 48.0, 
    "Wednesday": 72.0, "Thursday": 96.0, "Friday": 120.0, "Saturday": 144.0
}

def slugify(text: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9]', '_', str(text).strip())
    return re.sub(r'_+', '_', clean).strip('_').upper()

def parse_time_to_hours(time_str: str) -> float:
    parts = str(time_str).strip().split(":")
    return float(parts[0]) + (float(parts[1]) / 60.0 if len(parts) > 1 else 0.0)

def format_time_slot_label(slot_str: str) -> str:
    """Dynamically converts '06-10' or '06:00-10:00' to standardized 'HH:MM-HH:MM' format."""
    if not slot_str or str(slot_str).strip().lower() in ("main", "standard", "none", "", "nan"):
        return ""
    clean_str = str(slot_str).strip().replace("–", "-")
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?", clean_str)
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
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?", clean_str)
    if not match:
        return default_block_hours

    sh, sm, eh, em = match.groups()
    start = float(sh) + (float(sm) / 60.0 if sm else 0.0)
    end = float(eh) + (float(em) / 60.0 if em else 0.0)

    if end <= start:
        end += 24.0

    return round(end - start, 2)

def ingest_from_excel(file_path_or_buffer) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    xls = pd.ExcelFile(file_path_or_buffer)

    # 1. SHIFT BLOCKS
    df_blocks = pd.read_excel(xls, "Shift_Blocks")
    blocks_map = {}
    block_id_counts = {}

    for _, row in df_blocks.iterrows():
        label = str(row["block label"]).strip()
        base_slug = slugify(label)

        if base_slug in block_id_counts:
            block_id_counts[base_slug] += 1
            unique_block_id = f"{base_slug}_{block_id_counts[base_slug]}"
        else:
            block_id_counts[base_slug] = 1
            unique_block_id = base_slug

        start_h = parse_time_to_hours(row["start time"])
        end_h = parse_time_to_hours(row["end time"])
        duration = end_h - start_h if end_h > start_h else (24.0 - start_h) + end_h

        blocks_map[label.lower()] = {
            "block_id": unique_block_id,
            "label": label,
            "start_hour": start_h,
            "duration": duration
        }

    # 2. JOBS
    df_jobs = pd.read_excel(xls, "Jobs")
    jobs_map = {}
    job_id_counts = {}

    for _, row in df_jobs.iterrows():
        display_name = str(row["job name"]).strip()
        base_slug = slugify(display_name)
        
        if base_slug in job_id_counts:
            job_id_counts[base_slug] += 1
            unique_job_id = f"{base_slug}_{job_id_counts[base_slug]}"
        else:
            job_id_counts[base_slug] = 1
            unique_job_id = base_slug

        raw_quals = str(row["required talents"]) if pd.notna(row.get("required talents")) else ""
        priority_val = str(row.get("priority", "High")).strip().capitalize()
        
        jobs_map[display_name.lower()] = {
            "job_id": unique_job_id,
            "name": display_name,
            "required_talents": [q.strip().lower() for q in raw_quals.split(",") if q.strip()],
            "is_critical": priority_val in ["High", "Critical", "1"],
            "min_rest_override": float(row["min res override"]) if pd.notna(row.get("min res override")) else None
        }

    # 3. WEEKLY GRID
    df_grid = pd.read_excel(xls, "Weekly_Grid")
    unrolled_shifts = []

    for _, row in df_grid.iterrows():
        base_job_name = str(row["job name"]).strip()
        time_slot_val = str(row.get("Time slot", "")).strip() if pd.notna(row.get("Time slot")) else "Main"
        block_label_val = str(row["block label"]).strip()

        block_key = block_label_val.lower()
        job_key = base_job_name.lower()

        if block_key not in blocks_map:
            raise ValueError(f"Block '{block_label_val}' in Weekly_Grid is missing from Shift_Blocks tab.")
        if job_key not in jobs_map:
            raise ValueError(f"Job '{base_job_name}' in Weekly_Grid is missing from Jobs tab.")

        block_def = blocks_map[block_key]
        job_def = jobs_map[job_key]

        formatted_slot = format_time_slot_label(time_slot_val)
        full_job_name = f"{base_job_name} {formatted_slot}".strip() if formatted_slot else base_job_name
        shift_duration = parse_time_slot_duration(time_slot_val, default_block_hours=block_def["duration"])

        for day_name, day_offset in DAY_OFFSETS.items():
            if day_name in row and pd.notna(row[day_name]):
                workers_needed = int(row[day_name])
                
                for slot_idx in range(1, workers_needed + 1):
                    abs_start = day_offset + block_def["start_hour"]
                    shift_id = f"S_{job_def['job_id']}_{day_name[:3]}_{block_def['block_id']}_{slot_idx}"

                    unrolled_shifts.append({
                        "id": shift_id,
                        "slot_label": f"{full_job_name}_slot_{slot_idx}",
                        "day": day_name,
                        "job": job_def,
                        "job_group": base_job_name,
                        "job_name": full_job_name,
                        "time_block": block_label_val.lower(),
                        "time_slot": time_slot_val,
                        "hours_duration": shift_duration,
                        "abs_start_hour": abs_start
                    })

    # 4. EMPLOYEES
    df_emp = pd.read_excel(xls, "Employees")
    workers = []
    for _, row in df_emp.iterrows():
        raw_quals = str(row["qualifications"]) if pd.notna(row.get("qualifications")) else ""
        workers.append({
            "uid": str(row["uid"]).strip(),
            "name": str(row["name"]).strip(),
            "talents": [t.strip().lower() for t in raw_quals.split(",") if t.strip()],
            "prefers_night": str(row.get("prefers_night", "N")).strip().upper() in ["Y", "YES", "1", "TRUE"],
            "unavailable_days": [d.strip().capitalize() for d in str(row.get("unavailable_days", "")).split(",") if d.strip()]
        })

    return workers, unrolled_shifts