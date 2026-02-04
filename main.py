import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
from concurrent.futures import ThreadPoolExecutor
import pdfplumber
import numpy as np
import pandas as pd
from pdf2image import convert_from_path
from PIL import Image
import re

# Surya OCR
from surya.foundation import FoundationPredictor
from surya.detection import DetectionPredictor
from surya.recognition import RecognitionPredictor


# ---------------- CONFIG ----------------
DPI = 600
LINE_TOL = 2.0

MIN_LINE_LEN = 50


# ---------------- HELPERS ----------------
def clean_extracted_text(text):
    """
    ✅ VERIFIED Kaggle text cleaning implementation
    Applies all regex fixes from the working Kaggle notebook
    """
    if not text:
        return ""
    
    # 1. REMOVE HTML TAGS
    text = re.sub(r"<[^>]+>", "", text)
    
    # 2. FIX SPELLING: Change "लिग :", "लीग :", "लंग :" to "लिंग :"
    text = re.sub(r"(?:लिग|लीग|लंग)\s*:", "लिंग :", text)
    
    # 3. GENDER FIX (Now works because spelling is fixed)
    def fix_gender(match):
        val = match.group(1)
        return "लिंग : स्त्री" if "स्त्री" in val else "लिंग : पु"
    
    text = re.sub(r"लिंग\s*:\s*([^\s|,\n]+)", fix_gender, text)
    
    # 4. AGE NUMBER FIX (8->४, 0->७, etc)
    def fix_age_digits(match):
        prefix = match.group(1)
        digits = match.group(2)
        mapping = {'8': '४', '9': '९', '0': '७', '4': '५', '3': '३', '2': '२'}
        new_digits = "".join([mapping.get(c, c) for c in digits])
        return prefix + new_digits
    
    text = re.sub(r"(वय\s*:\s*)([\d]+)", fix_age_digits, text)
    
    # 5. GLOBAL QUESTION MARK FIX (? -> २)
    text = re.sub(r"[\d२-९]*\?+[\d२-९]*", lambda m: m.group(0).replace("?", "२"), text)
    
    return text


# ---------------- PARSING (KAGGLE-FAITHFUL) ----------------
def parse_header(text):
    """Parse header text into structured data"""
    data = {}
    
    # Division -> "निवडणूक विभाग"
    div_match = re.search(r"विभाग\s*:\s*(.*?)\s*निवार्चन", text)
    data['division'] = div_match.group(1).strip() if div_match else ""

    # Gan -> "निवार्चन गण"
    gan_match = re.search(r"गण\s*:\s*(.*?)\s*यादी", text)
    data['gan'] = gan_match.group(1).strip() if gan_match else ""

    # Part No -> "यादी भाग क्र."
    part_match = re.search(r"भाग क्र\.\s*(.*?)\s*पत्ता", text)
    data['part_no'] = part_match.group(1).strip() if part_match else ""

    # Address -> "पत्ता"
    addr_match = re.search(r"पत्ता\s*:\s*(.*?)\s*मतदान", text)
    data['address'] = addr_match.group(1).strip() if addr_match else ""

    # Polling Station -> "मतदान केंद्र"
    poll_match = re.search(r"केंद्र\s*:\s*(.*)", text)
    data['polling_station'] = poll_match.group(1).strip() if poll_match else ""

    return data


def parse_box_text(text, header_data):
    """Parse box text into structured voter data"""
    row_data = {}
    
    # Header columns
    row_data['निवडणूक विभाग'] = header_data.get('division', '')
    row_data['निवार्चन गण'] = header_data.get('gan', '')
    row_data['यादी भाग क्र.'] = header_data.get('part_no', '')
    row_data['पत्ता'] = header_data.get('address', '')
    row_data['मतदान केंद्र'] = header_data.get('polling_station', '')
    row_data['संपूर्ण शीर्षक'] = header_data.get('raw_header', '')  # ✅ Full header text

    # Voter ID (First alphanumeric token)
    vid_match = re.search(r"^([A-Z0-9]+)", text)
    row_data['voter_id'] = vid_match.group(1) if vid_match else ""

    # s (Pattern like 95/153/1)
    s_match = re.search(r"(\d+/\d+/\d+)", text)
    row_data['s'] = s_match.group(1) if s_match else ""

    # sr.no (Digits between pipes ONLY - strict mode)
    sr_match = re.search(r"\|\s*(\d+)\s*\|", text)
    row_data['sr.no'] = sr_match.group(1) if sr_match else ""

    # Name (मतदाराचे पूर्ण)
    name_match = re.search(r"मतदाराचे पूर्ण:\s*([^|]+?)(?:\s*(?:वडिलांचे|पतीचे|नांव|लिंग)|$)", text)
    row_data['मतदाराचे पूर्ण'] = name_match.group(1).strip() if name_match else ""

    # Gender (लिंग)
    gender_match = re.search(r"लिंग\s*:\s*([^\s|]+)", text)
    row_data['लिंग'] = gender_match.group(1).strip() if gender_match else ""

    # Age (वय)
    age_match = re.search(r"वय\s*:\s*([\d०-९]+)", text)
    row_data['वय'] = age_match.group(1).strip() if age_match else ""

    return row_data


