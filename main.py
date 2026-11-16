import os
import asyncio
import re
import time
from html import unescape
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession
from google import genai

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION = os.environ.get("TG_SESSION", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def generar_resumen_con_respaldo(ai_client, contenido):
    """Reintenta errores temporales y cambia a otro modelo Flash estable si hay saturación."""
    modelos = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    errores_temporales = {429, 500, 502, 503, 504}

    for modelo in modelos:
        for intento in range(3):
            try:
                print(f"Solicitando resumen con {modelo} (intento {intento + 1}/3)...")
                return ai_client.models.generate_content(model=modelo, contents=contenido)
            except Exception as exc:
                codigo = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if codigo not in errores_temporales and not any(str(n) in str(exc) for n in errores_temporales):
                    raise
                if intento < 2:
                    espera = 5 * (2 ** intento)
                    print(f"{modelo} temporalmente no disponible (HTTP {codigo or 'temporal'}). Reintentando en {espera} segundos...")
                    time.sleep(espera)
                else:
                    print(f"{modelo} sigue saturado; probando el siguiente modelo Flash.")

    raise RuntimeError("Gemini no estuvo disponible en los tres modelos Flash. No se modificará el cursor de Telegram; vuelve a ejecutar más tarde.")


CANAL = os.environ.get("TG_TEST_CHANNEL", "rtnoticias").strip().lstrip("@")
ARCHIVO_ESTADO = "ultimo_id.txt"
ARCHIVO_RESUMEN = "resumen_hoy.txt"
MAX_PUBLICACIONES = 500


def obtener_ultimo_id():
    if os.path.exists(ARCHIVO_ESTADO):
        with open(ARCHIVO_ESTADO, "r", encoding="utf-8") as f:
            valor = f.read().strip()
            return int(valor) if valor.isdigit() else 0
    return 0


def guardar_ultimo_id(msg_id):
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        f.write(str(msg_id))


def extraer_publicaciones_web(ultimo_id):
    """Fallback: lee el historial público desde la vista web oficial de Telegram."""
    publicaciones = {}
    url = f"https://t.me/s/{CANAL}"
    antes_de = None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RTNewsDigest/1.0)",
        "Accept-Language": "es,en;q=0.8",
    }

    alcanzado_cursor = ultimo_id == 0
    for pagina in range(100):
        params = {"before": antes_de} if antes_de else {}
        respuesta = requests.get(url, params=params, headers=headers, timeout=25)
        respuesta.raise_for_status()
        soup = BeautifulSoup(respuesta.text, "html.parser")
        bloques = soup.select(".tgme_widget_message_wrap")
        if not bloques:
            # Diagnóstico del HTML real recibido: Telegram puede variar el marcado o servir una página de bloqueo.
            titulo = soup.title.get_text(" ", strip=True) if soup.title else "(sin título)"
            print(
                f"Telegram web sin bloques: status={respuesta.status_code}, "
                f"url={respuesta.url}, bytes={len(respuesta.text)}, "
                f"title={titulo!r}, contiene_data_post={'data-post' in respuesta.text}, "
                f"contiene_widget={'tgme_widget_message' in respuesta.text}."
            )
            print(f"Inicio de respuesta HTML: {respuesta.text[:700]!r}")
            # Segundo selector menos estricto por si cambia el contenedor exterior.
            nodos = soup.select("[data-post]")
            if nodos:
                print(f"Se encontraron {len(nodos)} nodos alternativos con data-post.")
                bloques = nodos
            else:
                print(f"Vista web de Telegram: no se encontraron publicaciones en la página {pagina + 1}.")
                break

        ids_pagina = []
        for bloque in bloques:
            nodo = bloque if bloque.has_attr("data-post") else bloque.select_one(".tgme_widget_message")
            texto_nodo = bloque.select_one(".tgme_widget_message_text") if hasattr(bloque, "select_one") else None
            if not nodo:
                continue

            data_post = nodo.get("data-post", "")
            match = re.search(r"/(\d+)$", data_post)
            if not match:
                continue
            msg_id = int(match.group(1))
            ids_pagina.append(msg_id)

            if msg_id <= ultimo_id:
                continue

            texto = texto_nodo.get_text("\n", strip=True) if texto_nodo else ""
            texto = unescape(texto).strip()
            if not texto:
                continue

            enlace = f"https://t.me/{CANAL}/{msg_id}"
            publicaciones[msg_id] = {
                "id": msg_id,
                "text": texto,
                "link": enlace,
            }

        print(f"Vista web página {pagina + 1}: {len(bloques)} publicaciones visibles.")
        if not ids_pagina:
            break

        menor_id = min(ids_pagina)
        if ultimo_id > 0 and menor_id <= ultimo_id:
            alcanzado_cursor = True
            break
        if ultimo_id == 0 and len(publicaciones) >= MAX_PUBLICACIONES:
            break
        if antes_de == menor_id:
            break
        antes_de = menor_id

    if ultimo_id > 0 and not alcanzado_cursor:
        raise RuntimeError(
            "El respaldo web no alcanzó el último ID guardado dentro de 100 páginas; "
            "se detiene para no saltarse publicaciones ni avanzar el cursor."
        )

    resultado = sorted(publicaciones.values(), key=lambda x: x["id"])
    # Con cursor, procesa primero las publicaciones más antiguas pendientes.
    # En la primera ejecución, toma las 500 más recientes.
    return resultado[:MAX_PUBLICACIONES] if ultimo_id > 0 else resultado[-MAX_PUBLICACIONES:]


