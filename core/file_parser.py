import io
import os
import re
import urllib.parse
import logging
from pypdf import PdfReader
import docx

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".csv")


def is_valid_document(file_bytes: bytes, filename: str) -> bool:
    """Проверяет валидность типа файла по расширению и сигнатуре magic bytes."""
    filename_lower = filename.lower()
    if file_bytes.startswith(b"%PDF") or filename_lower.endswith(".pdf"):
        return file_bytes.startswith(b"%PDF")
    if file_bytes.startswith(b"PK\x03\x04") or filename_lower.endswith((".docx", ".doc")):
        return file_bytes.startswith(b"PK\x03\x04")
    return filename_lower.endswith(SUPPORTED_EXTENSIONS)



def extract_text_from_bytes(file_bytes: bytes, filename: str) -> str:
    """
    Извлекает текст из байтов файла (PDF, DOCX, TXT, MD, RTF, CSV).
    Использует проверку сигнатур (magic bytes) для надежного распознавания форматов.
    """
    filename_lower = filename.lower()

    # 1. Распознавание PDF по сигнатуре %PDF или расширению
    if file_bytes.startswith(b"%PDF") or filename_lower.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            pages_text = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages_text.append(text.strip())
            result = "\n\n".join(pages_text).strip()
            if not result:
                raise ValueError(
                    f"PDF-файл «{filename}» не содержит печатного текста (возможно, это сканированное изображение)."
                )
            return result
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"Не удалось прочитать PDF-файл «{filename}»: {e}")

    # 2. Проверка на устаревший бинарный формат Word 97-2003 (.doc)
    if file_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError(
            f"Файл «{filename}» сохранен в устаревшем бинарном формате Word 97-2003 (.doc). "
            f"Пожалуйста, сохраните его как современный документ .docx или .pdf и отправьте снова."
        )

    # 3. Распознавание DOCX (ZIP-архив с сигнатурой PK\x03\x04) или расширению .docx/.doc
    if file_bytes.startswith(b"PK\x03\x04") or filename_lower.endswith((".docx", ".doc")):
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
        except Exception as e:
            raise ValueError(f"Не удалось открыть документ Word «{filename}»: {e}")

        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)

        result = "\n\n".join(paragraphs).strip()
        if not result:
            raise ValueError(f"Документ «{filename}» пуст.")
        return result

    # 4. Текстовые файлы (TXT, MD, RTF, CSV)
    if filename_lower.endswith((".txt", ".md", ".rtf", ".csv")):
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                decoded = file_bytes.decode(enc).strip()
                if decoded:
                    return decoded
            except UnicodeDecodeError:
                continue
        raise ValueError(f"Не удалось распознать кодировку текстового файла «{filename}».")

    # 5. Попытка декодировать как utf-8 текст на случай файла без расширения
    try:
        decoded = file_bytes.decode("utf-8").strip()
        if decoded and len(decoded) > 10:
            return decoded
    except UnicodeDecodeError:
        pass

    raise ValueError(
        f"Формат файла «{filename}» не поддерживается. Пожалуйста, отправьте конспект в формате PDF, DOCX или TXT."
    )


async def extract_materials_from_message(bot, message) -> list[tuple[str, str]]:
    """
    Проверяет наличие файлов во вложениях сообщения, скачивает их и извлекает текст.
    Возвращает список кортежей: [(filename, text), ...]
    """
    extracted = []
    body = getattr(message, "body", None)
    if not body:
        return extracted

    attachments = getattr(body, "attachments", None)
    if not attachments:
        return extracted

    for att in attachments:
        filename = (
            getattr(att, "filename", None)
            or getattr(att, "name", None)
            or getattr(att, "title", None)
        )
        payload = getattr(att, "payload", None)
        url = getattr(payload, "url", None) if payload else None

        if not filename and url:
            parsed_path = urllib.parse.urlparse(url).path
            base = os.path.basename(parsed_path)
            if base and "." in base:
                filename = urllib.parse.unquote(base)

        if not filename:
            filename = "документ"

        if not url:
            continue

        try:
            file_bytes = await bot.download_bytes(url)
            # Уточняем расширение по magic bytes, если имя было дефолтным
            if filename == "документ":
                if file_bytes.startswith(b"%PDF"):
                    filename = "документ.pdf"
                elif file_bytes.startswith(b"PK\x03\x04"):
                    filename = "документ.docx"

            text = extract_text_from_bytes(file_bytes, filename)
            extracted.append((filename, text))
        except Exception as e:
            logger.warning(f"Ошибка при обработке файла {filename}: {e}")
            raise

    return extracted
