"""Local Tesseract CLI OCR with PDFium rendering; no remote APIs."""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from intellidocs_intelligence.models import Page, ParseResult

from .base import DocumentParser, dependency, result_for
from .languages import language_codes
from .ocr_structure import ocr_blocks


class TesseractParser(DocumentParser):
    def __init__(self, executable: str = "tesseract", scale: float = 3.0) -> None:
        if scale <= 0:
            raise ValueError("Render scale must be positive")
        self.executable = executable
        self.scale = scale

    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        codes = language_codes(languages)
        available = self._run(["--list-langs"]).splitlines()[1:]
        for language, code in codes.items():
            if code not in available:
                raise ValueError(
                    f"Unsupported Tesseract language {language!r}: install pack {code!r}"
                )
        pdfium = dependency("pypdfium2", "tesseract")
        pdf = pdfium.PdfDocument(str(file_path))
        pages: list[Page] = []
        warnings: list[str] = []
        sizes: dict[int, tuple[float, float]] = {}
        try:
            with TemporaryDirectory(prefix="intellidocs-ocr-") as directory:
                for index in range(len(pdf)):
                    page = pdf[index]
                    bitmap = None
                    try:
                        width, height = page.get_size()
                        sizes[index + 1] = (width, height)
                        bitmap = page.render(scale=self.scale)
                        image_path = Path(directory) / f"{index}.png"
                        bitmap.to_pil().save(image_path)
                        data = self._run(
                            [
                                str(image_path),
                                "stdout",
                                "-l",
                                "+".join(codes.values()),
                                "tsv",
                            ]
                        )
                        blocks = ocr_blocks(data, self.scale, width, height)
                        if not blocks:
                            warnings.append(f"Page {index + 1}: OCR returned no text")
                        pages.append(
                            Page(
                                page=index + 1,
                                language=languages[0],
                                blocks=[*blocks],
                            )
                        )
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
        finally:
            pdf.close()
        version = self._run(["--version"]).splitlines()[0]
        return result_for(
            file_path,
            pages,
            "tesseract",
            version,
            warnings,
            sizes=sizes,
            languages=languages,
        )

    def _run(self, arguments: list[str]) -> str:
        try:
            return subprocess.run(
                [self.executable, *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            ).stdout
        except FileNotFoundError as error:
            raise RuntimeError(
                "Tesseract is not installed; install the system OCR binary"
            ) from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Tesseract failed: {error.stderr.strip()}") from error
