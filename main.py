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

    @field_validator('leave_days', mode='before')
    def clean_leave_days(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return [day.strip() for day in v if day and day.strip()]
        return []




class OptimizeCompactRequest(BaseModel):
    employees: List[Employee]
    days: List[str]                             # ["Mon","Tue",...]
    shifts: List[str]                           # ["Morning","Afternoon","Night"]
    defaults: Dict[str, Dict[str, int]]         # { "hours_per_shift": {...}, "min_coverage": {...}, "max_coverage": {...} }
    weekly_hours: int = 40                      # ✅ GLOBAL DEFAULT: 40h for ALL employees
    historical_shifts: Dict[str, List[str]] = {}
    calendar_events: Dict[str, Dict[str, int]] = {}


# ===================== PREFERENCE ENGINE =====================

def calculate_preferences_for_employee(
    emp: Employee,
    shifts: List[str],
    past_shifts: List[str],
) -> Dict[str, float]:
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






def build_preferences(
    employees: List[Employee],
    shifts: List[str],
    historical_shifts: Dict[str, List[str]],
) -> Dict[str, Dict[str, float]]:
    prefs: Dict[str, Dict[str, float]] = {}
    for emp in employees:
        history = historical_shifts.get(emp.name, [])
        prefs[emp.name] = calculate_preferences_for_employee(emp, shifts, history)
    return prefs


# ===================== HELPER FUNCTIONS =====================

def build_employee_roster(solver, x_vars, employee_names: List[str], days: List[str], shifts: List[str]) -> Dict:
    """Build employee-centric roster format"""
    roster = {}
    for e in employee_names:
        employee_shifts = {}
        for d in days:
            assigned_shift = None
            for s in shifts:
                if x_vars[(e, d, s)].solution_value() > 0.5:
                    assigned_shift = s
                    break
            employee_shifts[d] = assigned_shift or "OFF"
        roster[e] = employee_shifts
    return roster


def detect_breached_constraint(employees, days, shifts, leave_data, weekly_hours_global, 
                              hours_per_shift, min_coverage, max_coverage):
    """Diagnose which constraint is blocking"""
    employee_names = [e.name for e in employees]
    
    # Test 1: Hours balance (most common with leaves)
    solver = pywraplp.Solver.CreateSolver("SCIP")
    x_test = {(e,d,s): solver.BoolVar(f"test_{e}_{d}_{s}") for e in employee_names 
              for d in days for s in shifts}
    
    # Always add: leaves, coverage, 1-shift/day
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                solver.Add(sum(x_test[(e, leave_day, s)] for s in shifts) == 0)
    
    for d in days:
        for s in shifts:
            coverage = sum(x_test[(e,d,s)] for e in employee_names)
            solver.Add(coverage >= min_coverage[s])
    
    for e in employee_names:
        for d in days:
            solver.Add(sum(x_test[(e,d,s)] for s in shifts) <= 1)
    
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

    # NEW: Safe calendar events lookup (0 if missing employee/shift)
    calendar_events = payload.get("calendar_events", {})  # {emp_name: {shift: count}}



    preferences = build_preferences(employees, shifts, historical_shifts)

    def get_calendar_bonus(e: str, s: str) -> float:
        emp_cal = calendar_events.get(e, {})  # Missing emp → {}
        count = emp_cal.get(s, 0)             # Missing shift → 0
        return min(count * 0.25, 1.0)         # +0.25/event, cap 1.0

    # Phase 1: Strict solve
    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        return {"status": "SOLVER_ERROR", "roster": {}}

    x = {(e,d,s): solver.BoolVar(f"x_{e}_{d}_{s}") for e in employee_names 
         for d in days for s in shifts}

    solver.Maximize(solver.Sum(preferences.get(e, {}).get(s, 0.0)+ get_calendar_bonus(e, s) * x[(e,d,s)] 
                              for e in employee_names for d in days for s in shifts))
    


    
    
    

    leave_hours_credit = {e: len(leave_data.get(e, [])) * 8 for e in employee_names}

    # 1) Paid leave hours
    for e in employee_names:
        actual_work_hours = sum(hours_per_shift[s] * x[(e,d,s)] for d in days for s in shifts)
        required_work_hours = weekly_hours_global - leave_hours_credit[e]
        solver.Add(actual_work_hours == required_work_hours)

    # 2) No work on leave days
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                solver.Add(sum(x[(e,leave_day,s)] for s in shifts) == 0)

    # 3) Coverage limits
    for d in days:
        for s in shifts:
            coverage = sum(x[(e,d,s)] for e in employee_names)
            solver.Add(coverage >= min_coverage[s])
            solver.Add(coverage <= max_coverage[s])

    # 4) Max 1 shift per day
    for e in employee_names:
        for d in days:
            solver.Add(sum(x[(e,d,s)] for s in shifts) <= 1)

    # 5) No Night→Morning
    if "Night" in shifts and "Morning" in shifts:
        for e in employee_names:
            for i in range(len(days)-1):
                d1, d2 = days[i], days[i+1]
                solver.Add(x[(e,d1,"Night")] + x[(e,d2,"Morning")] <= 1)

    status_code = solver.Solve()

    if status_code == pywraplp.Solver.OPTIMAL or status_code == pywraplp.Solver.FEASIBLE:
        roster = build_employee_roster(solver, x, employee_names, days, shifts)
        return {
            "status": "OPTIMAL" if status_code == pywraplp.Solver.OPTIMAL else "FEASIBLE",
            "objective_value": solver.Objective().Value(),
            "roster": roster,
            "num_employees": len(employee_names),
            "num_leaves": sum(len(leave_data.get(e, [])) for e in employee_names),
            "breached_constraints": []
        }

    # Phase 2+3: RELAXATION (Coverage ALWAYS enforced)
    breached = detect_breached_constraint(employees, days, shifts, leave_data, 
                                         weekly_hours_global, hours_per_shift, 
                                         min_coverage, max_coverage)

    relaxed_solver = pywraplp.Solver.CreateSolver("SCIP")
    relaxed_x = {(e,d,s): relaxed_solver.BoolVar(f"rx_{e}_{d}_{s}") for e in employee_names 
                for d in days for s in shifts}

    relaxed_solver.Maximize(relaxed_solver.Sum(5.0 * relaxed_x[(e,d,s)] 
                                              for e in employee_names for d in days for s in shifts))

    # ✅ ALWAYS: Coverage (never relaxed)
    for d in days:
        for s in shifts:
            coverage = sum(relaxed_x[(e,d,s)] for e in employee_names)
            relaxed_solver.Add(coverage == min_coverage[s])


    # Always: Leaves
    for e in employee_names:
        for leave_day in leave_data.get(e, []):
            if leave_day in days:
                relaxed_solver.Add(sum(relaxed_x[(e,leave_day,s)] for s in shifts) == 0)

    # Hours: Skip if that's the blocker
    if breached != "HOURS_BALANCE":
        for e in employee_names:
            actual_work_hours = sum(hours_per_shift[s] * relaxed_x[(e,d,s)] for d in days for s in shifts)
            required_work_hours = weekly_hours_global - leave_hours_credit[e]
            relaxed_solver.Add(actual_work_hours == required_work_hours)

    # Always: 1 shift per day
    for e in employee_names:
        for d in days:
            relaxed_solver.Add(sum(relaxed_x[(e,d,s)] for s in shifts) <= 1)

    relaxed_solver.Solve()
    roster = build_employee_roster(relaxed_solver, relaxed_x, employee_names, days, shifts)

    return {
        "status": "RELAXED",
        "objective_value": relaxed_solver.Objective().Value(),
        "roster": roster,  # ✅ Always roster!
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
