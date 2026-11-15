"""Generate original tiny fixtures; run from the intelligence service directory.

Requires the development dependencies only. No input documents or fonts download.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 16 Tf 25 350 Td (Original digital fixture text.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(directory / "digital.pdf")

    image = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(image).text(
        (50, 80),
        "Original scanned fixture text.",
        fill="black",
        font=ImageFont.load_default(size=30),
    )
    image.save(directory / "scanned.pdf", "PDF", resolution=144)
    image.close()
    generate_structure(directory)


def generate_structure(directory: Path) -> None:
    """Original two-page fixture with recurring furniture, clauses, and a table."""
    pages = []
    font = ImageFont.load_default(size=36)
    body = [
        [
            "Section 3 Duties",
            "1. Deliver the report.",
            "1.1 Submit monthly updates.",
            "(a) Keep records.",
        ],
        ["Article 5 Notices", "Clause 4.2 Send written notice.", "Section 3 Retain copies."],
    ]
    for index, lines in enumerate(body):
        image = Image.new("RGB", (1200, 1600), "white")
        draw = ImageDraw.Draw(image)
        draw.text((100, 30), "ORIGINAL FIXTURE AGREEMENT", font=font, fill="black")
        for line_index, line in enumerate(lines):
            draw.text((100, 240 + line_index * 80), line, font=font, fill="black")
        for row_index, (item, count) in enumerate(
            [("Item", "Count"), ("Reports", "12"), ("Notices", "3")]
        ):
            draw.text((100, 700 + row_index * 60), item, font=font, fill="black")
            draw.text((800, 700 + row_index * 60), count, font=font, fill="black")
        draw.text((520, 1530), f"Page {index + 1} of 2", font=font, fill="black")
        pages.append(image)
    pages[0].save(
        directory / "structured-scanned.pdf",
        "PDF",
        resolution=144,
        save_all=True,
        append_images=pages[1:],
    )
    for image in pages:
        image.close()


if __name__ == "__main__":
    generate(Path(__file__).parent)