# ---------------- CORE PROCESSING (KAGGLE-FAITHFUL) ----------------
def get_boxes_and_header(pdf_path, page_num, img_w, img_h):
    """
    ✅ VERIFIED Kaggle implementation
    Uses proper pdfplumber keys: 'top', 'bottom', 'x0', 'x1'
    """
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]
        lines = page.lines

        def get_filtered_coords(lines_subset, coord_key, len_key, tol=2.0, min_len=50):
            """Cluster lines and filter by max length in cluster"""
            lines_subset.sort(key=lambda x: x[coord_key])
            if not lines_subset:
                return []

            unique_coords = []
            curr_pos = [lines_subset[0][coord_key]]
            curr_len = [abs(lines_subset[0][len_key[1]] - lines_subset[0][len_key[0]])]

            for l in lines_subset[1:]:
                pos = l[coord_key]
                length = abs(l[len_key[1]] - l[len_key[0]])

                if abs(pos - curr_pos[-1]) <= tol:
                    curr_pos.append(pos)
                    curr_len.append(length)
                else:
                    if max(curr_len) > min_len:
                        unique_coords.append(sum(curr_pos) / len(curr_pos))
                    curr_pos = [pos]
                    curr_len = [length]

            if max(curr_len) > min_len:
                unique_coords.append(sum(curr_pos) / len(curr_pos))

            return unique_coords

        # ✅ CORRECT: Use 'top'/'bottom' and 'x0'/'x1' (guaranteed by pdfplumber)
        v_raw = [l for l in lines if abs(l["x1"] - l["x0"]) < 1]
        h_raw = [l for l in lines if abs(l["bottom"] - l["top"]) < 1]

        unique_vs = get_filtered_coords(v_raw, "x0", ("top", "bottom"))
        unique_hs = get_filtered_coords(h_raw, "top", ("x0", "x1"))

        if not unique_vs or not unique_hs:
            return [], None

        scale_x = img_w / page.width
        scale_y = img_h / page.height

        # Header
        header_bottom = int(unique_hs[0] * scale_y)
        header_rect = (0, 0, img_w, max(50, header_bottom - 5))

        boxes = []
        for i in range(len(unique_hs) - 1):
            y_top = unique_hs[i]
            y_bottom = unique_hs[i + 1]
            if y_bottom - y_top < 40:
                continue

            for j in range(len(unique_vs) - 1):
                x_left = unique_vs[j]
                x_right = unique_vs[j + 1]
                if x_right - x_left < 100:
                    continue

                x = int(x_left * scale_x)
                y = int(y_top * scale_y)
                w = int((x_right - x_left) * scale_x)
                h = int((y_bottom - y_top) * scale_y)

                boxes.append((x, y, w, h))

        boxes.sort(key=lambda b: (b[1] // 50, b[0]))
        return boxes, header_rect


# ---------------- GUI APP ----------------
class VoterExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Voter PDF OCR Extractor")
        # ✅ Increased window size for 3x bigger UI
        self.root.geometry("2000x1600")

        # ✅ Configure Large Styles (3x bigger)
        style = ttk.Style()
        style.configure("Big.TLabel", font=("Helvetica", 20))
        style.configure("Big.TButton", font=("Helvetica", 24))
        style.configure("Big.TEntry", font=("Helvetica", 20))
        style.configure("Big.TCheckbutton", font=("Helvetica", 18))
        style.configure("Big.TLabelframe.Label", font=("Helvetica", 24, "bold"))

        self.file_path_var = tk.StringVar()
        self.threads_var = tk.StringVar(value="4")
        self.start_page_var = tk.StringVar(value="1")
        self.end_page_var = tk.StringVar(value="")
        
        # ✅ Column selection mapping
        self.column_map = {
            "sr.no": "sr.no",
            "s": "s",
            "voter_id": "voter_id",
            "name": "मतदाराचे पूर्ण",
            "gender": "लिंग",
            "age": "वय",
            "division": "निवडणूक विभाग",
            "gan": "निवार्चन गण",
            "address": "पत्ता",
            "polling_station": "मतदान केंद्र",
            "part_no": "यादी भाग क्र.",
            "header": "संपूर्ण शीर्षक"
        }
        
        # ✅ Column selection checkboxes (all enabled by default)
        self.column_vars = {k: tk.BooleanVar(value=True) for k in self.column_map}
        
        # Build UI first (creates log_area)
        self.build_ui()
        
        # Load Surya OCR models (needs log_area to exist)
        self.log("⏳ Loading Surya OCR models...")
        self.foundation_predictor = FoundationPredictor()
        self.det_predictor = DetectionPredictor()
        self.rec_predictor = RecognitionPredictor(
            foundation_predictor=self.foundation_predictor
        )
        self.log("✅ Models loaded successfully!")

        self.ocr_lock = threading.Lock()

    # ---------- UI ----------
    def build_ui(self):
        # Step 1: PDF Selection
        frame_top = ttk.LabelFrame(self.root, text="Step 1: Select PDF", style="Big.TLabelframe")
        frame_top.pack(fill="x", padx=20, pady=20)

        ttk.Entry(frame_top, textvariable=self.file_path_var, width=60, font=("Helvetica", 20)).pack(
            side="left", padx=10, pady=10
        )
        ttk.Button(frame_top, text="Browse", command=self.browse, style="Big.TButton").pack(
            side="left", padx=10
        )

        # Step 2: Settings
        frame_settings = ttk.LabelFrame(self.root, text="Step 2: Settings", style="Big.TLabelframe")
        frame_settings.pack(fill="x", padx=20, pady=20)
        
        # DPI info
        ttk.Label(frame_settings, text=f"DPI: {DPI}", style="Big.TLabel").pack(
            side="left", padx=20, pady=10
        )
        
        # Threads
        ttk.Label(frame_settings, text="Threads:", style="Big.TLabel").pack(side="left", padx=20)
        ttk.Spinbox(frame_settings, from_=1, to=16, textvariable=self.threads_var, width=5, font=("Helvetica", 20)).pack(
            side="left", padx=10
        )
        
        # Start Page
        ttk.Label(frame_settings, text="Start Page:", style="Big.TLabel").pack(side="left", padx=20)
        ttk.Entry(frame_settings, textvariable=self.start_page_var, width=5, font=("Helvetica", 20)).pack(
            side="left", padx=10
        )
        
        # End Page
        ttk.Label(frame_settings, text="End Page:", style="Big.TLabel").pack(side="left", padx=20)
        ttk.Entry(frame_settings, textvariable=self.end_page_var, width=5, font=("Helvetica", 20)).pack(
            side="left", padx=10
        )
        ttk.Label(frame_settings, text="(leave empty for all)", style="Big.TLabel").pack(side="left", padx=10)

        # Step 3: Column Selection
        frame_columns = ttk.LabelFrame(self.root, text="Step 3: Select Columns for Excel", style="Big.TLabelframe")
        frame_columns.pack(fill="x", padx=20, pady=20)
        
        # Create 3 rows of checkboxes (4 columns each)
        column_labels = {
            "sr.no": "Serial No",
            "s": "S (95/153/1)",
            "voter_id": "Voter ID",
            "name": "Name (मतदाराचे पूर्ण)",
            "gender": "Gender (लिंग)",
            "age": "Age (वय)",
            "division": "Division (निवडणूक विभाग)",
            "gan": "Gan (निवार्चन गण)",
            "address": "Address (पत्ता)",
            "polling_station": "Polling Station (मतदान केंद्र)",
            "part_no": "Part No (यादी भाग क्र.)",
            "header": "Complete Header (संपूर्ण शीर्षक)"
        }
        
        # Row 1
        row1_frame = ttk.Frame(frame_columns)
        row1_frame.pack(fill="x", padx=10, pady=5)
        for key in ["sr.no", "s", "voter_id", "name"]:
            ttk.Checkbutton(row1_frame, text=column_labels[key], 
                          variable=self.column_vars[key], style="Big.TCheckbutton").pack(side="left", padx=20)
        
        # Row 2
        row2_frame = ttk.Frame(frame_columns)
        row2_frame.pack(fill="x", padx=10, pady=5)
        for key in ["gender", "age", "division", "gan"]:
            ttk.Checkbutton(row2_frame, text=column_labels[key], 
                          variable=self.column_vars[key], style="Big.TCheckbutton").pack(side="left", padx=20)
        
        # Row 3
        row3_frame = ttk.Frame(frame_columns)
        row3_frame.pack(fill="x", padx=10, pady=5)
        for key in ["address", "polling_station", "part_no", "header"]:
            ttk.Checkbutton(row3_frame, text=column_labels[key], 
                          variable=self.column_vars[key], style="Big.TCheckbutton").pack(side="left", padx=20)

        # Step 4: Start Button (Moved down)
        ttk.Button(
            self.root, text="🚀 Start Extraction", command=self.start_processing, style="Big.TButton"
        ).pack(pady=20)

        # Log Area
        self.log_area = scrolledtext.ScrolledText(
            self.root, height=18, font=("Consolas", 16)  # Larger font for log
        )
        self.log_area.pack(fill="both", expand=True, padx=20, pady=20)

    # ---------- LOG ----------
    def log(self, msg):
        self.log_area.insert(tk.END, msg + "\n")
        self.log_area.see(tk.END)
        self.root.update_idletasks()

    # ---------- FILE ----------
    def browse(self):
        f = filedialog.askopenfilename(filetypes=[("PDF Files", "*.pdf")])
        if f:
            self.file_path_var.set(f)

    # ---------- PROCESS ----------
    def start_processing(self):
        if not self.file_path_var.get():
            messagebox.showerror("Error", "Select a PDF first")
            return

        threading.Thread(target=self.process_pdf, daemon=True).start()

    def process_pdf(self):
        """
        NEW STRATEGY: Box-level parallelism
        - Process pages sequentially for better progress tracking
        - Detect all boxes on current page
        - Distribute box OCR across threads
        - Move to next page after all boxes complete
        """
        pdf = self.file_path_var.get()
        self.log("Opening PDF...")

        with pdfplumber.open(pdf) as p:
            total_pages = len(p.pages)

        # Get page range from UI
        try:
            start_page = int(self.start_page_var.get()) - 1  # Convert to 0-indexed
            if start_page < 0:
                start_page = 0
        except:
            start_page = 0
        
        try:
            end_page_str = self.end_page_var.get().strip()
            if end_page_str:
                end_page = int(end_page_str) - 1  # Convert to 0-indexed
                if end_page >= total_pages:
                    end_page = total_pages - 1
            else:
                end_page = total_pages - 1
        except:
            end_page = total_pages - 1
        
        self.log(f"Total pages in PDF: {total_pages}")
        self.log(f"Processing pages {start_page + 1} to {end_page + 1}")
        
        # Get thread count from settings
        max_workers = int(self.threads_var.get())
        self.log(f"Using {max_workers} threads for box OCR")

        all_results = []

        # Process pages sequentially
        for page_num in range(start_page, end_page + 1):
            self.log(f"📄 Page {page_num + 1}/{total_pages}: Detecting boxes...")
            
            # Step 1: Convert page to image
            img = convert_from_path(
                pdf, dpi=DPI, first_page=page_num + 1, last_page=page_num + 1
            )[0]
            
            # Step 2: Detect boxes
            boxes, header_rect = get_boxes_and_header(pdf, page_num, img.width, img.height)
            
            if not boxes:
                self.log(f"⚠️  Page {page_num + 1}: No boxes detected, skipping")
                continue
            
            self.log(f"✓ Page {page_num + 1}: {len(boxes)} boxes detected")
            
            # Step 3: Extract header (single-threaded, fast)
            header_data = {}
            if header_rect:
                hc = img.crop(header_rect)
                h_preds = self.rec_predictor([hc], det_predictor=self.det_predictor)
                header_text = " ".join([l.text for l in h_preds[0].text_lines])
                header_text = clean_extracted_text(header_text)
                
                # ✅ Store raw header text
                header_data['raw_header'] = header_text
                
                # ✅ Parse header into structured data
                parsed_header = parse_header(header_text)
                header_data.update(parsed_header)
            
            # Step 4: Prepare box crops
            box_crops = []
            for b in boxes:
                c = img.crop((b[0], b[1], b[0] + b[2], b[1] + b[3]))
                if np.mean(np.array(c.convert("L"))) < 250:
                    box_crops.append(c)
            
            if not box_crops:
                self.log(f"⚠️  Page {page_num + 1}: No valid boxes, skipping")
                continue
            
            self.log(f"🔄 Page {page_num + 1}: Processing {len(box_crops)} boxes across {max_workers} threads...")
            
            # Step 5: Process boxes in parallel (BOX-LEVEL PARALLELISM)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for i, crop in enumerate(box_crops):
                    future = executor.submit(self.process_single_box, crop, header_data, page_num, i)
                    futures.append(future)
                
                # Collect results with progress tracking
                page_results = []
                completed = 0
                for future in futures:
                    row = future.result()
                    if row:
                        page_results.append(row)
                    completed += 1
                    
                    # Log progress every 10 boxes
                    if completed % 10 == 0:
                        self.log(f"   ✓ Page {page_num + 1}: {completed}/{len(box_crops)} boxes completed")
            self.log(f"✅ Page {page_num + 1}: Complete! Extracted {len(page_results)} voters")
            all_results.extend(page_results)

        self.log(f"\n🎉 All pages complete! Total voters: {len(all_results)}")
        
        # ✅ Save Excel only
        self.save_excel(all_results)

    def process_single_box(self, crop, header_data, page_num, box_num):
        """
        Process a single box with OCR
        This runs in parallel across multiple threads
        """
        try:
            # Get thread ID for logging
            thread_id = threading.current_thread().name
            self.log(f"   🧵 Thread {thread_id}: Processing box {box_num + 1} on page {page_num + 1}")
            
            # OCR this single box (thread-safe with lock)
            with self.ocr_lock:
                preds = self.rec_predictor([crop], det_predictor=self.det_predictor)
            
            # Extract text
            raw_txt = " ".join([l.text for l in preds[0].text_lines])
            
            # ✅ Apply Kaggle-faithful text cleaning
            cleaned_txt = clean_extracted_text(raw_txt)
            
            # ✅ Parse into structured data (Kaggle-faithful)
            row = parse_box_text(cleaned_txt, header_data)
            
            self.log(f"   ✅ Thread {thread_id}: Completed box {box_num + 1}")
            return row
        except Exception as e:
            self.log(f"❌ Error processing box {box_num} on page {page_num + 1}: {e}")
            return None


    # ---------- EXCEL ----------
    def save_excel(self, data):
        """Save extracted data to Excel with Kaggle-faithful column structure"""
        if not data:
            self.log("⚠️  No data to save")
            return

        self.log("📊 Formatting Excel...")
        df = pd.DataFrame(data)
        
        # ✅ Build column list based on user selection
        all_columns = [
            "sr.no",
            "s",
            "voter_id",
            "मतदाराचे पूर्ण",
            "लिंग",
            "वय",
            "निवडणूक विभाग",
            "निवार्चन गण",
            "पत्ता",
            "मतदान केंद्र",
            "यादी भाग क्र.",
            "संपूर्ण शीर्षक"
        ]
        
        # Map internal column names to Marathi names
        column_name_map = {
            "sr.no": "sr.no",
            "s": "s",
            "voter_id": "voter_id",
            "name": "मतदाराचे पूर्ण",
            "gender": "लिंग",
            "age": "वय",
            "division": "निवडणूक विभाग",
            "gan": "निवार्चन गण",
            "address": "पत्ता",
            "polling_station": "मतदान केंद्र",
            "part_no": "यादी भाग क्र.",
            "header": "संपूर्ण शीर्षक"
        }
        
        # ✅ Get only selected columns
        final_columns = []
        for key, marathi_name in column_name_map.items():
            if self.column_vars[key].get():  # Check if checkbox is selected
                final_columns.append(marathi_name)
        
        if not final_columns:
            self.log("⚠️  No columns selected! Please select at least one column.")
            messagebox.showwarning("Warning", "Please select at least one column to export.")
            return
        
        # Ensure all columns exist in dataframe
        for col in final_columns:
            if col not in df.columns:
                df[col] = ""
        
        # Reorder to only selected columns
        df = df[final_columns]
        
        # Generate output filename
        input_file = self.file_path_var.get()
        if input_file:
            output_file = input_file.replace(".pdf", "_Voter_List_Final.xlsx")
        else:
            output_file = "Voter_List_Final.xlsx"
        
        # Save with formatting
        with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Voters")
            
            # Auto-adjust column widths
            worksheet = writer.sheets["Voters"]
            for i, col in enumerate(final_columns):
                max_len = max(
                    df[col].astype(str).apply(len).max(),
                    len(col)
                ) + 2
                worksheet.set_column(i, i, min(max_len, 50))
        
        self.log(f"✅ Excel saved: {output_file}")
        self.log(f"📊 Total voters extracted: {len(df)}")
        messagebox.showinfo("Success", f"✅ Saved {len(df)} voters to:\n{output_file}")


# ---------------- RUN ----------------
if __name__ == "__main__":
    root = tk.Tk()
    app = VoterExtractorApp(root)
    root.mainloop()
