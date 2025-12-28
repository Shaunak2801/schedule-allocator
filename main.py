import os
from flask import Flask, send_file, request, jsonify
from ortools.linear_solver import pywraplp

app = Flask(__name__)


@app.route("/")
def index():
    return send_file("src/index.html")


# ---------- Core Solver Logic (no FastAPI) ----------

def solve_shift_roster(payload: dict) -> dict:
    """
    Expects the same JSON structure you used with OptimizeRequest:
    {
      "employees": [{"name": "Alice_Smith"}, ...],
      "days": [...],
      "shifts": [...],
      "hours_per_shift": {...},
      "preferences": {...},
      "leave_days": {...},
      "min_coverage": {...},
      "max_coverage": {...},
      "weekly_required_hours": {...}
    }
    Returns a dict ready for jsonify().
    """
    employees = [e["name"] for e in payload["employees"]]
    days = payload["days"]
    shifts = payload["shifts"]
    hours_per_shift = payload["hours_per_shift"]
    preferences = payload["preferences"]
    leave_data = payload["leave_days"]
    min_coverage = payload["min_coverage"]
    max_coverage = payload["max_coverage"]
    weekly_required_hours = payload["weekly_required_hours"]

    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        return {
            "status": "UNKNOWN",
            "objective_value": 0.0,
            "assignments": [],
            "num_employees": len(employees),
            "num_leaves": sum(len(leave_data.get(e, [])) for e in employees),
        }

    # Decision variables x[e,d,s] in {0,1}
    x = {
        (e, d, s): solver.BoolVar(f"x_{e}_{d}_{s}")
        for e in employees
        for d in days
        for s in shifts
    }

    # Objective
    solver.Maximize(
        solver.Sum(
            preferences.get(e, {}).get(s, 0.0) * x[(e, d, s)]
            for e in employees
            for d in days
            for s in shifts
        )
    )

    # 1) Leave constraints
    for e in employees:
        for leave_day in leave_data.get(e, []):
            if leave_day not in days:
                continue
            solver.Add(sum(x[(e, leave_day, s)] for s in shifts) == 0)

    # 2) Weekly hours
    for e in employees:
        solver.Add(
            sum(
                hours_per_shift[s] * x[(e, d, s)]
                for d in days
                for s in shifts
            )
            == weekly_required_hours[e]
        )

    # 3) Coverage limits
    for d in days:
        for s in shifts:
            coverage = sum(x[(e, d, s)] for e in employees)
            solver.Add(coverage >= min_coverage[s])
            solver.Add(coverage <= max_coverage[s])

    # 4) Max 1 shift per day
    for e in employees:
        for d in days:
            solver.Add(sum(x[(e, d, s)] for s in shifts) <= 1)

    # 5) No Night → Morning
    if "Night" in shifts and "Morning" in shifts:
        for e in employees:
            for i in range(len(days) - 1):
                d1, d2 = days[i], days[i + 1]
                solver.Add(
                    x[(e, d1, "Night")] + x[(e, d2, "Morning")] <= 1
                )

    status_code = solver.Solve()
    if status_code == pywraplp.Solver.OPTIMAL:
        status_str = "OPTIMAL"
    elif status_code == pywraplp.Solver.FEASIBLE:
        status_str = "FEASIBLE"
    elif status_code == pywraplp.Solver.INFEASIBLE:
        status_str = "INFEASIBLE"
    else:
        status_str = "UNKNOWN"

    assignments = []
    for e in employees:
        for d in days:
            assigned_shift = None
            for s in shifts:
                if x[(e, d, s)].solution_value() > 0.5:
                    assigned_shift = s
                    break
            assignments.append(
                {
                    "employee": e,
                    "day": d,
                    "shift": assigned_shift,  # null in JSON if None
                }
            )

    return {
        "status": status_str,
        "objective_value": solver.Objective().Value(),
        "assignments": assignments,
        "num_employees": len(employees),
        "num_leaves": sum(len(leave_data.get(e, [])) for e in employees),
    }


# ---------- JSON API Endpoint ----------

@app.route("/optimize", methods=["POST"])
def optimize():
    payload = request.get_json(force=True)
    result = solve_shift_roster(payload)
    return jsonify(result)


def main():
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host='0.0.0.0', port=port)


if __name__ == "__main__":
    main()
