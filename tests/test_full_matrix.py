import sys
import json
from pathlib import Path

# Add project root directory to Python search path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Project imports
from models import Worker, Shift, Job, PolicyConfig, LayoutBlueprint
from solver import generate_schedule
from excel_exporter import generate_excel_schedule

# Path setup for JSON files in tests/
TEST_DIR = Path(__file__).resolve().parent
JOBS_FILE = TEST_DIR / "jobs.json"
SOLDIERS_FILE = TEST_DIR / "soldiers.json"


def load_jobs() -> dict[str, Job]:
    with open(JOBS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {key: Job(**val) for key, val in data.items()}


def load_workers() -> list[Worker]:
    with open(SOLDIERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Worker(**item) for item in data]


def build_test_schedule():
    jobs = load_jobs()
    workers = load_workers()

    all_day_posts = [
        {"block": "All Day 00:00-24:00", "job": jobs["kitchen"], "slots": ["s11"], "start": 0.0, "dur": 24.0},
        {"block": "All Day 00:00-24:00", "job": jobs["base_officer"], "slots": ["c4"], "start": 0.0, "dur": 24.0},
    ]
    post_definitions = [
        # MORNING BLOCK (06:00-14:00)
        {"block": "Morning 06:00-14:00", "job": jobs["kitchen"], "slots": ["s11"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["base_officer"], "slots": ["c4"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["medic_in_charge"], "slots": ["s12"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["patrol_45_commander"], "slots": ["c1"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["patrol_45_soldier"], "slots": ["s1", "s2"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["patrol_95_commander"], "slots": ["c2"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["patrol_95_soldier"], "slots": ["s3", "s4"], "start": 6.0, "dur": 8.0},
        {"block": "Morning 06:00-14:00", "job": jobs["entrance"], "slots": ["s5"], "start": 6.0, "dur": 4.0},
        {"block": "Morning 06:00-14:00", "job": jobs["entrance"], "slots": ["s6"], "start": 10.0, "dur": 4.0},

        # AFTERNOON BLOCK (14:00-22:00)
        {"block": "Afternoon 14:00-22:00", "job": jobs["patrol_45_commander"], "slots": ["c1"], "start": 14.0, "dur": 8.0},
        {"block": "Afternoon 14:00-22:00", "job": jobs["patrol_45_soldier"], "slots": ["s1", "s2"], "start": 14.0, "dur": 8.0},
        {"block": "Afternoon 14:00-22:00", "job": jobs["patrol_95_commander"], "slots": ["c2"], "start": 14.0, "dur": 8.0},
        {"block": "Afternoon 14:00-22:00", "job": jobs["patrol_95_soldier"], "slots": ["s3", "s4"], "start": 14.0, "dur": 8.0},
        {"block": "Afternoon 14:00-22:00", "job": jobs["entrance"], "slots": ["s5"], "start": 14.0, "dur": 4.0},
        {"block": "Afternoon 14:00-22:00", "job": jobs["entrance"], "slots": ["s6"], "start": 18.0, "dur": 4.0},

        # NIGHT BLOCK (22:00-06:00)
        {"block": "Night 22:00-06:00", "job": jobs["patrol_45_commander"], "slots": ["c1"], "start": 22.0, "dur": 8.0},
        {"block": "Night 22:00-06:00", "job": jobs["patrol_45_soldier"], "slots": ["s1", "s2"], "start": 22.0, "dur": 8.0},
        {"block": "Night 22:00-06:00", "job": jobs["patrol_95_commander"], "slots": ["c2"], "start": 22.0, "dur": 8.0},
        {"block": "Night 22:00-06:00", "job": jobs["patrol_95_soldier"], "slots": ["s3", "s4"], "start": 22.0, "dur": 8.0},
        {"block": "Night 22:00-06:00", "job": jobs["entrance"], "slots": ["s5"], "start": 22.0, "dur": 4.0},
        {"block": "Night 22:00-06:00", "job": jobs["entrance"], "slots": ["s6"], "start": 26.0, "dur": 4.0},
    ]

    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    shifts = []

    day_offset = 0.0
    for day_name in days:
        for p_idx, post in enumerate(post_definitions):
            for slot_code in post["slots"]:
                shift_id = f"{day_name[:3].lower()}_{p_idx}_{slot_code}"
                shifts.append(
                    Shift(
                        id=shift_id,
                        slot_label=slot_code,
                        day=day_name,
                        job=post["job"],
                        time_slot=post["block"],
                        hours_duration=post["dur"],
                        abs_start_hour=day_offset + post["start"]
                    )
                )
        day_offset += 24.0

    return workers, shifts


def run_test():
    print("1. Reading test datasets from JSON...")
    workers, shifts = build_test_schedule()
    print(f"    Loaded {len(workers)} workers and generated {len(shifts)} shift slots.")

    policy = PolicyConfig(
        max_weekly_hours_per_worker=60.0,
        min_rest_hours_default=4.0
    )

    print("\n--- CAPACITY CHECK ---")
    total_shift_hours = sum(s.hours_duration * getattr(s.job, 'required_number_workers', 1) for s in shifts)
    total_capacity_hours = sum(policy.max_weekly_hours_per_worker - getattr(w, 'total_hours', 0) for w in workers)
    print(f"Total Shift Hours Demanded: {total_shift_hours}h")
    print(f"Total Worker Capacity Available: {total_capacity_hours}h")

    print("\n--- TESTING CONSTRAINTS ONE BY ONE ---")
    tests = [
        ("Strict Rest Constraints", PolicyConfig(enforce_strict_rest=False)),
        ("Max Hours Cap", PolicyConfig(enforce_max_hours_cap=False)),
        ("Consecutive Day Limits", PolicyConfig(enforce_consecutive_day_limit=False)),
        ("All Policy Caps Relaxed", PolicyConfig(enforce_strict_rest=False, enforce_max_hours_cap=False, enforce_consecutive_day_limit=False)),
    ]

    for name, test_policy in tests:
        res = generate_schedule(workers, shifts, test_policy)
        status = res.get("status")
        print(f"Disabling '{name}' -> Status: {status}")
    print("--------------------------------------\n")

    print("2. Solving matrix constraints...")
    result = generate_schedule(workers, shifts, policy)
    if result.get("status") == "infeasible":
        print("REASON:", result.get("message"))

    print("\n--- SOLVER RESULT CHECK ---")
    if isinstance(result, dict):
        print("Status:", result.get("status"))
        print("Keys present in result:", list(result.keys()))
        assignments = result.get("assignments") or result.get("schedule") or []
        print(f"Total assignments generated: {len(assignments)}")
        if assignments:
            print("First assignment item structure:", assignments[0])
    else:
        print("Result object:", result)
    print("---------------------------\n")

    print("3. Exporting output Excel file...")
    assignments = result.get("assignments", [])
    if assignments:
        print("Assignment object sample:", assignments[0])
    else:
        print("No assignments found in result! Solver status:", result.get("status"))

    blueprint = LayoutBlueprint(
        layout_type="hierarchical",
        x_axis_field="day_name",
        y_axis_hierarchy=["time_block", "job_category", "slot_code"],
        auto_merge_parent_cells=True
    )
    excel_bytes = generate_excel_schedule(result, blueprint)

    output_path = TEST_DIR / "Test_Full_Matrix_Output.xlsx"
    with open(output_path, "wb") as f:
        f.write(excel_bytes.getvalue())

    print(f"SUCCESS! Output saved: '{output_path}'")


# EXPLICIT EXECUTION TRIGGER
if __name__ == "__main__":
    run_test()