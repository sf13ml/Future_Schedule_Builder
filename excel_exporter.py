import io
import re
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from models import LayoutBlueprint
from typing import Any, List, Dict
from pydantic import BaseModel

# --- DATA CONTRACT / NORMALIZATION LAYER ---

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
        """Normalizes heterogeneous input dictionary into a strict schema contract."""
        
        # 1. Resolve Job Display Name
        job_raw = (
            data.get("job_name")
            or data.get("job_group")
            or data.get("role")
            or data.get("job_category")
            or (data.get("job", {}).get("name") if isinstance(data.get("job"), dict) else data.get("job"))
            or "General Shift"
        )

        # 2. Resolve Time Block & Time Slot separately
        time_slot_raw = data.get("time_slot") or data.get("shift_hours") or "Unassigned"
        time_block_raw = data.get("time_block") or "Unassigned"

        # 3. Resolve Day Name
        day_raw = data.get("day_name") or data.get("day") or ""

        # 4. Resolve Worker Identifiers
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

        # 5. Resolve Shift ID
        shift_id_raw = data.get("id") or data.get("shift_id") or ""

        return cls(
            shift_id=str(shift_id_raw).strip(),
            day=str(day_raw).strip().capitalize(),
            time_block=str(time_block_raw).strip(),
            time_slot=str(time_slot_raw).strip(),
            job_name=str(job_raw).strip(),
            worker_uid=str(worker_uid_raw).strip(),
            worker_name=str(worker_name_raw).strip() if str(worker_name_raw) != "None" else ""
        )


# --- MAIN EXPORT FUNCTION ---

def generate_excel_schedule(schedule_data: Any, blueprint: LayoutBlueprint = LayoutBlueprint()) -> io.BytesIO:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Schedule"
    
    if hasattr(ws, 'sheet_view'):
        ws.sheet_view.showGridLines = blueprint.show_gridlines
    elif len(ws.views.sheetView) > 0:
        ws.views.sheetView[0].showGridLines = blueprint.show_gridlines

    # Unpack Payload
    if isinstance(schedule_data, dict):
        raw_assignments = schedule_data.get("assignments") or schedule_data.get("schedule") or []
        metrics = schedule_data.get("metrics") or {}
        diagnostics = schedule_data.get("diagnostics") or schedule_data.get("audit_logs") or schedule_data.get("warnings") or []
        status = schedule_data.get("status", "unknown")
    else:
        raw_assignments = schedule_data if isinstance(schedule_data, list) else []
        metrics = {}
        diagnostics = []
        status = "optimal"

    # Normalize incoming assignments via Canonical Contract
    assignments: List[CanonicalAssignment] = [
        CanonicalAssignment.from_dict(a) for a in raw_assignments if isinstance(a, dict)
    ]

    # Tab 1: Schedule Matrix / Tabular
    if blueprint.layout_type == "tabular":
        _render_tabular_layout(ws, assignments, blueprint)
    else:
        _render_matrix_layout(ws, assignments, blueprint)

    # Tab 2: Analytics & Metrics
    if metrics:
        _render_analytics_sheet(wb, metrics)

    # Tab 3: System Diagnostics & Debug
    if not diagnostics and status in ("infeasible", "infeasable"):
        diagnostics = [{
            "severity": "CRITICAL",
            "code": "SOLVER_INFEASIBLE",
            "entity": "Global Matrix",
            "reason": "The model could not be resolved under strict constraints.",
            "remediation": "Check for conflicting hard constraints or manpower shortages."
        }]
    elif not diagnostics:
        diagnostics = [{
            "severity": "INFO",
            "code": "STATUS_OK",
            "entity": "Global Schedule",
            "reason": "Schedule compiled successfully with all constraints satisfied.",
            "remediation": "None required."
        }]

    _render_diagnostics_sheet(wb, diagnostics)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output

def get_time_block_sort_rank(time_block_name: str) -> int:
    if not time_block_name:
        return 99
    name = str(time_block_name).strip().lower()

    if any(k in name for k in ["morning", "boker", "early", "morning_slot", "shift_1"]):
        return 1
    if any(k in name for k in ["afternoon", "tzohorayim", "midday", "noon", "day", "shift_2"]):
        return 2
    if any(k in name for k in ["night", "evening", "laila", "late", "pm", "shift_3"]):
        return 3

    match = re.search(r"^(\d{1,2})", name)
    if match:
        start_hour = int(match.group(1))
        if 5 <= start_hour < 12:
            return 1
        elif 12 <= start_hour < 18:
            return 2
        else:
            return 3

    return 50

def map_hours_to_block(slot: str) -> str:
    """Helper to map shift hours to standard time blocks if block is unassigned."""
    s = str(slot).strip()
    if s in ["06-10", "10:00-14:00", "06-14", "08-16"]:
        return "morning"
    elif s in ["14-18", "18:00-22:00", "14-22", "16-24"]:
        return "afternoon"
    elif s in ["22-02", "02-06", "22-06", "00-08"]:
        return "night"
    return "main"

def get_block_rank(block_name: str) -> int:
    b = str(block_name).lower()
    if "morning" in b:
        return 1
    if "afternoon" in b:
        return 2
    if "night" in b:
        return 3
    return 4

