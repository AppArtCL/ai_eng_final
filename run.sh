#!/usr/bin/env bash
# Levanta el sistema completo con un solo comando: ./run.sh
#
# Hace lo mismo que `docker compose up --build`, pero antes revisa lo que suele fallar en una
# máquina limpia (Docker apagado, falta el .env o las claves están vacías) y explica cómo arreglarlo.
set -euo pipefail
cd "$(dirname "$0")"

error() { echo "❌ $*" >&2; exit 1; }

command -v docker >/dev/null || error "No se encontró Docker. Instálalo desde https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || error "Docker no está corriendo. Abre Docker Desktop (o inicia el daemon) y vuelve a intentar."
docker compose version >/dev/null 2>&1 || error "Falta Docker Compose v2 (el comando 'docker compose')."

if [[ ! -f .env ]]; then
  cp .env.example .env
  error "Creé .env a partir de .env.example. Completa OPENAI_API_KEY y PINECONE_API_KEY en .env y vuelve a correr ./run.sh"
fi
for clave in OPENAI_API_KEY PINECONE_API_KEY; do
  grep -qE "^${clave}=.+" .env || error "Falta ${clave} en .env (la línea existe pero está vacía)."
done

echo "▶ Construyendo y levantando Redis, Phoenix, la ingesta del índice y la API…"
docker compose up --build --detach --wait

cat <<'FIN'

✅ Sistema arriba.
   API y documentación (OpenAPI):  http://localhost:8000/docs
   Trazas (Arize Phoenix):         http://localhost:6006

   Probar:  curl -s -X POST localhost:8000/tasks -H 'Content-Type: application/json' \
              -d '{"pregunta":"¿Puedo tener mascota?"}'
   Logs:    docker compose logs -f api
   Apagar:  docker compose down        (los datos de Redis y Phoenix se conservan en volúmenes)
FIN
