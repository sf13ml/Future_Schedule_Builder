from ortools.sat.python import cp_model
from models import Worker, Shift, PolicyConfig
from datetime import datetime
import statistics
from typing import Optional, Dict, List, Tuple, Any

def generate_schedule(workers: List[Worker], shifts: List[Shift], policy: PolicyConfig = PolicyConfig()) -> dict:
    model = cp_model.CpModel()
    objective_terms = []

    # 1. ASSIGNMENT VARIABLES
    assign_vars = {}
    for w in workers:
        for s in shifts:
            assign_vars[(w.uid, s.id)] = model.NewBoolVar(f"w_{w.uid}_s_{s.id}")

    # 2. SHIFT CAPACITY & SKILL CONSTRAINT WITH SLACK (SOFT DEMAND)
    unstaffed_vars = {}
    
    # Large penalty weight to ensure filling shifts is prioritized over worker preference/despair optimization
    UNSTAFFED_SLOT_PENALTY = 100000

    for s in shifts:
        req_workers = getattr(s.job, 'required_number_workers', 1)
        shift_workers = []

        for w in workers:
            if w.is_eligible_for_job(s.job):
                shift_workers.append(assign_vars[(w.uid, s.id)])
            else:
                model.Add(assign_vars[(w.uid, s.id)] == 0)

        # Create slack variable representing missing workers for this shift
        unstaffed_slack = model.NewIntVar(0, req_workers, f"unstaffed_shift_{s.id}")
        unstaffed_vars[s.id] = unstaffed_slack

        # Soft Constraint Equation: Assigned Workers + Missing Workers = Required Workers
        model.Add(sum(shift_workers) + unstaffed_slack == req_workers)

        # Add heavy penalty to objective function for any unstaffed slot
        objective_terms.append(unstaffed_slack * UNSTAFFED_SLOT_PENALTY)

    # 3. LEAVE & UNAVAILABILITY HARD CONSTRAINTS
    for w in workers:
        for s in shifts:
            if s.day in w.unavailable_days:
                model.Add(assign_vars[(w.uid, s.id)] == 0)
                continue
            
            s_end = s.abs_start_hour + s.hours_duration
            is_blocked = any(
                s.abs_start_hour < b_end and b_start < s_end
                for (b_start, b_end) in getattr(w, 'blocked_intervals', [])
            )
            if is_blocked:
                model.Add(assign_vars[(w.uid, s.id)] == 0)

    # 4. OVERLAPS & REST CONSTRAINTS (Unified Sweep)
    sorted_shifts = sorted(shifts, key=lambda s: s.abs_start_hour)
    enforce_rest = getattr(policy, 'enforce_strict_rest', False)
    
    for w in workers:
        for i, s1 in enumerate(sorted_shifts):
            s1_end = s1.abs_start_hour + s1.hours_duration
            
            required_rest = 0.0
            if enforce_rest:
                required_rest = getattr(s1.job, 'min_rest_override', None)
                if required_rest is None:
                    required_rest = getattr(s1.job, 'min_rest_hours', policy.min_rest_hours_default)
            
            min_next_allowed_start = s1_end + required_rest

            for s2 in sorted_shifts[i+1:]:
                if s2.abs_start_hour >= min_next_allowed_start:
                    break
                
                model.AddAtMostOne([
                    assign_vars[(w.uid, s1.id)], 
                    assign_vars[(w.uid, s2.id)]
                ])

    # 3.6 MAX HOURS CAPACITY
    if getattr(policy, 'enforce_max_hours_cap', False):
        for w in workers:
            max_cap = int(getattr(w, 'max_hours', None) or policy.max_weekly_hours_per_worker)
            model.Add(
                sum(assign_vars[(w.uid, s.id)] * int(s.hours_duration) for s in shifts) <= max_cap
            )

    # 5. WORKLOAD & PREFERENCE OBJECTIVE TERMS
    for w in workers:
        worker_assigned_hours = model.NewIntVar(0, 168, f"total_hours_{w.uid}")
        model.Add(
            worker_assigned_hours == sum(
                assign_vars[(w.uid, s.id)] * int(s.hours_duration) for s in shifts
            )
        )

        t1 = model.NewIntVar(0, 24, f"t1_{w.uid}")
        t2 = model.NewIntVar(0, 16, f"t2_{w.uid}")
        t3 = model.NewIntVar(0, 16, f"t3_{w.uid}")
        t4 = model.NewIntVar(0, 112, f"t4_{w.uid}")

        model.Add(worker_assigned_hours == t1 + t2 + t3 + t4)
        tier_cost = (t1 * 10) + (t2 * 18) + (t3 * 35) + (t4 * 70)
        objective_terms.append(tier_cost)

        for s in shifts:
            hour_of_day = s.abs_start_hour % 24
            is_night = (hour_of_day >= 22.0) or (hour_of_day < 6.0) or ("night" in s.time_slot.lower())
            
            time_multiplier = policy.night_shift_penalty_multiplier if is_night else 1.0
            if is_night and getattr(w, 'prefers_night', False):
                time_multiplier = policy.night_owl_discount

            job_despair_factor = getattr(s.job, 'despair_factor', 1.0)
            hist_hours = getattr(w, 'total_hours', 0) or 0
            historical_factor = 1.0 + (min(hist_hours / 40.0, 1.5) * policy.historical_load_scaling_weight) if hist_hours else 1.0

            shift_base_cost = int(round(s.hours_duration * time_multiplier * job_despair_factor * historical_factor * 100))
            objective_terms.append(assign_vars[(w.uid, s.id)] * shift_base_cost)

    model.Minimize(sum(objective_terms))

    # 6. SOLVE MODEL
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    solver.parameters.search_branching = cp_model.PORTFOLIO_SEARCH
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = []
        unstaffed_report = []
        diagnostics = []

        worker_hours_map = {w.uid: getattr(w, 'total_hours', 0) for w in workers}
        worker_despair_map = {w.uid: getattr(w, 'despair', 0.0) for w in workers}
        job_stats = {}

        # Collect unstaffed slot diagnostics
        for s in shifts:
            missing_count = solver.Value(unstaffed_vars[s.id])
            if missing_count > 0:
                unstaffed_report.append({
                    "shift_id": s.id,
                    "job_name": getattr(s.job, 'name', str(s.job)),
                    "missing_slots": missing_count,
                    "required_slots": getattr(s.job, 'required_number_workers', 1)
                })
                diagnostics.append({
                    "severity": "WARNING",
                    "code": "UNDERSTAFFED_SHIFT",
                    "entity": s.id,
                    "reason": f"Shift '{s.id}' is short by {missing_count} worker(s).",
                    "remediation": "Adjust worker availability or recruit extra staff."
                })

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
                    if is_night and getattr(w, 'prefers_night', False):
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

        if not diagnostics:
            diagnostics.append({
                "severity": "INFO",
                "code": "OPTIMAL_SCHEDULE",
                "entity": "Global Roster",
                "reason": "All demand slots satisfied under soft load balancing.",
                "remediation": "None required."
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
            {"label": "Total Unstaffed Slots", "value": sum(r["missing_slots"] for r in unstaffed_report)},
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
            "status": "optimal" if (status == cp_model.OPTIMAL and not unstaffed_report) else "feasible",
            "meta": {
                "generated_at": datetime.utcnow().isoformat(),
                "status": "success",
                "total_assignments": len(assignments),
                "unstaffed_slots": unstaffed_report,
                "policy_applied": policy.to_dict() if hasattr(policy, "to_dict") else str(policy)
            },
            "assignments": assignments,
            "worker_summaries": worker_summaries,
            "metrics": {
                "summary_cards": summary_cards,
                "job_coverage": job_coverage,
                "manpower_utilization": manpower_utilization
            },
            "diagnostics": diagnostics
        }

    return {
        "status": "infeasible",
        "detail": "Optimization Infeasible: Hard structural constraint collision.",
        "diagnostics": [{
            "severity": "CRITICAL",
            "code": "SOLVER_INFEASIBLE",
            "entity": "Global Matrix",
            "reason": "Hard constraints (unavailability/rest blocks) prevented finding any valid schedule.",
            "remediation": "Relax strict rest constraints or check worker availability windows."
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

def build_and_solve_schedule(
    workers: List[Worker],
    shifts: List[Shift],
    policy: PolicyConfig
) -> Dict[str, Any]:
    model = cp_model.CpModel()
    
    # 1. Filter Decision Variables (Skip Fixed/Historical Shifts)
    # Decision variables are only created for shifts that need assignment (is_fixed == False)
    assign_vars: Dict[Tuple[str, str], cp_model.BoolVar] = {}
    
    planning_shifts = [s for s in shifts if not s.is_fixed]
    
    for w in workers:
        for s in planning_shifts:
            if w.is_eligible_for_job(s.job):
                assign_vars[(w.uid, s.id)] = model.NewBoolVar(f"assign_{w.uid}_{s.id}")
            else:
                # Ineligible workers cannot be assigned
                pass


    # 2. Hard Rest Constraints across $T_{cutoff}$ Boundary
    min_rest = policy.min_rest_hours_default

    for w in workers:
        # Check if worker worked right up to or near T_cutoff
        if w.last_assigned_end_hour > -8.0:
            for s in planning_shifts:
                if (w.uid, s.id) in assign_vars:
                    job_rest = s.job.min_rest_override if s.job.min_rest_override is not None else min_rest
                    
                    # If shift starts before (last_end_hour + rest_period), forbid assignment
                    if s.abs_start_hour < w.last_assigned_end_hour + job_rest:
                        model.Add(assign_vars[(w.uid, s.id)] == 0)

    # 3. Workload Balance & Convex Tiering Offset
    # Define Tiers (in integer hours for CP-SAT integer precision)
    # Tier 1: 0 - 40 hours (Standard rate)
    # Tier 2: 40 - 50 hours (Overtime penalty)
    # Tier 3: 50+ hours (Severe overtime penalty)
    
    SCALE = 10 
    
    total_cost_terms = []

    for w in workers:
        # Calculate planned duration integer variable
        planned_hours_expr = []
        for s in planning_shifts:
            if (w.uid, s.id) in assign_vars:
                planned_hours_expr.append(assign_vars[(w.uid, s.id)] * int(s.hours_duration * SCALE))

        # Total work variable = scaled_historical + scaled_planned
        hist_scaled = int(w.historical_hours * SCALE)
        
        total_hours_var = model.NewIntVar(
            hist_scaled, 
            int(policy.max_weekly_hours_per_worker * SCALE), 
            f"total_hours_{w.uid}"
        )
        model.Add(total_hours_var == hist_scaled + sum(planned_hours_expr))

        # --- Tier 2 Overtime Penalty ( > 40 hours ) ---
        # Penalize only hours beyond 40.0 cumulative total
        tier2_threshold = 40 * SCALE
        tier2_excess = model.NewIntVar(0, int(policy.max_weekly_hours_per_worker * SCALE), f"t2_excess_{w.uid}")
        
        # tier2_excess >= total_hours_var - tier2_threshold
        model.Add(tier2_excess >= total_hours_var - tier2_threshold)
        model.Add(tier2_excess >= 0)
        
        # Cost penalty applied to tier 2 hours
        total_cost_terms.append(tier2_excess * 15)

        # --- Tier 3 Heavy Overtime Penalty ( > 50 hours ) ---
        tier3_threshold = 50 * SCALE
        tier3_excess = model.NewIntVar(0, int(policy.max_weekly_hours_per_worker * SCALE), f"t3_excess_{w.uid}")
        
        model.Add(tier3_excess >= total_hours_var - tier3_threshold)
        model.Add(tier3_excess >= 0)
        
        # Progressive penalty multiplier for severe load
        total_cost_terms.append(tier3_excess * 50)

    # 4. Objective Function & Solving
    model.Minimize(sum(total_cost_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)

    # --- Post-Processing: Update Worker Objects ---
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for w in workers:
            assigned_planned_hours = 0.0
            for s in planning_shifts:
                if (w.uid, s.id) in assign_vars and solver.Value(assign_vars[(w.uid, s.id)]) == 1:
                    assigned_planned_hours += s.hours_duration
                    s.assigned_worker_uids.append(w.uid)
            
            # Update Pydantic model state
            w.planned_hours = assigned_planned_hours
            
        return {"status": "SUCCESS"}
    
    return {"status": "INFEASIBLE"}