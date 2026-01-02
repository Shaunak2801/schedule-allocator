import os
from typing import Dict, List

from flask import Flask, send_file, request, jsonify
from ortools.linear_solver import pywraplp
from pydantic import BaseModel, ValidationError, field_validator

app = Flask(__name__)

# ===================== MODELS (Pydantic) =====================

class Employee(BaseModel):
    name: str
    preferred_shift: str | None = None          # "Morning" | "Afternoon" | "Night" | None
    criticality: str = "low"                    # "low" | "medium" | "high"
    leave_days: List[str] = []                  # e.g. ["Sat","Sun"]

    @field_validator("leave_days", mode="before")
    @classmethod
    def clean_leave_days(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return [day.strip() for day in v if isinstance(day, str) and day.strip()]
        return []

class OptimizeCompactRequest(BaseModel):
    employees: List[Employee]
    days: List[str]                              # ["Mon","Tue",...]
    shifts: List[str]                            # ["Morning","Afternoon","Night"]
    defaults: Dict[str, Dict[str, int]]          # hours_per_shift/min_coverage/max_coverage
    weekly_hours: int = 40                       # GLOBAL DEFAULT
    historical_shifts: Dict[str, List[str]] = {}
    calendar_events: Dict[str, Dict[str, int]] = {}   # per-employee: { emp: {shift:count} }

# ===================== PREFERENCE ENGINE =====================

def calculate_preferences_for_employee(emp: Employee, shifts: List[str], past_shifts: List[str]) -> Dict[str, float]:
    base_score = 5.0
    prefs = {s: base_score for s in shifts}

    # Preferred shift boost/penalty
    if emp.preferred_shift:
        for s in shifts:
            if s == emp.preferred_shift:
                prefs[s] += 5.0
            else:
                prefs[s] -= 2.0

    # Fatigue penalty from last 7 shifts
    recent = past_shifts[-7:]
    for s in shifts:
        consec = 0
        for sh in reversed(recent):
            if sh == s:
                consec += 1
            else:
                break

        if consec >= 6:
            penalty = -20.0
        elif consec == 5:
            penalty = -10.0
        elif consec == 4:
            penalty = -5.0
        else:
            penalty = 0.0

        if emp.criticality == "high":
            penalty *= 0.25
        elif emp.criticality == "medium":
            penalty *= 0.6

        prefs[s] += penalty

    return {s: max(1.0, v) for s, v in prefs.items()}

def build_preferences(employees: List[Employee], shifts: List[str], historical_shifts: Dict[str, List[str]]) -> Dict[str, Dict[str, float]]:
    prefs: Dict[str, Dict[str, float]] = {}
    for emp in employees:
        history = historical_shifts.get(emp.name, [])
        prefs[emp.name] = calculate_preferences_for_employee(emp, shifts, history)
    return prefs

# ===================== HELPERS =====================

def build_employee_roster(solver, x_vars, employee_names: List[str], days: List[str], shifts: List[str], leave_data: Dict[str, List[str]]) -> Dict:
    roster = {}
    for e in employee_names:
        employee_shifts = {}
        leave_set = set(leave_data.get(e, []))
        for d in days:
            if d in leave_set:
                employee_shifts[d] = "OFF (PAID LEAVE)"
                continue

            assigned_shift = None
            for s in shifts:
                if x_vars[(e, d, s)].solution_value() > 0.5:
                    assigned_shift = s
                    break
            employee_shifts[d] = assigned_shift or "OFF"
        roster[e] = employee_shifts
    return roster

def normalize_calendar_events(calendar_events: Dict[str, Dict[str, int]], shifts: List[str]) -> Dict[str, Dict[str, int]]:
    """
    Normalizes shift keys (maps 'Evening' -> 'Night') and keeps counts as ints.
    Stored as a case-insensitive dict by employee key (lowercase).
    """
    out: Dict[str, Dict[str, int]] = {}
    for emp_key, shift_map in (calendar_events or {}).items():
        ek = (emp_key or "").strip().lower()
        if not ek:
            continue
        sm = shift_map or {}
        norm_sm: Dict[str, int] = {}
        for k, v in sm.items():
            shift_key = (k or "").strip()
            if shift_key.lower() == "evening":
                shift_key = "Night"
            # only keep keys that match shifts list OR are mappable to one of them
            try:
                norm_sm[shift_key] = int(v) if v is not None else 0
            except Exception:
                norm_sm[shift_key] = 0
        out[ek] = norm_sm
    return out

def detect_breached_constraint(
    employees, days, shifts, leave_data, weekly_hours_global,
    hours_per_shift, min_coverage, max_coverage
):
    """
    Quick diagnosis: If it becomes feasible when hours/shifts targets are removed,
    then HOURS_BALANCE is the blocker; otherwise it's coverage/structure.
    """
    employee_names = [e.name for e in employees]
    solver = pywraplp.Solver.CreateSolver("SCIP")
    x_test = {(e, d, s): solver.BoolVar(f"test_{e}_{d}_{s}") for e in employee_names for d in days for s in shifts}

    # Leaves
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                solver.Add(solver.Sum(x_test[(e, leave_day, s)] for s in shifts) == 0)

    # Coverage
    for d in days:
        for s in shifts:
            coverage = solver.Sum(x_test[(e, d, s)] for e in employee_names)
            solver.Add(coverage >= min_coverage[s])
            solver.Add(coverage <= max_coverage[s])

    # 1 shift/day
    for e in employee_names:
        for d in days:
            solver.Add(solver.Sum(x_test[(e, d, s)] for s in shifts) <= 1)

    # Night -> Morning
    if "Night" in shifts and "Morning" in shifts:
        for e in employee_names:
            for i in range(len(days) - 1):
                d1, d2 = days[i], days[i + 1]
                solver.Add(x_test[(e, d1, "Night")] + x_test[(e, d2, "Morning")] <= 1)

    if solver.Solve() != pywraplp.Solver.INFEASIBLE:
        return "HOURS_BALANCE"
    return "COVERAGE_LIMITS"

# ===================== CORE SOLVER LOGIC =====================

def solve_shift_roster(payload: dict) -> dict:
    try:
        req = OptimizeCompactRequest(**payload)
    except ValidationError as e:
        return {"status": "INVALID_REQUEST", "error": e.errors()}

    employees: List[Employee] = req.employees
    days: List[str] = req.days
    shifts: List[str] = req.shifts
    defaults = req.defaults
    weekly_hours_global = req.weekly_hours
    historical_shifts = req.historical_shifts

    hours_per_shift = defaults.get("hours_per_shift", {"Morning": 8, "Afternoon": 8, "Night": 8})
    min_coverage = defaults.get("min_coverage", {"Morning": 1, "Afternoon": 1, "Night": 1})
    max_coverage = defaults.get("max_coverage", {"Morning": 20, "Afternoon": 20, "Night": 10})

    employee_names = [e.name for e in employees]
    leave_data = {e.name: e.leave_days for e in employees}

    # Calendar events: per employee, case-insensitive keys; map "Evening" -> "Night"
    calendar_events_raw = req.calendar_events
    calendar_events_ci = normalize_calendar_events(calendar_events_raw, shifts)

    # Preferences
    preferences = build_preferences(employees, shifts, historical_shifts)

    # ----- 2 default OFFs + additional paid leave OFFs -----
    # Requires weekly_hours to be divisible by shift length (common in your data: 45/9, 40/8, etc.)
    if not shifts:
        return {"status": "INVALID_REQUEST", "error": "No shifts provided"}

    # Assumption: shift lengths are uniform (your config uses 9/9/9). Use Morning if present else first shift.
    ref_shift = "Morning" if "Morning" in hours_per_shift else shifts[0]
    shift_hours = int(hours_per_shift.get(ref_shift, 0))
    if shift_hours <= 0:
        return {"status": "INVALID_REQUEST", "error": f"Invalid hours_per_shift for '{ref_shift}'"}

    if weekly_hours_global % shift_hours != 0:
        return {
            "status": "INVALID_REQUEST",
            "error": f"weekly_hours ({weekly_hours_global}) must be divisible by shift_hours ({shift_hours}) to enforce 2 OFF + paid leaves."
        }

    weekly_shifts_target = weekly_hours_global // shift_hours  # e.g. 45/9 = 5
    default_off_days = 7 - weekly_shifts_target               # e.g. 7-5 = 2

    if default_off_days != 2:
        # You can relax this if you want, but your requirement says 2.
        return {
            "status": "INVALID_REQUEST",
            "error": f"Given weekly_hours={weekly_hours_global} and shift_hours={shift_hours}, default OFF days would be {default_off_days}, not 2."
        }

    # Calendar bonus helpers
    def get_calendar_bonus(e: str, s: str) -> float:
        emp_map = calendar_events_ci.get(e.lower(), {})
        # also accept "Evening" in incoming but normalized already
        count = emp_map.get(s, 0)
        try:
            count = int(count) if count is not None else 0
        except Exception:
            count = 0
        return min(count * 0.25, 1.0)  # tune these weights freely

    # ===================== Phase 1: Strict solve =====================
    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        return {"status": "SOLVER_ERROR", "roster": {}}

    x = {(e, d, s): solver.BoolVar(f"x_{e}_{d}_{s}") for e in employee_names for d in days for s in shifts}

    # Objective: (base preference + calendar bonus) * assignment
    # IMPORTANT FIX: parentheses so x multiplies the full coefficient.
    solver.Maximize(
        solver.Sum(
            (preferences.get(e, {}).get(s, 0.0) + get_calendar_bonus(e, s)) * x[(e, d, s)]
            for e in employee_names for d in days for s in shifts
        )
    )

    # 1) One shift/day
    for e in employee_names:
        for d in days:
            solver.Add(solver.Sum(x[(e, d, s)] for s in shifts) <= 1)

    # 2) No work on leave days (paid leave OFF)
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                solver.Add(solver.Sum(x[(e, leave_day, s)] for s in shifts) == 0)

    # 3) Coverage min/max
    for d in days:
        for s in shifts:
            coverage = solver.Sum(x[(e, d, s)] for e in employee_names)
            solver.Add(coverage >= min_coverage[s])
            solver.Add(coverage <= max_coverage[s])

    # 4) Enforce: 2 default OFFs + paid leaves are additional
    #    => worked_shifts = weekly_shifts_target - leave_count
    for e in employee_names:
        leave_count = len([ld for ld in leave_data.get(e, []) if ld in days])
        required_work_shifts = weekly_shifts_target - leave_count
        if required_work_shifts < 0:
            return {"status": "INVALID_REQUEST", "error": f"{e} has too many leave days ({leave_count}) for weekly_hours={weekly_hours_global}."}

        worked_shifts = solver.Sum(x[(e, d, s)] for d in days for s in shifts)
        solver.Add(worked_shifts == required_work_shifts)

        # Optional (redundant if uniform shift hours): exact work hours
        worked_hours = solver.Sum(hours_per_shift[s] * x[(e, d, s)] for d in days for s in shifts)
        solver.Add(worked_hours == required_work_shifts * shift_hours)

    # 5) No Night -> Morning
    if "Night" in shifts and "Morning" in shifts:
        for e in employee_names:
            for i in range(len(days) - 1):
                d1, d2 = days[i], days[i + 1]
                solver.Add(x[(e, d1, "Night")] + x[(e, d2, "Morning")] <= 1)

    status_code = solver.Solve()

    if status_code in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        roster = build_employee_roster(solver, x, employee_names, days, shifts, leave_data)
        return {
            "status": "OPTIMAL" if status_code == pywraplp.Solver.OPTIMAL else "FEASIBLE",
            "objective_value": solver.Objective().Value(),
            "roster": roster,
            "num_employees": len(employee_names),
            "num_leaves": sum(len(leave_data.get(e, [])) for e in employee_names),
            "breached_constraints": []
        }

    # ===================== Phase 2: Relaxation (coverage preserved) =====================
    breached = detect_breached_constraint(
        employees, days, shifts, leave_data,
        weekly_hours_global, hours_per_shift,
        min_coverage, max_coverage
    )

    relaxed_solver = pywraplp.Solver.CreateSolver("SCIP")
    relaxed_x = {(e, d, s): relaxed_solver.BoolVar(f"rx_{e}_{d}_{s}") for e in employee_names for d in days for s in shifts}

    # In relaxed mode, keep the same objective (it helps pick a nicer feasible roster)
    relaxed_solver.Maximize(
        relaxed_solver.Sum(
            (preferences.get(e, {}).get(s, 0.0) + get_calendar_bonus(e, s)) * relaxed_x[(e, d, s)]
            for e in employee_names for d in days for s in shifts
        )
    )

    # Always: 1 shift/day
    for e in employee_names:
        for d in days:
            relaxed_solver.Add(relaxed_solver.Sum(relaxed_x[(e, d, s)] for s in shifts) <= 1)

    # Always: coverage min/max (never relaxed)
    for d in days:
        for s in shifts:
            coverage = relaxed_solver.Sum(relaxed_x[(e, d, s)] for e in employee_names)
            relaxed_solver.Add(coverage >= min_coverage[s])
            relaxed_solver.Add(coverage <= max_coverage[s])

    # Always: leaves OFF
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                relaxed_solver.Add(relaxed_solver.Sum(relaxed_x[(e, leave_day, s)] for s in shifts) == 0)

    # Always: Night -> Morning
    if "Night" in shifts and "Morning" in shifts:
        for e in employee_names:
            for i in range(len(days) - 1):
                d1, d2 = days[i], days[i + 1]
                relaxed_solver.Add(relaxed_x[(e, d1, "Night")] + relaxed_x[(e, d2, "Morning")] <= 1)

    # Hours/2-OFF rule: relax only if it's the diagnosed blocker
    if breached != "HOURS_BALANCE":
        for e in employee_names:
            leave_count = len([ld for ld in leave_data.get(e, []) if ld in days])
            required_work_shifts = weekly_shifts_target - leave_count
            if required_work_shifts < 0:
                continue
            worked_shifts = relaxed_solver.Sum(relaxed_x[(e, d, s)] for d in days for s in shifts)
            relaxed_solver.Add(worked_shifts == required_work_shifts)

    relaxed_solver.Solve()
    roster = build_employee_roster(relaxed_solver, relaxed_x, employee_names, days, shifts, leave_data)

    return {
        "status": "RELAXED",
        "objective_value": relaxed_solver.Objective().Value(),
        "roster": roster,
        "num_employees": len(employee_names),
        "num_leaves": sum(len(leave_data.get(e, [])) for e in employee_names),
        "breached_constraints": [breached],
        "relaxation_note": f"Ignored '{breached}' (coverage preserved)"
    }

# ===================== FLASK ROUTES =====================

@app.route("/")
def index():
    return send_file("src/index.html")

@app.route("/optimize", methods=["POST"])
def optimize():
    payload = request.get_json(force=True)
    result = solve_shift_roster(payload)
    return jsonify(result)

def main():
    port = int(os.environ.get("PORT", 80))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
