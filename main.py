"""
LexAI — MVP de Análisis Inteligente de Contratos
Corre con: uvicorn main:app --reload
Abre: http://localhost:8000
"""

import json
import os
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

import anthropic
import pdfplumber
import docx as python_docx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="LexAI", version="1.0.0-MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def get_cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            file_size   INTEGER,
            status      TEXT NOT NULL DEFAULT 'pending',
            raw_text    TEXT,
            analysis    TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trial_users (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            trial_key     TEXT NOT NULL UNIQUE,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 1,
            uploads_count INTEGER NOT NULL DEFAULT 0,
            chat_count    INTEGER NOT NULL DEFAULT 0,
            last_used_at  TEXT,
            notes         TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "lexai-admin-2026")
SERVER_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _check_admin(request_secret: str):
    if request_secret != ADMIN_SECRET:
        raise HTTPException(401, "Admin secret inválido")


def _validate_trial_key(trial_key: str) -> str:
    """Valida un trial key y retorna la API key del servidor. Lanza HTTPException si no es válida."""
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT * FROM trial_users WHERE trial_key=%s", (trial_key,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        raise HTTPException(401, "Trial key inválida. Contacta a LexAI para obtener acceso.")
    if not user["is_active"]:
        raise HTTPException(403, "Tu acceso de prueba ha sido desactivado. Contacta a LexAI.")
    if datetime.fromisoformat(user["expires_at"]) < datetime.now():
        raise HTTPException(
            403,
            f"Tu prueba gratuita de 7 días expiró el {user['expires_at'][:10]}. "
            "Contacta a LexAI para continuar usando el servicio."
        )
    if not SERVER_API_KEY:
        raise HTTPException(500, "Server API key not configured")
    return SERVER_API_KEY


def _log_trial_usage(trial_key: str, action: str = "upload"):
    conn = get_db()
    cur = get_cur(conn)
    field = "uploads_count" if action == "upload" else "chat_count"
    cur.execute(
        f"UPDATE trial_users SET {field}={field}+1, last_used_at=%s WHERE trial_key=%s",
        (datetime.now().isoformat(), trial_key),
    )
    conn.commit()
    cur.close()
    conn.close()

# ── Text extraction ───────────────────────────────────────────────────────────
def extract_pdf(path: Path) -> str:
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

    if not text.strip():
        # Fallback: OCR for scanned PDFs
        try:
            import pytesseract
            from pdf2image import convert_from_path
            images = convert_from_path(str(path))
            for image in images:
                text += pytesseract.image_to_string(image, lang="spa+eng") + "\n"
        except ImportError:
            pass  # OCR dependencies not installed

    return text.strip()


def extract_docx(path: Path) -> str:
    doc = python_docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


# ── AI Analysis ───────────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """Eres un abogado senior especializado en derecho español y europeo (Civil, Mercantil, Laboral).
Analiza el siguiente contrato y devuelve ÚNICAMENTE un objeto JSON válido con esta estructura exacta:

{
  "tipo_contrato": "string (ej: Contrato de Prestación de Servicios)",
  "nivel_riesgo": "BAJO | MEDIO | ALTO",
  "puntuacion_riesgo": number (0-100),
  "resumen_ejecutivo": "string (3-5 frases que resuman lo más importante del contrato)",
  "partes": [
    {
      "nombre": "string",
      "tipo": "Persona física | Persona jurídica",
      "rol": "string (ej: Prestador de servicios, Cliente, Arrendador...)",
      "cif_nif": "string o null"
    }
  ],
  "objeto": "string (descripción clara de qué regula el contrato)",
  "condiciones_economicas": {
    "precio_total": "string",
    "moneda": "string",
    "forma_pago": "string",
    "penalizaciones": "string o null",
    "garantias": "string o null"
  },
  "fechas_clave": [
    {
      "tipo": "string (ej: Firma, Inicio vigencia, Vencimiento, Plazo de pago)",
      "fecha": "string",
      "descripcion": "string"
    }
  ],
  "clausulas_riesgo": [
    {
      "titulo": "string (nombre corto de la cláusula)",
      "nivel": "ALTO | MEDIO | BAJO",
      "descripcion": "string (qué dice la cláusula problemática)",
      "impacto": "string (qué puede pasar si se aplica)",
      "recomendacion": "string (qué hacer al respecto)"
    }
  ],
  "obligaciones_principales": [
    {
      "parte": "string (nombre de la parte)",
      "obligacion": "string"
    }
  ],
  "jurisdiccion": "string o null",
  "ley_aplicable": "string o null",
  "alertas": ["string"]
}

