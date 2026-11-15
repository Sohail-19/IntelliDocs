"""Generate original image, DOCX, EML, and Outlook MSG upload fixtures."""

import struct
from email.message import EmailMessage
from pathlib import Path

from docx import Document
from extract_msg.ole_writer import OleWriter
from PIL import Image, ImageDraw, ImageFont


def generate(directory: Path) -> None:
    with Image.new("RGB", (600, 300), "white") as image:
        ImageDraw.Draw(image).text(
            (30, 60), "Original upload fixture", fill="black", font=ImageFont.load_default(size=30)
        )
        image.save(directory / "upload.png")
        image.save(directory / "upload.jpg")
    document = Document()
    document.add_heading("Original upload", level=1)
    document.add_paragraph("1. Deliver the original fixture report.")
    table = document.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, [["Item", "Count"], ["Reports", "12"]], strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    document.save(directory / "upload.docx")
    attachment = (directory / "digital.pdf").read_bytes()
    email = EmailMessage()
    email["From"] = "author@example.invalid"
    email["To"] = "reader@example.invalid"
    email["Subject"] = "Original email fixture"
    email.set_content("1. Read the original attachment.")
    email.add_attachment(attachment, maintype="application", subtype="pdf", filename="attached.pdf")
    (directory / "upload.eml").write_bytes(email.as_bytes())

    writer = OleWriter()
    writer.addEntry("__nameid_version1.0", storage=True)
    for number in ("0002", "0003", "0004"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{number}0102", b"")
    properties = struct.pack("<8sIIII8s", b"\0" * 8, 0, 1, 0, 1, b"\0" * 8)
    properties += struct.pack("<IIII", 0x340D0003, 6, 0x40000, 0)  # Unicode store.
    writer.addEntry("__properties_version1.0", properties)
    for tag, value in {
        "001A": "IPM.Note",
        "0037": "Original MSG fixture",
        "1000": "1. Read the original attachment.",
    }.items():
        writer.addEntry(f"__substg1.0_{tag}001F", (value + "\0").encode("utf-16-le"))
    folder = "__attach_version1.0_#00000000"
    writer.addEntry(folder, storage=True)
    writer.addEntry(
        f"{folder}/__properties_version1.0", b"\0" * 8 + struct.pack("<IIII", 0x37050003, 6, 1, 0)
    )
    writer.addEntry(f"{folder}/__substg1.0_3707001F", "attached.pdf\0".encode("utf-16-le"))
    writer.addEntry(f"{folder}/__substg1.0_37010102", attachment)
    writer.write(directory / "upload.msg")


if __name__ == "__main__":
    generate(Path(__file__).parent)
