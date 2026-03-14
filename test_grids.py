import pdfplumber
from backend.ocr_engine import get_boxes_and_header
with pdfplumber.open("demo.pdf") as pdf:
    print(f"Total pages: {len(pdf.pages)}")
    for page_num in range(len(pdf.pages)):
        boxes, header = get_boxes_and_header("demo.pdf", page_num, 1000, 1000)
        if boxes:
            print(f"Page {page_num+1} has {len(boxes)} boxes")
