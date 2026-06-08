# Exam Duty Allocation (AVIT) — Run Instructions

## What this project is
- **Backend**: Flask API + MongoDB (round-robin duty allocation)
- **Frontend**: Static UI under `frontend/static/` (note: current `script.js` is mock/in-memory and may not call the backend APIs)

## Prerequisites
1. **Python 3.10+** installed
2. **MongoDB** installed and running (local)
   - Default connection in code: `mongodb://localhost:27017/`

## Windows: Run the backend
Open **Command Prompt** (or VSCode terminal) and run:

### 1) Create a virtual environment
```bat
cd "c:/Users/Naveen kumar/Downloads/exam duty allocation"
python -m venv venv
```

### 2) Activate the environment
```bat
venv\Scripts\activate
```

### 3) Install dependencies
```bat
pip install -r backend\requirements.txt
```

### 4) Start MongoDB
- Start MongoDB Service (commonly named **MongoDB**)
- Verify it’s running on `localhost:27017`

### 5) Run the Flask server
```bat
python backend\app.py
```

You should see log output ending with:
- `ExamDuty API — MongoDB + Round-Robin Engine`
- Server URL: `http://127.0.0.1:5000`
- It will also seed default users on first run.

## Test the API
Visit in your browser:
- `http://127.0.0.1:5000/api/health`

Expected: JSON with counts and `status: "ok"`.

## Login (seeded demo users)
Use `POST /api/auth/login` with JSON body:
- `Content-Type: application/json`

Seed credentials:
- **Admin**: `admin@cse.edu` / `Admin@123`
- **Faculty**: `kumar@cse.edu` / `Faculty@123` (and others created by the seed)

Example using curl (PowerShell may need `curl.exe`):
```bat
curl.exe -X POST http://127.0.0.1:5000/api/auth/login -H "Content-Type: application/json" -d "{\"email\":\"admin@cse.edu\",\"password\":\"Admin@123\"}"
```

## Admin round-robin allocation endpoints
After login as admin (JWT), use:
- `POST /api/admin/exams/<exam_id>/allocate`
- `GET  /api/admin/exams/<exam_id>/preview-allocation`
- `GET  /api/admin/rr-state`
- `POST /api/admin/rr-state/reset`

(Exams must exist; create with `POST /api/admin/exams`.)

## Stopping
In the terminal running Flask:
- Press **Ctrl+C**

To deactivate venv:
```bat
deactivate
```

