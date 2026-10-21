Duty Roster & Work Schedule Optimization Platform

An automated, constraint-satisfaction schedule builder designed for operational environments, military reserve duty units, and shift-based organizations. Built with FastAPI, SQLAlchemy, OR-Tools (CP-SAT Solver), and Pandas.

🚀 Key Features

Automated CP-SAT Scheduling Engine: Uses Google OR-Tools integer programming to generate balanced, fully compliant duty rosters.

Strict Fair-Load Distribution: Implements quadratic and absolute-deviation penalty models to eliminate extreme hour disparities across personnel.

Excel Ingestion & Parsing Pipeline: Upload standard .xlsx roster inputs for jobs, shift blocks, and employee lists with automatic case-insensitive string parsing.

Hard & Soft Constraint Satisfaction:

Qualification and job-level talent matching.

Personal availability and requested days_off enforcement.

Rest period enforcement between shifts.

High-load willingness weighting.

RESTful API Endpoint Layer: Built on FastAPI with automated OpenAPI interactive documentation (/docs).

Database Persistence: SQLAlchemy ORM layer with PostgreSQL compatibility and Alembic database migrations.

🛠️ Tech Stack

Backend Framework: Python 3.14, FastAPI, Uvicorn

Optimization Solver: Google OR-Tools (CP-SAT)

Data Engineering: Pandas, OpenPyXL

Database & Migration: PostgreSQL, SQLAlchemy, Alembic

Validation: Pydantic v2

📂 Project Structure

Schedule_Builder/
├── main.py                   # FastAPI application routes & endpoint handlers
├── database.py               # Database engine connection & session management
├── models.py                 # SQLAlchemy ORM models
├── schemas.py                # Pydantic request/response schemas
├── solver.py                 # CP-SAT constraint formulation & solver engine
├── excel_parser.py           # Pandas excel parsing & case-normalization utilities
├── alembic/                  # Database migration scripts
├── alembic.ini              # Alembic configuration
└── requirements.txt          # Project dependencies


⚙️ Installation & Local Setup

1. Prerequisites

Python 3.11 or higher

PowerShell / Command Prompt / Bash

PostgreSQL (or local SQLite for dev testing)

2. Clone Repository

git clone https://github.com/your-username/Schedule_Builder.git
cd Schedule_Builder


3. Virtual Environment Setup

On Windows (PowerShell):

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
python -m venv .venv
.\.venv\Scripts\Activate.ps1


On Linux / macOS:

python3 -m venv .venv
source .venv/bin/activate


4. Install Dependencies

pip install -r requirements.txt


(If installing individually: pip install fastapi uvicorn sqlalchemy psycopg2-binary ortools pandas openpyxl python-multipart alembic)

🗄️ Database Migrations

Apply database schema migrations using Alembic:

alembic upgrade head


🏃 Running the Application

Start the local API development server:

uvicorn main:app --reload


The API will be available at:

Server Root: http://127.0.0.1:8000

Interactive API Docs (Swagger UI): http://127.0.0.1:8000/docs

📊 Schedule Input Excel Format

The solver expects an .xlsx workbook containing the following sheets:

Employees: uid, name, qualifications, days_off, high_load_willingness

Weekly_Grid: Job names, time slots, and requested headcount per day (Sunday, Monday, ..., Saturday)

Jobs: job name, required talents, priority

Shift_Blocks: Shift names (בוקר, צהריים, לילה) and corresponding start/end times.

📄 License

Distributed under the MIT License. See LICENSE for more information.
