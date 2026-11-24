from io import BytesIO

import pandas as pd

from .service import activity, roster


def workbook(event_id):
    participants = roster(event_id)
    logs = activity(event_id)
    sheets = {
        "Participants": (participants, {"name": "Participant", "status": "Registration status",
                                         "registered_at": "Registered at (UTC)"}),
        "Assignments": (participants, {"name": "Participant", "recipient": "Assigned recipient",
                                        "registered_at": "Registered at (UTC)",
                                        "assignment_status": "Assignment status"}),
        "Activity": (logs, {"occurred_at": "Time (UTC)", "name": "Participant",
                            "action": "Activity", "status": "Status"}),
    }
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet, (rows, columns) in sheets.items():
            data = [[row[key] for key in columns] for row in rows]
            pd.DataFrame(data, columns=list(columns.values())).to_excel(writer, sheet_name=sheet, index=False)
            worksheet = writer.sheets[sheet]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column in worksheet.columns:
                worksheet.column_dimensions[column[0].column_letter].width = min(
                    60, max(14, max(len(str(cell.value or "")) for cell in column) + 3)
                )
                for cell in column:
                    # Treat roster-supplied text as literal Excel values.
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
    output.seek(0)
    return output