NOTAS:
- clausulas_riesgo: identifica TODAS las cláusulas que puedan ser desfavorables, ambiguas o peligrosas
- alertas: lista de avisos importantes que el abogado debe revisar urgentemente
- Si algún dato no está en el contrato, usa null o []
- Responde SOLO con el JSON, sin markdown, sin texto extra

CONTRATO:
"""

ANALYSIS_PROMPT_USA = """You are a senior attorney specializing in US contract law, with expertise across all 50 states and federal law (UCC, FLSA, ADA, FCRA, etc.). You apply the governing law specified in the contract; if no governing law is stated, you analyze under general US common law principles while noting any significant state-by-state variations that may apply.

Analyze the following contract and return ONLY a valid JSON object with this exact structure:

{
  "tipo_contrato": "string (e.g.: Service Agreement, NDA, Lease Agreement, Employment Contract)",
  "nivel_riesgo": "LOW | MEDIUM | HIGH",
  "puntuacion_riesgo": number (0-100),
  "resumen_ejecutivo": "string (3-5 sentences summarizing the most important aspects of the contract)",
  "partes": [
    {
      "nombre": "string",
      "tipo": "Individual | Corporation | LLC | Partnership",
      "rol": "string (e.g.: Service Provider, Client, Landlord, Employer...)",
      "cif_nif": "string or null (EIN, SSN last 4, or null)"
    }
  ],
  "objeto": "string (clear description of what the contract governs)",
  "condiciones_economicas": {
    "precio_total": "string",
    "moneda": "USD",
    "forma_pago": "string",
    "penalizaciones": "string or null",
    "garantias": "string or null"
  },
  "fechas_clave": [
    {
      "tipo": "string (e.g.: Effective Date, Expiration, Payment Due, Notice Period)",
      "fecha": "string",
      "descripcion": "string"
    }
  ],
  "clausulas_riesgo": [
    {
      "titulo": "string (short clause name)",
      "nivel": "HIGH | MEDIUM | LOW",
      "descripcion": "string (what the problematic clause says)",
      "impacto": "string (what can happen if applied)",
      "recomendacion": "string (what to do about it)"
    }
  ],
  "obligaciones_principales": [
    {
      "parte": "string (party name)",
      "obligacion": "string"
    }
  ],
  "jurisdiccion": "string or null (state/federal jurisdiction)",
  "ley_aplicable": "string or null (governing law clause)",
  "alertas": ["string"]
}

NOTES:
- clausulas_riesgo: identify ALL clauses that may be unfavorable, ambiguous, or dangerous — pay special attention to: non-compete clauses (enforceability varies by state), arbitration clauses (FAA vs state rules), limitation of liability caps, indemnification, auto-renewal terms, IP ownership, at-will employment provisions, and choice-of-law clauses. Apply the governing law stated in the contract; if none, apply the most relevant state law based on parties and context.
- alertas: list important warnings the attorney must urgently review — flag any clauses that may conflict with federal law (UCC, FLSA, ADA, etc.) or applicable state law
- If any data is not in the contract, use null or []
- Respond ONLY with the JSON, no markdown, no extra text

