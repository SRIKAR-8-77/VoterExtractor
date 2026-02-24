"""
Data Parser Module — Stage 2: Text Cleaning & Parsing

Reads raw OCR text, applies regex-based cleaning for Marathi voter data,
and produces structured records ready for Excel export.
"""

import re
import logging

logger = logging.getLogger(__name__)


# ── Text Cleaning ────────────────────────────────────────────


def clean_extracted_text(text: str) -> str:
    """
    Apply comprehensive regex fixes to clean OCR text.

    Handles HTML tags, Marathi keyword typos (gender, age, house number),
    serial number injection, gender normalization, and age digit correction.
    """
    if not text:
        return ""

    # 1. Remove noise & tags
    text = re.sub(r"<[^>]+>", " ", text)        # HTML tags
    text = re.sub(r"\bPhoto\b|\bAvailable\b", " ", text)
    text = re.sub(r"[·•]", " ", text)            # Bullet points
    text = re.sub(r"\s+", " ", text)             # Normalize whitespace

    # 2. Fix Marathi keyword typos
    text = re.sub(r"(?:लिग|लीग|लंग)\s*[:\.]?", "लिंग :", text)
    text = re.sub(r"(?:विय)\s*[:\.]?", "वय :", text)
    text = re.sub(r"(?:घर\s*क्रमाक|घर\s*क्रमांक)\s*[:\.]?", "घर क्रमांक :", text)

    # 3. Serial number injection
    def fix_missing_serial(match):
        full_id = match.group(1)
        serial = match.group(2)
        rest = match.group(3)
        if not rest.strip().startswith(serial):
            return f"{full_id} | {serial} | {rest}"
        return match.group(0)

    text = re.sub(r"(\d+/\d+/(\d+))\s*\|\s*(.*)", fix_missing_serial, text)

    # 4. Gender normalization
    def fix_gender(match):
        val = match.group(1).strip()
        return "लिंग : स्त्री" if "स्त्री" in val else "लिंग : पु"

    text = re.sub(r"लिंग\s*:\s*([^\s|,\n]*)", fix_gender, text)

    # 5. Age digit correction
    def fix_age_digits(match):
        prefix = match.group(1)
        digits = match.group(2)
        mapping = {"8": "४", "9": "९", "0": "७", "4": "५", "3": "३", "2": "२"}
        new_digits = "".join([mapping.get(c, c) for c in digits])
        return prefix + new_digits

    text = re.sub(r"(वय\s*:\s*)([\d]+)", fix_age_digits, text)

    # 6. Question mark fix
    text = re.sub(
        r"[\d२-९]*\?+[\d२-९]*",
        lambda m: m.group(0).replace("?", "२"),
        text,
    )

    return text


# ── Header Parsing ───────────────────────────────────────────


def parse_header(text: str) -> dict:
    """Parse header text into structured data fields."""
    data = {}

    # Electoral division
    div_match = re.search(
        r"निवडणूक\s+विभाग\s*:\s*([^निवार्चन]+?)(?=\s*निवार्चन|$)", text
    )
    if not div_match:
        div_match = re.search(r"प्रभाग\s+क्र\s*[:\s]+(\d+)", text)
    data["division"] = div_match.group(1).strip() if div_match else ""

    # Electoral constituency (निवार्चन गण)
    gan_match = re.search(r"निवार्चन\s+गण\s*:\s*(\d+)", text)
    data["gan"] = gan_match.group(1).strip() if gan_match else ""

    # Part number
    part_match = re.search(r"(यादी\s+भाग\s+क्र\..*?)(?=\s*पत्ता|$)", text)
    data["part_no"] = part_match.group(1).strip() if part_match else ""

    # Address
    addr_match = re.search(r"पत्ता\s*:\s*(.+?)(?=\s*मतदान|$)", text)
    data["address"] = addr_match.group(1).strip() if addr_match else ""

    # Polling station
    poll_match = re.search(r"मतदान\s+केंद्र\s*:\s*(.+?)(?=\s*$)", text)
    data["polling_station"] = poll_match.group(1).strip() if poll_match else ""

    return data


# ── Box/Voter Parsing ────────────────────────────────────────


