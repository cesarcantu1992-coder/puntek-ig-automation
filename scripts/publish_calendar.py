#!/usr/bin/env python3
"""
Publicador automático de Instagram para Puntek.

Lee el calendario de contenido directamente desde el Google Sheet
(publicado como CSV, sin necesidad de credenciales de Google), y
publica en Instagram — vía la Graph API de Meta — cualquier fila cuya
fecha/hora programada ya llegó y que todavía no se haya publicado.

Este script está pensado para correr cada 5 minutos vía GitHub Actions
(ver .github/workflows/publish.yml). No requiere intervención humana
una vez configurado: solo hay que mantener el Sheet actualizado con
contenido nuevo.

Variables de entorno requeridas (se configuran como "Secrets" en GitHub,
nunca se escriben en este archivo ni en el chat):
  SHEET_CSV_URL           URL pública del Sheet publicado como CSV
  IG_BUSINESS_ACCOUNT_ID  ID numérico de la cuenta de Instagram business (ig-user-id)
  IG_ACCESS_TOKEN         Token de acceso de larga duración de la Graph API

Variables opcionales:
  GRAPH_API_VERSION       Default "v21.0" — súbela si Meta la deprecó
  STATE_PATH              Default "state/published_state.json"
"""
import csv
import io
import json
import os
import time
from datetime import datetime

import requests

SHEET_CSV_URL = os.environ["SHEET_CSV_URL"]
IG_USER_ID = os.environ["IG_BUSINESS_ACCOUNT_ID"]
ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
STATE_PATH = os.environ.get("STATE_PATH", "state/published_state.json")

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

COL_FECHA = "Fecha y hora de publicación"
COL_PIEZA = "Pieza"
COL_TIPO = "Tipo"
COL_MEDIA = "Media URL(s) (separadas por coma si es carrusel)"
COL_CAPTION = "Caption"
COL_ESTADO = "Estado"
COL_ID = "ID"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_state(done_ids):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(done_ids), f, indent=2, ensure_ascii=False)


def fetch_rows():
    resp = requests.get(SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def parse_dt(s):
    # Espera ISO8601 con offset, ej. 2026-09-29T21:00:00-06:00
    return datetime.fromisoformat(s.strip())


def graph_post(path, **params):
    params["access_token"] = ACCESS_TOKEN
    r = requests.post(f"{GRAPH}/{path}