CONTRACT:
"""


def analyze_contract(text: str, api_key: str, jurisdiction: str = "es") -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    # Truncate to avoid token limits while keeping meaningful content
    contract_text = text[:10000] if len(text) > 10000 else text

    prompt = ANALYSIS_PROMPT_USA if jurisdiction == "us" else ANALYSIS_PROMPT

    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": prompt + contract_text,
            }
        ],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    return json.loads(raw)


# ── API Routes ────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0-MVP"}


@app.post("/api/contracts/upload")
async def upload_contract(
    file: UploadFile = File(...),
    api_key: str = Form(""),
    jurisdiction: str = Form("es"),
):
    # Validate
    ext = Path(file.filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, "Solo se aceptan archivos PDF o DOCX")

    # Save file
    contract_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{contract_id}{ext}"
    content = await file.read()
    file_path.write_bytes(content)

    # Extract text
    try:
        if ext == ".pdf":
            raw_text = extract_pdf(file_path)
        else:
            raw_text = extract_docx(file_path)
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Error extrayendo texto del documento: {e}")

    if not raw_text.strip():
        raise HTTPException(422, "No se pudo extraer texto del documento. ¿Es un PDF escaneado?")

    # Save to DB
    conn = get_db()
    cur = get_cur(conn)
    cur.execute(
        "INSERT INTO contracts (id, filename, uploaded_at, file_size, status, raw_text) VALUES (%s,%s,%s,%s,%s,%s)",
        (contract_id, file.filename, datetime.now().isoformat(), len(content), "pending", raw_text),
    )
    conn.commit()
    cur.close()
    conn.close()

    # Resolve API key — support both Anthropic keys and LexAI trial keys
    resolved_key = api_key.strip()
    is_trial = resolved_key.startswith("LEXAI-")
    if is_trial:
        resolved_key = _validate_trial_key(resolved_key)

    # Analyze with Claude if API key provided
    if resolved_key:
        if is_trial:
            _log_trial_usage(api_key.strip(), "upload")
        try:
            analysis = analyze_contract(raw_text, resolved_key, jurisdiction)
            conn = get_db()
            cur = get_cur(conn)
            cur.execute(
                "UPDATE contracts SET status=%s, analysis=%s WHERE id=%s",
                ("analyzed", json.dumps(analysis, ensure_ascii=False), contract_id),
            )
            conn.commit()
            cur.close()
            conn.close()
            return {"id": contract_id, "status": "analyzed", "analysis": analysis}
        except json.JSONDecodeError as e:
            conn = get_db()
            cur = get_cur(conn)
            cur.execute("UPDATE contracts SET status='error' WHERE id=%s", (contract_id,))
            conn.commit()
            cur.close()
            conn.close()
            raise HTTPException(500, f"La IA devolvió un formato inesperado: {e}")
        except anthropic.AuthenticationError:
            raise HTTPException(401, "API Key de Anthropic inválida. Compruébala en Configuración.")
        except Exception as e:
            conn = get_db()
            cur = get_cur(conn)
            cur.execute("UPDATE contracts SET status='error' WHERE id=%s", (contract_id,))
            conn.commit()
            cur.close()
            conn.close()
            raise HTTPException(500, f"Error en análisis IA: {e}")

    return {"id": contract_id, "status": "pending", "message": "Documento subido. Introduce una API Key para analizar."}


@app.get("/api/contracts")
def list_contracts():
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT id, filename, uploaded_at, file_size, status, analysis FROM contracts ORDER BY uploaded_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for r in rows:
        a = json.loads(r["analysis"]) if r["analysis"] else None
        result.append(
            {
                "id": r["id"],
                "filename": r["filename"],
                "uploaded_at": r["uploaded_at"],
                "file_size": r["file_size"],
                "status": r["status"],
                "tipo_contrato": a.get("tipo_contrato") if a else None,
                "nivel_riesgo": a.get("nivel_riesgo") if a else None,
                "puntuacion_riesgo": a.get("puntuacion_riesgo") if a else None,
                "resumen_ejecutivo": a.get("resumen_ejecutivo") if a else None,
            }
        )
    return result


@app.get("/api/contracts/{contract_id}")
def get_contract(contract_id: str):
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT * FROM contracts WHERE id=%s", (contract_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(404, "Contrato no encontrado")

    return {
        "id": row["id"],
        "filename": row["filename"],
        "uploaded_at": row["uploaded_at"],
        "file_size": row["file_size"],
        "status": row["status"],
        "analysis": json.loads(row["analysis"]) if row["analysis"] else None,
    }


@app.delete("/api/contracts/{contract_id}")
def delete_contract(contract_id: str):
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT id FROM contracts WHERE id=%s", (contract_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(404, "Contrato no encontrado")
    cur.execute("DELETE FROM contracts WHERE id=%s", (contract_id,))
    conn.commit()
    cur.close()
    conn.close()
    # Remove uploaded file
    for ext in (".pdf", ".docx"):
        p = UPLOAD_DIR / f"{contract_id}{ext}"
        p.unlink(missing_ok=True)
    return {"message": "Contrato eliminado"}


@app.post("/api/contracts/{contract_id}/reanalyze")
def reanalyze_contract(contract_id: str, payload: dict):
    api_key = payload.get("api_key", "")
    if not api_key:
        raise HTTPException(400, "Se requiere API Key para re-analizar")

    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT raw_text FROM contracts WHERE id=%s", (contract_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(404, "Contrato no encontrado")

    try:
        analysis = analyze_contract(row["raw_text"], api_key.strip())
        conn = get_db()
        cur = get_cur(conn)
        cur.execute(
            "UPDATE contracts SET status=%s, analysis=%s WHERE id=%s",
            ("analyzed", json.dumps(analysis, ensure_ascii=False), contract_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"id": contract_id, "status": "analyzed", "analysis": analysis}
    except anthropic.AuthenticationError:
        raise HTTPException(401, "API Key inválida")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/chat")
async def chat_direct(payload: dict):
    """Endpoint de chat directo (sin contract_id) — usado por las PWAs."""
    api_key = payload.get("api_key", "")
    message = payload.get("message", "")
    context = payload.get("context", "")
    jurisdiction = payload.get("jurisdiction", "es")

    if not api_key:
        raise HTTPException(400, "Se requiere API Key para usar el chat")
    if not message:
        raise HTTPException(400, "El mensaje no puede estar vacío")

    raw_key = api_key.strip()
    is_trial = raw_key.startswith("LEXAI-")
    if is_trial:
        resolved_key = _validate_trial_key(raw_key)
        _log_trial_usage(raw_key, "chat")
    else:
        resolved_key = raw_key

    if jurisdiction == "us":
        system = "You are a senior US attorney. Answer clearly and practically in English. " + (f"\n\nContext:\n{context}" if context else "")
    else:
        system = "Eres un abogado senior especializado en derecho español. Responde de forma clara y práctica. " + (f"\n\nContexto:\n{context}" if context else "")

    client = anthropic.Anthropic(api_key=resolved_key)
    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        return {"response": response.content[0].text}
    except anthropic.AuthenticationError:
        raise HTTPException(401, "API Key inválida")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/contracts/{contract_id}/chat")
def chat_with_contract(contract_id: str, payload: dict):
    api_key = payload.get("api_key", "")
    message = payload.get("message", "")
    history = payload.get("history", [])

    if not api_key:
        raise HTTPException(400, "Se requiere API Key para usar el chat")
    if not message:
        raise HTTPException(400, "El mensaje no puede estar vacío")

    raw_key = api_key.strip()
    is_trial = raw_key.startswith("LEXAI-")
    if is_trial:
        resolved_key = _validate_trial_key(raw_key)
        _log_trial_usage(raw_key, "chat")
    else:
        resolved_key = raw_key

    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT raw_text, analysis FROM contracts WHERE id=%s", (contract_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(404, "Contrato no encontrado")

    contract_text = (row["raw_text"] or "")[:8000]
    analysis_summary = ""
    if row["analysis"]:
        a = json.loads(row["analysis"])
        analysis_summary = f"""
