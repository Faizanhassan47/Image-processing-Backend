# Backend Architecture Guide

Welcome to the Digital Pathology Platform backend. This guide explains where everything is, how data flows, and where to look when debugging or adding features.

---

## 📁 Directory Structure at a Glance

```text
backend/
├── core/                  # Foundation: Config, Database, and Security
│   ├── config.py          # Environment variables & constants (.env loading)
│   ├── database.py        # SQL Server DBWrapper & pyodbc connection management
│   ├── security.py        # Passwords (bcrypt), JWT, RBAC roles & audit logging
│   └── audit_context.py   # Request-scoped audit context tracking
│
├── middleware/            # Application HTTP Middlewares
│   ├── auth_access.py     # User token extraction, signature validation & active check
│   ├── observability.py   # Request correlation IDs, IP/Device detection & logs
│   └── csrf.py            # Cross-Site Request Forgery (CSRF) token validation
│
├── routes/                # Modular API Controllers
│   ├── api.py             # Master router mounting all domain routers
│   ├── auth_api.py        # /api/auth/* and /api/users/* (Auth, sessions, user CRUD)
│   ├── cases_api.py       # /api/cases/* and /api/slides/* (Cases, WSI tiles, QC)
│   ├── annotations_api.py # /api/slides/{id}/annotations (Pathologist ROI & annotations)
│   ├── reports_api.py     # /api/reports/* (Pathology reporting & PDF/CSV/HTML export)
│   ├── patient_api.py     # /api/patients/* (Demographics, Medical History, Notes)
│   ├── platform_api.py    # /api/platform/* (System Health, Ingestion, Worklists)
│   └── ai_api.py          # /api/slides/{id}/ai/* (HoVer-Net analysis endpoints)

│
├── services/              # Business Logic & External Adapters
│   ├── slide_cache.py     # OpenSlide LRU caching, DeepZoom generator & tile pool
│   ├── ai_engine.py       # AI HoVer-Net segmentation, morphology & PDF reports
│   ├── image_validation.py# Image validation, dimensions, and OpenSlide helpers
│   └── scanner_adapters.py# Physical scanner ingestion adapters (Aperio, Hamamatsu)
│
├── sql/                   # Database Schemas & Queries
│   ├── init_tables.py     # Automatic idempotent table creation & migration logic
│   ├── schema.sql         # SQL Server comprehensive DDL schema
│   └── queries.py         # Reusable parameterized SQL statements
│
├── uploads/               # Persistent file storage for Whole Slide Images (WSI)
├── main.py                # Clean server entry point: FastAPI app, middleware & router mount
└── requirements.txt       # Python dependencies
```

---

## 🔍 Where Do I Find Things?

| I need to... | Go to: | File / Function |
| :--- | :--- | :--- |
| **Change database settings or environment variables** | `core/` | [`core/config.py`](file:///d:/Code/Hello/first/backend/core/config.py) |
| **Fix a database connection or query execution issue** | `core/` | [`core/database.py`](file:///d:/Code/Hello/first/backend/core/database.py) (`DBWrapper`, `connect`) |
| **Debug authentication, passwords, or permissions** | `core/` | [`core/security.py`](file:///d:/Code/Hello/first/backend/core/security.py) (`require_roles`, `verify_password`) |
| **Add or modify authentication or user endpoints** | `routes/` | [`routes/auth_api.py`](file:///d:/Code/Hello/first/backend/routes/auth_api.py) |
| **Modify case or slide endpoints** | `routes/` | [`routes/cases_api.py`](file:///d:/Code/Hello/first/backend/routes/cases_api.py) |
| **Modify annotations or measurements** | `routes/` | [`routes/annotations_api.py`](file:///d:/Code/Hello/first/backend/routes/annotations_api.py) |
| **Modify pathology reporting or exports** | `routes/` | [`routes/reports_api.py`](file:///d:/Code/Hello/first/backend/routes/reports_api.py) |
| **Debug AI segmentation, ROI extraction, or PDF reports** | `services/` | [`services/ai_engine.py`](file:///d:/Code/Hello/first/backend/services/ai_engine.py) |
| **Inspect table auto-creation or write SQL queries** | `sql/` | [`sql/init_tables.py`](file:///d:/Code/Hello/first/backend/sql/init_tables.py) or [`sql/queries.py`](file:///d:/Code/Hello/first/backend/sql/queries.py) |
| **Add CORS, middleware, or start the server** | Root | [`main.py`](file:///d:/Code/Hello/first/backend/main.py) |


---

## 🚀 Running the Server Locally

```powershell
# In d:\Code\Hello\first\backend:
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --reload
```
API Documentation (Swagger UI): `http://127.0.0.1:8000/docs`
