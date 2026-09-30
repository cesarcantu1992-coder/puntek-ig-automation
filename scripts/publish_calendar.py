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


def graph_post(path, retries=3, **params):
    params["access_token"] = ACCESS_TOKEN
    last_error = None
    for attempt in range(retries):
        r = requests.post(f"{GRAPH}/{path}", data=params, timeout=60)
        data = r.json()
        if "error" not in data:
            return data
        last_error = data["error"]
        if last_error.get("is_transient") and attempt < retries - 1:
            print(f"[RETRY] {path}: error transitorio, reintentando en 15s — {last_error}")
            time.sleep(15)
            continue
        break
    raise RuntimeError(f"Graph API error en POST {path}: {last_error}")


def graph_get(path, **params):
    params["access_token"] = ACCESS_TOKEN
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Graph API error en GET {path}: {data['error']}")
    return data


def wait_for_container(container_id, max_wait=180, interval=5):
    """Espera a que Meta termine de procesar un contenedor de media
    (foto, carrusel o video) antes de publicarlo. Sin esto, publicar
    demasiado rápido produce 'Media ID is not available'."""
    waited = 0
    while waited < max_wait:
        info = graph_get(container_id, fields="status_code")
        status = info.get("status_code")
        if status == "FINISHED":
            return True
        if status == "ERROR":
            raise RuntimeError(f"El contenedor {container_id} falló al procesar: {info}")
        time.sleep(interval)
        waited += interval
    raise RuntimeError(f"El contenedor {container_id} tardó demasiado en procesar (timeout)")


def publish_photo(media_url, caption):
    container = graph_post(f"{IG_USER_ID}/media", image_url=media_url, caption=caption)
    creation_id = container["id"]
    wait_for_container(creation_id)
    return graph_post(f"{IG_USER_ID}/media_publish", creation_id=creation_id)


def publish_carousel(media_urls, caption):
    child_ids = []
    for url in media_urls:
        child = graph_post(f"{IG_USER_ID}/media", image_url=url, is_carousel_item="true")
        child_ids.append(child["id"])
    container = graph_post(
        f"{IG_USER_ID}/media",
        media_type="CAROUSEL",
        children=",".join(child_ids),
        caption=caption,
    )
    creation_id = container["id"]
    wait_for_container(creation_id)
    return graph_post(f"{IG_USER_ID}/media_publish", creation_id=creation_id)


def publish_video(video_url, caption, as_reel=True):
    media_type = "REELS" if as_reel else "VIDEO"
    container = graph_post(
        f"{IG_USER_ID}/media",
        media_type=media_type,
        video_url=video_url,
        caption=caption,
    )
    creation_id = container["id"]
    wait_for_container(creation_id, max_wait=300, interval=10)
    return graph_post(f"{IG_USER_ID}/media_publish", creation_id=creation_id)


def main():
    now = datetime.now().astimezone()
    rows = fetch_rows()
    done = load_state()
    published_any = False

    for row in rows:
        row_id = (row.get(COL_ID) or "").strip()
        if not row_id or row_id in done:
            continue

        estado = (row.get(COL_ESTADO) or "").strip().lower()
        if estado.startswith("falta"):
            print(f"[SKIP] {row_id}: marcado como archivo faltante en el Sheet.")
            continue

        try:
            scheduled = parse_dt(row[COL_FECHA])
        except Exception as e:
            print(f"[WARN] {row_id}: no se pudo leer la fecha ({e}), se salta.")
            continue

        if scheduled > now:
            continue  # aún no le toca

        tipo = (row.get(COL_TIPO) or "").strip().lower()
        media_field = (row.get(COL_MEDIA) or "").strip()
        caption = row.get(COL_CAPTION) or ""
        urls = [u.strip() for u in media_field.split(",") if u.strip()]

        if not urls:
            print(f"[WARN] {row_id}: no hay URL de media, se salta.")
            continue

        print(f"[PUBLISH] {row_id} — {row.get(COL_PIEZA)} ({tipo}) programado {scheduled}")

        try:
            if tipo == "foto":
                publish_photo(urls[0], caption)
            elif tipo == "carrusel":
                publish_carousel(urls, caption)
elif tipo in ("reel", "video"):
       publish_video(urls[0], caption, as_reel=True)
            else:
                print(f"[WARN] {row_id}: tipo desconocido '{tipo}', se salta.")
                continue
        except Exception as e:
            print(f"[ERROR] {row_id}: fallo al publicar — {e}")
            continue

        done.add(row_id)
        published_any = True
        print(f"[OK] {row_id} publicado correctamente.")

    save_state(done)
    print("Estado actualizado." if published_any else "Nada nuevo que publicar en esta corrida.")


if __name__ == "__main__":
    main()