def standardize_shift_hours(block: str, job_name: str, slot_idx: int, raw_slot: str) -> str:
    """Uses the true time slot provided by the input data without hardcoded overrides."""
    if raw_slot and str(raw_slot).strip().lower() not in ("main", "standard", "none", "", "nan"):
        clean_str = str(raw_slot).strip().replace("–", "-")
        return clean_str
    
    b = str(block).strip().lower()
    if "morning" in b:
        return "06:00-14:00"
    elif "afternoon" in b:
        return "14:00-22:00"
    elif "night" in b:
        return "22:00-06:00"
    return "06:00-14:00"

def _render_matrix_layout(ws, assignments, blueprint):
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_body = Font(name="Calibri", size=10)
    fill_header = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    fill_white = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    
    thin_side = Side(border_style="thin", color="D9D9D9")
    border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center")

    x_headers = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

    row_tree = {}
    for a in assignments:
        raw_block = str(getattr(a, "time_block", getattr(a, "block_label", "")) or "").strip().lower()
        slot = str(getattr(a, "time_slot", getattr(a, "shift_hours", "")) or "").strip()
        job_name = str(getattr(a, "job_name", getattr(a, "job_category", "General Shift"))).strip()
        shift_id = str(getattr(a, "shift_id", ""))

        if "morning" in raw_block or "boker" in raw_block or "06" in raw_block or "10" in raw_block:
            block = "Morning"
        elif "afternoon" in raw_block or "tzohorayim" in raw_block or "day" in raw_block or "14" in raw_block:
            block = "Afternoon"
        elif "night" in raw_block or "laila" in raw_block or "evening" in raw_block or "22" in raw_block:
            block = "Night"
        else:
            block = map_hours_to_block(slot).capitalize()
            if block not in ["Morning", "Afternoon", "Night"]:
                block = "Morning"

        # Extract true slot/worker index natively from the shift definition (e.g., required_number_workers instances)
        slot_idx = 1
        parts = shift_id.split("_")
        if parts and parts[-1].isdigit():
            slot_idx = int(parts[-1])
        elif hasattr(a, "slot_label") and "_slot_" in str(a.slot_label):
            try:
                slot_idx = int(a.slot_label.split("_")[-1])
            except ValueError:
                pass

        display_slot = standardize_shift_hours(block, job_name, slot_idx, slot)
        day_key = getattr(a, "day", "").capitalize()

        # Map unique worker instances based on actual slot requirement index rather than arbitrary limits
        current_s_idx = slot_idx
        while True:
            candidate_key = (block, job_name, display_slot, current_s_idx)
            if candidate_key not in row_tree or day_key not in row_tree[candidate_key]:
                row_key = candidate_key
                break
            current_s_idx += 1

        if row_key not in row_tree:
            row_tree[row_key] = {}
        
        row_tree[row_key][day_key] = getattr(a, "worker_name", "")

    sorted_row_keys = sorted(
        row_tree.keys(),
        key=lambda k: (get_block_rank(k[0]), k[1], k[2], k[3])
    )

    headers = ["Time Block", "Job Category", "Shift Hours"] + x_headers
    for col_idx, h_text in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h_text)
        cell.font, cell.fill, cell.alignment = font_header, fill_header, align_center

    start_row = 2
    for r_idx, (t_block, j_name, t_slot, s_idx) in enumerate(sorted_row_keys, start=start_row):
        c1 = ws.cell(row=r_idx, column=1, value=t_block)
        c2 = ws.cell(row=r_idx, column=2, value=j_name)
        c3 = ws.cell(row=r_idx, column=3, value=t_slot)
        
        for c in (c1, c2, c3):
            c.font, c.fill, c.border = font_body, fill_white, border_cell
        c1.alignment, c3.alignment = align_center, align_center
        c2.alignment = align_left

        for c_idx, day in enumerate(x_headers, start=4):
            val = row_tree[(t_block, j_name, t_slot, s_idx)].get(day, "")
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.font, cell.fill, cell.border, cell.alignment = font_body, fill_white, border_cell, align_center

    end_row = start_row + len(sorted_row_keys) - 1

    if sorted_row_keys and getattr(blueprint, "auto_merge_parent_cells", True):
        merge_start = start_row
        for r in range(start_row + 1, end_row + 2):
            prev_val = ws.cell(row=r - 1, column=1).value
            curr_val = ws.cell(row=r, column=1).value if r <= end_row else None
            if curr_val != prev_val:
                if r - 1 > merge_start:
                    ws.merge_cells(start_row=merge_start, end_row=r - 1, start_column=1, end_column=1)
                ws.cell(row=merge_start, column=1).alignment = align_center
                merge_start = r

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

