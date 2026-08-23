from ortools.sat.python import cp_model
from models import Worker, Shift, PolicyConfig
from datetime import datetime
import statistics
from typing import Optional, Dict, List

def generate_schedule(workers: list[Worker], shifts: list[Shift], policy: PolicyConfig = PolicyConfig()) -> dict:
    model = cp_model.CpModel()

    # 1. ASSIGNMENT VARIABLES
    assign_vars = {}
    for w in workers:
        for s in shifts:
            assign_vars[(w.uid, s.id)] = model.NewBoolVar(f"w_{w.uid}_s_{s.id}")

    # 2. SHIFT CAPACITY & SKILL CONSTRAINT
    for s in shifts:
        shift_workers = []
        for w in workers:
            if w.is_eligible_for_job(s.job):
                shift_workers.append(assign_vars[(w.uid, s.id)])
            else:
                model.Add(assign_vars[(w.uid, s.id)] == 0)
        
        if shift_workers:
            model.Add(sum(shift_workers) == s.job.required_number_workers)
        else:
            return {
                "status": "infeasible",
                "message": f"Infeasibility Forensic: Shift '{s.id}' requires talents {s.job.required_talents}, but zero workers in the roster qualify.",
                "diagnostics": [{
                    "severity": "CRITICAL",
                    "code": "TALENT_MISSING",
                    "entity": getattr(s.job, 'name', str(s.job)),
                    "reason": f"Shift '{s.id}' requires talents {getattr(s.job, 'required_talents', [])}, but zero workers in the roster qualify.",
                    "remediation": "Add qualified personnel to the workforce roster or adjust shift skill requirements."
                }]
            }

    # 3. LEAVE & UNAVAILABILITY HARD CONSTRAINTS
    for w in workers:
        for s in shifts:
            if s.day in w.unavailable_days:
                model.Add(assign_vars[(w.uid, s.id)] == 0)
                continue
            
            s_end = s.abs_start_hour + s.hours_duration
            is_blocked = False
            for (b_start, b_end) in w.blocked_intervals:
                if s.abs_start_hour < b_end and b_start < s_end:
                    is_blocked = True
                    break
            if is_blocked:
                model.Add(assign_vars[(w.uid, s.id)] == 0)

    # 3.5 NO CONCURRENT OVERLAPS 
    for w in workers:
        for i, s1 in enumerate(shifts):
            for s2 in shifts[i+1:]:
                if s1.id == s2.id:
                    continue
            
                s1_end = s1.abs_start_hour + s1.hours_duration
                s2_end = s2.abs_start_hour + s2.hours_duration
            
                # Check for temporal overlap
                if s1.abs_start_hour < s2_end and s2.abs_start_hour < s1_end:
                    model.AddAtMostOne([
                        assign_vars[(w.uid, s1.id)], 
                        assign_vars[(w.uid, s2.id)]
                ])

    # 3.6 MAX HOURS CAPACITY (Conditional on Policy Flag)
    if getattr(policy, 'enforce_max_hours_cap', False):
        for w in workers:
            max_cap = int(getattr(w, 'max_hours', None) or policy.max_weekly_hours_per_worker)
            model.Add(
                sum(assign_vars[(w.uid, s.id)] * int(s.hours_duration) for s in shifts) <= max_cap
            )

    # 4. CONFIGURABLE REST CONSTRAINTS (Conditional on Policy Flag)
    if getattr(policy, 'enforce_strict_rest', False):
        for w in workers:
            for s1 in shifts:
                s1_end = s1.abs_start_hour + s1.hours_duration
                required_rest = getattr(s1.job, 'min_rest_override', None)
                if required_rest is None:
                    required_rest = getattr(s1.job, 'min_rest_hours', policy.min_rest_hours_default)

                for s2 in shifts:
                    if s1.id == s2.id:
                        continue
                    if s1.abs_start_hour <= s2.abs_start_hour < (s1_end + required_rest):
                        model.AddForbiddenAssignments([assign_vars[(w.uid, s1.id)], assign_vars[(w.uid, s2.id)]], [(1, 1)])

    # 5. OBJECTIVE FUNCTION: Convex Workload Tiering & Shift Despair Minimization
        objective_terms = []

        for w in workers:
            # 5.1 Create intermediate total assigned hours for worker in current schedule
            worker_assigned_hours = model.NewIntVar(0, 168, f"total_hours_{w.uid}")
            model.Add(
                worker_assigned_hours == sum(
                    assign_vars[(w.uid, s.id)] * int(s.hours_duration) for s in shifts
                )
            )

            # 5.2 Convex Tier Variables for current schedule
            # Tier 1: 0 - 24 hours
            # Tier 2: 24 - 40 hours
            # Tier 3: 40 - 56 hours
            # Tier 4: > 56 hours
            t1 = model.NewIntVar(0, 24, f"t1_{w.uid}")
            t2 = model.NewIntVar(0, 16, f"t2_{w.uid}")
            t3 = model.NewIntVar(0, 16, f"t3_{w.uid}")
            t4 = model.NewIntVar(0, 112, f"t4_{w.uid}")

            model.Add(worker_assigned_hours == t1 + t2 + t3 + t4)

            # Add convex bucket costs (scaled to integers for CP-SAT)
            # Tier 1 = 1.0x (10), Tier 2 = 1.8x (18), Tier 3 = 3.5x (35), Tier 4 = 7.0x (70)
            tier_cost = (t1 * 10) + (t2 * 18) + (t3 * 35) + (t4 * 70)
            objective_terms.append(tier_cost)

            # 5.3 Shift Preference & Job Specific Modifiers
            for s in shifts:
                hour_of_day = s.abs_start_hour % 24
                is_night = (hour_of_day >= 22.0) or (hour_of_day < 6.0) or ("night" in s.time_slot.lower())
                
                time_multiplier = policy.night_shift_penalty_multiplier if is_night else 1.0
                if is_night and w.prefers_night:
                    time_multiplier = policy.night_owl_discount

                job_despair_factor = getattr(s.job, 'despair_factor', 1.0)
                
                # Historical context penalty from past weeks
                historical_factor = 1.0 + (min(w.total_hours / 40.0, 1.5) * policy.historical_load_scaling_weight) if w.total_hours else 1.0

                shift_base_cost = int(s.hours_duration * time_multiplier * job_despair_factor * historical_factor * 5)
                objective_terms.append(assign_vars[(w.uid, s.id)] * shift_base_cost)

                model.Minimize(sum(objective_terms))

    # 6. SOLVE MODEL
    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = []
        worker_hours_map = {w.uid: w.total_hours for w in workers}
        worker_despair_map = {w.uid: getattr(w, 'despair', 0.0) for w in workers}

        job_stats = {}

        for s in shifts:
            job_name = s.job.name if hasattr(s.job, "name") else str(s.job)
            req_workers = getattr(s.job, 'required_number_workers', 1)

            if job_name not in job_stats:
                job_stats[job_name] = {
                    "required_slots": 0,
                    "filled_slots": 0,
                    "total_despair": 0.0,
                    "assigned_hours": 0.0
                }
            job_stats[job_name]["required_slots"] += req_workers

            for w in workers:
                if solver.Value(assign_vars[(w.uid, s.id)]) == 1:
                    hour_of_day = s.abs_start_hour % 24
                    is_night = (hour_of_day >= 22.0) or (hour_of_day < 6.0) or ("night" in s.time_slot.lower())
                    multiplier = policy.night_shift_penalty_multiplier if is_night else 1.0
                    if is_night and w.prefers_night:
                        multiplier = policy.night_owl_discount
                    
                    job_despair_factor = getattr(s.job, 'despair_factor', 1.0)
                    despair_added = s.hours_duration * multiplier * job_despair_factor

                    worker_despair_map[w.uid] += despair_added
                    worker_hours_map[w.uid] += s.hours_duration

                    job_stats[job_name]["filled_slots"] += 1
                    job_stats[job_name]["total_despair"] += despair_added
                    job_stats[job_name]["assigned_hours"] += s.hours_duration

                    assignments.append({
                        "shift_id": s.id,
                        "day_name": s.day,
                        "time_slot": s.time_slot,
                        "time_block": getattr(s, "time_block", "Morning"), 
                        "job_category": job_name,
                        "slot_code": getattr(s, "slot_label", "") or (s.id.split("_")[-1] if "-" in s.id else s.id),  
                        "abs_start_hour": s.abs_start_hour,
                        "hours_duration": s.hours_duration,
                        "worker_uid": w.uid,
                        "worker_name": w.name
                    })

        manpower_utilization = []
        total_pool_capacity_hours = 0.0
        total_assigned_hours = 0.0
        total_pool_despair = 0.0

        for w in workers:
            max_cap = float(getattr(w, 'max_hours', None) or 96.0)
            assigned_hrs = worker_hours_map[w.uid]
            cdr_score = round(worker_despair_map[w.uid], 2)
            tcu_pct = (assigned_hrs / max_cap * 100.0) if max_cap > 0 else 0.0

            total_pool_capacity_hours += max_cap
            total_assigned_hours += assigned_hrs
            total_pool_despair += cdr_score

            manpower_utilization.append({
                "worker_id": w.uid,
                "name": w.name,
                "assigned_hours": assigned_hrs,
                "max_capacity_hours": max_cap,
                "tcu_percent": f"{tcu_pct:.1f}%",
                "cdr_score": cdr_score
            })

        overall_tcu_pct = (total_assigned_hours / total_pool_capacity_hours * 100.0) if total_pool_capacity_hours > 0 else 0.0
        avg_pool_cdr = (total_pool_despair / len(workers)) if workers else 0.0

        summary_cards = [
            {"label": "Overall Pool Capacity Utilization (TCU)", "value": f"{overall_tcu_pct:.1f}%"},
            {"label": "Overall Avg. Cumulative Despair (CDR)", "value": f"{avg_pool_cdr:.2f}"},
            {"label": "Total Pool Capacity", "value": f"{total_pool_capacity_hours:.0f} hrs"},
            {"label": "Total Hours Assigned", "value": f"{total_assigned_hours:.0f} hrs"},
            {"label": "Total Shifts Scheduled", "value": len(assignments)}
        ]

        job_coverage = []
        for j_name, j_data in job_stats.items():
            req_slots = j_data["required_slots"]
            filled_slots = j_data["filled_slots"]
            job_tcu_val = (filled_slots / req_slots * 100.0) if req_slots > 0 else 0.0
            job_avg_cdr_val = (j_data["total_despair"] / filled_slots) if filled_slots > 0 else 0.0

            job_coverage.append({
                "job_id": j_name,
                "job_name": j_name,
                "required_slots": req_slots,
                "filled_slots": filled_slots,
                "job_tcu": f"{job_tcu_val:.1f}%",
                "job_avg_cdr": f"{job_avg_cdr_val:.2f}"
            })

        worker_summaries = []
        for w in workers:
            w_dict = w.to_dict() if hasattr(w, "to_dict") else {"uid": w.uid, "name": w.name}
            w_dict["total_hours"] = worker_hours_map[w.uid]
            w_dict["despair"] = round(worker_despair_map[w.uid], 2)
            worker_summaries.append(w_dict)

        return {
            "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
            "meta": {
                "generated_at": datetime.utcnow().isoformat(),
                "status": "success",
                "total_assignments": len(assignments),
                "policy_applied": policy.to_dict() if hasattr(policy, "to_dict") else str(policy)
            },
            "assignments": assignments,
            "worker_summaries": worker_summaries,
            "metrics": {
                "summary_cards": summary_cards,
                "job_coverage": job_coverage,
                "manpower_utilization": manpower_utilization
            },
            "diagnostics": [{
                "severity": "INFO",
                "code": "OPTIMAL_SCHEDULE",
                "entity": "Global Roster",
                "reason": "All demand slots satisfied under soft load balancing.",
                "remediation": "None required."
            }]
        }
    else:
        return {
            "status": "infeasible",
            "detail": "Optimization Infeasible: Hard structural constraint collision.",
            "diagnostics": [{
                "severity": "CRITICAL",
                "code": "SOLVER_INFEASIBLE",
                "entity": "Global Matrix",
                "reason": "Optimization Infeasible: Shift demand overlaps exceed available personnel windows, or strict rest constraints caused a deadlock.",
                "remediation": "Relax strict rest constraints or increase worker pool capacity."
            }]
        }

def _add_inter_shift_rest_constraints(
    model: cp_model.CpModel,
    assign_vars: dict,
    workers: list[Worker],
    shifts: list[Shift],
    policy: PolicyConfig
):
    """
    Enforces minimum mandatory rest between shifts using the employer's policy.
    
    Defaults to 8.0 hours rest. Allows Morning (06:00-14:00) + Night (22:00-06:00) 
    shifts on the same day when gap >= min_rest_hours.
    """
    # TODO: Add job-level or employee-level rest override checks here if specific roles require longer rest
    min_rest = policy.min_rest_hours_default

    for w in workers:
        for s1 in shifts:
            s1_end = s1.abs_start_hour + s1.hours_duration

            for s2 in shifts:
                if s1.id == s2.id:
                    continue

                # Block s2 if it starts after s1 starts, but before (s1_end + min_rest)
                if s1.abs_start_hour <= s2.abs_start_hour < (s1_end + min_rest):
                    model.Add(assign_vars[(w.uid, s1.id)] + assign_vars[(w.uid, s2.id)] <= 1)