async def extraer_telethon(client, entidad, ultimo_id):
    # Diagnóstico independiente: comprueba si Telegram entrega mensajes recientes
    # antes de aplicar el filtro por último ID.
    recientes = await client.get_messages(entidad, limit=5)
    print(
        f"Prueba directa get_messages: {len(recientes)} mensajes; "
        f"IDs={[m.id for m in recientes]}; "
        f"fechas={[str(m.date) for m in recientes]}"
    )
    for mensaje in recientes[:3]:
        muestra = (mensaje.raw_text or "").replace("\n", " ")[:160]
        print(f"Mensaje reciente id={mensaje.id}, texto={muestra!r}")

    if ultimo_id > 0:
        # Recupera las publicaciones pendientes en orden cronológico, empezando
        # por las más antiguas después del cursor para no saltarse noticias.
        iterador = client.iter_messages(
            entidad, min_id=ultimo_id, limit=MAX_PUBLICACIONES, reverse=True
        )
    else:
        # Primera ejecución: toma las 500 más recientes.
        iterador = client.iter_messages(entidad, limit=MAX_PUBLICACIONES)

    mensajes = [m async for m in iterador]
    print(
        f"Prueba iter_messages: {len(mensajes)} mensajes recibidos "
        f"antes de filtrar texto; IDs={[m.id for m in mensajes[:5]]}"
    )
    resultado = []
    for m in sorted(mensajes, key=lambda x: x.id):
        texto = (m.raw_text or "").strip()
        if texto:
            resultado.append({
                "id": m.id,
                "text": texto,
                "link": f"https://t.me/{CANAL}/{m.id}",
            })
    return resultado


