from datetime import datetime
import os
import math
import re
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

HEADER_MAP = {
  'sr.no': 'sr_no',
  's': 's',
  'voter_id': 'voter_id',
  'निवार्चन गण': 'constituency',
  'यादी भाग क्र.': 'list_part_no',
  'पत्ता': 'address',
  'मतदाराचे पूर्ण': 'voter_full_name',
  'घर क्रमांक': 'house_number',
  'लिंग': 'gender',
  'वय': 'age',
  'header': 'header',
  'is_deleted': 'is_deleted',
}

def split_full_name(full_name):
    if not isinstance(full_name, str) or not full_name.strip():
        return None, None, None
        
    name_str = full_name.strip()
    name_str = re.sub(r'^(?:नाव|पूर्ण\s*नाव)\s*[:\-\.]?\s*', '', name_str, flags=re.IGNORECASE).strip()
    
    parts = name_str.split()
    if len(parts) == 0:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]

def normalize_header(s):
    if not isinstance(s, str):
        return ""
    s = s.strip().lower()
    s = re.sub(r'[\u200b-\u200d\ufeff\u00a0]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s

def process_voter_excel_to_db(excel_path: str, params: dict, db_url: str):
    """
    Identical logic to Next.js route for uploading voter Excel to DB:
    1. Parse Excel
    2. Map Marathi formatting to columns
    3. Bulk Delete existing data matching local body parameters inside a transaction
    4. Bulk Insert parsed data
    """
    
    local_body_id = int(params.get('localBodyId'))
    prabhag_no = str(params.get('prabhagNo')).strip() if params.get('prabhagNo') else None
    zp_ps_sub_type = params.get('zpPsSubType') or None
    ward_no = str(params.get('wardNo')).strip() if params.get('wardNo') else (None if zp_ps_sub_type else '-')
    booth_no = str(params.get('boothNo')).strip() if params.get('boothNo') else '-'
    r2_base_path = params.get('r2_base_path')
    
    gat = prabhag_no if zp_ps_sub_type else None
    gan = None if (ward_no == '-' or not ward_no) else ward_no

    df = pd.read_excel(excel_path)
    if df.empty:
        return 0
        
    voters = []
    
    for idx, row in df.iterrows():
        mapped = {}
        # Simple header mapping mimicking JS implementation
        for key in row.keys():
            normalized_key = normalize_header(str(key))
            matched_english = None
            
            # 1. Try exact exact match first
            for marathi, english in HEADER_MAP.items():
                if normalize_header(marathi) == normalized_key:
                    matched_english = english
                    break
            
            # 2. Try substring match if no exact match
            if not matched_english:
                for marathi, english in HEADER_MAP.items():
                    if normalize_header(marathi) in normalized_key:
                        matched_english = english
                        break
                        
            if matched_english:
                val = row[key]
                mapped[matched_english] = val if pd.notna(val) else None
                    
        if not mapped.get('voter_full_name') and not mapped.get('voter_id'):
            continue
            
        first_name, middle_name, last_name = split_full_name(mapped.get('voter_full_name'))
        
        # Build image URL if we uploaded to R2 (matching srNo format from worker)
        sr_no = int(mapped.get('sr_no')) if mapped.get('sr_no') else None
        img_url = None
        if r2_base_path and sr_no:
            # Reconstruct the R2 public path - usually frontends prepend their domain but we can save the path
            img_url = f"{r2_base_path}/{sr_no}.jpg"
            
        is_deleted_raw = mapped.get('is_deleted')
        is_deleted = False
        if isinstance(is_deleted_raw, str) and is_deleted_raw.strip().lower() in ('deleted', 'del'):
            is_deleted = True
        elif is_deleted_raw is True:
            is_deleted = True
        elif isinstance(is_deleted_raw, str) and "**" in is_deleted_raw:
            is_deleted = True
            
        age_raw = mapped.get('age')
        age_str = str(int(age_raw)) if pd.notna(age_raw) and isinstance(age_raw, (int, float)) else str(age_raw) if age_raw else None
        if age_str and age_str.lower() == 'nan':
            age_str = None
            
        common_data = {
            'local_body_id': local_body_id,
            'sr_no': sr_no,
            's': str(mapped.get('s')) if mapped.get('s') else None,
            'voter_id': str(mapped.get('voter_id')).strip() if mapped.get('voter_id') else None,
            'epic_no': None, # We don't extract this right now
            'constituency': str(mapped.get('constituency')).strip() if mapped.get('constituency') else None,
            'list_part_no': str(mapped.get('list_part_no')).strip() if mapped.get('list_part_no') else None,
            'address': str(mapped.get('address')).strip() if mapped.get('address') else None,
            'voter_first_name': first_name,
            'voter_middle_name': middle_name,
            'voter_last_name': last_name,
            'house_number': str(mapped.get('house_number')).strip() if mapped.get('house_number') else None,
            'gender': str(mapped.get('gender')).strip() if mapped.get('gender') else None,
            'age': age_str,
            'header': str(mapped.get('header')) if mapped.get('header') else None,
            'is_deleted': is_deleted,
            'img': img_url,
            'updated_at': datetime.utcnow()
        }

        if zp_ps_sub_type:
            voters.append({
                **common_data,
                'gat': gat,
                'gan': None if gan == '-' else gan,
                'booth_no': booth_no,
                'zp_ps_sub_type': zp_ps_sub_type
            })
        else:
            voters.append({
                **common_data,
                'prabhag_no': prabhag_no,
                'ward_no': ward_no,
                'zp_ps_sub_type': None
            })

    if not voters:
        return 0

    engine = create_engine(db_url)
    table_name = "zp_ps_voters" if zp_ps_sub_type else "voters"
    
    # Use a single connection for DELETE + INSERT to make it atomic (like prisma.$transaction)
    with engine.begin() as conn:
        # Step 1: Delete existing voters for this scope
        if zp_ps_sub_type:
            if zp_ps_sub_type == 'PS':
                if gan is None:
                    conn.execute(
                        text("DELETE FROM zp_ps_voters WHERE local_body_id = :lb AND gat = :gat AND gan IS NULL AND booth_no = :booth AND zp_ps_sub_type = :subtype"),
                        {"lb": local_body_id, "gat": gat, "booth": booth_no, "subtype": zp_ps_sub_type}
                    )
                else:
                    conn.execute(
                        text("DELETE FROM zp_ps_voters WHERE local_body_id = :lb AND gat = :gat AND gan = :gan AND booth_no = :booth AND zp_ps_sub_type = :subtype"),
                        {"lb": local_body_id, "gat": gat, "gan": gan, "booth": booth_no, "subtype": zp_ps_sub_type}
                    )
            else:  # ZP
                conn.execute(
                    text("DELETE FROM zp_ps_voters WHERE local_body_id = :lb AND gat = :gat AND booth_no = :booth AND zp_ps_sub_type = :subtype"),
                    {"lb": local_body_id, "gat": gat, "booth": booth_no, "subtype": zp_ps_sub_type}
                )
        else:
            conn.execute(
                text("DELETE FROM voters WHERE local_body_id = :lb AND prabhag_no = :prabhag AND ward_no = :ward"),
                {"lb": local_body_id, "prabhag": prabhag_no, "ward": ward_no}
            )
            
        # Step 2: Bulk Insert in batches (using same connection for atomicity)
        df_to_insert = pd.DataFrame(voters)
        # Ensure any remaining NaNs are None to prevent float conversion issues
        df_to_insert = df_to_insert.where(pd.notnull(df_to_insert), None)
        df_to_insert.to_sql(table_name, conn, if_exists="append", index=False, method="multi", chunksize=1000)

    return len(voters)
