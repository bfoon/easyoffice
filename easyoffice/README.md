# 🏢 EasyOffice — Virtual Office Management Platform

A comprehensive Django-based virtual office system with real-time chat, task management, project tracking, HR, finance, IT support, and more.

---

## 🚀 Quick Start (Docker)

```bash
# 1. Clone / copy this project
cd easyoffice

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your values (at minimum set SECRET_KEY)

# 3. Start all services
docker-compose up -d

# 4. Access the application
open http://localhost
```

**Default Admin Credentials:**
- Email: `admin@easyoffice.com`
- Password: `Admin@123!`

---

## 🏗️ Architecture

```
easyoffice/
├── docker-compose.yml        # PostgreSQL + Redis + Web + Celery + Nginx
├── Dockerfile
├── requirements.txt
├── manage.py
├── easyoffice/               # Django project config
│   ├── settings.py           # Full settings with OFFICE_CONFIG
│   ├── urls.py               # Root URL routing
│   ├── asgi.py               # WebSocket (Channels) ASGI
│   ├── wsgi.py
│   └── celery.py             # Celery task queue
├── apps/
│   ├── core/                 # Custom User, OfficeSettings, AuditLog, Notifications
│   ├── organization/         # Departments, Units, Positions, Locations
│   ├── staff/                # Staff profiles, Leave requests
│   ├── tasks/                # Task assignment, collaboration, time logs
│   ├── projects/             # Projects, milestones, updates, risks
│   ├── messaging/            # Real-time chat (WebSocket + fallback)
│   ├── files/                # File storage and sharing
│   ├── finance/              # Budget, purchase requests, payments
│   ├── hr/                   # Performance appraisals, payroll
│   ├── it_support/           # IT tickets, equipment management
│   ├── dashboard/            # Role-aware dashboards
│   └── reports/              # Office-wide reporting
├── templates/                # Bootstrap 5 HTML templates
├── static/
│   ├── css/easyoffice.css    # Custom design system
│   └── js/easyoffice.js      # UI interactions + WS client
└── nginx/nginx.conf
```

---

## 📦 Services

| Service        | Description                         | Port  |
|---------------|-------------------------------------|-------|
| `web`         | Django / Gunicorn (ASGI)             | 8000  |
| `nginx`       | Reverse proxy + static files        | 80    |
| `db`          | PostgreSQL 16                        | 5432  |
| `redis`       | Cache + Channels + Celery broker    | 6379  |
| `celery`      | Background tasks (email, reports)   | —     |
| `celery-beat` | Scheduled tasks (payroll, appraisals) | —   |

---

## 🔑 Key Features

### 👥 Staff & HR
- Staff directory with profiles, org chart, unit view
- Supervisor → supervisee hierarchy
- Leave requests with approval workflow
- Performance appraisals (self → supervisor → HR review cycle)
- Payroll record management

### ✅ Task Management
- Create, assign, and track tasks with priority & status
- Full or partial task reassignment / delegation
- Collaborators on tasks
- Subtasks, comments, file attachments
- Time logging per task
- Overdue alerts with supervisor notification

### 📊 Projects
- Project creation with team, milestones, risks
- Progress tracking (% complete) + status timeline
- Project updates/notes by any team member
- Budget tracking per project
- Unit-level project visibility

### 💬 Real-time Messaging
- Direct messages between staff
- Group chat rooms
- Unit and department channels
- File sharing in chat
- WebSocket powered (Django Channels + Redis)

### 📁 File Management
- Upload and share files: private / unit / department / office-wide
- Folder organization
- Version tracking
- Download counter

### 💰 Finance
- Department budgets per fiscal year
- Purchase request workflow (submit → approve → order → deliver)
- Payment recording with receipts
- Budget utilization tracking

### 🖥️ IT Support
- Ticket system with auto-generated ticket numbers
- Equipment asset register with issuance tracking
- Email/account creation requests
- SLA tracking

### 📈 Reports & Dashboards
- **Staff dashboard**: My tasks, projects, quick actions
- **Supervisor dashboard**: Unit overview + team task status
- **CEO/Admin dashboard**: Office-wide KPIs, project timeline, stats
- Report modules: Tasks, Projects, Staff, Finance
- Audit log for all significant actions

---

## ⚙️ Configuration

All configurable via `.env` or `OfficeSettings` table (editable in admin):

```env
OFFICE_NAME=EasyOffice
OFFICE_PRIMARY_COLOR=#1e3a5f
OFFICE_ACCENT_COLOR=#2196f3
CURRENCY=USD
ENABLE_CHAT=true
ENABLE_PAYROLL=true
MAX_FILE_UPLOAD_MB=50
APPRAISAL_CYCLE_MONTHS=12
FISCAL_YEAR_START_MONTH=1
```

---

## 🔧 Development

```bash
# Local development (without Docker)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set up local .env (use sqlite for dev)
# DB_ENGINE=django.db.backends.sqlite3
# DB_NAME=db.sqlite3

python manage.py migrate
python manage.py create_default_data
python manage.py runserver

# Run Celery (separate terminal)
celery -A easyoffice worker -l info

# Run Channels (use daphne for WS support)
daphne -b 0.0.0.0 -p 8000 easyoffice.asgi:application
```

---

## 🏢 User Roles

| Role              | Access                                        |
|-------------------|-----------------------------------------------|
| Staff             | Own tasks, projects, chat, files, leave       |
| Supervisor/HOU    | + Team tasks, leave approval, unit dashboard  |
| HR                | + All appraisals, payroll, staff records      |
| Finance           | + Budgets, purchase requests, payments        |
| IT                | + All tickets, equipment, user management     |
| CEO / Admin       | Full office dashboard, all modules            |

Roles are managed via Django's `Groups` (HR, IT, Finance, Sales, CEO, Admin, etc.)

---

## 📧 Email Notifications

Triggered automatically for:
- Task assignment / status change
- Leave request approval/rejection
- IT ticket resolution
- Project milestone completion
- Appraisal phase transitions

Configure SMTP in `.env` or use console backend for development.

---

## 🔮 Extending EasyOffice

The app is designed to be modular. Each app under `apps/` is independently extendable:

- Add new notification types in `apps/core/models.py`
- Add new modules by creating a new Django app under `apps/`
- Configure feature flags via `OfficeSettings` (no code change needed)
- Celery tasks go in `apps/<module>/tasks.py`
- REST API endpoints: add serializers and views to `apps/<module>/api_urls.py`

---

## 📄 License

MIT — Free to use and customize for your organization.