async def main():
    modo_prueba = bool(os.environ.get("TG_TEST_CHANNEL", "").strip())
    if not modo_prueba and not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en GitHub Secrets.")

    # La prueba de otro canal nunca usa ni modifica el cursor de RT.
    ultimo_id = 0 if modo_prueba else obtener_ultimo_id()
    print(f"Último ID guardado: {ultimo_id}")
    publicaciones = []

    # Primero intenta la API oficial de Telegram si hay credenciales.
    if API_ID and API_HASH and TG_SESSION:
        client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH)
        try:
            print("Conectando con Telegram mediante Telethon...")
            await client.start()
            if not await client.is_user_authorized():
                raise RuntimeError("La sesión de Telegram no está autorizada.")
            usuario = await client.get_me()
            print(f"Sesión autorizada: {getattr(usuario, 'username', None) or 'usuario sin username'}")
            entidad = await client.get_entity(f"@{CANAL}")
            print(
                f"Canal resuelto: título={getattr(entidad, 'title', 'sin título')!r}, "
                f"id={getattr(entidad, 'id', 'desconocido')}"
            )
            publicaciones = await extraer_telethon(client, entidad, ultimo_id)
            print(f"Telethon recuperó {len(publicaciones)} publicaciones con texto.")
        except Exception as exc:
            print(f"Telethon no pudo recuperar el historial: {type(exc).__name__}: {exc}")
        finally:
            if client.is_connected():
                await client.disconnect()
    else:
        print("Credenciales de Telethon incompletas; se usará la vista pública.")

    if modo_prueba:
        if not publicaciones:
            raise RuntimeError(f"No se recuperaron publicaciones para la prueba de @{CANAL}.")

        bloque_noticias = "\n\n---\n\n".join(
            f"[ID {p['id']}] {p['text']}\nEnlace: {p['link']}"
            for p in publicaciones
        )
        with open("publicaciones_prueba.txt", "w", encoding="utf-8") as f:
            f.write(
                f"Canal: @{CANAL}\n"
                f"Publicaciones recuperadas: {len(publicaciones)}\n\n"
                + bloque_noticias
            )

        if not GEMINI_API_KEY:
            raise RuntimeError("Falta GEMINI_API_KEY; no se puede generar el resumen de prueba.")

        prompt_prueba = f"""
Prepara un reporte completo en español a partir de estas publicaciones del canal @{CANAL}.
1. Incluye todas las noticias distintas; no omitas ninguna.
2. Une publicaciones que hablen del mismo suceso o sean actualizaciones.
3. Organiza cada noticia con un titular y un resumen claro con los datos importantes.
4. Conserva el enlace de Telegram al final de cada noticia.
5. No inventes datos que no aparezcan en las publicaciones.

Publicaciones:
{bloque_noticias}
"""
        print(f"Prueba: enviando {len(publicaciones)} publicaciones a Gemini...")
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        respuesta = generar_resumen_con_respaldo(ai_client, prompt_prueba)
        if not respuesta.text or not respuesta.text.strip():
            raise RuntimeError("Gemini no devolvió el resumen de prueba.")
        with open("resumen_prueba.txt", "w", encoding="utf-8") as f:
            f.write(respuesta.text.strip())

        print("RESUMEN_PRUEBA_INICIO")
        print(respuesta.text.strip())
        print("RESUMEN_PRUEBA_FIN")

        print(
            f"PRUEBA COMPLETADA para @{CANAL}: {len(publicaciones)} publicaciones "
            "procesadas por Gemini. Se guardaron publicaciones_prueba.txt y "
            "resumen_prueba.txt. No se modificaron ultimo_id.txt ni resumen_hoy.txt."
        )
        # Deja el texto disponible también en los logs para poder recuperarlo.
        for p in publicaciones:
            print(f"PUBLICACION_ID={p['id']}\n{p['text']}\nENLACE={p['link']}\n---")
        return

    # Si Telethon no devuelve contenido, usa la vista pública oficial t.me/s.
    if not publicaciones:
        print("Activando respaldo: historial público web de Telegram...")
        try:
            publicaciones = extraer_publicaciones_web(ultimo_id)
            print(f"Respaldo web recuperó {len(publicaciones)} publicaciones con texto.")
        except Exception as exc:
            raise RuntimeError(
                f"Fallaron tanto la extracción por Telethon como el respaldo web: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # Elimina duplicados por ID y conserva solo publicaciones posteriores al cursor.
    unicas = {p["id"]: p for p in publicaciones if p["id"] > ultimo_id}
    publicaciones = [unicas[k] for k in sorted(unicas)]
    print(f"Total de publicaciones nuevas para resumir: {len(publicaciones)}")

    if not publicaciones:
        with open(ARCHIVO_RESUMEN, "w", encoding="utf-8") as f:
            f.write(
                "No se encontraron publicaciones nuevas con texto.\n"
                f"Último ID guardado: {ultimo_id}\n"
                "La ejecución terminó sin modificar el cursor."
            )
        print("No hay publicaciones nuevas. El cursor permanece intacto.")
        return

    bloque_noticias = "\n\n---\n\n".join(
        f"[ID {p['id']}] {p['text']}\nEnlace: {p['link']}"
        for p in publicaciones
    )
    prompt = f"""
Prepara un reporte completo en español a partir de estas publicaciones de RT en Español.
1. Incluye todas las noticias distintas; no omitas ninguna.
2. Une publicaciones que hablen del mismo suceso o sean actualizaciones.
3. Organiza cada noticia con un titular y un resumen claro con los datos importantes.
4. Conserva el enlace de Telegram al final de cada noticia.
5. No inventes datos que no aparezcan en las publicaciones.

Publicaciones:
{bloque_noticias}
"""

    print(f"Enviando {len(publicaciones)} publicaciones a Gemini...")
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    respuesta = generar_resumen_con_respaldo(ai_client, prompt)

    if not respuesta.text or not respuesta.text.strip():
        raise RuntimeError("Gemini no devolvió un resumen. El cursor no se actualizará.")

    with open(ARCHIVO_RESUMEN, "w", encoding="utf-8") as f:
        f.write(respuesta.text.strip())

    nuevo_max_id = max(p["id"] for p in publicaciones)
    guardar_ultimo_id(nuevo_max_id)
    print(
        f"Proceso completado: {len(publicaciones)} publicaciones resumidas. "
        f"Último ID guardado: {nuevo_max_id}"
    )


if __name__ == "__main__":
    asyncio.run(main())
