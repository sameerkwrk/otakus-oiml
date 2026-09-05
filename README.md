# NAWI Legal Metrology Suite

A comprehensive digital certification and verification platform for Non-Automatic Weighing Instruments (NAWIs). Developed to replace manual spreadsheets, this system automates test data recording, metrological compliance evaluation, and the generation of standardized test reports strictly aligned with **OIML Recommendation R-76**.

## 1. System Architecture

The application is built on a modern, asynchronous architecture prioritizing exact arithmetic and secure separation of duties.

* **Backend Framework:** FastAPI (Python 3.13+) providing high-performance, asynchronous REST APIs.
* **Database:** SQLite (deployed with schema auto-generation). Utilizes exact string storage (`TEXT`) for sensitive floating-point metrological values to prevent IEEE-754 precision loss during round-trips.
* **Frontend:** Vanilla JavaScript + HTML5, styled with Tailwind CSS. Utilizes client-side routing, JWT Bearer token authentication, and a custom UI engine.
* **Dependency Management:** `uv` for ultra-fast, deterministic environment resolution.
* **Document Generation:** `reportlab` for PDF certificate generation and `python-docx` for editable MS Word documents. PKCS#7 X.509 digital signatures are handled via `pyHanko`.

## 2. Calculation & Metrology Methodology

All metrological evaluations are encapsulated in an isolated rules engine (`app/mpe_engine.py`) to support future OIML revisions without altering core business logic.

* **Exact Arithmetic:** The engine exclusively uses Python's `decimal.Decimal` class. Numbers are parsed directly from string inputs. Ordinary floating-point math is explicitly avoided to ensure legally defensible pass/fail boundary checks.
* **MPE Zones:** The Maximum Permissible Error (MPE) is calculated dynamically based on the instrument's accuracy class (I, II, III, IIII), verification interval (`e`), and the specific applied load.
* **Test Coverage:**
  * **Accuracy/Linearity & Eccentricity:** Evaluated as `|error| <= MPE` at the specific reference load.
  * **Repeatability:** Evaluated by analyzing the spread (`max - min`) of multiple runs against the load-appropriate MPE (not a flat 1e).
  * **Discrimination:** Evaluates if a gentle `+1.4d` load addition produces a visible `>= 1d` change in indication.
  * **Tare Testing:** Evaluates accuracy explicitly on net loads.

## 3. Workflow & Separation of Duties

The system enforces strict Role-Based Access Control (RBAC) via stateless JWTs to fulfill legal compliance:
1. **Tester:** Registers instrument parameters and inputs observed readings. Submits reports to a read-only `DRAFT` state. Cannot approve their own work.
2. **Approver:** Reviews staged reports and attachments. Can `APPROVE` (which digitally signs and releases the certificate) or `REJECT` (requiring a reason). 
3. **Revision Control:** Rejected reports cannot be overwritten. A `Create Revision` action spawns a linked child record to preserve complete audit traceability of failed tests.

## 4. Deployment Framework

### Prerequisites
* Python 3.13+
* `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Local Setup & Launch
1. **Install Dependencies:**
   ```bash
   uv pip install fastapi uvicorn pydantic python-multipart bcrypt pyjwt reportlab qrcode pillow python-docx pyhanko