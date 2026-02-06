"""
Resume Parser - Extracts text from any PDF resume in the folder.
"""

import os
import glob

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Installing pymupdf...")
    os.system("pip install pymupdf --quiet")
    import fitz


def find_resume_pdf(folder: str = ".") -> str:
    """Find the first PDF file in the folder (assumed to be the resume)."""
    pdf_files = glob.glob(os.path.join(folder, "*.pdf"))
    
    # Filter out any obvious non-resume files
    resume_files = [f for f in pdf_files if "job" not in f.lower() and "output" not in f.lower()]
    
    if not resume_files:
        if pdf_files:
            return pdf_files[0]  # Use any PDF if no clear resume found
        raise FileNotFoundError("No PDF resume found in the folder")
    
    return resume_files[0]


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF file."""
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


def parse_resume(folder: str = ".") -> dict:
    """
    Parse the resume PDF and extract key information.
    Returns a dict with resume text and extracted skills.
    """
    pdf_path = find_resume_pdf(folder)
    resume_text = extract_text_from_pdf(pdf_path)
    
    # Common technical skills to look for
    skill_keywords = [
        # CAD Software
        "CATIA", "CATIA V5", "Autodesk Inventor", "SolidWorks", "AutoCAD",
        "Creo", "NX", "Siemens NX", "Fusion 360",
        # Simulation
        "ANSYS", "FEA", "CFD", "ABAQUS", "HyperMesh", "LS-DYNA",
        # PLM/PDM
        "Teamcenter", "SmarTeam", "Windchill", "ENOVIA", "SAP",
        # Programming
        "Python", "MATLAB", "VBA", "C++", "Java",
        # Manufacturing
        "GD&T", "DFM", "DFMEA", "PFMEA", "Lean", "Six Sigma",
        # Industries
        "Automotive", "Railway", "Aerospace", "Medical Device",
        # Specific domains
        "Wire Harness", "Wireharness", "Body Systems", "Chassis",
        "Pneumatic", "Hydraulic", "HVAC", "Powertrain"
    ]
    
    # Extract skills found in resume
    found_skills = []
    resume_lower = resume_text.lower()
    for skill in skill_keywords:
        if skill.lower() in resume_lower:
            found_skills.append(skill)
    
    # Extract experience summary (look for years of experience)
    import re
    years_match = re.search(r'(\d+)\+?\s*years?', resume_lower)
    years_exp = years_match.group(1) if years_match else "unknown"
    
    return {
        "pdf_path": pdf_path,
        "full_text": resume_text,
        "skills": list(set(found_skills)),
        "years_experience": years_exp
    }


def extract_job_titles(resume_text: str) -> list:
    """
    Extract likely job titles from resume text.
    Looks for lines containing common title keywords.
    """
    import re
    title_keywords = [
        "engineer", "designer", "konstrukteur", "entwickler",
        "techniker", "mechanical", "cad", "design"
    ]
    titles = set()
    for line in resume_text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if not cleaned or len(cleaned) > 80:
            continue
        lower = cleaned.lower()
        if any(k in lower for k in title_keywords):
            # Keep short, title-like lines
            if 2 <= len(cleaned.split()) <= 6:
                titles.add(cleaned)
    return list(titles)


def generate_config_from_resume(
    folder: str = ".",
    default_locations: list = None,
    exclude_keywords: list = None,
    exclude_description_keywords: list = None,
    include_description_keywords: list = None
) -> dict:
    """
    Build a config.json dict from the resume PDF plus provided defaults.
    Does not write to disk.
    """
    data = parse_resume(folder)
    resume_text = data["full_text"]

    # Default job search keywords if none are found
    base_keywords = [
        "Mechanical Engineer",
        "Mechanical Design Engineer",
        "CAD Engineer",
        "Design Engineer",
        "Konstrukteur",
        "Maschinenbauingenieur",
        "Produktentwickler"
    ]

    extracted_titles = extract_job_titles(resume_text)

    # Add skills as keyword variants (only if they look like role terms)
    skill_keywords = []
    for skill in data.get("skills", []):
        if any(k in skill.lower() for k in ["cad", "design", "mechanical", "nx", "catia"]):
            skill_keywords.append(skill)

    keywords = list(dict.fromkeys(extracted_titles + base_keywords + skill_keywords))
    if not keywords:
        keywords = base_keywords

    config = {
        "search": {
            "keywords": keywords,
            "location": default_locations or ["Germany"],
            "max_days_old": 2
        },
        "exclude_keywords": exclude_keywords or [],
        "exclude_description_keywords": exclude_description_keywords or [],
        "include_description_keywords": include_description_keywords or [],
        "sources": {
            "linkedin": True,
            "arbeitnow": True,
            "simplyhired": True,
            "company_careers": True
        },
        "output": {
            "excel_file": "daily_jobs.xlsx"
        }
    }

    return config


def get_resume_summary(folder: str = ".") -> str:
    """Get a concise summary of the resume for AI prompts."""
    data = parse_resume(folder)
    
    # Truncate text to first 3000 chars for API efficiency
    truncated_text = data["full_text"][:3000]
    
    summary = f"""
CANDIDATE RESUME:
{truncated_text}

KEY SKILLS DETECTED: {', '.join(data['skills'])}
YEARS OF EXPERIENCE: {data['years_experience']}
"""
    return summary


if __name__ == "__main__":
    # Test the parser
    try:
        data = parse_resume()
        print(f"✓ Found resume: {data['pdf_path']}")
        print(f"✓ Skills: {', '.join(data['skills'][:10])}...")
        print(f"✓ Experience: {data['years_experience']} years")
        print(f"✓ Text length: {len(data['full_text'])} characters")
    except Exception as e:
        print(f"✗ Error: {e}")