Tipo: {a.get('tipo_contrato', '—')}
Riesgo: {a.get('nivel_riesgo', '—')} ({a.get('puntuacion_riesgo', 0)}/100)
Resumen: {a.get('resumen_ejecutivo', '—')}
Cláusulas de riesgo: {len(a.get('clausulas_riesgo', []))}
Alertas: {'; '.join(a.get('alertas', []))}
"""

    jur = payload.get("jurisdiction", "es")
    if jur == "us":
        system_intro = "You are a senior US attorney with expertise across all 50 states and federal law. You have access to the contract and its prior analysis. Respond clearly, directly, and practically in English. If something requires in-person consultation with a licensed attorney, say so."
    else:
        system_intro = "Eres un abogado senior especializado en derecho español y europeo (Civil, Mercantil, Laboral). Tienes acceso al contrato y su análisis previo. Responde de forma clara, directa y práctica en español. Si algo requiere consulta presencial con un abogado colegiado, indícalo."

    system = f"""{system_intro}

CONTRATO:
{contract_text}

ANÁLISIS PREVIO:
{analysis_summary}"""

    client = anthropic.Anthropic(api_key=resolved_key)
    messages = [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": message}]

    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        return {"response": response.content[0].text}
    except anthropic.AuthenticationError:
        raise HTTPException(401, "API Key inválida")
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Admin — Trial Users ───────────────────────────────────────────────────────

@app.post("/api/admin/create-trial")
def create_trial(payload: dict):
    """Crea un trial key para un usuario beta. Requiere X-Admin-Secret."""
    _check_admin(payload.get("admin_secret", ""))

    name  = payload.get("name", "").strip()
    email = payload.get("email", "").strip().lower()
    days  = int(payload.get("days", 7))
    notes = payload.get("notes", "")

    if not name or not email:
        raise HTTPException(400, "name y email son obligatorios")

    # Check duplicate email
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT trial_key FROM trial_users WHERE email=%s", (email,))
    existing = cur.fetchone()
    if existing:
        cur.close()
        conn.close()
        raise HTTPException(409, f"Ya existe un trial para {email}: {existing['trial_key']}")

    trial_key  = "LEXAI-" + secrets.token_urlsafe(12).upper()
    user_id    = str(uuid.uuid4())
    created_at = datetime.now()
    expires_at = created_at + timedelta(days=days)

    cur.execute(
        """INSERT INTO trial_users
           (id, name, email, trial_key, created_at, expires_at, is_active, notes)
           VALUES (%s,%s,%s,%s,%s,%s,1,%s)""",
        (user_id, name, email, trial_key,
         created_at.isoformat(), expires_at.isoformat(), notes),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {
        "trial_key":  trial_key,
        "name":       name,
        "email":      email,
        "expires_at": expires_at.strftime("%Y-%m-%d"),
        "days":       days,
        "message":    f"Trial creado. El usuario debe introducir '{trial_key}' en el campo API Key de la app.",
    }


@app.get("/api/admin/users")
def list_trial_users(admin_secret: str = ""):
    _check_admin(admin_secret)
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT * FROM trial_users ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for r in rows:
        expired = datetime.fromisoformat(r["expires_at"]) < datetime.now()
        result.append({
            "name":          r["name"],
            "email":         r["email"],
            "trial_key":     r["trial_key"],
            "created_at":    r["created_at"][:10],
            "expires_at":    r["expires_at"][:10],
            "is_active":     bool(r["is_active"]),
            "expired":       expired,
            "uploads_count": r["uploads_count"],
            "chat_count":    r["chat_count"],
            "last_used_at":  r["last_used_at"],
            "notes":         r["notes"],
        })
    return result


@app.patch("/api/admin/users/{email}/deactivate")
def deactivate_trial(email: str, payload: dict):
    _check_admin(payload.get("admin_secret", ""))
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("UPDATE trial_users SET is_active=0 WHERE email=%s", (email,))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": f"Trial de {email} desactivado"}


@app.patch("/api/admin/users/{email}/extend")
def extend_trial(email: str, payload: dict):
    _check_admin(payload.get("admin_secret", ""))
    extra_days = int(payload.get("days", 7))
    conn = get_db()
    cur = get_cur(conn)
    cur.execute("SELECT expires_at FROM trial_users WHERE email=%s", (email,))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        raise HTTPException(404, f"Usuario {email} no encontrado")
    current_expiry = datetime.fromisoformat(user["expires_at"])
    base = max(current_expiry, datetime.now())
    new_expiry = base + timedelta(days=extra_days)
    cur.execute(
        "UPDATE trial_users SET expires_at=%s, is_active=1 WHERE email=%s",
        (new_expiry.isoformat(), email),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"message": f"Trial de {email} extendido hasta {new_expiry.strftime('%Y-%m-%d')}"}


# ── Serve frontend (must be LAST) ────────────────────────────────────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
