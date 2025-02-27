import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet
import re  


class WBSitem(NestedSet):
    def autoname(self):
        # Generate name based on a sequence
        last_number = frappe.db.get_value("WBS item", {}, "name", order_by="creation desc")
        
        if last_number:
            # Extract and increment the last number
            parts = last_number.split('-')
            if len(parts) > 1 and parts[-1].isdigit():
                new_number = int(parts[-1]) + 1
            else:
                new_number = 1  # Reset to 1 if last number part isn't valid
        else:
            new_number = 1  # Start at 1 if no WBS item exists

        # Format the new name (e.g., WBS-0001)
        new_name = f"WBS-{new_number:04d}"
        self.name = new_name  # Set the name field for the new record
        
        # Set a unique serial number, avoid None or empty value
        if not self.serial_no:
            self.serial_no = new_name  # Optionally use the name as the serial number
    def validate(self):
        """
        Enforce hierarchy rules and ensure cost structure is correct.
        """
        if not self.parent_wbs_item:
            # Level 1 - Root Node
            self.level = 1
            self.is_group = 1  # Level 1 must be a group

            if not self.project:
                frappe.throw(_("Project is mandatory for Level 1 WBS Items."))

            project = frappe.get_doc("Project", self.project)
            if not project.get("custom_project_abbr") or not project.get("custom_start_year"):
                frappe.throw(_("Project must have a Project Abbreviation and Start Date."))

            self.cost_code = f"{project.custom_project_abbr}-{project.custom_start_year}"

        else:
            # Fetch Parent WBS Item
            parent = frappe.get_doc("WBS item", self.parent_wbs_item)
            self.level = int(parent.level) + 1

            # Inherit Project from Parent
            self.project = parent.project

            if self.level == 2:
                # Level 2 - Cost Center Node
                if not self.cost_center:
                    frappe.throw(_("Cost Center is mandatory for Level 2 WBS Items."))
                
                cc = frappe.get_doc("Cost Center", self.cost_center)
                if not cc.get("custom_code") or not cc.get("custom_abbr"):
                    frappe.throw(_("Selected Cost Center must have both a Custom Code and an Abbreviation."))

                cost_center_abbr = cc.get("custom_abbr")
                self.cost_code = f"{parent.cost_code}-{cost_center_abbr}"

                self.cost_center_code = cc.get("custom_code")
                self.is_group = 1  

            elif self.level == 3:
                # Level 3 - First Auto-Incremented Group
                self.is_group = 1
                child_count = frappe.db.count("WBS item", {"parent_wbs_item": self.parent_wbs_item}) + 1
                self.cost_code = f"{parent.cost_code}-{child_count}"
                self.cost_center_code = parent.cost_center_code
                self.cost_center = parent.cost_center

            elif self.level == 4:
   
                self.is_group = 1
                last_child = frappe.db.sql("""
                    SELECT cost_code FROM `tabWBS item`
                    WHERE parent_wbs_item = %s
                    ORDER BY creation DESC LIMIT 1
                """, (self.parent_wbs_item,), as_dict=True)

                
                if last_child:
                    last_numeric_part = re.findall(r'\d+', last_child[0].cost_code)  # Extract numbers safely
                    last_number = int(last_numeric_part[-1]) if last_numeric_part else 0
                    new_number = last_number + 5
                else:
                    new_number = 5  # Start from 5

                self.cost_code = f"{parent.cost_code}-{new_number}"
                self.cost_center_code = parent.cost_center_code
                self.cost_center = parent.cost_center

            elif self.level == 5:
                # ✅ Level 5 is always a leaf node
                self.is_group = 0  

                # ✅ Inherit Parent Values
                self.cost_code = parent.cost_code
                self.cost_center_code = parent.cost_center_code
                self.cost_center = parent.cost_center
                self.project = parent.project  # ✅ Added Project Inheritance

                # ✅ Validate Mandatory Fields
                if not self.item_group:
                    frappe.throw("Item Group is mandatory for Level 5 WBS Items.")
                if not self.unit_rate:
                    frappe.throw("Unit Rate is mandatory for Level 5 WBS Items.")

                # ✅ Fetch Item Group Abbreviation
                item_group_doc = frappe.get_doc("Item Group", self.item_group)
                if not item_group_doc.get("custom_abbreviation"):
                    frappe.throw("Item Group must have an Abbreviation (abbr).")

                abbr = item_group_doc.get("custom_abbreviation")

                # ✅ Find Existing Level 5 Items Under This Parent
                existing_items = frappe.db.sql("""
                    SELECT item_group, combined_code FROM `tabWBS item`
                    WHERE parent_wbs_item = %s AND level = 5
                    ORDER BY creation ASC
                """, (self.parent_wbs_item,), as_dict=True)

                # ✅ Count Occurrences of the Same Item Group
                group_counts = {}  
                for item in existing_items:
                    group = item["item_group"]
                    if group:
                        group_counts[group] = group_counts.get(group, 0) + 1  

                # ✅ Assign Number to This Item Group
                new_number = group_counts.get(self.item_group, 0) + 1  # LAB1, LAB2, etc.

                # ✅ Generate Combined Code with Correct Numbering
                self.combined_code = f"{self.cost_code}-{abbr}{new_number}-{self.unit_rate}"

        # Ensure Unique Serial No
        if frappe.db.exists("WBS item", {"serial_no": self.serial_no, "name": ["!=", self.name]}):
            frappe.throw(_("A WBS item with Serial No {0} already exists.").format(self.serial_no))
