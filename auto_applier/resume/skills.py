"""Extract and normalize skills from resume text.

Uses simple keyword matching for the MVP. Can be replaced with NLP later.
"""

import re

# Common tech/professional skills to look for. Extend as needed.
KNOWN_SKILLS = {
    # Programming
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
    "ruby", "php", "swift", "kotlin", "scala", "r",
    # Web
    "react", "angular", "vue", "node.js", "django", "flask", "fastapi",
    "html", "css", "tailwind", "next.js",
    # Data
    "sql", "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "pandas", "numpy", "spark", "hadoop", "tableau", "power bi",
    # Cloud / DevOps
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
    "ci/cd", "jenkins", "github actions", "linux",
    # AI/ML
    "machine learning", "deep learning", "pytorch", "tensorflow",
    "natural language processing", "computer vision", "llm",
    # Sales / Business
    "salesforce", "hubspot", "crm", "b2b sales", "b2c sales", "enterprise sales",
    "account management", "pipeline management", "lead generation", "cold calling",
    "prospecting", "negotiation", "closing", "contract management",
    "revenue forecasting", "territory management", "consultative selling",
    "solution selling", "upselling", "cross-selling", "sales analytics",
    "client relationship management",
    # General
    "git", "agile", "scrum", "jira", "project management",
    "communication", "leadership", "problem solving",
    "excel", "powerpoint", "microsoft office", "google analytics",
    "strategic planning",
}


def extract_skills(resume_text: str) -> set[str]:
    """Find known skills mentioned in the resume text."""
    text_lower = resume_text.lower()
    found = set()

    for skill in KNOWN_SKILLS:
        # Word boundary match to avoid partial matches (e.g., "r" in "react")
        pattern = r"\b" + re.escape(skill) + r"\b"
        if re.search(pattern, text_lower):
            found.add(skill)

    return found


def find_missing_skills(resume_skills: set[str], job_description: str) -> set[str]:
    """Find skills mentioned in job description but not in the resume."""
    job_text_lower = job_description.lower()
    missing = set()

    for skill in KNOWN_SKILLS:
        pattern = r"\b" + re.escape(skill) + r"\b"
        if re.search(pattern, job_text_lower) and skill not in resume_skills:
            missing.add(skill)

    return missing
