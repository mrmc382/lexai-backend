"""
LexAI — MVP de Análisis Inteligente de Contratos
Corre con: uvicorn main:app --reload
Abre: http://localhost:8000
"""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

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
DB_PATH = "lexai.db"

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
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
    conn.commit()
    conn.close()


init_db()

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


def analyze_contract(text: str, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    # Truncate to avoid token limits while keeping meaningful content
    contract_text = text[:10000] if len(text) > 10000 else text

    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": ANALYSIS_PROMPT + contract_text,
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
    conn.execute(
        "INSERT INTO contracts (id, filename, uploaded_at, file_size, status, raw_text) VALUES (?,?,?,?,?,?)",
        (contract_id, file.filename, datetime.now().isoformat(), len(content), "pending", raw_text),
    )
    conn.commit()
    conn.close()

    # Analyze with Claude if API key provided
    if api_key.strip():
        try:
            analysis = analyze_contract(raw_text, api_key.strip())
            conn = get_db()
            conn.execute(
                "UPDATE contracts SET status=?, analysis=? WHERE id=?",
                ("analyzed", json.dumps(analysis, ensure_ascii=False), contract_id),
            )
            conn.commit()
            conn.close()
            return {"id": contract_id, "status": "analyzed", "analysis": analysis}
        except json.JSONDecodeError as e:
            conn = get_db()
            conn.execute("UPDATE contracts SET status='error' WHERE id=?", (contract_id,))
            conn.commit()
            conn.close()
            raise HTTPException(500, f"La IA devolvió un formato inesperado: {e}")
        except anthropic.AuthenticationError:
            raise HTTPException(401, "API Key de Anthropic inválida. Compruébala en Configuración.")
        except Exception as e:
            conn = get_db()
            conn.execute("UPDATE contracts SET status='error' WHERE id=?", (contract_id,))
            conn.commit()
            conn.close()
            raise HTTPException(500, f"Error en análisis IA: {e}")

    return {"id": contract_id, "status": "pending", "message": "Documento subido. Introduce una API Key para analizar."}


@app.get("/api/contracts")
def list_contracts():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, filename, uploaded_at, file_size, status, analysis FROM contracts ORDER BY uploaded_at DESC"
    ).fetchall()
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
    row = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
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
    row = conn.execute("SELECT id FROM contracts WHERE id=?", (contract_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Contrato no encontrado")
    conn.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
    conn.commit()
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
    row = conn.execute("SELECT raw_text FROM contracts WHERE id=?", (contract_id,)).fetchone()
    conn.close()

    if not row:
        raise HTTPException(404, "Contrato no encontrado")

    try:
        analysis = analyze_contract(row["raw_text"], api_key.strip())
        conn = get_db()
        conn.execute(
            "UPDATE contracts SET status=?, analysis=? WHERE id=?",
            ("analyzed", json.dumps(analysis, ensure_ascii=False), contract_id),
        )
        conn.commit()
        conn.close()
        return {"id": contract_id, "status": "analyzed", "analysis": analysis}
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

    conn = get_db()
    row = conn.execute("SELECT raw_text, analysis FROM contracts WHERE id=?", (contract_id,)).fetchone()
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

    system = f"""Eres un abogado senior especializado en derecho español y europeo (Civil, Mercantil, Laboral).
Tienes acceso al contrato y su análisis previo. Responde de forma clara, directa y práctica en español.
Si algo requiere consulta presencial con un abogado colegiado, indícalo.

CONTRATO:
{contract_text}

ANÁLISIS PREVIO:
{analysis_summary}"""

    client = anthropic.Anthropic(api_key=api_key)
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


# ── Serve frontend (must be LAST) ────────────────────────────────────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
