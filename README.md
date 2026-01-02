# Shift Scheduling Optimizer

This Flask application provides a powerful API for optimizing weekly employee shift schedules. It takes into account various constraints and preferences to generate a fair, balanced, and efficient roster.

## Core Logic

The application is built around a sophisticated optimization engine that uses **Google's OR-Tools** library (`pywraplp`). The core of the logic is to solve a linear optimization problem to find the best possible shift assignments.

### 1. Input Validation

All incoming requests to the `/optimize` endpoint are validated using **Pydantic** models. This ensures that the input data structure is correct before any processing begins. The main models are:
- `Employee`: Defines an employee's name, preferred shift, criticality, and leave days.
- `OptimizeCompactRequest`: Defines the entire request payload, including a list of employees, days, shifts, and various default settings.

### 2. Preference Engine

Before solving, the application calculates a "preference score" for each possible shift assignment for every employee. This scoring system helps the optimizer make more human-centric decisions. The score is influenced by:
- **Preferred Shifts:** Employees get a significant bonus for being assigned their preferred shift and a penalty otherwise.
- **Fatigue Management:** The system penalizes assigning the same shift consecutively for too many days to prevent burnout. The penalty is adjusted based on the employee's "criticality".

### 3. Optimization & Constraints

The solver works to maximize the total preference score across all assignments while adhering to a strict set of rules (constraints):

- **Weekly Hours:** Each employee must be assigned a total number of hours that matches the globally defined weekly hours, adjusted for any paid leave.
- **Leave Days:** No employee will be assigned a shift on their requested leave days.
- **Shift Coverage:** The number of employees assigned to any given shift must be within the defined `min_coverage` and `max_coverage` limits.
- **One Shift Per Day:** An employee can be assigned a maximum of one shift per day.
- **No Clopenings:** To ensure adequate rest, the solver prevents assigning an employee a "Morning" shift on the day immediately following a "Night" shift.

### 4. Relaxation Logic

In scenarios where a perfect solution is impossible (e.g., not enough staff to cover all shifts due to leaves), the application can't find an "optimal" solution. In this case, it enters a **relaxation phase**:
1. It first diagnoses which major constraint is impossible to satisfy (`HOURS_BALANCE` or `COVERAGE_LIMITS`).
2. It then re-runs the solver, ignoring the problematic constraint (e.g., the weekly hours rule) but **always** enforcing minimum shift coverage.
3. The result is a "FEASIBLE" but non-optimal roster that at least keeps the business running. The response indicates which constraint was breached.

## API Endpoint

### `POST /optimize`

This is the main endpoint for generating a shift roster.

**Sample Request Body:**
```json
{
    "employees": [
        {"name": "Alice", "preferred_shift": "Morning", "leave_days": ["Mon"]},
        {"name": "Bob", "criticality": "high"},
        {"name": "Charlie", "preferred_shift": "Night", "leave_days": ["Wed", "Thu"]}
    ],
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "shifts": ["Morning", "Afternoon", "Night"],
    "defaults": {
        "hours_per_shift": {"Morning": 8, "Afternoon": 8, "Night": 8},
        "min_coverage": {"Morning": 1, "Afternoon": 1, "Night": 1},
        "max_coverage": {"Morning": 2, "Afternoon": 2, "Night": 1}
    },
    "weekly_hours": 40,
    "historical_shifts": {
        "Alice": ["Night", "Night", "Afternoon", "Morning", "Morning"]
        },
    "calender_events":{
    "Alice":{
        "Morning":3,
        "Afternoon":1,
        "Evening":0
    }
  }
}
```

**Sample Success Response (`OPTIMAL`):**
```json
{
    "status": "OPTIMAL",
    "roster": {
        "Alice": { "Mon": "OFF", "Tue": "Morning", ... },
        "Bob": { "Mon": "Afternoon", "Tue": "Afternoon", ... },
        "Charlie": { "Mon": "Night", "Tue": "Night", ... }
    },
    ...
}
```

## How to Run the Application

### Prerequisites
- Python 3

### Installation & Setup

1.  **Activate the virtual environment:** The project is configured to work with a Nix-based environment. You must first activate the virtual environment to access the installed dependencies.
    ```bash
    source .venv/bin/activate
    ```

2.  **Install dependencies (if needed):** If you add new packages, make sure they are added to `requirements.txt` and run:
    ```bash
    pip install -r requirements.txt
    ```

### Running the Server

-   Execute the development server script:
    ```bash
    ./devserver.sh
    ```
-   The application will start, and you can access it via the URL provided in the development environment's preview panel. The root URL (`/`) serves a simple HTML page for interacting with the API.

### How to Use

You can send POST requests to the `/optimize` endpoint using the provided `index.html` interface or any API client like `curl` or Postman.

**Example with `curl`:**
```bash
curl -X POST -H "Content-Type: application/json" -d '{
    "employees": [{"name": "Alice"}, {"name": "Bob"}],
    "days": ["Mon", "Tue"],
    "shifts": ["Morning", "Afternoon"],
    "defaults": {
        "hours_per_shift": {"Morning": 8, "Afternoon": 8},
        "min_coverage": {"Morning": 1, "Afternoon": 1},
        "max_coverage": {"Morning": 1, "Afternoon": 1}
    },
    "weekly_hours": 16
}' http://<your-preview-url>/optimize
```
