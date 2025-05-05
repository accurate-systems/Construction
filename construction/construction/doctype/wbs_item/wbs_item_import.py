import frappe
import pandas as pd
import re
from frappe.utils import now_datetime

def normalize_cost_code(raw_code):
    # Ensure prefix like 'ASF-24' becomes 'ASF24'
    raw_code = raw_code.replace('ASF-', 'ASF').upper()

    parts = raw_code.split('-')
    normalized_parts = []

    for idx, part in enumerate(parts):
        if idx == 2:
            # Preserve the third segment as-is (e.g., '03')
            normalized_parts.append(part)
        else:
            # Normalize numeric-only segments
            if part.isdigit():
                normalized_parts.append(str(int(part)))
            else:
                # Handle alphanumeric like 'ST03'
                match = re.match(r'([A-Z]+)(\d+)', part, re.I)
                if match:
                    prefix, number = match.groups()
                    normalized_parts.append(f"{prefix.upper()}{int(number)}")
                else:
                    normalized_parts.append(part)

    return '-'.join(normalized_parts)

@frappe.whitelist()
def import_wbs_from_file(file_name):
    if not file_name:
        frappe.throw("No file provided.")

    # 1) Load file
    file_doc = frappe.get_doc("File", {"file_url": file_name})
    file_path = file_doc.get_full_path()
    df = pd.read_excel(file_path)
    df.columns = df.columns.str.strip()

    # 2) Detect & rename WBS code column
    wbs_col = next((c for c in df.columns if 'wbs' in c.lower() or 'cost code' in c.lower()), None)
    if not wbs_col:
        frappe.throw("WBS Code column not found.")
    df = df.rename(columns={wbs_col: "wbs_code"})
    df['wbs_code'] = df['wbs_code'].astype(str).str.strip()

    # 3) Detect & clean Res. Type column
    res_cols = [c for c in df.columns if 'res' in c.lower() and 'type' in c.lower()]
    if not res_cols:
        frappe.throw("Res. Type column not found. Available columns: " + ", ".join(df.columns))
    res_col = res_cols[0]
    df = df.rename(columns={res_col: "res_type"})
    df['res_type'] = df['res_type'].apply(lambda v: v.strip() if isinstance(v, str) else v)
    df.loc[df['res_type'] == '', 'res_type'] = pd.NA

    frappe.msgprint(f"[Debug] res_type header=`{res_col}`, samples: {df['res_type'].dropna().unique()[:5]}")

    # 4) Extract project from first WBS code
    first_code = df.iloc[0]['wbs_code']
    m = re.match(r"([A-Z]+)(\d{2})", first_code)
    if not m:
        frappe.throw(f"Invalid Cost Code format: {first_code}")
    project_abbr, start_year = m.group(1), m.group(2)
    project = frappe.get_value("Project", {
        "custom_project_abbr": project_abbr,
        "custom_start_year": ["in", [start_year, int(start_year)]]}, "name")
    if not project:
        frappe.throw(f"No Project found for {project_abbr}{start_year}")

    # 5) Compute level & parent_code
    def lvl_parent(code, res_type):
        parts = code.split('-')
        n = len(parts)

        if n == 1:
            return 1, None
        elif n == 3:
            return 2, parts[0]
        elif n == 4:
            return 3, '-'.join(parts[:3])
        elif n == 5:
            parent = '-'.join(parts[:4])  # Parent is level 4, same code
            if  pd.notna(res_type):
                return 5, parent
            else:
                return 4, parent
        return None, None

    df[['level', 'parent_code']] = df.apply(lambda row: pd.Series(lvl_parent(row['wbs_code'], row['res_type'])), axis=1)
    frappe.msgprint(f"[Debug] Sample WBS Levels and Parent Codes:\n{df[['wbs_code', 'level', 'parent_code']].head(10).to_string()}")

    df = df.sort_values('level').reset_index(drop=True)

    # 6) Prep for insertion
    meta = frappe.get_meta("WBS item")
    valid_fields = {f.fieldname for f in meta.fields}
    column_map = {
        "BOQ Qty": "qty", "Resource QTY": "resource_qty", "Waste": "waste",
        "Total Resource QTY": "custom_total_resource_qty",
        "Material Rate": "unit_cost", "Budget Rate": "unit_rate",
        "Total Budget": "budget", "Total Price": "total_price",
    }

    inserted = {}
    success = 0
    failed = []
    total = len(df)
    # Correct column names
    # msg = df[['wbs_code', 'level', 'parent_code']].to_string(index=False)
    # frappe.msgprint(msg)

    # # 7) Insert loop
    # frappe.msgprint(str(df.columns.tolist()))

    level_5_rows = []

    # First pass: Insert all levels except Level 5
    for _, row in df.iterrows():
        code, lvl, parent, res_type = row['wbs_code'], row['level'], row['parent_code'], row['res_type']

        if not lvl or lvl > 5:
            failed.append(f"{code} (invalid level: {lvl})")
            continue

        if lvl == 5:
            level_5_rows.append(row)  # Save Level 5 rows for the second pass
            continue  # Skip everything else in the loop for Level 5

        # Now process other levels (1–4)
        doc = frappe.new_doc("WBS item")
        doc.cost_code = code
        doc.level = int(lvl)
        doc.is_group = 1

        if lvl == 1:
            doc.project = project
        else:
            p_name = inserted.get(parent)
            if not p_name:
                frappe.msgprint(f"[Error] Missing parent for {code}: {parent}")
                failed.append(f"{code} (missing parent: {parent})")
                continue
            doc.parent_wbs_item = p_name
            doc.project = frappe.get_value("WBS item", p_name, "project")

        if lvl == 2:
            seg = "-".join(code.split("-")[1:])
            cc = frappe.get_value("Cost Center", {"custom_abbr": seg}, "name")
            if cc:
                doc.cost_center = cc
            else:
                frappe.msgprint(f"[Warning] No Cost Center found for {seg}")

        # Column field mappings
        for col, fld in column_map.items():
            if col in df.columns and fld in valid_fields:
                val = row.get(col)
                if not pd.isna(val):
                    doc.set(fld, val)

        try:
            doc.insert()
            frappe.db.commit()
            inserted[code] = doc.name
            


            success += 1
        except Exception as e:
            frappe.msgprint(f"[Exception] {code} → {str(e)}")
            failed.append(f"{code} (Insert Error: {e})")

    # === Second pass: Handle Level 5 WBS items ===
    for row in level_5_rows:
        frappe.msgprint(str(len(level_5_rows)))
        code, lvl, parent, res_type = row['wbs_code'], row['level'], row['parent_code'], row['res_type']
        desc = row.get("Item Description")
        ur = row.get("Budget Rate") or 0

        if pd.isna(desc):
            frappe.msgprint(f"[Error] Skipping {code} → Missing Item Description")
            failed.append(f"{code} (missing Item Description)")
            continue

        if not res_type:
            frappe.msgprint(f"[Error] Resource Type is missing for {code}")
            failed.append(f"{code} (missing Resource Type)")
            continue

        # Lookup parent WBS item (Level 4) using the cost code of current record
        parent_item = None
        frappe.msgprint("inserted")
        frappe.msgprint(str(inserted))

        for parent_code in inserted:
            parent_wbs = frappe.get_doc("WBS item", inserted[parent_code])
           
           
            frappe.msgprint(f"parent_wbs.cost_code: {parent_wbs.cost_code}")
            frappe.msgprint(f"target code: {code}")
           
            
            if (normalize_cost_code(parent_wbs.cost_code) == normalize_cost_code(code)):
                    frappe.msgprint(parent_wbs.level)
                    frappe.msgprint(parent_wbs.name)
                    if(int(parent_wbs.level)==int(4)):
                        frappe.msgprint(f"Match found with parent: {parent_wbs.name}")
                        parent_item = parent_wbs.name
                        break



        if not parent_item:
            frappe.msgprint(f"[Error] No level 4 parent found for level 5 item {code}")
            failed.append(f"{code} (missing level 4 parent)")
            continue

        # Create Level 5 WBS item
        doc = frappe.new_doc("WBS item")
        doc.cost_code = code
        doc.level = 5
        doc.is_group = 0
        doc.parent_wbs_item = parent_item
        doc.project = frappe.get_value("WBS item", parent_item, "project")

        # Get Item Group based on resource type
        grp = frappe.get_value("Item Group", {"custom_abbreviation": res_type}, "name")
        if not grp:
            frappe.msgprint(f"[Error] Item Group not found for res_type {res_type} at {code}")
            failed.append(f"{code} (Item Group not found for res_type: {res_type})")
            continue

        doc.item_group = grp

        # Find or create the item
        item = frappe.db.get_value("Item", {
            "item_name": ["like", f"%{desc}%"],
            "item_group": grp
        }, "name")

        if not item:
            ic = f"{desc[:90]}-{now_datetime().strftime('%Y%m%d%H%M%S')}"
            itm = frappe.get_doc({
                "doctype": "Item",
                "item_code": ic,
                "item_name": desc,
                "item_group": grp,
                "is_stock_item": 0
            })
            itm.insert(ignore_permissions=True)
            frappe.db.commit()
            item = itm.name

        doc.item = item
        doc.unit_rate = ur

        # Set other mapped fields
        for col, fld in column_map.items():
            if col in df.columns and fld in valid_fields:
                val = row.get(col)
                if not pd.isna(val):
                    doc.set(fld, val)

        try:
            doc.insert()
            frappe.db.commit()
            inserted[code] = doc.name
            success += 1
        except Exception as e:
            frappe.msgprint(f"[Exception] {code} → {str(e)}")
            failed.append(f"{code} (Insert Error: {e})")

    # === Final Result ===
    return {
        "total": total,
        "success": success,
        "failed": failed
    }
