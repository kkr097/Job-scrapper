"""
Excel Exporter - Exports jobs to a formatted, color-coded Excel file.
"""

from datetime import datetime
from typing import List, Dict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


def export_to_excel(jobs: List[Dict], output_path: str = "daily_jobs.xlsx"):
    """
    Export jobs to an Excel file with color coding.
    
    Colors:
    - Green (8-10): Great match
    - Yellow (5-7): Okay match
    - Red (1-4): Poor match
    """
    
    # Sort by score (highest first)
    jobs_sorted = sorted(jobs, key=lambda x: x.get('score', 0), reverse=True)
    
    # Create workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Job Matches"
    
    # Define styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")
    
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    
    border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    
    # Headers
    headers = ["Score", "Title", "Company", "Location", "Source", "Match Reasons", "Missing", "URL"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border
    
    # Column widths
    widths = [8, 45, 25, 20, 12, 40, 30, 50]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    
    # Data rows
    for row, job in enumerate(jobs_sorted, 2):
        score = job.get('score', 0)
        
        # Determine row color
        if score >= 8:
            row_fill = green_fill
        elif score >= 5:
            row_fill = yellow_fill
        else:
            row_fill = red_fill
        
        # Write cells
        values = [
            score,
            job.get('title', ''),
            job.get('company', ''),
            job.get('location', ''),
            job.get('source', ''),
            job.get('match_reasons', ''),
            job.get('missing_skills', ''),
            job.get('url', '')
        ]
        
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.fill = row_fill
            cell.border = border
            cell.alignment = Alignment(vertical='center', wrap_text=True)
            
            # Make URL clickable
            if col == 8 and value:
                cell.hyperlink = value
                cell.font = Font(color="0563C1", underline="single")
    
    # Freeze header row
    ws.freeze_panes = 'A2'
    
    # Add summary sheet
    summary = wb.create_sheet("Summary")
    summary['A1'] = "Job Search Summary"
    summary['A1'].font = Font(bold=True, size=14)
    
    summary['A3'] = "Date:"
    summary['B3'] = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    summary['A4'] = "Total Jobs:"
    summary['B4'] = len(jobs)
    
    summary['A5'] = "Great Matches (8-10):"
    summary['B5'] = len([j for j in jobs if j.get('score', 0) >= 8])
    summary['B5'].fill = green_fill
    
    summary['A6'] = "Good Matches (5-7):"
    summary['B6'] = len([j for j in jobs if 5 <= j.get('score', 0) < 8])
    summary['B6'].fill = yellow_fill
    
    summary['A7'] = "Poor Matches (1-4):"
    summary['B7'] = len([j for j in jobs if j.get('score', 0) < 5])
    summary['B7'].fill = red_fill
    
    # Save
    wb.save(output_path)
    print(f"✓ Saved to: {output_path}")
    
    return output_path


if __name__ == "__main__":
    # Test with sample data
    sample_jobs = [
        {
            'title': 'Mechanical Design Engineer',
            'company': 'BMW Group',
            'location': 'Munich, Germany',
            'source': 'LinkedIn',
            'url': 'https://linkedin.com/jobs/123',
            'score': 9,
            'match_reasons': 'CATIA V5, automotive, design',
            'missing_skills': 'German B2'
        },
        {
            'title': 'CAD Engineer',
            'company': 'Siemens',
            'location': 'Berlin, Germany',
            'source': 'Indeed',
            'url': 'https://indeed.com/jobs/456',
            'score': 7,
            'match_reasons': 'CAD design experience',
            'missing_skills': 'NX experience'
        },
        {
            'title': 'Software Engineer',
            'company': 'Google',
            'location': 'Munich, Germany',
            'source': 'StepStone',
            'url': 'https://stepstone.de/jobs/789',
            'score': 2,
            'match_reasons': 'Wrong field',
            'missing_skills': 'Programming skills'
        }
    ]
    
    export_to_excel(sample_jobs, "test_jobs.xlsx")
    print("Test file created: test_jobs.xlsx")