def _render_analytics_sheet(wb, metrics: dict):
    ws = wb.create_sheet(title="Analytics")
    ws.views.sheetView[0].showGridLines = True

    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_section = Font(name="Calibri", size=12, bold=True, color="1F4E78")
    font_bold =Font(name="Calibri", size=10, bold=True)
    font_body = Font(name="Calibri", size=10)

    fill_header = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    fill_card = PatternFill(start_color="F2F4F7", end_color="F2F4F7", fill_type="solid")

    thin_side = Side(border_style="thin", color="D9D9D9")
    border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")

    current_row = 1

    summary_cards = metrics.get("summary_cards", [])
    if summary_cards:
        ws.cell(row=current_row, column=1, value="Overall Manpower Pool Metrics").font = font_section
        current_row += 1
        for card in summary_cards:
            c1 = ws.cell(row=current_row, column=1, value=card.get("label", ""))
            c2 = ws.cell(row=current_row, column=2, value=str(card.get("value", "")))
            c1.font, c2.font = font_body, font_bold
            c1.fill, c2.fill = fill_card, fill_card
            c1.border, c2.border = border_cell, border_cell
            c2.alignment = align_center
            current_row += 1
        current_row += 2

    jobs = metrics.get("job_coverage", [])
    if jobs:
        ws.cell(row=current_row, column=1, value="Job Category Performance (TCU & CDR)").font = font_section
        current_row += 1
        headers = ["Job Category", "Required Slots", "Filled Slots", "Job Utilization (TCU)", "Avg. Job Despair (CDR)"]
        for col_idx, h_text in enumerate(headers, start=1):
            c = ws.cell(row=current_row, column=col_idx, value=h_text)
            c.font, c.fill, c.alignment = font_header, fill_header, align_center
        current_row += 1
        for j in jobs:
            vals = [
                j.get("job_name", j.get("job_id", "")),
                j.get("required_slots", 0),
                j.get("filled_slots", 0),
                str(j.get("job_tcu", "0.0%")),
                str(j.get("job_avg_cdr", "0.00"))
            ]
            for col_idx, val in enumerate(vals, start=1):
                c = ws.cell(row=current_row, column=col_idx, value=val)
                c.font, c.border = font_body, border_cell
                c.alignment = align_left if col_idx == 1 else align_center
            current_row += 1
        current_row += 2

    manpower = metrics.get("manpower_utilization", [])
    if manpower:
        ws.cell(row=current_row, column=1, value="Individual Worker Capacity & Despair").font = font_section
        current_row += 1
        headers = ["Worker ID", "Name", "Assigned Hrs", "Max Capacity Hrs", "Worker TCU", "Worker CDR"]
        for col_idx, h_text in enumerate(headers, start=1):
            c = ws.cell(row=current_row, column=col_idx, value=h_text)
            c.font, c.fill, c.alignment = font_header, fill_header, align_center
        current_row += 1
        for w in manpower:
            vals = [
                w.get("worker_id", ""),
                w.get("name", ""),
                w.get("assigned_hours", 0),
                w.get("max_capacity_hours", 0),
                str(w.get("tcu_percent", "0.0%")),
                str(w.get("cdr_score", "0.00"))
            ]
            for col_idx, val in enumerate(vals, start=1):
                c = ws.cell(row=current_row, column=col_idx, value=val)
                c.font, c.border = font_body, border_cell
                c.alignment = align_left if col_idx in (1, 2) else align_center
            current_row += 1

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 18)

def _render_diagnostics_sheet(wb, diagnostics: list):
    ws = wb.create_sheet(title="System_Diagnostics")
    ws.views.sheetView[0].showGridLines = True

    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_body = Font(name="Calibri", size=10)
    fill_header = PatternFill(start_color="333333", end_color="333333", fill_type="solid")
    
    thin_side = Side(border_style="thin", color="D9D9D9")
    border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    headers = ["Severity", "Code", "Affected Entity / Role", "Diagnostic Reason", "Actionable Remediation"]
    for col_idx, h_text in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h_text)
        cell.font, cell.fill, cell.alignment = font_header, fill_header, align_center

    for r_idx, d in enumerate(diagnostics, start=2):
        c1 = ws.cell(row=r_idx, column=1, value=d.get("severity", "INFO"))
        c2 = ws.cell(row=r_idx, column=2, value=d.get("code", "LOG"))
        c3 = ws.cell(row=r_idx, column=3, value=d.get("entity", "System"))
        c4 = ws.cell(row=r_idx, column=4, value=d.get("reason", d.get("description", "")))
        c5 = ws.cell(row=r_idx, column=5, value=d.get("remediation", ""))

        for cell in (c1, c2, c3, c4, c5):
            cell.font, cell.border = font_body, border_cell

        c1.alignment, c2.alignment = align_center, align_center
        c3.alignment, c4.alignment, c5.alignment = align_left, align_left, align_left

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 20
    ws.column_dimensions['C'].width = 28
    ws.column_dimensions['D'].width = 45
    ws.column_dimensions['E'].width = 45

def _render_tabular_layout(ws, assignments: List[CanonicalAssignment], blueprint: LayoutBlueprint):
    headers = ["Shift ID", "Day", "Block Label", "Time Slot", "Job", "Worker UID", "Worker Name"]
    ws.append(headers)
    for a in assignments:
        ws.append([
            a.shift_id,
            a.day,
            a.time_block,
            a.time_slot,
            a.job_name,
            a.worker_uid,
            a.worker_name
        ])