def parse_box_text(text: str, header_data: dict) -> dict:
    """Parse a voter box's OCR text into a structured record."""
    row_data = {}

    # Voter ID
    vid_match = re.search(
        r"\b([A-Z]{3}\d{7}|[A-Z]{3}\d{6}|[A-Z]{2}[A-Z0-9]\d{7})\b", text
    )
    row_data["voter_id"] = vid_match.group(1) if vid_match else ""

    # S-number (XX/XXX/XXX)
    s_match = re.search(r"(\d{2,3}/\d{2,3}/\d{1,4})", text)
    row_data["s"] = s_match.group(1) if s_match else ""

    # Serial number
    sr_match = re.search(r"\|\s*(\d{1,4})\s*\|", text)
    if sr_match:
        row_data["sr.no"] = sr_match.group(1)
    elif row_data["s"]:
        parts = row_data["s"].split("/")
        if len(parts) == 3 and parts[2].isdigit():
            row_data["sr.no"] = parts[2]
        else:
            row_data["sr.no"] = ""
    else:
        row_data["sr.no"] = ""

    # Header-derived columns
    row_data["निवार्चन गण"] = header_data.get("gan", "")
    row_data["यादी भाग क्र."] = header_data.get("part_no", "")
    row_data["पत्ता"] = header_data.get("address", "")

    # Name
    name_match = re.search(r"मतदाराचे पूर्ण[:\s]*([^|]+)", text)
    row_data["मतदाराचे पूर्ण"] = name_match.group(1).strip() if name_match else ""

    # House number
    house_match = re.search(r"घर क्रमां?क\s*[:\.\s]*([^|]+)", text)
    row_data["घर क्रमांक"] = house_match.group(1).strip() if house_match else ""

    # Gender
    gender_match = re.search(r"लिंग\s*:\s*([^|]+)", text)
    row_data["लिंग"] = gender_match.group(1).strip() if gender_match else ""

    # Age
    age_match = re.search(r"वय\s*[:\s]*([\d०-९]+)", text)
    row_data["वय"] = age_match.group(1).strip() if age_match else ""

    # Raw header reference
    row_data["header"] = header_data.get("raw_header", "")

    # Deleted flag
    row_data["is_deleted"] = "**" if "**" in text else ""

    return row_data


# ── Serial Number Interpolation ──────────────────────────────


def interpolate_serial_numbers(page_results: list[dict]) -> None:
    """Fill missing serial numbers by interpolation from neighbors."""
    valid_indices = []
    for i, row in enumerate(page_results):
        sr = row.get("sr.no", "")
        if sr and sr.isdigit():
            valid_indices.append((i, int(sr)))

    if not valid_indices:
        return

    for i, row in enumerate(page_results):
        if not row.get("sr.no") or not row["sr.no"].isdigit():
            prev_anchor = next(
                ((idx, v) for idx, v in reversed(valid_indices) if idx < i), None
            )
            next_anchor = next(
                ((idx, v) for idx, v in valid_indices if idx > i), None
            )

            interpolated_val = None
            if prev_anchor:
                interpolated_val = prev_anchor[1] + (i - prev_anchor[0])
            elif next_anchor:
                interpolated_val = next_anchor[1] - (next_anchor[0] - i)

            if interpolated_val and interpolated_val > 0:
                row["sr.no"] = str(interpolated_val)


# ── Main Parse Pipeline ──────────────────────────────────────

# Column order for output
FINAL_COLUMNS = [
    "sr.no",
    "s",
    "voter_id",
    "निवार्चन गण",
    "यादी भाग क्र.",
    "पत्ता",
    "मतदाराचे पूर्ण",
    "घर क्रमांक",
    "लिंग",
    "वय",
    "header",
    "is_deleted",
]


def parse_raw_text(raw_lines: list[str]) -> list[dict]:
    """
    Process raw text lines (from Stage 1) into structured voter records.

    Args:
        raw_lines: List of strings (same format as _RAW.txt files).

    Returns:
        List of dicts, each representing a voter record with FINAL_COLUMNS keys.
    """
    all_results = []
    current_header = {}

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("HEADER:"):
            header_text = line.replace("HEADER:", "").strip()
            cleaned_header = clean_extracted_text(header_text)
            current_header = parse_header(cleaned_header)
            current_header["raw_header"] = header_text

        elif line.startswith("BOX"):
            box_text = re.sub(r"BOX \d+-\d+:\s*", "", line).strip()
            cleaned_box = clean_extracted_text(box_text)
            row = parse_box_text(cleaned_box, current_header)
            if row.get("voter_id", "").strip():
                all_results.append(row)

    if not all_results:
        return []

    # Interpolate missing serial numbers
    interpolate_serial_numbers(all_results)

    # Renumber sequentially
    for i, row in enumerate(all_results, 1):
        row["sr.no"] = str(i)

    # Ensure all columns exist
    for row in all_results:
        for col in FINAL_COLUMNS:
            if col not in row:
                row[col] = ""

    logger.info("Parsed %d voter records", len(all_results))
    return all_results
