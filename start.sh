#!/bin/bash
# LexAI — Script de arranque
# Uso: bash start.sh

set -e
cd "$(dirname "$0")"

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║        LexAI — Análisis de Contratos  ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 no encontrado. Instálalo desde https://python.org"
  exit 1
fi

# Install deps if needed
if ! python3 -c "import fastapi" &>/dev/null 2>&1; then
  echo "📦 Instalando dependencias..."
  pip3 install -r requirements.txt
fi

echo "✅ Dependencias OK"
echo "🚀 Iniciando servidor en http://localhost:8000"
echo "   Pulsa Ctrl+C para detener"
echo ""

# Open browser after 1.5 seconds
(sleep 1.5 && open http://localhost:8000) &

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
