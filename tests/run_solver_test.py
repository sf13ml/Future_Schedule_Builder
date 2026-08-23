from models import Worker, Shift
from solver import generate_schedule


def run_solver_test():
    # 1. Define Worker Pool
    workers = [
        Worker(uid=1, name="Alice", talents=["Patrol", "Kitchen"]),
        Worker(uid=2, name="Bob", talents=["Patrol", "Entrance"]),
        Worker(uid=3, name="Charlie", talents=["Entrance", "Kitchen"]),
        Worker(uid=4, name="David", talents=["Patrol", "Entrance", "Kitchen"]),
    ]

    # 2. Define Shift Schedule
    shifts = [
        # Monday Shifts
        Shift(id="m1_p1", day="Monday 24.08", job_name="Patrol 1", time_slot="06-14", hours_duration=8, abs_start_hour=6.0),
        Shift(id="m1_ent", day="Monday 24.08", job_name="Entrance", time_slot="06-14", hours_duration=8, abs_start_hour=6.0),
        Shift(id="m2_p1", day="Monday 24.08", job_name="Patrol 1", time_slot="14-22", hours_duration=8, abs_start_hour=14.0),
        Shift(id="m2_kit", day="Monday 24.08", job_name="Kitchen", time_slot="14-22", hours_duration=8, abs_start_hour=14.0),
        Shift(id="m3_p1", day="Monday 24.08", job_name="Patrol 1", time_slot="22-06", hours_duration=8, abs_start_hour=22.0),
        
        # Tuesday Shifts
        Shift(id="t1_p1", day="Tuesday 25.08", job_name="Patrol 1", time_slot="06-14", hours_duration=8, abs_start_hour=30.0),
        Shift(id="t1_ent", day="Tuesday 25.08", job_name="Entrance", time_slot="06-14", hours_duration=8, abs_start_hour=30.0),
        Shift(id="t2_kit", day="Tuesday 25.08", job_name="Kitchen", time_slot="14-22", hours_duration=8, abs_start_hour=38.0),
    ]



    print("--- Running OR-Tools Schedule Generator ---\n")
    schedule = generate_schedule(workers, shifts)

    print("--- Generated Schedule ---")
    for shift in shifts:
        worker = schedule.get(shift.id)
        worker_name = worker.name if worker else "UNASSIGNED"
        print(f"[{shift.day} | {shift.time_slot}] {shift.job_name:<10} -> Assigned: {worker_name}")

    print("\n--- Final Worker Stats ---")
    for w in workers:
        print(f"{w.name:<8} | Total Hours: {w.total_hours}h | Despair Score: {w.despair:.2f}")

if __name__ == "__main__":
    run_solver_test()

    

