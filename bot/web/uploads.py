import asyncio
import io
import uuid
import zipfile
from pathlib import Path

from PIL import Image as PILImage

from bot.sources import (BINARY_TYPES, TEXT_TYPES, PDF_CONVERTIBLE, SourceFile,
                         convert_to_pdf, extract_text, UnreadableFile)


def inspect_image(data):
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width*image.height > 30_000_000:
                raise ValueError('Поддерживаются JPG, PNG и WebP до 30 мегапикселей')
            size, fmt = image.size, image.format
            image.verify()
        return size, {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[fmt]
    except (OSError, SyntaxError, PILImage.DecompressionBombError) as e:
        raise ValueError('Не удалось прочитать изображение') from e


async def source_file(folder: Path, name: str, data: bytes, soffice=None):
    suffix = Path(name).suffix.lower()
    if suffix not in BINARY_TYPES and suffix not in TEXT_TYPES and not (soffice and suffix in PDF_CONVERTIBLE):
        raise ValueError('Поддерживаются PDF, DOCX, XLSX, PPTX, TXT, CSV, JPG, PNG и WebP')
    if suffix in ('.jpg', '.jpeg', '.png', '.webp'):
        _, suffix = await asyncio.to_thread(inspect_image, data)
    elif suffix == '.pdf' and not data.lstrip().startswith(b'%PDF-'):
        raise ValueError('Файл не является PDF')
    elif suffix in ('.docx', '.xlsx', '.pptx'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 100_000_000 or len(archive.infolist()) > 10000:
                    raise ValueError('Слишком большой распакованный документ')
                if '[Content_Types].xml' not in archive.namelist():
                    raise ValueError('Неверный формат документа')
        except zipfile.BadZipFile as e:
            raise ValueError('Не удалось прочитать документ') from e
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + suffix)
    path.write_bytes(data)
    try:
        mime, text = BINARY_TYPES.get(suffix, ''), None
        if soffice and suffix in PDF_CONVERTIBLE:
            try:
                pdf = await convert_to_pdf(soffice, path)
                if pdf.stat().st_size > 20_000_000:
                    pdf.unlink()
                    raise UnreadableFile('PDF после преобразования больше 20 МБ')
                path.unlink()
                path, mime = pdf, 'application/pdf'
            except UnreadableFile:
                if suffix not in TEXT_TYPES:
                    raise
        if path.suffix in TEXT_TYPES:
            text = await asyncio.to_thread(extract_text, path)
            if not text:
                raise ValueError('В документе не найден читаемый текст')
        return SourceFile(Path(name).name[:200], path, mime, text)
    except BaseException as error:
        path.unlink(missing_ok=True)
        if isinstance(error, UnreadableFile):
            raise ValueError('Не удалось прочитать документ. Проверьте файл и его формат.') from error
        raise
