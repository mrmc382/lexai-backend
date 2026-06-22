# Cómo publicar LexAI en Render.com (GRATIS)

Tiempo estimado: 15 minutos. No necesitas tarjeta de crédito.

---

## Paso 1 — Crear cuenta en GitHub (si no tienes)

1. Ve a https://github.com/signup
2. Regístrate con tu email (mrmc382@gmail.com)
3. Elige el plan **Free**

---

## Paso 2 — Subir el código a GitHub

1. En GitHub, clic en **+ New repository**
2. Nombre: `lexai-app`
3. Visibilidad: **Private**
4. Clic en **Create repository**
5. En la página del repositorio vacío, elige **uploading an existing file**
6. Arrastra TODOS los archivos de la carpeta `LexAI/` (excepto `uploads/` y `lexai.db`)
7. Clic en **Commit changes**

Archivos que deben estar en GitHub:
- main.py
- requirements.txt
- runtime.txt
- Procfile
- render.yaml
- .gitignore
- frontend/ (toda la carpeta)

---

## Paso 3 — Crear cuenta en Render.com

1. Ve a https://render.com
2. Clic en **Get Started for Free**
3. Regístrate con GitHub (más fácil) o con email

---

## Paso 4 — Crear el servicio web

1. En el dashboard de Render, clic en **New +** → **Web Service**
2. Conecta tu repositorio de GitHub → selecciona `lexai-app`
3. Configuración:
   - **Name:** lexai-app
   - **Region:** Frankfurt (EU) — más cerca de España
   - **Branch:** main
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
4. Clic en **Create Web Service**

El deploy tarda ~3 minutos la primera vez.

---

## Paso 5 — Tu URL pública

Render te da una URL así:
`https://lexai-app.onrender.com`

Esa es la URL que das a los clientes para la prueba gratuita.

---

## Notas importantes

- **La app duerme** después de 15 min sin uso (plan gratuito). El primer acceso tarda ~30 segundos en despertar — avisa al cliente antes de la demo.
- **Los contratos subidos no persisten** entre reinicios — está bien para demos.
- **La API Key de Anthropic** la introduce cada usuario en la app (no necesitas configurar nada en el servidor).
- Cuando tengas clientes de pago, puedes pasar al plan **Starter de Render** ($7/mes) para que no duerma.

---

## Para actualizar el código

Cada vez que cambies algo en `main.py` o el frontend, sube el archivo actualizado a GitHub y Render redeploya automáticamente en ~2 minutos.
